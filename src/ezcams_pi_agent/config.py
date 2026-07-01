from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

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
    device_secret_path: str
    cert_path: str
    cert_key_path: str
    cameras_path: str
    recordings_dir: str = ""
    allow_loopback_unsigned: bool = True
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


def loopback_unsigned_enabled(config: AgentConfig) -> bool:
    """Return True when same-host (loopback) requests may skip signed-request auth.

    This lets the on-device inference process pull camera frames over localhost
    without the backend's signing key. It applies only to loopback peers, never
    to LAN/WAN clients. Override with EZCAMS_PI_ALLOW_LOOPBACK_UNSIGNED.
    """
    env = os.getenv("EZCAMS_PI_ALLOW_LOOPBACK_UNSIGNED", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return config.allow_loopback_unsigned


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
    cfg_dir = config_dir or default_config_dir()
    path = config_path(cfg_dir)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    raw_runtime = data.pop("runtime", None)
    runtime = _runtime_from_raw(raw_runtime)
    if "allow_loopback_unsigned" in data:
        data["allow_loopback_unsigned"] = bool(data["allow_loopback_unsigned"])
    # Tolerate configs written by older agent versions: drop keys the current
    # schema no longer accepts (e.g. the pre-bearer-auth private_key_path) and
    # fall back to the standard path for newly-required keys, so a slightly
    # stale config does not hard-crash the agent.
    known = {f.name for f in fields(AgentConfig)} - {"runtime"}
    unknown = sorted(set(data) - known)
    if unknown:
        log.warning("ignoring unknown config keys: %s", ", ".join(unknown))
        for key in unknown:
            data.pop(key, None)
    if "device_secret_path" not in data:
        data["device_secret_path"] = str(Path(cfg_dir) / "device.secret")
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
