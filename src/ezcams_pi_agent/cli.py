from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn

from ezcams_pi_agent.backend_client import heartbeat_once, sync_cameras_once, unregister_once
from ezcams_pi_agent.config import config_path, default_config_dir, load_config, read_text
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
    print(f"Success: registered EZ Cams Pi device {config.device_id}")
    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    print(f"Config written to {config_path(config_dir)}")


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
        await heartbeat_once(config)
        await sync_cameras_once(config)

    asyncio.run(run())
    print("Heartbeat and camera sync completed")


def _set_config_dir_arg(args: argparse.Namespace) -> Path:
    if args.config_dir:
        os.environ["EZCAMS_PI_CONFIG_DIR"] = args.config_dir
        return Path(args.config_dir)
    return default_config_dir()


def _validate_local_config(config_dir: Path):
    config = load_config(config_dir)
    parsed = urlparse(config.backend_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("backend_url must be an http(s) URL")
    required_paths = [
        config.device_secret_path,
        config.cert_path,
        config.cert_key_path,
        config.cameras_path,
    ]
    for raw_path in required_paths:
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if not read_text(path).strip():
            raise ValueError(f"{path} is empty")
    return config


def _ensure(args: argparse.Namespace) -> int:
    config_dir = _set_config_dir_arg(args)
    try:
        config = _validate_local_config(config_dir)
    except Exception as exc:
        print(f"Invalid EZ Cams Pi config: {exc}", file=sys.stderr)
        return 2

    async def run() -> int:
        try:
            await heartbeat_once(config)
            print("EZ Cams Pi config valid; backend accepted device credentials.")
            return 0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 404}:
                print(
                    f"Backend rejected device credentials: HTTP {exc.response.status_code}",
                    file=sys.stderr,
                )
                return 3
            print(
                f"Backend reachable but returned HTTP {exc.response.status_code}; camera service can still start.",
                file=sys.stderr,
            )
            return 0
        except Exception as exc:
            print(
                f"Backend unreachable during ensure: {exc}. Camera service can still start.",
                file=sys.stderr,
            )
            return 0

    return asyncio.run(run())


def _remove_local_registration(config_dir: Path, config=None) -> None:
    paths = [config_path(config_dir)]
    if config is not None:
        paths.append(Path(config.device_secret_path))
    else:
        paths.append(config_dir / "device.secret")
    for path in paths:
        try:
            path.unlink()
            print(f"Removed {path}")
        except FileNotFoundError:
            continue


def _unregister(args: argparse.Namespace) -> int:
    config_dir = _set_config_dir_arg(args)
    try:
        config = load_config(config_dir)
    except Exception as exc:
        if args.local_only:
            print(f"Warning: config unreadable ({exc}); removing local registration files only.")
            _remove_local_registration(config_dir)
            return 0
        print(f"Invalid EZ Cams Pi config: {exc}", file=sys.stderr)
        return 2

    if args.local_only:
        print(
            "Warning: local-only unregister does not notify backend. Revoke this Pi in the app."
        )
        _remove_local_registration(config_dir, config)
        return 0

    async def run() -> int:
        try:
            await unregister_once(config)
        except httpx.HTTPStatusError as exc:
            print(
                f"Backend unregister failed: HTTP {exc.response.status_code}",
                file=sys.stderr,
            )
            return 3
        except Exception as exc:
            print(f"Backend unregister failed: {exc}", file=sys.stderr)
            return 3
        _remove_local_registration(config_dir, config)
        print("Raspberry Pi unregistered from backend.")
        return 0

    return asyncio.run(run())


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

    ensure = sub.add_parser("ensure", help="Validate local config before starting service")
    ensure.add_argument("--config-dir", default="")
    ensure.set_defaults(func=_ensure)

    unregister = sub.add_parser("unregister", help="Unregister this Raspberry Pi")
    unregister.add_argument("--config-dir", default="")
    unregister.add_argument(
        "--local-only",
        action="store_true",
        help="Remove local registration without notifying backend",
    )
    unregister.set_defaults(func=_unregister)

    args = parser.parse_args()
    result = args.func(args)
    if isinstance(result, int):
        raise SystemExit(result)


if __name__ == "__main__":
    main()
