from __future__ import annotations

from pathlib import Path

import httpx

from ezcams_pi_agent.cameras import write_empty_camera_file
from ezcams_pi_agent.config import AgentConfig, default_config_dir, read_text, save_config, write_secret
from ezcams_pi_agent.crypto import (
    generate_device_private_key_pem,
    generate_self_signed_cert,
    load_private_key_pem,
    public_key_pem,
)


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

    private_key_path = root / "device.key"
    cert_path = root / "agent.crt"
    cert_key_path = root / "agent-tls.key"
    cameras_path = root / "cameras.json"

    if force or not private_key_path.exists():
        write_secret(private_key_path, generate_device_private_key_pem())
    private_key = load_private_key_pem(read_text(private_key_path))
    public_key = public_key_pem(private_key)

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
                "public_key_pem": public_key,
                "https_cert_pem": cert_pem,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    config = AgentConfig(
        backend_url=backend_url.rstrip("/"),
        device_id=str(data["device_id"]),
        name=name,
        static_ip=static_ip,
        port=port,
        backend_public_key_pem=str(data["backend_public_key_pem"]),
        private_key_path=str(private_key_path),
        cert_path=str(cert_path),
        cert_key_path=str(cert_key_path),
        cameras_path=str(cameras_path),
    )
    save_config(config, root)
    write_empty_camera_file(cameras_path)
    cameras_path.chmod(0o600)
    return config
