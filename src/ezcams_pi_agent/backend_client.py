from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone

import httpx

from ezcams_pi_agent.cameras import load_cameras, sync_payload
from ezcams_pi_agent.config import AgentConfig, read_text
from ezcams_pi_agent.crypto import load_private_key_pem, sign_payload

log = logging.getLogger(__name__)


def _device_assertion(config: AgentConfig) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    nonce = secrets.token_urlsafe(24)
    payload = {
        "device_id": config.device_id,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    signature = sign_payload(payload, load_private_key_pem(read_text(config.private_key_path)))
    return {
        **payload,
        "signature": signature,
    }


async def get_device_token(config: AgentConfig) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{config.backend_url.rstrip('/')}/devices/token",
            json=_device_assertion(config),
        )
        resp.raise_for_status()
        return str(resp.json()["access_token"])


async def heartbeat_once(config: AgentConfig, token: str | None = None) -> None:
    token = token or await get_device_token(config)
    camera_count = len(load_cameras(config.cameras_path))
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{config.backend_url.rstrip('/')}/devices/heartbeat",
            headers={"Authorization": f"Bearer {token}"},
            json={"status": "online", "agent_version": "0.1.0", "camera_count": camera_count},
        )
        resp.raise_for_status()


async def sync_cameras_once(config: AgentConfig, token: str | None = None) -> None:
    token = token or await get_device_token(config)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{config.backend_url.rstrip('/')}/devices/cameras",
            headers={"Authorization": f"Bearer {token}"},
            json=sync_payload(config.cameras_path),
        )
        resp.raise_for_status()


async def background_sync_loop(config: AgentConfig, interval_seconds: int = 30) -> None:
    while True:
        try:
            token = await get_device_token(config)
            await heartbeat_once(config, token)
            await sync_cameras_once(config, token)
        except Exception as exc:
            log.warning("backend sync failed: %s", exc)
        await asyncio.sleep(interval_seconds)
