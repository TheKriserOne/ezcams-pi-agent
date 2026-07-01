"""Per-camera clip recorder — mux already-JPEG frames into an MJPEG ``.mkv``.

Decision B: the frames arriving from the agent are already JPEG, so a clip is
just those bytes piped to ``ffmpeg -c copy`` — no decode, no re-encode (the
Pi 5 has no hardware video encoder, so re-encoding N streams would burn CPU).

Lifecycle (driven from two threads, lock-protected):
- the source thread calls :meth:`feed` for EVERY frame,
- the output/detection thread calls :meth:`note_detection` when a person is
  present, which (re)arms a ``record_until`` deadline.

A clip opens on the first active frame, keeps recording while detections keep
arriving, and closes ``grace_seconds`` after the last detection — but never
shorter than ``min_clip_seconds``.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ezcams_pi_agent.inference")


class ClipRecorder:
    def __init__(
        self,
        camera_key: str,
        clip_dir: Path,
        min_clip_seconds: float = 10.0,
        grace_seconds: float = 3.0,
    ) -> None:
        self._key = camera_key
        self._dir = Path(clip_dir) / camera_key
        self._min = float(min_clip_seconds)
        self._grace = float(grace_seconds)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._path: Path | None = None
        self._started_at = 0.0
        self._record_until = 0.0

    def note_detection(self) -> None:
        """Arm or extend recording; called whenever a person is present."""
        with self._lock:
            self._record_until = time.monotonic() + self._grace

    def feed(self, jpeg: bytes) -> None:
        """Write one frame if recording is active; open/close as needed."""
        with self._lock:
            now = time.monotonic()
            active = now < self._record_until
            # Honor the minimum clip length once a clip has started.
            if self._proc is not None and now < self._started_at + self._min:
                active = True
            if active:
                if self._proc is None:
                    self._open(now)
                self._write(jpeg)
            elif self._proc is not None:
                self._close()

    def stop(self) -> None:
        """Finalize any open clip (called on shutdown)."""
        with self._lock:
            if self._proc is not None:
                self._close()

    # --- internals; call with the lock held ---

    def _open(self, now: float) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = self._dir / f"{self._key}_{stamp}.mkv"
        # image2pipe + mjpeg parses the concatenated JPEGs; wallclock timestamps
        # keep real-time playback under variable frame arrival; -c copy = no
        # re-encode.
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-use_wallclock_as_timestamps", "1",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-i", "-",
            "-c:v", "copy", "-an", str(self._path),
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            self._started_at = now
            log.info("camera %s: recording -> %s", self._key, self._path.name)
        except Exception as exc:
            log.error("camera %s: failed to start recorder: %s", self._key, exc)
            self._proc = None
            self._path = None

    def _write(self, jpeg: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(jpeg)
        except (BrokenPipeError, ValueError, OSError) as exc:
            log.warning("camera %s: recorder write failed: %s", self._key, exc)
            self._close()

    def _close(self) -> None:
        proc, path = self._proc, self._path
        self._proc, self._path = None, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        dur = time.monotonic() - self._started_at
        log.info(
            "camera %s: clip saved %s (%.1fs)",
            self._key,
            path.name if path else "?",
            dur,
        )
