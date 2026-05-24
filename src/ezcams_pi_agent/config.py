from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
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


def config_path(config_dir: Path | None = None) -> Path:
    return (config_dir or default_config_dir()) / "config.json"


def load_config(config_dir: Path | None = None) -> AgentConfig:
    path = config_path(config_dir)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return AgentConfig(**data)


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
