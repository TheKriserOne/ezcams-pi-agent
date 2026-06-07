from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ezcams_pi_agent.backend_client import background_sync_loop
from ezcams_pi_agent.camera_manager import (
    CameraManager,
    CameraUnavailableError,
    open_camera_stream_async,
)
from ezcams_pi_agent.cameras import LocalCamera, find_camera
from ezcams_pi_agent.config import AgentConfig, load_config, public_api_enabled
from ezcams_pi_agent.crypto import payload_from_header, verify_payload_signature
from ezcams_pi_agent.streams.common import MJPEG_MEDIA_TYPE

log = logging.getLogger(__name__)

_used_nonces: dict[str, datetime] = {}
_config: AgentConfig | None = None
_manager: CameraManager | None = None

STREAM_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "X-Accel-Buffering": "no",
}


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


def _get_manager() -> CameraManager:
    """Lazily build and start the CameraManager on first access.

    Used as a fallback when lifespan startup didn't run (e.g. TestClient
    used without `with`). Lifespan also calls this to eagerly warm it up.
    """
    global _manager
    if _manager is None:
        config = _get_config()
        manager = CameraManager.from_config(config)
        manager.start()
        _manager = manager
    return _manager


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
    camera = find_camera(config.cameras_path, camera_key)
    if camera is None or not camera.is_active:
        raise HTTPException(status_code=404, detail="Camera not found")

    if public_api_enabled(config):
        return config, camera

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

    return config, camera


@asynccontextmanager
async def _lifespan(app: FastAPI):
    sync_task: asyncio.Task | None = None
    try:
        config = _get_config()
    except Exception as exc:
        log.warning("Pi agent config is not ready: %s", exc)
        config = None

    if config is not None:
        if public_api_enabled(config):
            log.warning(
                "allow_public_api is enabled: /stream and /snapshot accept "
                "unsigned requests (testing only)"
            )
        try:
            _get_manager()
        except Exception as exc:
            log.warning("camera manager startup failed: %s", exc)

        if os.getenv("EZCAMS_PI_DISABLE_SYNC", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            sync_task = asyncio.create_task(background_sync_loop(config))

    try:
        yield
    finally:
        if sync_task is not None:
            sync_task.cancel()
            try:
                await sync_task
            except (asyncio.CancelledError, Exception):
                pass

        global _manager
        manager = _manager
        _manager = None
        if manager is not None:
            try:
                await manager.stop_async()
            except Exception as exc:
                log.warning("camera manager shutdown failed: %s", exc)


app = FastAPI(title="EZ Cams Pi Agent", version="0.1.0", lifespan=_lifespan)


@app.get("/health")
async def health():
    payload: dict = {"status": "ok"}
    try:
        config = _get_config()
        payload["allow_public_api"] = public_api_enabled(config)
    except Exception as exc:
        log.warning("Pi agent health check failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    if _manager is not None:
        payload.update(_manager.health())
    else:
        payload["cameras"] = {}
    return payload


@app.get("/snapshot/{camera_key}")
async def snapshot(camera_key: str, request: Request):
    _, camera = await _verify_backend_request(
        request, action="snapshot", camera_key=camera_key
    )

    # If a dedicated still-image URL is configured, fetch it directly. This
    # preserves the previous behavior and yields a higher-quality still than
    # the next streaming frame would.
    if camera.snapshot_url:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.get(camera.snapshot_url)
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502, detail=f"Camera snapshot source failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Camera snapshot source failed: {resp.status_code}",
                )
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
            )

    # Otherwise return the worker's most recent JPEG instantly from cache.
    try:
        manager = _get_manager()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"camera manager unavailable: {exc}"
        ) from exc

    try:
        _, _, frame = await open_camera_stream_async(manager, camera_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Camera not found") from exc
    except CameraUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return Response(content=frame, media_type="image/jpeg")


@app.get("/stream/{camera_key}")
async def stream(camera_key: str, request: Request):
    _, _ = await _verify_backend_request(
        request, action="stream", camera_key=camera_key
    )
    try:
        manager = _get_manager()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"camera manager unavailable: {exc}"
        ) from exc

    try:
        worker, sequence, frame = await open_camera_stream_async(manager, camera_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Camera not found") from exc
    except CameraUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return StreamingResponse(
        worker.mjpeg_chunks_async(sequence, frame),
        media_type=MJPEG_MEDIA_TYPE,
        headers=STREAM_HEADERS,
    )
