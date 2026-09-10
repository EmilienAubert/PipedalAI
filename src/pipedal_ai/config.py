from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"La section [{name}] doit être une table TOML.")
    return value


def _secret_from_env(name: str, required: bool = False) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise ConfigurationError(f"La variable d'environnement {name} est requise.")
    return value


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    allowed_cidrs: tuple[str, ...]
    api_key: str = ""
    bearer_token: str = ""


@dataclass(frozen=True)
class StorageConfig:
    database: Path
    artifact_root: Path
    upload_root: Path


@dataclass(frozen=True)
class RTXClientConfig:
    base_url: str
    timeout_seconds: float
    bearer_token: str
    ca_file: Path | None
    client_cert_file: Path | None
    client_key_file: Path | None
    enabled: bool


@dataclass(frozen=True)
class PiPedalConfig:
    http_base_url: str
    websocket_url: str
    upload_path: str
    max_upload_bytes: int
    allow_import: bool
    allow_activation: bool


@dataclass(frozen=True)
class PolicyConfig:
    max_chain_length: int
    max_prompt_chars: int
    max_artifact_uncompressed_bytes: int
    default_input_volume_db: float
    default_output_volume_db: float
    max_load_per_cpu: float
    min_free_mb: int


@dataclass(frozen=True)
class PiConfig:
    server: ServerConfig
    storage: StorageConfig
    rtx: RTXClientConfig
    pipedal: PiPedalConfig
    policy: PolicyConfig


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    model: str
    timeout_seconds: float
    temperature: float


@dataclass(frozen=True)
class RTXConfig:
    server: ServerConfig
    ollama: OllamaConfig


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Configuration illisible {path}: {exc}") from exc
    return value


def _optional_path(value: Any) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def load_pi_config(path: Path) -> PiConfig:
    data = _load(path)
    server = _section(data, "server")
    storage = _section(data, "storage")
    rtx = _section(data, "rtx")
    pipedal = _section(data, "pipedal")
    policy = _section(data, "policy")
    api_env = str(server.get("api_key_env", "PIPEDAL_AI_API_KEY"))
    token_env = str(rtx.get("bearer_token_env", "PIPEDAL_AI_RTX_TOKEN"))
    enabled = bool(rtx.get("enabled", True))
    return PiConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 8090)),
            allowed_cidrs=tuple(server.get("allowed_cidrs", ["127.0.0.0/8"])),
            api_key=_secret_from_env(api_env, required=False),
        ),
        storage=StorageConfig(
            database=Path(storage.get("database", "./data/pipedal-ai.db")),
            artifact_root=Path(storage.get("artifact_root", "./data/artifacts")),
            upload_root=Path(storage.get("upload_root", "/var/pipedal/audio_uploads")),
        ),
        rtx=RTXClientConfig(
            base_url=str(rtx.get("base_url", "https://127.0.0.1:8091")).rstrip("/"),
            timeout_seconds=float(rtx.get("timeout_seconds", 90)),
            bearer_token=_secret_from_env(token_env, required=enabled),
            ca_file=_optional_path(rtx.get("ca_file")),
            client_cert_file=_optional_path(rtx.get("client_cert_file")),
            client_key_file=_optional_path(rtx.get("client_key_file")),
            enabled=enabled,
        ),
        pipedal=PiPedalConfig(
            http_base_url=str(pipedal.get("http_base_url", "http://127.0.0.1:8080")).rstrip("/"),
            websocket_url=str(pipedal.get("websocket_url", "ws://127.0.0.1:8080/pipedal")),
            upload_path=str(pipedal.get("upload_path", "/var/uploadPreset")),
            max_upload_bytes=int(pipedal.get("max_upload_bytes", 1048576)),
            allow_import=bool(pipedal.get("allow_import", False)),
            allow_activation=bool(pipedal.get("allow_activation", False)),
        ),
        policy=PolicyConfig(
            max_chain_length=int(policy.get("max_chain_length", 10)),
            max_prompt_chars=int(policy.get("max_prompt_chars", 2000)),
            max_artifact_uncompressed_bytes=int(policy.get("max_artifact_uncompressed_bytes", 64 * 1024 * 1024)),
            default_input_volume_db=float(policy.get("default_input_volume_db", -6)),
            default_output_volume_db=float(policy.get("default_output_volume_db", -6)),
            max_load_per_cpu=float(policy.get("max_load_per_cpu", 0.90)),
            min_free_mb=int(policy.get("min_free_mb", 256)),
        ),
    )


def load_rtx_config(path: Path) -> RTXConfig:
    data = _load(path)
    server = _section(data, "server")
    ollama = _section(data, "ollama")
    token_env = str(server.get("bearer_token_env", "PIPEDAL_AI_RTX_TOKEN"))
    return RTXConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 8091)),
            allowed_cidrs=tuple(server.get("allowed_cidrs", ["127.0.0.0/8"])),
            bearer_token=_secret_from_env(token_env, required=True),
        ),
        ollama=OllamaConfig(
            base_url=str(ollama.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
            model=str(ollama.get("model", "qwen3:14b")),
            timeout_seconds=float(ollama.get("timeout_seconds", 120)),
            temperature=float(ollama.get("temperature", 0.2)),
        ),
    )
