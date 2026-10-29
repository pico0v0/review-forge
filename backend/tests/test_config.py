from pathlib import Path

import pytest

from app.config import AppSettings, ConfigurationError, load_settings, resolve_api_key


def test_defaults_keep_service_on_loopback_and_egress_disabled(tmp_path: Path) -> None:
    settings = load_settings(tmp_path)
    assert settings.server.host == "127.0.0.1"
    assert settings.security.code_egress_acknowledged is False
    assert settings.automation.finalize.auto_commit is False
    assert settings.paths.data_dir == (Path.home() / ".reviewforge/data").resolve()


def test_non_loopback_bind_is_rejected() -> None:
    with pytest.raises(ValueError, match="loopback"):
        AppSettings(server={"host": "0.0.0.0"})


def test_environment_overrides_yaml(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("server:\n  port: 9000\n", encoding="utf-8")
    monkeypatch.setenv("AIH_SERVER__PORT", "9001")
    assert load_settings(tmp_path).server.port == 9001


def test_plaintext_api_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "models:\n  providers:\n    - id: example\n      api_key: unsafe\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="Inline API keys"):
        load_settings(tmp_path)


def test_api_key_is_resolved_from_environment_without_exposing_it() -> None:
    settings = AppSettings(
        models={"providers": [{"id": "example", "api_key_env": "REVIEWFORGE_TEST_KEY"}]}
    )
    provider = settings.models.providers[0]
    key = resolve_api_key(provider, environ={"REVIEWFORGE_TEST_KEY": "secret-value"})
    assert key is not None
    assert key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(provider)


def test_missing_api_key_environment_variable_fails_closed() -> None:
    settings = AppSettings(models={"providers": [{"id": "example", "api_key_env": "MISSING_KEY"}]})
    with pytest.raises(ConfigurationError, match="is not set"):
        resolve_api_key(settings.models.providers[0], environ={})
