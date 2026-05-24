from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ezcams_pi_agent.backend_client import background_sync_loop
from ezcams_pi_agent.cameras import LocalCamera, find_camera, load_cameras
from ezcams_pi_agent.config import AgentConfig, load_config
from ezcams_pi_agent.crypto import payload_from_header, verify_payload_signature

log = logging.getLogger(__name__)
app = FastAPI(title="EZ Cams Pi Agent", version="0.1.0")

_used_nonces: dict[str, datetime] = {}
_config: AgentConfig | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get_config() -> AgentConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _purge_nonces(now: datetime) -> None:
    for nonce, expires_at in list(_used_nonces.items()):
        if expires_at <= now:
            _used_nonces.pop(nonce, None)


async def _verify_backend_request(
    request: Request,
    *,
    action: str,
    camera_key: str,
) -> tuple[AgentConfig, LocalCamera]:
    config = _get_config()
    payload_header = request.headers.get("X-EZCams-Payload", "")
    signature = request.headers.get("X-EZCams-Signature", "")
    if not payload_header or not signature:
        raise HTTPException(status_code=401, detail="Missing backend signature")

    try:
        payload = payload_from_header(payload_header)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid signed payload")

    if not verify_payload_signature(payload, signature, config.backend_public_key_pem):
        raise HTTPException(status_code=401, detail="Invalid backend signature")

    expected = {
        "device_id": config.device_id,
        "device_camera_key": camera_key,
        "action": action,
        "method": request.method,
        "path": request.url.path,
        "body_sha256": hashlib.sha256(b"").hexdigest(),
    }
    for key, value in expected.items():
        if not secrets.compare_digest(str(payload.get(key, "")), str(value)):
            raise HTTPException(status_code=403, detail=f"Signed payload mismatch: {key}")

    try:
        expires_at = _parse_datetime(str(payload["expires_at"]))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid signed payload expiry")

    now = _now()
    if expires_at <= now:
        raise HTTPException(status_code=401, detail="Signed request has expired")

    nonce = str(payload.get("nonce") or "")
    if not nonce:
        raise HTTPException(status_code=401, detail="Signed request missing nonce")
    _purge_nonces(now)
    if nonce in _used_nonces:
        raise HTTPException(status_code=401, detail="Signed request nonce was already used")
    _used_nonces[nonce] = expires_at

    camera = find_camera(config.cameras_path, camera_key)
    if camera is None or not camera.is_active:
        raise HTTPException(status_code=404, detail="Camera not found")
    return config, camera


@app.on_event("startup")
async def startup() -> None:
    try:
        config = _get_config()
    except Exception as exc:
        log.warning("Pi agent config is not ready: %s", exc)
        return
    if os.getenv("EZCAMS_PI_DISABLE_SYNC", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    asyncio.create_task(background_sync_loop(config))


@app.get("/health")
async def health():
    try:
        config = _get_config()
        load_cameras(config.cameras_path)
    except Exception as exc:
        log.warning("Pi agent health check failed: %s", exc)
        return {"status": "error"}
    return {"status": "ok"}


@app.get("/snapshot/{camera_key}")
async def snapshot(camera_key: str, request: Request):
    _, camera = await _verify_backend_request(request, action="snapshot", camera_key=camera_key)
    source_url = camera.snapshot_url or camera.stream_url
    if not source_url:
        raise HTTPException(status_code=404, detail="Camera snapshot source is not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        if camera.snapshot_url:
            resp = await client.get(source_url)
            if resp.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Camera snapshot source failed: {resp.status_code}")
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
            )

        buf = b""
        async with client.stream("GET", source_url) as resp:
            if resp.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Camera stream source failed: {resp.status_code}")
            async for chunk in resp.aiter_bytes(4096):
                buf += chunk
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                if start != -1 and end != -1:
                    return Response(content=buf[start : end + 2], media_type="image/jpeg")
    raise HTTPException(status_code=504, detail="No camera frame received")


@app.get("/stream/{camera_key}")
async def stream(camera_key: str, request: Request):
    _, camera = await _verify_backend_request(request, action="stream", camera_key=camera_key)
    if not camera.stream_url:
        raise HTTPException(status_code=404, detail="Camera stream source is not configured")

    client = httpx.AsyncClient(timeout=None)
    try:
        req = client.build_request("GET", camera.stream_url)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Camera stream source failed: {exc}")

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Camera stream source failed: {resp.status_code}")

    async def generate():
        try:
            async for chunk in resp.aiter_bytes(4096):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        media_type=resp.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame"),
    )
