from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ezcams_pi_agent.cli import _ensure, _unregister  # noqa: E402
from ezcams_pi_agent.config import AgentConfig, config_path, save_config, write_secret  # noqa: E402


def _write_valid_config(root: Path) -> AgentConfig:
    secret = root / "device.secret"
    cert = root / "agent.crt"
    key = root / "agent-tls.key"
    cameras = root / "cameras.json"
    write_secret(secret, "secret")
    write_secret(cert, "cert")
    write_secret(key, "key")
    write_secret(cameras, '{"cameras": []}')
    config = AgentConfig(
        backend_url="https://backend.example",
        device_id="device-id",
        name="Pi",
        static_ip="127.0.0.1",
        port=8443,
        backend_public_key_pem="public",
        device_secret_path=str(secret),
        cert_path=str(cert),
        cert_key_path=str(key),
        cameras_path=str(cameras),
        recordings_dir=str(root / "clips"),
    )
    save_config(config, root)
    return config


class CliLifecycleTests(unittest.TestCase):
    def test_ensure_returns_nonzero_for_missing_config(self) -> None:
        with TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                code = _ensure(argparse.Namespace(config_dir=tmp))

        self.assertEqual(code, 2)

    def test_ensure_returns_zero_for_valid_local_config(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_valid_config(Path(tmp))
            with contextlib.redirect_stdout(io.StringIO()):
                code = _ensure(argparse.Namespace(config_dir=tmp))

        self.assertEqual(code, 0)

    def test_unregister_local_only_removes_registration_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_valid_config(root)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = _unregister(argparse.Namespace(config_dir=tmp, local_only=True))

            self.assertEqual(code, 0)
            self.assertFalse(config_path(root).exists())
            self.assertFalse(Path(config.device_secret_path).exists())


if __name__ == "__main__":
    unittest.main()
