from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR_NAME = ".ezcams-pi"


def repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def default_config_dir() -> Path:
    if env_dir := os.getenv("EZCAMS_PI_CONFIG_DIR"):
        return Path(env_dir)
    if root := repo_root():
        return root / CONFIG_DIR_NAME
    return Path.cwd() / CONFIG_DIR_NAME


@dataclass(frozen=True)
class RuntimeConfig:
    jpeg_quality: int = 80
    reconnect_delay_seconds: float = 5.0
    client_start_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class AgentConfig:
    backend_url: str
    device_id: str
    name: str
    static_ip: str
    port: int
    backend_public_key_pem: str
    private_key_path: str
    cert_path: str
    cert_key_path: str
    cameras_path: str
    allow_public_api: bool = False
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


def public_api_enabled(config: AgentConfig) -> bool:
    """Return True when signed-request auth is disabled for local testing."""
    env = os.getenv("EZCAMS_PI_ALLOW_PUBLIC_API", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return config.allow_public_api


def config_path(config_dir: Path | None = None) -> Path:
    return (config_dir or default_config_dir()) / "config.json"


def _runtime_from_raw(raw: object) -> RuntimeConfig:
    if not isinstance(raw, dict):
        return RuntimeConfig()
    defaults = RuntimeConfig()
    return RuntimeConfig(
        jpeg_quality=int(raw.get("jpeg_quality", defaults.jpeg_quality)),
        reconnect_delay_seconds=float(
            raw.get("reconnect_delay_seconds", defaults.reconnect_delay_seconds)
        ),
        client_start_timeout_seconds=float(
            raw.get("client_start_timeout_seconds", defaults.client_start_timeout_seconds)
        ),
    )


def load_config(config_dir: Path | None = None) -> AgentConfig:
    path = config_path(config_dir)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    raw_runtime = data.pop("runtime", None)
    runtime = _runtime_from_raw(raw_runtime)
    if "allow_public_api" in data:
        data["allow_public_api"] = bool(data["allow_public_api"])
    return AgentConfig(runtime=runtime, **data)


def save_config(config: AgentConfig, config_dir: Path | None = None) -> None:
    root = config_dir or default_config_dir()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = config_path(root)
    with path.open("w", encoding="utf-8") as f:
        json.dump(asdict(config), f, indent=2)
        f.write("\n")
    path.chmod(0o600)


def write_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("w", encoding="utf-8") as f:
        f.write(content)
    path.chmod(0o600)


def read_text(path: str | Path) -> str:
    with Path(path).open("r", encoding="utf-8") as f:
        return f.read()
