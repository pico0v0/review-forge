"""Validated configuration loading and secret resolution."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class ConfigurationError(ValueError):
    """Raised when ReviewForge configuration is missing or unsafe."""


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)

    @model_validator(mode="after")
    def require_loopback(self) -> ServerSettings:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host.lower() == "localhost"
        if not loopback:
            raise ValueError("ReviewForge must bind to a loopback address")
        return self


class PathsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("~/.reviewforge/data")


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_egress_acknowledged: bool = False
    secret_scan_action: Literal["block"] = "block"
    allow_http_localhost: bool = True
    max_response_bytes: int = Field(default=20_000_000, gt=0)


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    context_window: int = Field(default=128_000, gt=0)
    max_output_tokens: int = Field(default=16_000, gt=0)
    input_usd_per_1m: float = Field(default=0, ge=0)
    output_usd_per_1m: float = Field(default=0, ge=0)


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str | None = None
    api_key_file: Path | None = None
    timeout_sec: int = Field(default=600, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    models: list[ModelSettings] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def reject_inline_api_key(cls, value: Any) -> Any:
        if isinstance(value, dict) and "api_key" in value:
            raise ValueError("Put API keys in an environment variable or protected file")
        return value

    @model_validator(mode="after")
    def validate_provider(self) -> ProviderSettings:
        if self.api_key_env and self.api_key_file:
            raise ValueError("Configure only one of api_key_env or api_key_file")
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("Provider base_url must be an HTTP(S) URL")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Provider base_url cannot contain credentials, query, or fragment")
        if url.scheme == "http":
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = url.hostname.lower() == "localhost"
            if not local:
                raise ValueError("HTTP is only permitted for loopback model services")
        model_ids = [model.id for model in self.models]
        if len(set(model_ids)) != len(model_ids):
            raise ValueError(f"Provider {self.id!r} contains duplicate model IDs")
        return self


class ModelRoleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    temperature: float = Field(default=0.1, ge=0, le=2)


class RolesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: ModelRoleSettings | None = None
    fixer: ModelRoleSettings | None = None


class ModelsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderSettings] = Field(default_factory=list)
    roles: RolesSettings = Field(default_factory=RolesSettings)


class ContextSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(default=200_000, gt=0)
    max_total_bytes: int = Field(default=8_000_000, gt=0)


class GlobalLimitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(default=3, ge=1, le=100)
    max_fix_attempts_per_round: int = Field(default=2, ge=0, le=20)
    max_total_tokens: int = Field(default=1_000_000, ge=0)
    max_cost_usd: float = Field(default=5, ge=0)
    max_wall_time_min: int = Field(default=120, ge=1, le=7 * 24 * 60)


class AutomationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_clean_worktree: bool = True
    max_parallel_tasks: int = Field(default=1, ge=1, le=16)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIH_",
        env_nested_delimiter="__",
        extra="forbid",
        case_sensitive=False,
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    limits: GlobalLimitSettings = Field(default_factory=GlobalLimitSettings)
    automation: AutomationSettings = Field(default_factory=AutomationSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, init_settings, file_secret_settings

    @model_validator(mode="after")
    def validate_model_roles(self) -> AppSettings:
        providers = {provider.id: provider for provider in self.models.providers}
        if len(providers) != len(self.models.providers):
            raise ValueError("Provider IDs must be unique")
        for role_name in ("reviewer", "fixer"):
            role = getattr(self.models.roles, role_name)
            if role is None:
                continue
            provider = providers.get(role.provider)
            if provider is None:
                raise ValueError(f"{role_name} references unknown provider {role.provider!r}")
            if role.model not in {model.id for model in provider.models}:
                raise ValueError(
                    f"{role_name} references unknown model {role.model!r} "
                    f"in provider {role.provider!r}"
                )
        if not self.security.allow_http_localhost and any(
            provider.base_url.startswith("http://") for provider in providers.values()
        ):
            raise ValueError("HTTP model providers are disabled by security configuration")
        return self


def _contains_inline_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() == "api_key" or _contains_inline_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_inline_key(item) for item in value)
    return False


def default_config_dir() -> Path:
    return (
        Path(os.environ.get("AIH_CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))
        .expanduser()
        .resolve()
    )


def load_settings(config_dir: Path | None = None) -> AppSettings:
    """Load config.yaml, then apply AIH_* environment overrides."""
    configured_dir = (config_dir or default_config_dir()).expanduser().resolve()
    config_path = configured_dir / "config.yaml"
    raw: dict[str, Any] = {}

    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"Could not read {config_path}: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigurationError("config.yaml must contain a YAML object")
        raw = loaded or {}

    if _contains_inline_key(raw):
        raise ConfigurationError("Inline API keys are forbidden; use api_key_env or api_key_file")

    try:
        settings = AppSettings(**raw)
    except Exception as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc

    data_dir = settings.paths.data_dir.expanduser()
    if not data_dir.is_absolute():
        data_dir = configured_dir / data_dir
    settings.paths.data_dir = data_dir.resolve()
    return settings


def resolve_api_key(
    provider: ProviderSettings,
    *,
    config_dir: Path | None = None,
    environ: dict[str, str] | None = None,
) -> SecretStr | None:
    """Resolve a provider key without including it in config or API representations."""
    env = os.environ if environ is None else environ
    if provider.api_key_env:
        value = env.get(provider.api_key_env)
        if not value:
            raise ConfigurationError(f"Environment variable {provider.api_key_env} is not set")
        return SecretStr(value)

    if provider.api_key_file:
        path = provider.api_key_file.expanduser()
        if not path.is_absolute():
            path = (config_dir or Path.cwd()) / path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Could not read API key file {path}: {exc}") from exc
        if not value:
            raise ConfigurationError(f"API key file {path} is empty")
        return SecretStr(value)
    return None
