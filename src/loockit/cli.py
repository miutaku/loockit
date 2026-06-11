"""Command-line entry point: ``loockit run``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .app import Application
from .config import ConfigError, load_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loockit",
        description="Local-first SESAME4 / SESAME Bot1 controller "
        "(BLE + gRPC + real-time monitoring + Matter bridge).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the gRPC server and device manager.")
    run.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="Path to the TOML config file (default: config.toml).",
    )
    run.add_argument(
        "--simulate",
        action="store_true",
        help="Use in-memory simulated devices instead of real BLE.",
    )
    run.add_argument(
        "--enable-matter",
        dest="enable_matter",
        action="store_true",
        default=None,
        help="Start the Matter bridge (requires the 'matter' extra). "
        "Overrides [matter].enabled in config.",
    )
    run.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v info, -vv debug).",
    )

    scan = sub.add_parser(
        "scan", help="Scan for nearby SESAME devices and print their BLE address."
    )
    scan.add_argument(
        "-d",
        "--duration",
        type=int,
        default=15,
        help="Scan duration in seconds (default: 15).",
    )
    scan.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase log verbosity."
    )
    return parser


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _run_scan(duration: int) -> int:
    from .discover import scan

    print(f"Scanning for SESAME devices for {duration}s...", file=sys.stderr)
    try:
        devices = asyncio.run(scan(duration))
    except Exception as exc:  # bleak/BlueZ errors
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1
    if not devices:
        print("No SESAME devices found. Move closer and retry.", file=sys.stderr)
        return 0
    print(f"{'BLE ADDRESS':<20} {'MODEL':<12} {'REGISTERED':<11} RSSI")
    for d in devices:
        print(
            f"{d.ble_address:<20} {(d.model or '?'):<12} "
            f"{str(d.registered):<11} {d.rssi}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "scan":
        return _run_scan(args.duration)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    app = Application(
        config, simulate=args.simulate, enable_matter=args.enable_matter
    )
    try:
        asyncio.run(app.run_forever())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
