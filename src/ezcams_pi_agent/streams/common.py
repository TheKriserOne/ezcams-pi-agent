from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MJPEG_BOUNDARY = "frame"
MJPEG_MEDIA_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"


class CameraStreamError(RuntimeError):
    """Base error for camera stream providers."""


class CameraStream(ABC):
    """Common interface for all camera stream providers."""

    @abstractmethod
    def prepare(self) -> None:
        """Provider-specific setup before frames are pulled."""

    @abstractmethod
    def jpeg_frames(self) -> Iterator[bytes]:
        """Yield individual JPEG frame bytes from the underlying source."""

    def stop(self) -> None:
        """Optional cleanup hook for persistent providers."""
        return


def build_mjpeg_chunk(frame_bytes: bytes) -> bytes:
    """Wrap a JPEG frame into a multipart MJPEG chunk."""
    return (
        b"--" + MJPEG_BOUNDARY.encode("ascii") + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: "
        + str(len(frame_bytes)).encode("ascii")
        + b"\r\n\r\n"
        + frame_bytes
        + b"\r\n"
    )


def extract_jpeg_frames(byte_chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Extract JPEG frames from an arbitrary byte stream by scanning SOI/EOI markers.

    Tolerant of arbitrary boundary formats so remote MJPEG sources can be
    normalized into a single internal frame representation.
    """
    buffer = bytearray()
    for chunk in byte_chunks:
        if not chunk:
            continue

        buffer.extend(chunk)
        while True:
            start_idx = buffer.find(JPEG_SOI)
            if start_idx == -1:
                # Avoid unbounded growth between frames.
                if len(buffer) > 2_000_000:
                    del buffer[:-2]
                break

            end_idx = buffer.find(JPEG_EOI, start_idx + 2)
            if end_idx == -1:
                if start_idx > 0:
                    del buffer[:start_idx]
                break

            frame_end = end_idx + 2
            frame = bytes(buffer[start_idx:frame_end])
            del buffer[:frame_end]
            yield frame
