from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ezcams_pi_agent.config import (  # noqa: E402
    AgentConfig,
    RuntimeConfig,
    load_config,
    save_config,
    write_secret,
)


class ConfigPathTests(unittest.TestCase):
    def test_save_relative_paths_and_load_resolves(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_secret(root / "device.secret", "secret")
            config = AgentConfig(
                backend_url="https://backend.example",
                device_id="device-id",
                name="Pi",
                static_ip="127.0.0.1",
                port=8443,
                backend_public_key_pem="public",
                device_secret_path=str(root / "device.secret"),
                cert_path=str(root / "agent.crt"),
                cert_key_path=str(root / "agent-tls.key"),
                cameras_path=str(root / "cameras.json"),
                recordings_dir=str(root / "clips"),
                runtime=RuntimeConfig(),
            )
            save_config(config, root)

            raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["device_secret_path"], "device.secret")
            self.assertEqual(raw["recordings_dir"], "clips")

            loaded = load_config(root)
            self.assertEqual(loaded.device_secret_path, str((root / "device.secret").resolve()))
            self.assertEqual(loaded.recordings_dir, str((root / "clips").resolve()))


if __name__ == "__main__":
    unittest.main()
