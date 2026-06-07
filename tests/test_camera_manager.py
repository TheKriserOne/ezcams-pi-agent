from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ezcams_pi_agent.camera_manager import (  # noqa: E402
    CameraManager,
    CameraUnavailableError,
    open_camera_stream_async,
)
from ezcams_pi_agent.cameras import HttpMjpegSource, LocalCamera  # noqa: E402
from ezcams_pi_agent.config import RuntimeConfig  # noqa: E402
from ezcams_pi_agent.streams.common import (  # noqa: E402
    CameraStream,
    CameraStreamError,
)


def _make_camera(key: str = "test-cam") -> LocalCamera:
    return LocalCamera(
        key=key,
        name=key,
        lat=0.0,
        lng=0.0,
        source=HttpMjpegSource(url="http://example.invalid/feed"),
        fps=30,
    )


class _ScriptedStream(CameraStream):
    """Deterministic CameraStream that yields canned JPEG frames on cue."""

    def __init__(self, queue: "asyncio.Queue[bytes | Exception | None]") -> None:
        self._queue = queue
        self._stopped = threading.Event()

    def prepare(self) -> None:
        return

    def stop(self) -> None:
        self._stopped.set()

    def jpeg_frames(self) -> Iterator[bytes]:
        while not self._stopped.is_set():
            try:
                item = self._queue.get(timeout=2.0)
            except Exception:
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item


class _StreamFactory:
    def __init__(self) -> None:
        self.queues: list[object] = []  # python queue.Queue per build
        self.build_count = 0

    def __call__(self, camera: LocalCamera, runtime: RuntimeConfig) -> CameraStream:
        import queue as _q

        q: _q.Queue = _q.Queue()
        self.queues.append(q)
        self.build_count += 1
        return _ScriptedStream(q)


class CameraManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.factory = _StreamFactory()
        self.camera = _make_camera()
        self.runtime = RuntimeConfig(
            jpeg_quality=70,
            reconnect_delay_seconds=0.05,
            client_start_timeout_seconds=2.0,
        )
        self.manager = CameraManager(
            [self.camera], self.runtime, stream_builder=self.factory
        )
        self.manager.start()
        # Wait for the worker thread to construct the first stream.
        for _ in range(100):
            if self.factory.queues:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.factory.queues, "stream was never built")

    async def asyncTearDown(self) -> None:
        await self.manager.stop_async()

    def _push_frame(self, queue_index: int, frame: bytes) -> None:
        self.factory.queues[queue_index].put_nowait(frame)

    async def test_latest_frame_available_after_push(self) -> None:
        frame = b"\xff\xd8frame-1\xff\xd9"
        self._push_frame(0, frame)
        worker, sequence, first = await open_camera_stream_async(
            self.manager, self.camera.key
        )
        self.assertEqual(first, frame)
        self.assertGreater(sequence, 0)

        cached_seq, cached_frame = worker.latest()
        self.assertEqual(cached_frame, frame)
        self.assertEqual(cached_seq, sequence)

    async def test_multiple_subscribers_receive_fanout(self) -> None:
        first_frame = b"\xff\xd8first\xff\xd9"
        self._push_frame(0, first_frame)
        worker, sequence, frame = await open_camera_stream_async(
            self.manager, self.camera.key
        )

        results_a: list[bytes] = []
        results_b: list[bytes] = []

        async def consume(into: list[bytes]) -> None:
            count = 0
            async for chunk in worker.mjpeg_chunks_async(sequence, frame):
                into.append(chunk)
                count += 1
                if count >= 3:
                    return

        task_a = asyncio.create_task(consume(results_a))
        task_b = asyncio.create_task(consume(results_b))

        # Give subscribers a moment to register before producing more frames.
        await asyncio.sleep(0.05)

        self._push_frame(0, b"\xff\xd8second\xff\xd9")
        await asyncio.sleep(0.05)
        self._push_frame(0, b"\xff\xd8third\xff\xd9")

        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)

        self.assertEqual(len(results_a), 3)
        self.assertEqual(len(results_b), 3)
        # Both consumers must see the first frame from the head.
        self.assertIn(b"first", results_a[0])
        self.assertIn(b"first", results_b[0])
        # And both must observe at least one subsequent frame body.
        joined_a = b"".join(results_a)
        joined_b = b"".join(results_b)
        self.assertTrue(b"second" in joined_a or b"third" in joined_a)
        self.assertTrue(b"second" in joined_b or b"third" in joined_b)

    async def test_worker_reconnects_after_stream_error(self) -> None:
        self._push_frame(0, b"\xff\xd8a\xff\xd9")
        worker, _, _ = await open_camera_stream_async(
            self.manager, self.camera.key
        )

        # Force the active stream to raise; worker should rebuild via factory.
        self.factory.queues[0].put_nowait(CameraStreamError("simulated upstream"))

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self.factory.build_count < 2:
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.factory.build_count, 2)

        self._push_frame(self.factory.build_count - 1, b"\xff\xd8b\xff\xd9")
        result = await asyncio.wait_for(
            worker.wait_for_first_frame_async(timeout_seconds=2.0),
            timeout=3.0,
        )
        self.assertIsNotNone(result)
        _, recovered = result
        self.assertEqual(recovered, b"\xff\xd8b\xff\xd9")

    async def test_unknown_camera_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            await open_camera_stream_async(self.manager, "no-such-cam")

    async def test_inactive_camera_raises_unavailable(self) -> None:
        inactive = LocalCamera(
            key="inactive",
            name="inactive",
            lat=0.0,
            lng=0.0,
            source=HttpMjpegSource(url="http://example.invalid/x"),
            is_active=False,
        )
        manager = CameraManager(
            [inactive], self.runtime, stream_builder=self.factory
        )
        try:
            with self.assertRaises(CameraUnavailableError):
                await open_camera_stream_async(manager, "inactive")
        finally:
            await manager.stop_async()


if __name__ == "__main__":
    unittest.main()
