"""CLI entry point for an Outpost voice client.

Examples:
    python scripts/run_client.py --config config/outpost.yaml --channel ops --ptt keyboard
    python scripts/run_client.py --config config/outpost.yaml --channel ops --ptt gpio --gpio-pin 17
    python scripts/run_client.py --config config/outpost.yaml --channel ops --ptt hid --vendor-id 0x1234 --product-id 0x5678
    python scripts/run_client.py --config config/outpost.yaml --channel ops --ptt serial --serial-port /dev/ttyUSB0
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import signal

from outpost.config import load_config
from outpost.client import OutpostClient
from outpost.ptt import PTTSource


def build_ptt(args, on_change) -> PTTSource:
    if args.ptt == "keyboard":
        from outpost.ptt.keyboard import KeyboardPTT
        return KeyboardPTT(on_change)
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
    parser.add_argument("--channel", required=True, help="name of the channel to join")
    parser.add_argument("--ptt", choices=["keyboard", "hid", "gpio", "serial"], default="keyboard")
    parser.add_argument("--vendor-id", type=lambda s: int(s, 0), default=None)
    parser.add_argument("--product-id", type=lambda s: int(s, 0), default=None)
    parser.add_argument("--gpio-pin", type=int, default=None)
    parser.add_argument("--serial-port", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config = load_config(args.config)
    client = OutpostClient(config, args.channel)
    ptt = build_ptt(args, client.set_ptt)

    await client.start()
    ptt.start()
    print(f"Outpost client running on channel {args.channel!r} (sender_id={config.sender_id}). "
          f"Hold the configured PTT to talk. Ctrl+C to quit.")
    if client.web_ui:
        print(f"Web UI: http://{config.web_ui_host}:{config.web_ui_port}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        ptt.stop()
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
