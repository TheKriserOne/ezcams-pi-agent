from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ezcams_pi_agent.backend_client import background_sync_loop
from ezcams_pi_agent.camera_manager import (
    CameraManager,
    CameraUnavailableError,
    open_camera_stream_async,
)
from ezcams_pi_agent.cameras import LocalCamera, find_camera
from ezcams_pi_agent.config import AgentConfig, load_config, loopback_unsigned_enabled
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
RECORDING_MEDIA_TYPE = "video/x-matroska"
_RECORDING_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+\.mkv$")


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


def _recordings_root(config: AgentConfig) -> Path:
    configured = (config.recordings_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(config.cameras_path).expanduser().parent / "clips"


def _recording_dir(config: AgentConfig, camera: LocalCamera) -> Path:
    return (_recordings_root(config) / camera.key).resolve()


def _recording_path(
    config: AgentConfig,
    camera: LocalCamera,
    recording_id: str,
) -> Path:
    if not _RECORDING_ID_RE.fullmatch(recording_id):
        raise HTTPException(status_code=404, detail="Recording not found")
    camera_dir = _recording_dir(config, camera)
    target = (camera_dir / recording_id).resolve()
    if target.parent != camera_dir or not target.is_file():
        raise HTTPException(status_code=404, detail="Recording not found")
    return target


def _recording_read(path: Path) -> dict:
    stat = path.stat()
    timestamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    return {
        "id": path.name,
        "filename": path.name,
        "started_at": timestamp.isoformat(),
        "duration_seconds": None,
        "size_bytes": stat.st_size,
        "content_type": RECORDING_MEDIA_TYPE,
    }


def _list_recordings(config: AgentConfig, camera: LocalCamera) -> list[dict]:
    camera_dir = _recording_dir(config, camera)
    if not camera_dir.is_dir():
        return []
    files: list[tuple[float, Path]] = []
    for path in camera_dir.iterdir():
        try:
            resolved = path.resolve()
            stat = resolved.stat()
        except OSError:
            continue
        if resolved.parent != camera_dir or not resolved.is_file():
            continue
        if not _RECORDING_ID_RE.fullmatch(path.name):
            continue
        files.append((stat.st_mtime, resolved))
    files.sort(key=lambda item: item[0], reverse=True)
    recordings: list[dict] = []
    for _, path in files:
        try:
            recordings.append(_recording_read(path))
        except OSError:
            continue
    return recordings


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

    # Same-host trust: the on-device inference process pulls frames over the
    # loopback interface and cannot produce backend signatures. Accept unsigned
    # requests only when the real socket peer is localhost. We read
    # request.client.host (the actual TCP peer), not a spoofable forwarded
    # header, and the agent serves uvicorn directly (no same-host reverse
    # proxy), so a remote client cannot masquerade as loopback. Disable with
    # allow_loopback_unsigned=false / EZCAMS_PI_ALLOW_LOOPBACK_UNSIGNED=0.
    if loopback_unsigned_enabled(config):
        client = request.client
        if client is not None and client.host in {"127.0.0.1", "::1"}:
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


async def _verify_backend_device_request(
    request: Request,
    *,
    action: str,
) -> AgentConfig:
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
    return config


@asynccontextmanager
async def _lifespan(app: FastAPI):
    sync_task: asyncio.Task | None = None
    try:
        config = _get_config()
    except Exception as exc:
        log.warning("Pi agent config is not ready: %s", exc)
        config = None

    if config is not None:
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


@app.get("/backend/heartbeat")
async def backend_heartbeat(request: Request):
    config = await _verify_backend_device_request(
        request, action="device:heartbeat"
    )
    payload: dict = {
        "device_id": config.device_id,
        "status": "ok",
        "agent_version": "0.1.0",
    }
    try:
        manager = _get_manager()
        payload.update(manager.health())
    except Exception as exc:
        payload["status"] = "degraded"
        payload["error"] = str(exc)
        payload["cameras"] = {}
    return payload


@app.get("/health")
async def health():
    payload: dict = {"status": "ok"}
    try:
        _get_config()
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


@app.get("/recordings/{camera_key}")
async def recordings(camera_key: str, request: Request):
    config, camera = await _verify_backend_request(
        request, action="recordings:list", camera_key=camera_key
    )
    return {"recordings": _list_recordings(config, camera)}


@app.get("/recordings/{camera_key}/{recording_id}")
async def recording_download(camera_key: str, recording_id: str, request: Request):
    config, camera = await _verify_backend_request(
        request, action="recordings:download", camera_key=camera_key
    )
    path = _recording_path(config, camera, recording_id)
    return FileResponse(
        path,
        media_type=RECORDING_MEDIA_TYPE,
        filename=path.name,
    )
