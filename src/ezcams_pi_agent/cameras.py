from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

_CAMERA_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RTSP_SCHEMES = ("rtsp://", "rtsps://")
_HTTP_SCHEMES = ("http://", "https://")


@dataclass(frozen=True)
class NativeSource:
    camera_index: int = 0
    width: int = 1280
    height: int = 720
    hflip: bool = False
    vflip: bool = False

    @property
    def kind(self) -> str:
        return "native"

    @property
    def stream_type(self) -> str:
        return "native"


@dataclass(frozen=True)
class RtspSource:
    url: str

    @property
    def kind(self) -> str:
        return "rtsp"

    @property
    def stream_type(self) -> str:
        return "rtsp"


@dataclass(frozen=True)
class HttpMjpegSource:
    url: str

    @property
    def kind(self) -> str:
        return "http_mjpeg"

    @property
    def stream_type(self) -> str:
        return "mjpeg"


CameraSource = Union[NativeSource, RtspSource, HttpMjpegSource]


@dataclass(frozen=True)
class LocalCamera:
    key: str
    name: str
    lat: float
    lng: float
    stream_url: str = ""
    snapshot_url: str = ""
    stream_type: str = "mjpeg"
    description: str = ""
    is_active: bool = True
    is_available: bool = True
    fps: int = 10
    source: CameraSource | None = field(default=None)

    @property
    def resolved_source(self) -> CameraSource | None:
        if self.source is not None:
            return self.source
        if self.stream_url:
            return HttpMjpegSource(url=self.stream_url)
        return None

    @property
    def effective_stream_type(self) -> str:
        resolved = self.resolved_source
        if resolved is not None:
            return resolved.stream_type
        return self.stream_type or "mjpeg"


def _validate_key(key: str) -> str:
    stripped = key.strip()
    if not _CAMERA_KEY_RE.fullmatch(stripped):
        raise ValueError(
            f"Invalid camera key {key!r}; use only letters, numbers, dots, underscores, and dashes"
        )
    return stripped


def _parse_source(raw: dict) -> CameraSource:
    if not isinstance(raw, dict):
        raise ValueError("camera source must be an object")
    source_type = str(raw.get("type") or "").strip().lower()
    if not source_type:
        raise ValueError("camera source missing required 'type'")

    if source_type == "native":
        resolution = raw.get("resolution") or {}
        if not isinstance(resolution, dict):
            raise ValueError("native source 'resolution' must be an object")
        return NativeSource(
            camera_index=int(raw.get("camera_index", 0)),
            width=int(resolution.get("width", 1280)),
            height=int(resolution.get("height", 720)),
            hflip=bool(raw.get("hflip", False)),
            vflip=bool(raw.get("vflip", False)),
        )

    if source_type == "rtsp":
        url = str(raw.get("url") or "").strip()
        if not url.startswith(_RTSP_SCHEMES):
            raise ValueError("rtsp source URL must start with rtsp:// or rtsps://")
        return RtspSource(url=url)

    if source_type in {"http_mjpeg", "mjpeg", "http"}:
        url = str(raw.get("url") or "").strip()
        if not url.startswith(_HTTP_SCHEMES):
            raise ValueError("http_mjpeg source URL must start with http:// or https://")
        return HttpMjpegSource(url=url)

    raise ValueError(f"Unsupported camera source type: {source_type!r}")


def load_cameras(path: str | Path) -> list[LocalCamera]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("cameras", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("cameras.json must contain a list or an object with a cameras list")

    cameras: list[LocalCamera] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each camera entry must be an object")

        source: CameraSource | None = None
        raw_source = item.get("source")
        if raw_source is not None:
            source = _parse_source(raw_source)

        cameras.append(
            LocalCamera(
                key=_validate_key(str(item["key"])),
                name=str(item.get("name") or item["key"]),
                lat=float(item["lat"]),
                lng=float(item["lng"]),
                stream_url=str(item.get("stream_url") or "").strip(),
                snapshot_url=str(item.get("snapshot_url") or "").strip(),
                stream_type=str(item.get("stream_type") or "mjpeg"),
                description=str(item.get("description") or ""),
                is_active=bool(item.get("is_active", True)),
                is_available=bool(item.get("is_available", True)),
                fps=int(item.get("fps", 10)),
                source=source,
            )
        )
    return cameras


def find_camera(path: str | Path, key: str) -> LocalCamera | None:
    for camera in load_cameras(path):
        if camera.key == key:
            return camera
    return None


def sync_payload(path: str | Path) -> dict:
    return {
        "cameras": [
            {
                "key": camera.key,
                "name": camera.name,
                "lat": camera.lat,
                "lng": camera.lng,
                "stream_type": camera.effective_stream_type,
                "description": camera.description,
                "is_active": camera.is_active,
                "is_available": camera.is_available,
            }
            for camera in load_cameras(path)
        ]
    }


def heartbeat_sync_items(
    path: str | Path,
    health: dict | None = None,
) -> list[dict]:
    """Camera catalog for backend heartbeat: cameras.json merged with runtime health."""
    health_map: dict = {}
    if isinstance(health, dict):
        raw = health.get("cameras")
        if isinstance(raw, dict):
            health_map = raw

    items: list[dict] = []
    for camera in load_cameras(path):
        runtime = health_map.get(camera.key)
        if isinstance(runtime, dict) and "healthy" in runtime:
            is_available = bool(runtime.get("healthy"))
        else:
            is_available = camera.is_available
        items.append(
            {
                "key": camera.key,
                "name": camera.name,
                "lat": camera.lat,
                "lng": camera.lng,
                "stream_type": camera.effective_stream_type,
                "description": camera.description,
                "is_active": camera.is_active,
                "is_available": is_available,
            }
        )
    return items


def write_empty_camera_file(path: str | Path) -> None:
    p = Path(path)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump({"cameras": []}, f, indent=2)
        f.write("\n")
