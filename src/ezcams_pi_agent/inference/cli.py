"""CLI for the EZ Cams Pi inference process.

Runs as a SEPARATE process from the streaming agent. By default it discovers
the agent's port and active cameras from the same ``.ezcams-pi`` config, then
consumes each camera's MJPEG stream over loopback.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ezcams_pi_agent.cameras import load_cameras
from ezcams_pi_agent.config import load_config
from ezcams_pi_agent.inference.config import InferenceConfig
from ezcams_pi_agent.inference.engine import run_engine

log = logging.getLogger("ezcams_pi_agent.inference")

_DEFAULT_HEF_HINT = "/usr/local/hailo/resources/models/hailo10h/yolov11s.hef"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ezcams-pi-inference",
        description=(
            "On-device YOLO person detection across cameras using one shared "
            "Hailo model; records clips on detection (separate process)."
        ),
    )
    p.add_argument(
        "--hef",
        required=True,
        help=f"Path to the HAILO10H detection HEF (e.g. {_DEFAULT_HEF_HINT}).",
    )
    p.add_argument(
        "--agent-url",
        default=None,
        help="Streaming agent base URL (default https://127.0.0.1:<config port>).",
    )
    p.add_argument(
        "--cameras",
        default=None,
        help="Comma-separated camera keys (default: all active cameras in cameras.json).",
    )
    p.add_argument(
        "--config-dir",
        default=None,
        help="Agent config dir (default EZCAMS_PI_CONFIG_DIR or .ezcams-pi).",
    )
    p.add_argument("--infer-fps", type=float, default=5.0, help="Inference rate per camera (default 5).")
    p.add_argument("--score-thres", type=float, default=0.4, help="Min person score (default 0.4).")
    p.add_argument("--max-boxes", type=int, default=50, help="Max person boxes per frame (default 50).")
    p.add_argument(
        "--clip-dir",
        default=None,
        help="Where to write clips (default config recordings_dir / .ezcams-pi/clips).",
    )
    p.add_argument("--min-clip-seconds", type=float, default=10.0, help="Minimum clip length (default 10).")
    p.add_argument(
        "--grace-seconds",
        type=float,
        default=3.0,
        help="Keep recording this long after the last person detection (default 3).",
    )
    p.add_argument(
        "--trigger-frames",
        type=int,
        default=2,
        help="Consecutive detections needed to start a clip (default 2; debounce).",
    )
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def _resolve(args: argparse.Namespace) -> InferenceConfig | None:
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else None
    agent_cfg = None
    try:
        agent_cfg = load_config(config_dir)
    except Exception as exc:
        log.debug("agent config not loaded: %s", exc)

    agent_url = args.agent_url
    if agent_url is None:
        if agent_cfg is None:
            log.error("Could not load agent config; pass --agent-url explicitly.")
            return None
        # The agent serves HTTPS (self-signed) — see AgentStreamSource verify_tls.
        agent_url = f"https://127.0.0.1:{agent_cfg.port}"

    if args.cameras:
        camera_keys = [k.strip() for k in args.cameras.split(",") if k.strip()]
    elif agent_cfg is not None:
        cameras = load_cameras(agent_cfg.cameras_path)
        camera_keys = [c.key for c in cameras if c.is_active and c.resolved_source is not None]
    else:
        log.error("No --cameras given and agent config unavailable.")
        return None

    if args.clip_dir:
        clip_dir = Path(args.clip_dir).expanduser()
    elif agent_cfg is not None and agent_cfg.recordings_dir:
        clip_dir = Path(agent_cfg.recordings_dir).expanduser()
    elif agent_cfg is not None:
        clip_dir = Path(agent_cfg.cameras_path).expanduser().parent / "clips"
    else:
        clip_dir = Path("clips")

    return InferenceConfig(
        hef_path=Path(args.hef).expanduser(),
        agent_url=agent_url,
        camera_keys=camera_keys,
        infer_fps=args.infer_fps,
        score_threshold=args.score_thres,
        max_boxes=args.max_boxes,
        clip_dir=clip_dir,
        min_clip_seconds=args.min_clip_seconds,
        grace_seconds=args.grace_seconds,
        trigger_frames=args.trigger_frames,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    cfg = _resolve(args)
    if cfg is None:
        return 2
    if not cfg.hef_path.exists():
        log.error("HEF not found: %s", cfg.hef_path)
        return 2
    return run_engine(cfg)
