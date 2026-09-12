from __future__ import annotations

import os
import ipaddress
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
class BenchConfig:
    enabled: bool = False
    di_root: Path = Path("./data/di")
    output_root: Path = Path("./data/bench")
    max_renders: int = 24
    max_audio_cpu_percent: float = 75.0
    tail_seconds: float = 2.0
    track_directory: str = "shared/audio/Tracks"
    record_directory: str = "shared/audio/Audio Recordings"
    nam_input_calibration_dbu: float | None = None

    def __post_init__(self):
        import math
        if not math.isfinite(self.max_audio_cpu_percent) or not 1 <= self.max_audio_cpu_percent <= 95:
            raise ConfigurationError("bench.max_audio_cpu_percent doit être compris entre 1 et 95")
        value = self.nam_input_calibration_dbu
        if value is not None and (not math.isfinite(value) or not -30 <= value <= 12):
            raise ConfigurationError("Calibration NAM du banc : mesure finie entre -30 et 12 dBu requise")


@dataclass(frozen=True)
class PiConfig:
    server: ServerConfig
    storage: StorageConfig
    rtx: RTXClientConfig
    pipedal: PiPedalConfig
    policy: PolicyConfig
    bench: BenchConfig = field(default_factory=BenchConfig)


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    model: str
    timeout_seconds: float
    temperature: float
    max_retries: int = 1
    max_plugin_candidates: int = 24
    max_assets_per_role: int = 12
    output_format: Literal["schema", "json"] = "schema"
    think: bool | Literal["low", "medium", "high"] | None = False
    num_ctx: int = 8192
    num_predict: int = 4096
    diagnostics_directory: Path | None = None
    planning_mode: Literal["musical", "raw"] = "musical"


@dataclass(frozen=True)
class FingerprintConfig:
    enabled: bool
    index_path: Path


@dataclass(frozen=True)
class RTXConfig:
    server: ServerConfig
    ollama: OllamaConfig
    fingerprints: FingerprintConfig


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Configuration illisible {path}: {exc}") from exc
    return value


def _optional_path(value: Any) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_pi_config(path: Path) -> PiConfig:
    data = _load(path)
    server = _section(data, "server")
    storage = _section(data, "storage")
    rtx = _section(data, "rtx")
    pipedal = _section(data, "pipedal")
    policy = _section(data, "policy")
    bench = _section(data, "bench")
    api_env = str(server.get("api_key_env", "PIPEDAL_AI_API_KEY"))
    api_key = _secret_from_env(api_env, required=False)
    server_host = str(server.get("host", "127.0.0.1"))
    if not api_key and not _loopback_host(server_host):
        raise ConfigurationError(
            f"La variable {api_env} est requise lorsque le service Pi écoute hors loopback."
        )
    token_env = str(rtx.get("bearer_token_env", "PIPEDAL_AI_RTX_TOKEN"))
    enabled = bool(rtx.get("enabled", True))
    return PiConfig(
        server=ServerConfig(
            host=server_host,
            port=int(server.get("port", 8090)),
            allowed_cidrs=tuple(server.get("allowed_cidrs", ["127.0.0.0/8"])),
            api_key=api_key,
        ),
        storage=StorageConfig(
            database=Path(storage.get("database", "./data/pipedal-ai.db")),
            artifact_root=Path(storage.get("artifact_root", "./data/artifacts")),
            upload_root=Path(storage.get("upload_root", "/var/pipedal/audio_uploads")),
        ),
        rtx=RTXClientConfig(
            base_url=str(rtx.get("base_url", "https://127.0.0.1:8091")).rstrip("/"),
            timeout_seconds=float(rtx.get("timeout_seconds", 300)),
            bearer_token=_secret_from_env(token_env, required=enabled),
            ca_file=_optional_path(rtx.get("ca_file")),
            client_cert_file=_optional_path(rtx.get("client_cert_file")),
            client_key_file=_optional_path(rtx.get("client_key_file")),
            enabled=enabled,
        ),
        pipedal=PiPedalConfig(
            http_base_url=str(pipedal.get("http_base_url", "http://127.0.0.1:80")).rstrip("/"),
            websocket_url=str(pipedal.get("websocket_url", "ws://127.0.0.1:80/pipedal")),
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
        bench=BenchConfig(enabled=bool(bench.get("enabled", False)),
            di_root=Path(bench.get("di_root", "./data/di")),
            output_root=Path(bench.get("output_root", "./data/bench")),
            max_renders=max(3, min(48, int(bench.get("max_renders", 24)))),
            max_audio_cpu_percent=float(bench.get("max_audio_cpu_percent", 75)),
            tail_seconds=max(0.5, min(10.0, float(bench.get("tail_seconds", 2)))),
            track_directory=str(bench.get("track_directory", "shared/audio/Tracks")),
            record_directory=str(bench.get("record_directory", "shared/audio/Audio Recordings")),
            nam_input_calibration_dbu=float(bench["nam_input_calibration_dbu"]) if "nam_input_calibration_dbu" in bench else None),
    )


def load_rtx_config(path: Path) -> RTXConfig:
    data = _load(path)
    server = _section(data, "server")
    ollama = _section(data, "ollama")
    fingerprints = _section(data, "fingerprints")
    token_env = str(server.get("bearer_token_env", "PIPEDAL_AI_RTX_TOKEN"))
    output_format = ollama.get("output_format", "schema")
    if output_format not in ("schema", "json"):
        raise ConfigurationError("ollama.output_format doit être 'schema' ou 'json'.")
    think = ollama.get("think", False)
    if type(think) is not bool and think not in ("low", "medium", "high", "auto"):
        raise ConfigurationError("ollama.think doit être un booléen, low, medium, high ou auto.")
    planning_mode = ollama.get("planning_mode", "musical")
    if planning_mode not in ("musical", "raw"):
        raise ConfigurationError("ollama.planning_mode doit être musical ou raw.")
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
            timeout_seconds=float(ollama.get("timeout_seconds", 90)),
            temperature=float(ollama.get("temperature", 0.2)),
            max_retries=max(0, min(3, int(ollama.get("max_retries", 1)))),
            max_plugin_candidates=max(8, min(64, int(ollama.get("max_plugin_candidates", 24)))),
            max_assets_per_role=max(1, min(64, int(ollama.get("max_assets_per_role", 12)))),
            output_format=output_format,
            think=None if think == "auto" else think,
            num_ctx=max(2048, min(65536, int(ollama.get("num_ctx", 8192)))),
            num_predict=max(512, min(16384, int(ollama.get("num_predict", 4096)))),
            diagnostics_directory=_optional_path(ollama.get("diagnostics_directory")),
            planning_mode=planning_mode,
        ),
        fingerprints=FingerprintConfig(
            enabled=bool(fingerprints.get("enabled", True)),
            index_path=Path(fingerprints.get("index_path", "./data/fingerprints.json")),
        ),
    )
