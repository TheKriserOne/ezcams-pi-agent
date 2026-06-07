from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ezcams_pi_agent.cameras import LocalCamera, load_cameras
from ezcams_pi_agent.config import AgentConfig, RuntimeConfig
from ezcams_pi_agent.streams.common import CameraStream, build_mjpeg_chunk
from ezcams_pi_agent.streams.factory import build_camera_stream

log = logging.getLogger(__name__)

StreamBuilder = Callable[[LocalCamera, RuntimeConfig], CameraStream]


class CameraUnavailableError(RuntimeError):
    """Raised when a known camera has no frame available within the timeout."""


def _set_event_threadsafe(event: asyncio.Event) -> None:
    event.set()


@dataclass
class _Subscription:
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event


class CameraWorker:
    """Owns one upstream camera stream; publishes latest JPEG with push fan-out.

    Subscribers register an `asyncio.Event`; the producer thread fires it via
    `loop.call_soon_threadsafe(event.set)` whenever a new frame lands. No
    polling — latency floor is the upstream frame interval.
    """

    def __init__(
        self,
        camera: LocalCamera,
        runtime: RuntimeConfig,
        stream_builder: StreamBuilder = build_camera_stream,
    ) -> None:
        self.camera = camera
        self.runtime = runtime
        self._stream_builder = stream_builder
        self._frame_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            name=f"camera-reader-{camera.key}",
            target=self._run,
            daemon=True,
        )
        self._started = False
        self._healthy = False
        self._last_error: str | None = None
        self._last_frame: bytes | None = None
        self._last_frame_at: float | None = None
        self._sequence = 0
        self._frames_seen = 0
        self._reconnects = 0
        self._started_at: float | None = None
        self._active_stream: CameraStream | None = None
        self._subscriptions: list[_Subscription] = []

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._started_at = time.time()
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._stream_lock:
            active = self._active_stream
        if active is not None:
            try:
                active.stop()
            except Exception:
                pass
        self._wake_all_subscribers()
        if self._started:
            self._thread.join(timeout=5.0)

    def latest(self) -> tuple[int, bytes | None]:
        with self._frame_lock:
            return self._sequence, self._last_frame

    def status(self) -> dict[str, Any]:
        with self._frame_lock:
            return {
                "key": self.camera.key,
                "source_type": self.camera.effective_stream_type,
                "running": self._thread.is_alive(),
                "healthy": self._healthy,
                "last_error": self._last_error,
                "last_frame_at": self._last_frame_at,
                "last_frame_age": (
                    time.time() - self._last_frame_at
                    if self._last_frame_at is not None
                    else None
                ),
                "frames_seen": self._frames_seen,
                "reconnects": self._reconnects,
                "started_at": self._started_at,
                "subscribers": len(self._subscriptions),
            }

    @contextmanager
    def _subscribe(self) -> Iterator[_Subscription]:
        loop = asyncio.get_running_loop()
        sub = _Subscription(loop=loop, event=asyncio.Event())
        with self._frame_lock:
            self._subscriptions.append(sub)
        try:
            yield sub
        finally:
            with self._frame_lock:
                try:
                    self._subscriptions.remove(sub)
                except ValueError:
                    pass

    async def wait_for_first_frame_async(
        self, timeout_seconds: float
    ) -> tuple[int, bytes] | None:
        with self._frame_lock:
            if self._last_frame is not None:
                return self._sequence, self._last_frame
            if self._stop_event.is_set():
                return None

        deadline = time.monotonic() + timeout_seconds
        with self._subscribe() as sub:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(sub.event.wait(), remaining)
                except TimeoutError:
                    return None
                sub.event.clear()
                with self._frame_lock:
                    if self._stop_event.is_set():
                        return None
                    if self._last_frame is not None:
                        return self._sequence, self._last_frame

    async def mjpeg_chunks_async(
        self,
        first_sequence: int,
        first_frame: bytes,
    ) -> AsyncIterator[bytes]:
        yield build_mjpeg_chunk(first_frame)
        last_seq = first_sequence
        with self._subscribe() as sub:
            while True:
                with self._frame_lock:
                    if self._stop_event.is_set():
                        return
                    seq = self._sequence
                    frame = self._last_frame
                if frame is not None and seq > last_seq:
                    last_seq = seq
                    yield build_mjpeg_chunk(frame)
                    continue

                # Clear before re-checking to avoid lost wakeups: if the
                # producer fires between our clear and the recheck we still
                # observe the new sequence on this iteration.
                sub.event.clear()
                with self._frame_lock:
                    if self._stop_event.is_set():
                        return
                    seq = self._sequence
                    frame = self._last_frame
                if frame is not None and seq > last_seq:
                    last_seq = seq
                    yield build_mjpeg_chunk(frame)
                    continue

                await sub.event.wait()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            stream: CameraStream | None = None
            try:
                stream = self._stream_builder(self.camera, self.runtime)
                with self._stream_lock:
                    self._active_stream = stream
                for frame_bytes in stream.jpeg_frames():
                    if self._stop_event.is_set():
                        break
                    if not frame_bytes:
                        continue
                    self._record_frame(frame_bytes)
                if not self._stop_event.is_set():
                    raise CameraUnavailableError(
                        f"camera stream ended: {self.camera.key}"
                    )
            except Exception as exc:
                self._record_error(exc)
                if self._stop_event.wait(self.runtime.reconnect_delay_seconds):
                    break
            finally:
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                with self._stream_lock:
                    if self._active_stream is stream:
                        self._active_stream = None

    def _record_frame(self, frame_bytes: bytes) -> None:
        now = time.time()
        with self._frame_lock:
            self._healthy = True
            self._last_error = None
            self._last_frame = frame_bytes
            self._last_frame_at = now
            self._sequence += 1
            self._frames_seen += 1
            subs = list(self._subscriptions)
        self._wake_subscribers(subs)

    def _record_error(self, exc: Exception) -> None:
        with self._frame_lock:
            self._healthy = False
            self._last_error = str(exc)
            self._reconnects += 1
            subs = list(self._subscriptions)
        log.warning("camera %s error: %s", self.camera.key, exc)
        self._wake_subscribers(subs)

    def _wake_all_subscribers(self) -> None:
        with self._frame_lock:
            subs = list(self._subscriptions)
        self._wake_subscribers(subs)

    @staticmethod
    def _wake_subscribers(subs: list[_Subscription]) -> None:
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(_set_event_threadsafe, sub.event)
            except RuntimeError:
                # Subscriber loop already closed; drop silently.
                continue


