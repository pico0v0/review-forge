import httpx
import pytest

from app.config import AppSettings
from app.errors import HubError
from app.providers import ModelGateway


@pytest.mark.anyio
async def test_provider_health_check_uses_configured_key_and_model() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    settings = AppSettings(
        models={
            "providers": [
                {
                    "id": "local",
                    "base_url": "http://127.0.0.1:9999/v1",
                    "api_key_env": "TEST_KEY",
                    "models": [{"id": "review"}],
                }
            ]
        }
    )

    def resolve_key(_provider):
        class Key:
            def get_secret_value(self) -> str:
                return "secret"

        return Key()

    gateway = ModelGateway(
        settings.models.providers,
        key_resolver=resolve_key,
        transport=httpx.MockTransport(handler),
    )

    result = await gateway.test("local", "review")

    assert result["ok"] is True
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert requests[0].url.path == "/v1/chat/completions"


@pytest.mark.anyio
async def test_provider_health_check_hides_provider_errors() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret details from provider")

    settings = AppSettings(
        models={
            "providers": [
                {
                    "id": "local",
                    "base_url": "http://localhost:9999",
                    "models": [{"id": "review"}],
                }
            ]
        }
    )
    gateway = ModelGateway(settings.models.providers, transport=httpx.MockTransport(handler))
    with pytest.raises(HubError, match="rejected the API key") as error:
        await gateway.test("local", "review")
    assert "secret details" not in str(error.value)


@pytest.mark.anyio
async def test_provider_health_check_enforces_response_size_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [], "padding": "x" * 100})

    settings = AppSettings(
        models={
            "providers": [
                {
                    "id": "local",
                    "base_url": "http://localhost:9999",
                    "models": [{"id": "review"}],
                }
            ]
        }
    )
    gateway = ModelGateway(
        settings.models.providers,
        transport=httpx.MockTransport(handler),
        max_response_bytes=20,
    )
    with pytest.raises(HubError, match="size limit"):
        await gateway.test("local", "review")
