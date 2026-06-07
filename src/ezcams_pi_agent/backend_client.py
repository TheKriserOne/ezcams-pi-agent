from __future__ import annotations

import asyncio
import logging

import httpx

from ezcams_pi_agent.cameras import load_cameras, sync_payload
from ezcams_pi_agent.config import AgentConfig, read_text

log = logging.getLogger(__name__)


def _device_auth_header(config: AgentConfig) -> dict[str, str]:
    secret = read_text(config.device_secret_path).strip()
    return {"Authorization": f"Bearer {secret}"}


async def heartbeat_once(config: AgentConfig) -> None:
    camera_count = len(load_cameras(config.cameras_path))
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{config.backend_url.rstrip('/')}/devices/heartbeat",
            headers=_device_auth_header(config),
            json={"status": "online", "agent_version": "0.1.0", "camera_count": camera_count},
        )
        resp.raise_for_status()


async def sync_cameras_once(config: AgentConfig) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{config.backend_url.rstrip('/')}/devices/cameras",
            headers=_device_auth_header(config),
            json=sync_payload(config.cameras_path),
        )
        resp.raise_for_status()


async def background_sync_loop(config: AgentConfig, interval_seconds: int = 30) -> None:
    while True:
        try:
            await heartbeat_once(config)
            await sync_cameras_once(config)
        except Exception as exc:
            log.warning("backend sync failed: %s", exc)
        await asyncio.sleep(interval_seconds)
