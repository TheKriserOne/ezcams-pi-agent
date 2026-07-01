"""Configuration for the inference process."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InferenceConfig:
    hef_path: Path
    agent_url: str
    camera_keys: list[str]
    infer_fps: float = 5.0
    score_threshold: float = 0.4
    max_boxes: int = 50
    # Recording knobs.
    clip_dir: Path = Path("clips")
    min_clip_seconds: float = 10.0
    grace_seconds: float = 3.0
    # Consecutive person detections required before a clip is triggered.
    trigger_frames: int = 2
