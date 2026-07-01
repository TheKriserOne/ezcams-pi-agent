"""Consume the local agent's MJPEG ``/stream`` for one camera over loopback.

The streaming agent already decodes every source type (native / rtsp /
http_mjpeg) into a single normalized JPEG MJPEG stream. The inference process
reuses that — one persistent loopback connection per camera — so frames are
never decoded twice on the agent side and native CSI cameras are not opened
by two processes at once.

JPEG frame extraction reuses the agent's own SOI/EOI scanner.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

import httpx

from ezcams_pi_agent.streams.common import extract_jpeg_frames

log = logging.getLogger(__name__)


class AgentStreamSource:
    """Yield JPEG frame bytes from ``GET {agent_url}/stream/{camera_key}``."""

    def __init__(
        self,
        agent_url: str,
        camera_key: str,
        stop_event: threading.Event,
        connect_timeout_s: float = 10.0,
        reconnect_backoff_s: float = 2.0,
        verify_tls: bool = False,
    ) -> None:
        self._url = f"{agent_url.rstrip('/')}/stream/{camera_key}"
        self._camera_key = camera_key
        self._stop = stop_event
        self._connect_timeout_s = connect_timeout_s
        self._reconnect_backoff_s = reconnect_backoff_s
        # The agent serves HTTPS with a self-signed cert. Over loopback we talk
        # to our own local agent, so certificate verification is off by default.
        self._verify_tls = verify_tls

    def jpeg_frames(self) -> Iterator[bytes]:
        """Yield JPEG frames, reconnecting with backoff until stopped."""
        # No read timeout: an MJPEG stream is long-lived. The connect timeout
        # still bounds the initial handshake.
        timeout = httpx.Timeout(self._connect_timeout_s, read=None)
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=timeout, verify=self._verify_tls) as client:
                    with client.stream("GET", self._url) as resp:
                        resp.raise_for_status()
                        log.info("camera %s: stream opened", self._camera_key)
                        for frame in extract_jpeg_frames(resp.iter_bytes()):
                            if self._stop.is_set():
                                return
                            yield frame
            except Exception as exc:
                log.warning(
                    "camera %s: stream error (%s); reconnecting in %.1fs",
                    self._camera_key,
                    exc,
                    self._reconnect_backoff_s,
                )
            if self._stop.wait(self._reconnect_backoff_s):
                return
