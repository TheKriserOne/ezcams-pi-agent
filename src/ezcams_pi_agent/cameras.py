from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_CAMERA_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class LocalCamera:
    key: str
    name: str
    lat: float
    lng: float
    stream_url: str
    snapshot_url: str = ""
    stream_type: str = "mjpeg"
    description: str = ""
    is_active: bool = True
    is_available: bool = True


def _validate_key(key: str) -> str:
    stripped = key.strip()
    if not _CAMERA_KEY_RE.fullmatch(stripped):
        raise ValueError(
            f"Invalid camera key {key!r}; use only letters, numbers, dots, underscores, and dashes"
        )
    return stripped


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
        cameras.append(
            LocalCamera(
                key=_validate_key(str(item["key"])),
                name=str(item.get("name") or item["key"]),
                lat=float(item["lat"]),
                lng=float(item["lng"]),
                stream_url=str(item["stream_url"]).strip(),
                snapshot_url=str(item.get("snapshot_url") or "").strip(),
                stream_type=str(item.get("stream_type") or "mjpeg"),
                description=str(item.get("description") or ""),
                is_active=bool(item.get("is_active", True)),
                is_available=bool(item.get("is_available", True)),
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
                "stream_type": camera.stream_type,
                "description": camera.description,
                "is_active": camera.is_active,
                "is_available": camera.is_available,
            }
            for camera in load_cameras(path)
        ]
    }


def write_empty_camera_file(path: str | Path) -> None:
    p = Path(path)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump({"cameras": []}, f, indent=2)
        f.write("\n")
