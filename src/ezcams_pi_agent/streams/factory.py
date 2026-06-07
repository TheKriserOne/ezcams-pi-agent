from __future__ import annotations

from ezcams_pi_agent.cameras import (
    HttpMjpegSource,
    LocalCamera,
    NativeSource,
    RtspSource,
)
from ezcams_pi_agent.config import RuntimeConfig
from ezcams_pi_agent.streams.common import CameraStream, CameraStreamError


class StreamBuilderError(CameraStreamError):
    """Raised when a camera config cannot be turned into a stream."""


def build_camera_stream(
    camera: LocalCamera,
    runtime: RuntimeConfig | None = None,
) -> CameraStream:
    runtime = runtime or RuntimeConfig()
    source = camera.resolved_source

    if source is None:
        raise StreamBuilderError(
            f"camera {camera.key!r} has no source: set `source` or `stream_url`"
        )

    if isinstance(source, NativeSource):
        from ezcams_pi_agent.streams.native import NativeStream

        return NativeStream(
            camera_index=source.camera_index,
            width=source.width,
            height=source.height,
            fps=camera.fps,
            hflip=source.hflip,
            vflip=source.vflip,
            jpeg_quality=runtime.jpeg_quality,
        )

    if isinstance(source, RtspSource):
        from ezcams_pi_agent.streams.rtsp import RtspStream

        return RtspStream(
            source_url=source.url,
            fps=camera.fps,
            jpeg_quality=runtime.jpeg_quality,
        )

    if isinstance(source, HttpMjpegSource):
        from ezcams_pi_agent.streams.http_mjpeg import HttpMjpegStream

        return HttpMjpegStream(
            source_url=source.url,
            fps=camera.fps,
        )

    raise StreamBuilderError(
        f"unsupported camera source for {camera.key!r}: {type(source).__name__}"
    )
