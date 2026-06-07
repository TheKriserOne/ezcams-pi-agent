from __future__ import annotations

import io
import queue
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from ezcams_pi_agent.streams.common import CameraStream, CameraStreamError


class NativeUnavailableError(CameraStreamError):
    """Raised when picamera2/libcamera is not importable on this host."""


class NativeCameraError(CameraStreamError):
    """Raised when the native Pi camera cannot be opened or read."""


def _import_picamera2():
    try:
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput
    except ImportError as exc:
        raise NativeUnavailableError(
            "picamera2 is not available. On Raspberry Pi install the system "
            "package (`sudo apt install -y python3-picamera2 python3-libcamera`) "
            "and recreate the venv with `--system-site-packages` so it can be "
            f"imported. Current interpreter: {sys.executable}"
        ) from exc
    return Picamera2, JpegEncoder, FileOutput


class _FrameSink(io.BufferedIOBase):
    """BufferedIOBase sink that picamera2's FileOutput writes encoded JPEGs into."""

    def __init__(self, max_queue: int = 4) -> None:
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max_queue)

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        # Drop oldest if a slow consumer falls behind; latest-frame semantics.
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(data)
            except queue.Full:
                pass
        return len(data)

    def flush(self) -> None:
        return

    def close(self) -> None:
        return

    def get(self, timeout: float) -> bytes | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class NativeStream(CameraStream):
    """Capture frames from a directly-attached Raspberry Pi camera with HW JPEG."""

    def __init__(
        self,
        camera_index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: float = 10.0,
        hflip: bool = False,
        vflip: bool = False,
        jpeg_quality: int = 80,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = float(fps)
        self.hflip = hflip
        self.vflip = vflip
        self.jpeg_quality = max(1, min(100, jpeg_quality))
        self._camera: Any = None
        self._sink: _FrameSink | None = None
        self._encoder: Any = None
        self._lock = threading.Lock()

    def prepare(self) -> None:
        with self._lock:
            if self._camera is not None:
                return

            Picamera2, JpegEncoder, FileOutput = _import_picamera2()
            try:
                camera = Picamera2(camera_num=self.camera_index)
                frame_period_us = int(1_000_000 / self.fps)
                video_config: dict[str, Any] = {
                    "main": {"size": (self.width, self.height), "format": "RGB888"},
                    "controls": {
                        "FrameDurationLimits": (frame_period_us, frame_period_us)
                    },
                }
                if self.hflip or self.vflip:
                    try:
                        from libcamera import Transform
                    except ImportError as exc:
                        raise NativeCameraError(
                            f"libcamera Transform unavailable for flip controls: {exc}"
                        ) from exc
                    video_config["transform"] = Transform(
                        hflip=self.hflip, vflip=self.vflip
                    )

                try:
                    config = camera.create_video_configuration(**video_config)
                except TypeError:
                    video_config.pop("controls", None)
                    config = camera.create_video_configuration(**video_config)

                camera.configure(config)

                sink = _FrameSink()
                encoder = JpegEncoder(q=self.jpeg_quality)
                output = FileOutput(sink)
                camera.start_recording(encoder, output)
                # Brief settle window so first frame is usable.
                time.sleep(0.2)
            except NativeUnavailableError:
                raise
            except NativeCameraError:
                raise
            except Exception as exc:
                raise NativeCameraError(
                    f"failed to start native camera index {self.camera_index}: {exc}"
                ) from exc

            self._camera = camera
            self._sink = sink
            self._encoder = encoder

    def stop(self) -> None:
        with self._lock:
            camera = self._camera
            self._camera = None
            self._sink = None
            self._encoder = None

        if camera is None:
            return
        try:
            camera.stop_recording()
        except Exception:
            pass
        try:
            camera.close()
        except Exception:
            pass

    def jpeg_frames(self) -> Iterator[bytes]:
        self.prepare()
        try:
            while True:
                with self._lock:
                    sink = self._sink
                    if sink is None:
                        raise NativeCameraError("native camera was stopped")
                frame = sink.get(timeout=5.0)
                if frame is None:
                    raise NativeCameraError(
                        f"no JPEG frame received from native camera index {self.camera_index}"
                    )
                yield frame
        except GeneratorExit:
            return
