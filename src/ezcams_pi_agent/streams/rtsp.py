from __future__ import annotations

import time
from collections.abc import Iterator
from os import environ

from ezcams_pi_agent.streams.common import CameraStream, CameraStreamError

_DEFAULT_FFMPEG_OPTIONS = "rtsp_transport;tcp|flags;low_delay"


class RtspError(CameraStreamError):
    """Raised when an RTSP source is unavailable or read fails."""


class RtspStream(CameraStream):
    """Decode video from an RTSP source to JPEG frames using OpenCV+FFmpeg."""

    def __init__(
        self,
        source_url: str,
        fps: float = 10.0,
        jpeg_quality: int = 80,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.source_url = source_url
        self.fps = float(fps)
        self.jpeg_quality = max(1, min(100, jpeg_quality))
        self._frame_interval = 1.0 / float(fps)

    def _capture(self):
        import cv2

        environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", _DEFAULT_FFMPEG_OPTIONS)
        cap = cv2.VideoCapture(self.source_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RtspError(f"failed to open RTSP stream {self.source_url}")
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5_000)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5_000)
        return cap

    def prepare(self) -> None:
        cap = self._capture()
        try:
            ok, _ = cap.read()
            if not ok:
                raise RtspError(f"no frames from RTSP stream {self.source_url}")
        finally:
            cap.release()

    def jpeg_frames(self) -> Iterator[bytes]:
        import cv2

        cap = self._capture()
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        next_frame_at = 0.0
        try:
            while True:
                try:
                    ok, frame = cap.read()
                except cv2.error as exc:
                    raise RtspError(
                        f"failed reading RTSP stream {self.source_url}: {exc}"
                    ) from exc
                if not ok or frame is None:
                    raise RtspError(
                        f"RTSP stream ended or produced no frame: {self.source_url}"
                    )

                now = time.monotonic()
                if now < next_frame_at:
                    continue
                next_frame_at = now + self._frame_interval

                ok_enc, buf = cv2.imencode(".jpg", frame, encode_params)
                if not ok_enc:
                    raise RtspError(
                        f"failed to encode JPEG from RTSP stream: {self.source_url}"
                    )
                yield buf.tobytes()
        except GeneratorExit:
            return
        finally:
            cap.release()
