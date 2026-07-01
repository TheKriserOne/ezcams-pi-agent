"""Letterbox preprocessing to match the HEF input shape.

Faithful port of the proven ``hailo-object-detection-rtsp`` preprocessor: a
small pool of reusable model-sized RGB buffers (zero-alloc hot path) plus a
letterbox resize that writes into a caller-owned buffer.
"""
from __future__ import annotations

import queue

import cv2
import numpy as np

_PAD_COLOR = (114, 114, 114)


class PreprocessBufferPool:
    """Small pool of reusable model-sized RGB input buffers."""

    def __init__(self, model_w: int, model_h: int, pool_size: int) -> None:
        self._queue: queue.LifoQueue[np.ndarray] = queue.LifoQueue(maxsize=pool_size)
        for _ in range(pool_size):
            self._queue.put(np.empty((model_h, model_w, 3), dtype=np.uint8))

    def acquire(self, timeout: float | None = None) -> np.ndarray:
        return self._queue.get(timeout=timeout)

    def release(self, buf: np.ndarray) -> None:
        self._queue.put_nowait(buf)


class LetterboxPreprocessor:
    """Resize/pad a BGR frame into a caller-owned RGB output buffer."""

    def __init__(self, model_w: int, model_h: int) -> None:
        self.model_w = model_w
        self.model_h = model_h
        self._canvas_bgr = np.empty((model_h, model_w, 3), dtype=np.uint8)
        self._resized_bgr: np.ndarray | None = None
        self._resized_shape: tuple[int, int, int] | None = None

    def prepare_into(self, frame_bgr: np.ndarray, out_rgb: np.ndarray) -> np.ndarray:
        """Write a letterboxed RGB tensor into ``out_rgb`` and return it."""
        img_h, img_w = frame_bgr.shape[:2]
        scale = min(self.model_w / img_w, self.model_h / img_h)
        new_w = max(1, int(img_w * scale))
        new_h = max(1, int(img_h * scale))
        target_shape = (new_h, new_w, 3)
        if self._resized_shape != target_shape:
            self._resized_bgr = np.empty(target_shape, dtype=np.uint8)
            self._resized_shape = target_shape

        assert self._resized_bgr is not None
        cv2.resize(
            frame_bgr,
            (new_w, new_h),
            dst=self._resized_bgr,
            interpolation=cv2.INTER_LINEAR,
        )

        self._canvas_bgr[:] = _PAD_COLOR
        x_off = (self.model_w - new_w) // 2
        y_off = (self.model_h - new_h) // 2
        self._canvas_bgr[y_off : y_off + new_h, x_off : x_off + new_w] = self._resized_bgr
        cv2.cvtColor(self._canvas_bgr, cv2.COLOR_BGR2RGB, dst=out_rgb)
        return out_rgb
