"""OpenAI-compatible provider client with bounded, redacted health checks."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx

from app.config import ConfigurationError, ProviderSettings, resolve_api_key
from app.errors import HubError


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
