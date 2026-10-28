"""OpenAI-compatible provider client with bounded, redacted health checks."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from app.config import (
    ConfigurationError,
    ModelRoleSettings,
    ProviderSettings,
    resolve_api_key,
)
from app.errors import HubError
from app.protocol import ReviewResponse, parse_response


class ModelGateway:
    def __init__(
        self,
        providers: list[ProviderSettings],
        *,
        key_resolver: Callable[[ProviderSettings], object] = resolve_api_key,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self.providers = {provider.id: provider for provider in providers}
        self.key_resolver = key_resolver
        self.transport = transport
        self.max_response_bytes = max_response_bytes

    def list_models(self) -> list[dict[str, object]]:
        return [
            {
                "id": provider.id,
                "type": provider.type,
                "base_url": provider.base_url,
                "models": [model.model_dump(mode="json") for model in provider.models],
            }
            for provider in self.providers.values()
        ]

    async def test(self, provider_id: str, model_id: str) -> dict[str, object]:
        provider = self.providers.get(provider_id)
        if provider is None:
            raise HubError("provider_not_found", f"Provider {provider_id!r} was not found", 404)
        if model_id not in {model.id for model in provider.models}:
            raise HubError("model_not_found", f"Model {model_id!r} was not found", 404)
        try:
            key = self.key_resolver(provider)
        except ConfigurationError as exc:
            raise HubError("provider_key_unavailable", str(exc), 503) from exc

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "temperature": 0,
            "max_tokens": 4,
        }
        started = time.perf_counter()
        response_body = bytearray()
        try:
            async with httpx.AsyncClient(
                timeout=provider.timeout_sec, transport=self.transport
            ) as client:
                async with client.stream(
                    "POST",
                    f"{provider.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise HubError(
                            "provider_auth_failed", "Model provider rejected the API key", 502
                        )
                    if response.status_code == 429 or response.status_code >= 500:
                        raise HubError(
                            "provider_unavailable",
                            "Model provider returned a temporary error",
                            502,
                        )
                    if response.status_code >= 400:
                        raise HubError(
                            "provider_error", "Model provider rejected the health check", 502
                        )
                    async for chunk in response.aiter_bytes():
                        if len(response_body) + len(chunk) > self.max_response_bytes:
                            raise HubError(
                                "provider_response_too_large",
                                "Model provider response exceeds the configured size limit",
                                502,
                            )
                        response_body.extend(chunk)
        except httpx.TimeoutException as exc:
            raise HubError("provider_timeout", "Model provider timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise HubError("provider_unavailable", "Model provider is unavailable", 502) from exc
        try:
            result = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HubError(
                "provider_invalid_response", "Model provider returned invalid JSON", 502
            ) from exc
        choices = result.get("choices") if isinstance(result, dict) else None
        if not isinstance(choices, list) or not choices:
            raise HubError(
                "provider_invalid_response", "Model provider returned no chat completion", 502
            )
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "model": model_id,
            "json_schema": False,
        }

    async def review(
        self,
        role: ModelRoleSettings,
        *,
        task_id: str,
        round_no: int,
        system_prompt: str,
        context: str,
        max_output_tokens: int = 16_000,
    ) -> tuple[ReviewResponse, dict[str, object]]:
        provider = self.providers.get(role.provider)
        if provider is None:
            raise HubError("provider_not_found", f"Provider {role.provider!r} was not found", 503)
        model = next((item for item in provider.models if item.id == role.model), None)
        if model is None:
            raise HubError("model_not_found", f"Model {role.model!r} was not found", 503)
        try:
            key = self.key_resolver(provider)
        except ConfigurationError as exc:
            raise HubError("provider_key_unavailable", str(exc), 503) from exc
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        protocol = (
            "Return exactly one JSON object with schema_version 2.0, task_id, round, verdict, "
            "summary, findings, and optional patch or edits. Do not use Markdown fences."
        )
        payload = {
            "model": role.model,
            "messages": [
                {"role": "system", "content": f"{system_prompt}\n\n{protocol}"},
                {
                    "role": "user",
                    "content": f"task_id={task_id}\nround={round_no}\n\n{context}",
                },
            ],
            "temperature": role.temperature,
            "max_tokens": min(max_output_tokens, model.max_output_tokens),
            "response_format": {"type": "json_object"},
        }
        response_body = bytearray()
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=provider.timeout_sec, transport=self.transport
            ) as client:
                async with client.stream(
                    "POST",
                    f"{provider.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code == 401 or response.status_code == 403:
                        raise HubError(
                            "provider_auth_failed", "Model provider rejected the API key", 502
                        )
                    if response.status_code == 429 or response.status_code >= 500:
                        raise HubError(
                            "provider_unavailable", "Model provider returned a temporary error", 502
                        )
                    if response.status_code >= 400:
                        raise HubError("provider_error", "Model provider rejected the review", 502)
                    async for chunk in response.aiter_bytes():
                        if len(response_body) + len(chunk) > self.max_response_bytes:
                            raise HubError(
                                "provider_response_too_large",
                                "Model response exceeds the configured size limit",
                                502,
                            )
                        response_body.extend(chunk)
        except httpx.TimeoutException as exc:
            raise HubError("provider_timeout", "Model provider timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise HubError("provider_unavailable", "Model provider is unavailable", 502) from exc
        try:
            result = json.loads(response_body)
            content = result["choices"][0]["message"]["content"]
            response = parse_response(content, task_id, round_no)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise HubError(
                "invalid_model_output", "Model returned an invalid review response", 502
            ) from exc
        usage = result.get("usage") or {}
        if not isinstance(usage, dict):
            raise HubError("invalid_model_output", "Model returned an invalid usage object", 502)
        for name in ("prompt_tokens", "completion_tokens"):
            value = usage.get(name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise HubError("invalid_model_output", "Model returned invalid token usage", 502)
        return response, {
            "provider": provider.id,
            "model": role.model,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "usage_reported": all(
                isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)
                for name in ("prompt_tokens", "completion_tokens")
            ),
        }
