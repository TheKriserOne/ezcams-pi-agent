from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import ezcams_pi_agent.main as agent_main  # noqa: E402
from ezcams_pi_agent.config import AgentConfig, save_config  # noqa: E402
from ezcams_pi_agent.crypto import (  # noqa: E402
    generate_device_private_key_pem,
    load_private_key_pem,
    payload_header_value,
    public_key_pem,
    sign_payload,
)  # backend signing key helpers for stream auth tests


class _FrameHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.end_headers()
        self.wfile.write(b"\xff\xd8\xff\xd9")

    def log_message(self, format: str, *args: object) -> None:
        return


class SignedRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.device_id = str(uuid.uuid4())
        self.backend_private_pem = generate_device_private_key_pem()
        self.backend_private_key = load_private_key_pem(self.backend_private_pem)
        self.backend_public_pem = public_key_pem(self.backend_private_key)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FrameHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        source_url = f"http://127.0.0.1:{self.server.server_port}/frame.jpg"

        device_secret_path = self.root / "device.secret"
        cert_path = self.root / "agent.crt"
        cert_key_path = self.root / "agent-tls.key"
        cameras_path = self.root / "cameras.json"
        recordings_dir = self.root / "clips"
        device_secret_path.write_text("test-device-secret", encoding="utf-8")
        cert_path.write_text("test-cert", encoding="utf-8")
        cert_key_path.write_text("test-cert-key", encoding="utf-8")
        cameras_path.write_text(
            json.dumps(
                {
                    "cameras": [
                        {
                            "key": "front-door",
                            "name": "Front Door",
                            "lat": 1.0,
                            "lng": 2.0,
                            "stream_url": source_url,
                            "snapshot_url": source_url,
                            "stream_type": "mjpeg",
                            "is_active": True,
                            "is_available": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        clip_dir = recordings_dir / "front-door"
        clip_dir.mkdir(parents=True)
        self.recording_name = "front-door_20260701_120000.mkv"
        self.recording_bytes = b"test-mkv-bytes"
        (clip_dir / self.recording_name).write_bytes(self.recording_bytes)
        save_config(
            AgentConfig(
                backend_url="https://backend.example",
                device_id=self.device_id,
                name="Test Pi",
                static_ip="127.0.0.1",
                port=8443,
                backend_public_key_pem=self.backend_public_pem,
                device_secret_path=str(device_secret_path),
                cert_path=str(cert_path),
                cert_key_path=str(cert_key_path),
                cameras_path=str(cameras_path),
                recordings_dir=str(recordings_dir),
            ),
            self.root,
        )
        os.environ["EZCAMS_PI_CONFIG_DIR"] = str(self.root)
        os.environ["EZCAMS_PI_DISABLE_SYNC"] = "1"
        agent_main._config = None
        agent_main._manager = None
        agent_main._used_nonces.clear()
        self.client = TestClient(agent_main.app)

    def tearDown(self) -> None:
        agent_main._config = None
        manager = agent_main._manager
        agent_main._manager = None
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        agent_main._used_nonces.clear()

    def _headers(
        self,
        *,
        camera_key: str = "front-door",
        action: str = "snapshot",
        path: str = "/snapshot/front-door",
        expires_delta: timedelta = timedelta(seconds=30),
        nonce: str = "nonce-1",
    ) -> dict[str, str]:
        payload = {
            "device_id": self.device_id,
            "camera_id": str(uuid.uuid4()),
            "device_camera_key": camera_key,
            "action": action,
            "method": "GET",
            "path": path,
            "body_sha256": hashlib.sha256(b"").hexdigest(),
            "expires_at": (datetime.now(timezone.utc) + expires_delta).isoformat(),
            "nonce": nonce,
        }
        return {
            "X-EZCams-Payload": payload_header_value(payload),
            "X-EZCams-Signature": sign_payload(payload, self.backend_private_key),
        }

    def test_unsigned_snapshot_request_is_rejected(self) -> None:
        response = self.client.get("/snapshot/front-door")

        self.assertEqual(response.status_code, 401)

    def test_expired_signed_request_is_rejected(self) -> None:
        response = self.client.get(
            "/snapshot/front-door",
            headers=self._headers(expires_delta=timedelta(seconds=-1)),
        )

        self.assertEqual(response.status_code, 401)

    def test_mismatched_path_is_rejected(self) -> None:
        response = self.client.get(
            "/snapshot/front-door",
            headers=self._headers(path="/snapshot/back-door"),
        )

        self.assertEqual(response.status_code, 403)

    def test_valid_signed_snapshot_reaches_camera_source_and_blocks_replay(self) -> None:
        headers = self._headers(nonce="nonce-replay")

        first = self.client.get("/snapshot/front-door", headers=headers)
        second = self.client.get("/snapshot/front-door", headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, b"\xff\xd8\xff\xd9")
        self.assertEqual(second.status_code, 401)

    def test_unsigned_recordings_request_is_rejected(self) -> None:
        response = self.client.get("/recordings/front-door")

        self.assertEqual(response.status_code, 401)

    def test_unsigned_backend_heartbeat_is_rejected(self) -> None:
        response = self.client.get("/backend/heartbeat")

        self.assertEqual(response.status_code, 401)

    def test_valid_signed_backend_heartbeat_returns_device_health(self) -> None:
        response = self.client.get(
            "/backend/heartbeat",
            headers=self._headers(
                action="device:heartbeat",
                path="/backend/heartbeat",
                nonce="nonce-device-heartbeat",
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["device_id"], self.device_id)
        self.assertIn(data["status"], {"ok", "degraded"})
        self.assertIsInstance(data["cameras"], list)
        self.assertGreaterEqual(len(data["cameras"]), 1)
        self.assertEqual(data["cameras"][0]["key"], "front-door")

    def test_valid_signed_recordings_list_returns_camera_clips(self) -> None:
        response = self.client.get(
            "/recordings/front-door",
            headers=self._headers(
                action="recordings:list",
                path="/recordings/front-door",
                nonce="nonce-recordings-list",
            ),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["recordings"][0]["id"], self.recording_name)
        self.assertEqual(data["recordings"][0]["size_bytes"], len(self.recording_bytes))

    def test_valid_signed_recording_download_streams_clip(self) -> None:
        path = f"/recordings/front-door/{self.recording_name}"
        response = self.client.get(
            path,
            headers=self._headers(
                action="recordings:download",
                path=path,
                nonce="nonce-recording-download",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.recording_bytes)
        self.assertIn("video/x-matroska", response.headers.get("content-type", ""))

    def test_recording_path_traversal_is_rejected(self) -> None:
        config = agent_main._get_config()
        camera = agent_main.find_camera(config.cameras_path, "front-door")
        self.assertIsNotNone(camera)

        with self.assertRaises(HTTPException) as ctx:
            agent_main._recording_path(config, camera, "../secret.mkv")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
