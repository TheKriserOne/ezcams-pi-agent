from __future__ import annotations

import time
from collections.abc import Iterator

import requests

from ezcams_pi_agent.streams.common import (
    CameraStream,
    CameraStreamError,
    extract_jpeg_frames,
)


class HttpMjpegError(CameraStreamError):
    """Raised when an HTTP MJPEG source is unavailable or produces no frames."""


class HttpMjpegStream(CameraStream):
    """Pull JPEG frames from an HTTP multipart MJPEG (or raw JPEG byte) source."""

    def __init__(
        self,
        source_url: str,
        fps: float = 10.0,
        timeout_seconds: float = 10.0,
        chunk_size: int = 8192,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.source_url = source_url
        self.fps = float(fps)
        self.timeout_seconds = timeout_seconds
        self.chunk_size = chunk_size
        self._frame_interval = 1.0 / float(fps)

    def prepare(self) -> None:
        try:
            with requests.get(
                self.source_url,
                stream=True,
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
        except requests.RequestException as exc:
            raise HttpMjpegError(
                f"failed to initialize HTTP MJPEG source {self.source_url}: {exc}"
            ) from exc

    def jpeg_frames(self) -> Iterator[bytes]:
        session = requests.Session()
        next_frame_at = 0.0
        yielded_any = False
        try:
            try:
                with session.get(
                    self.source_url,
                    stream=True,
                    timeout=self.timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    for frame_bytes in extract_jpeg_frames(
                        response.iter_content(chunk_size=self.chunk_size)
                    ):
                        yielded_any = True
                        now = time.monotonic()
                        if now < next_frame_at:
                            continue
                        next_frame_at = now + self._frame_interval
                        yield frame_bytes
            except requests.RequestException as exc:
                raise HttpMjpegError(
                    f"HTTP MJPEG source unavailable {self.source_url}: {exc}"
                ) from exc

            if not yielded_any:
                raise HttpMjpegError(
                    f"HTTP MJPEG source ended without JPEG frames: {self.source_url}"
                )
            raise HttpMjpegError(f"HTTP MJPEG source ended: {self.source_url}")
        except GeneratorExit:
            return
        finally:
            session.close()