class CameraManager:
    """Map of camera key -> CameraWorker; manages worker lifecycle."""

    def __init__(
        self,
        cameras: list[LocalCamera],
        runtime: RuntimeConfig,
        stream_builder: StreamBuilder = build_camera_stream,
    ) -> None:
        self._lock = threading.RLock()
        self._runtime = runtime
        self._stream_builder = stream_builder
        self._running = False
        self._workers: dict[str, CameraWorker] = {}
        self._known: dict[str, LocalCamera] = {}
        for camera in cameras:
            self._known[camera.key] = camera
            if camera.is_active and camera.resolved_source is not None:
                self._workers[camera.key] = CameraWorker(
                    camera, runtime, stream_builder
                )

    @classmethod
    def from_config(
        cls,
        config: AgentConfig,
        stream_builder: StreamBuilder = build_camera_stream,
    ) -> "CameraManager":
        cameras = load_cameras(config.cameras_path)
        return cls(cameras, config.runtime, stream_builder)

    def start(self) -> None:
        with self._lock:
            self._running = True
            workers = list(self._workers.values())
        for worker in workers:
            worker.start()

    async def start_async(self) -> None:
        await asyncio.to_thread(self.start)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop()

    async def stop_async(self) -> None:
        await asyncio.to_thread(self.stop)

    def get(self, key: str) -> CameraWorker | None:
        with self._lock:
            return self._workers.get(key)

    def known_camera(self, key: str) -> LocalCamera | None:
        with self._lock:
            return self._known.get(key)

    def all_status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            workers = dict(self._workers)
            known = dict(self._known)
        statuses: dict[str, dict[str, Any]] = {
            key: worker.status() for key, worker in workers.items()
        }
        for key, camera in known.items():
            if key in statuses:
                continue
            statuses[key] = {
                "key": key,
                "source_type": camera.effective_stream_type,
                "running": False,
                "healthy": False,
                "last_error": (
                    None if camera.is_active else "camera is inactive"
                ),
                "last_frame_at": None,
                "last_frame_age": None,
                "frames_seen": 0,
                "reconnects": 0,
                "started_at": None,
                "subscribers": 0,
            }
        return statuses

    def health(self) -> dict[str, Any]:
        statuses = self.all_status()
        active = [
            s
            for s in statuses.values()
            if s["last_error"] != "camera is inactive"
        ]
        if not active:
            overall = "ok"
        elif all(s["healthy"] for s in active):
            overall = "ok"
        else:
            overall = "degraded"
        return {"status": overall, "cameras": statuses}


async def open_camera_stream_async(
    manager: CameraManager,
    camera_key: str,
) -> tuple[CameraWorker, int, bytes]:
    worker = manager.get(camera_key)
    if worker is None:
        if manager.known_camera(camera_key) is None:
            raise KeyError(camera_key)
        raise CameraUnavailableError(
            f"camera {camera_key!r} is inactive or has no source"
        )

    timeout = worker.runtime.client_start_timeout_seconds
    result = await worker.wait_for_first_frame_async(timeout)
    if result is None:
        raise CameraUnavailableError(
            f"camera {camera_key!r} has no frame available"
        )
    sequence, frame = result
    return worker, sequence, frame
