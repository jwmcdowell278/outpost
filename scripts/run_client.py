"""CLI entry point for an Outpost voice client.

Examples:
    python scripts/run_client.py --config config/outpost.yaml --ptt keyboard
    python scripts/run_client.py --config config/outpost.yaml --channel tac --ptt gpio --gpio-pin 17
    python scripts/run_client.py --config config/outpost.yaml --channel ops --ptt hid --vendor-id 0x1234 --product-id 0x5678
    python scripts/run_client.py --config config/outpost.yaml --channel ops --ptt serial --serial-port /dev/ttyUSB0

--channel is optional - omit it to join whichever channel is configured
with id 1.

Relay only (a server or desktop bridging networks - no audio, PTT or channels):
    python scripts/run_client.py --config config/relay.yaml --relay-only
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

import ifaddr

from outpost.config import load_config, OutpostConfig
from outpost.client import OutpostClient
from outpost.ptt import NullPTT, PTTSource


def web_ui_urls(host: str, port: int) -> list[str]:
    """Reachable URL(s) for the web UI. A wildcard bind address
    (0.0.0.0/::) isn't itself a usable URL, so list every actual LAN
    address instead - important on a headless box, where this printout may
    be the only way to learn where to point a browser."""
    if host not in ("0.0.0.0", "::"):
        return [f"http://{host}:{port}"]
    addrs = []
    try:
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4 and ip.ip != "127.0.0.1":
                    addrs.append(ip.ip)
    except OSError:
        pass
    return [f"http://{addr}:{port}" for addr in addrs] or [f"http://<this-device-ip>:{port}"]


def build_ptt(args, on_change, key_name=None) -> PTTSource:
    if args.ptt == "keyboard":
        from outpost.ptt.keyboard import KeyboardPTT
        return KeyboardPTT(on_change, key_name=key_name)
    if args.ptt == "hid":
        from outpost.ptt.hid import HIDPTT
        if args.vendor_id is None or args.product_id is None:
            raise SystemExit("--vendor-id and --product-id are required for --ptt hid")
        return HIDPTT(on_change, vendor_id=args.vendor_id, product_id=args.product_id)
    if args.ptt == "gpio":
        from outpost.ptt.gpio import GPIOPTT
        if args.gpio_pin is None:
            raise SystemExit("--gpio-pin is required for --ptt gpio")
        return GPIOPTT(on_change, pin=args.gpio_pin)
    if args.ptt == "serial":
        from outpost.ptt.serial_ptt import SerialPTT
        if args.serial_port is None:
            raise SystemExit("--serial-port is required for --ptt serial")
        return SerialPTT(on_change, port=args.serial_port)
    raise SystemExit(f"unknown --ptt source {args.ptt!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an Outpost voice client.")
    parser.add_argument("--config", required=True, help="path to outpost.yaml")
    parser.add_argument("--channel", default=None, help="name of the channel to join (defaults to the channel configured with id 1)")
    parser.add_argument("--ptt", choices=["keyboard", "hid", "gpio", "serial"], default="keyboard")
    parser.add_argument("--vendor-id", type=lambda s: int(s, 0), default=None)
    parser.add_argument("--product-id", type=lambda s: int(s, 0), default=None)
    parser.add_argument("--gpio-pin", type=int, default=None)
    parser.add_argument("--serial-port", default=None)
    parser.add_argument("--relay-only", action="store_true", default=None,
                        help="run as a relay/bridge only - no audio devices, PTT or channel keys needed "
                             "(same as relay_only: true in the config)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", default=None,
                        help="path to a size-rotated log file for diagnosing issues after the fact "
                             "(overrides the config's log_file; omit both to log to console only)")
    return parser.parse_args()


def configure_logging(verbose: bool, config: OutpostConfig, log_file_override: str | None) -> None:
    """Console output is controlled by -v/--verbose. log_file (if set, via
    config or --log-file) also keeps a size-rotated file at log_level
    ("info", or "debug" for every detail) regardless of console verbosity.

    Kept light on disk writes (SD cards wear out): run as a systemd service
    with a log file, the console - which goes into the system journal -
    only gets warnings, since the file already has the rest; and the web
    UI's request log (a line per page refresh) is left out unless -v."""
    log_file = log_file_override if log_file_override is not None else config.log_file
    file_level = logging.DEBUG if config.log_level == "debug" else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console_handler = logging.StreamHandler()
    if verbose:
        console_level = logging.DEBUG
    elif log_file and os.environ.get("JOURNAL_STREAM"):
        console_level = logging.WARNING   # a service: the file has the rest
    else:
        console_level = logging.INFO
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    # The root logger has to pass everything any handler wants (logger-level
    # filtering happens before handler-level filtering).
    root.setLevel(min(console_level, file_level) if log_file else console_level)
    root.addHandler(console_handler)
    if not verbose:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count,
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


async def main() -> None:
    args = parse_args()
    config = load_config(args.config, relay_only=args.relay_only)
    configure_logging(args.verbose, config, args.log_file)
    if config.relay_only:
        channel_name = None
    elif args.channel is not None:
        channel_name = args.channel
    else:
        default_channel = config.channels.get(1)
        if default_channel is None:
            raise SystemExit("--channel not given and no channel with id 1 is configured - specify --channel explicitly")
        channel_name = default_channel.name

    client = OutpostClient(config, channel_name, config_path=args.config)
    if config.relay_only:
        ptt = NullPTT()
    else:
        try:
            # The key picked on the web UI's Buttons page, read live.
            ptt = build_ptt(args, client.set_ptt, key_name=lambda: client.config.ptt_key)
        except Exception as exc:
            # e.g. pynput's keyboard backend probes for an X11 display at
            # import time and fails outright on a headless box - don't take
            # the whole client (and its web UI PTT button, which doesn't go
            # through this PTTSource at all) down with it.
            logging.warning("--ptt %s could not be built (%s); PTT only available via the web UI if enabled", args.ptt, exc)
            ptt = NullPTT()

    await client.start()
    try:
        ptt.start()
    except Exception as exc:
        logging.warning("--ptt %s failed to start (%s); PTT only available via the web UI if enabled", args.ptt, exc)
    if config.relay_only:
        print(f"Outpost relay running on UDP port {config.listen_port} (sender_id={config.sender_id}). "
              f"Forwarding traffic between peers; no audio. Ctrl+C to quit.")
    else:
        print(f"Outpost client running on channel {channel_name!r} (sender_id={config.sender_id}). "
              f"Hold the configured PTT to talk. Ctrl+C to quit.")
    if client.web_ui:
        for url in web_ui_urls(config.web_ui_host, config.web_ui_port):
            print(f"Web UI: {url}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await asyncio.wait(
            [asyncio.create_task(stop_event.wait()), asyncio.create_task(client.restart_requested.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        ptt.stop()
        await client.stop()

    if client.restart_requested.is_set():
        print("Restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    asyncio.run(main())
