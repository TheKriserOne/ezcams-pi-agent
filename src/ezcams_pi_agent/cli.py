from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from ezcams_pi_agent.backend_client import get_device_token, heartbeat_once, sync_cameras_once
from ezcams_pi_agent.config import load_config
from ezcams_pi_agent.setup import setup_agent


def _setup(args: argparse.Namespace) -> None:
    config = setup_agent(
        backend_url=args.backend_url,
        claim_code=args.claim_code,
        name=args.name,
        static_ip=args.static_ip,
        port=args.port,
        config_dir=Path(args.config_dir) if args.config_dir else None,
        force=args.force,
        insecure_backend_tls=args.insecure_backend_tls,
    )
    print(f"Registered EZ Cams Pi device {config.device_id}")
    print(f"Config written to {(Path(args.config_dir) if args.config_dir else Path('/etc/ezcams-pi')) / 'config.json'}")


def _run(args: argparse.Namespace) -> None:
    if args.config_dir:
        os.environ["EZCAMS_PI_CONFIG_DIR"] = args.config_dir
    config = load_config()
    uvicorn.run(
        "ezcams_pi_agent.main:app",
        host=args.host,
        port=args.port or config.port,
        ssl_certfile=config.cert_path,
        ssl_keyfile=config.cert_key_path,
    )


def _sync_once(args: argparse.Namespace) -> None:
    if args.config_dir:
        os.environ["EZCAMS_PI_CONFIG_DIR"] = args.config_dir
    config = load_config()

    async def run() -> None:
        token = await get_device_token(config)
        await heartbeat_once(config, token)
        await sync_cameras_once(config, token)

    asyncio.run(run())
    print("Heartbeat and camera sync completed")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ezcams-pi-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="Register this Raspberry Pi with cams-server")
    setup.add_argument("--backend-url", required=True)
    setup.add_argument("--claim-code", required=True)
    setup.add_argument("--name", required=True)
    setup.add_argument("--static-ip", required=True)
    setup.add_argument("--port", type=int, required=True)
    setup.add_argument("--config-dir", default="")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--insecure-backend-tls", action="store_true")
    setup.set_defaults(func=_setup)

    run = sub.add_parser("run", help="Run the HTTPS Pi agent")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=0)
    run.add_argument("--config-dir", default="")
    run.set_defaults(func=_run)

    sync_once = sub.add_parser("sync-once", help="Send one heartbeat and camera sync")
    sync_once.add_argument("--config-dir", default="")
    sync_once.set_defaults(func=_sync_once)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
