from __future__ import annotations

from pathlib import Path

import httpx

from ezcams_pi_agent.cameras import write_empty_camera_file
from ezcams_pi_agent.config import AgentConfig, default_config_dir, read_text, save_config, write_secret
from ezcams_pi_agent.crypto import generate_self_signed_cert


def setup_agent(
    *,
    backend_url: str,
    claim_code: str,
    name: str,
    static_ip: str,
    port: int,
    config_dir: Path | None = None,
    force: bool = False,
    insecure_backend_tls: bool = False,
) -> AgentConfig:
    root = config_dir or default_config_dir()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)

    device_secret_path = root / "device.secret"
    cert_path = root / "agent.crt"
    cert_key_path = root / "agent-tls.key"
    cameras_path = root / "cameras.json"
    recordings_dir = root / "clips"

    if force or not cert_path.exists() or not cert_key_path.exists():
        cert_pem, cert_key_pem = generate_self_signed_cert(static_ip, name)
        write_secret(cert_path, cert_pem)
        write_secret(cert_key_path, cert_key_pem)
    cert_pem = read_text(cert_path)

    with httpx.Client(timeout=30, verify=not insecure_backend_tls) as client:
        resp = client.post(
            f"{backend_url.rstrip('/')}/devices/claim",
            json={
                "claim_code": claim_code,
                "name": name,
                "static_ip": static_ip,
                "agent_port": port,
                "https_cert_pem": cert_pem,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    write_secret(device_secret_path, str(data["device_secret"]).strip())

    config = AgentConfig(
        backend_url=backend_url.rstrip("/"),
        device_id=str(data["device_id"]),
        name=name,
        static_ip=static_ip,
        port=port,
        backend_public_key_pem=str(data["backend_public_key_pem"]),
        device_secret_path=str(device_secret_path),
        cert_path=str(cert_path),
        cert_key_path=str(cert_key_path),
        cameras_path=str(cameras_path),
        recordings_dir=str(recordings_dir),
    )
    save_config(config, root)
    write_empty_camera_file(cameras_path)
    cameras_path.chmod(0o600)
    return config
