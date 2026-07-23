"""Small operational CLI.

- ``relay openapi``          — dump the spec used to generate the TS SDK.
- ``relay outbox-relay``     — run the transactional-outbox relay (RFC-001 §6.5): a dedicated,
                               single-instance process that drains ``outbox`` to Redis
                               at-least-once (LISTEN/NOTIFY-woken, poll fallback). One per deploy.
- ``relay realtime-fanout``  — run the realtime-fanout consumer (RFC-001 §6.3): consumes the
                               outbox Redis stream and publishes conversation events to Centrifugo.
- ``relay help-center-revalidate`` — run the Help Center ISR revalidation consumer (P0.8):
                               consumes the outbox Redis stream and POSTs affected paths to the
                               hosted site's on-demand revalidation webhook.
"""

from __future__ import annotations

import argparse
import json
import sys


def _openapi() -> int:
    from relay.main import app

    json.dump(app.openapi(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _outbox_relay() -> int:
    from relay.core.outbox_relay import run_relay

    run_relay()
    return 0


def _realtime_fanout() -> int:
    from relay.core.realtime_fanout import main as run_fanout

    run_fanout()
    return 0


def _help_center_revalidate() -> int:
    from relay.modules.knowledge.revalidation import main as run_revalidation

    run_revalidation()
    return 0


def _channels_dispatch() -> int:
    from relay.modules.channels.dispatch import main as run_dispatch

    run_dispatch()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("openapi", help="Print the OpenAPI spec to stdout")
    sub.add_parser("outbox-relay", help="Run the transactional-outbox relay")
    sub.add_parser("realtime-fanout", help="Run the realtime-fanout consumer (outbox → Centrifugo)")
    sub.add_parser(
        "help-center-revalidate", help="Run the Help Center ISR revalidation consumer (P0.8)"
    )
    sub.add_parser(
        "channels-dispatch", help="Run the email outbound dispatcher (outbox → send.email)"
    )

    args = parser.parse_args(argv)
    if args.command == "openapi":
        return _openapi()
    if args.command == "outbox-relay":
        return _outbox_relay()
    if args.command == "realtime-fanout":
        return _realtime_fanout()
    if args.command == "help-center-revalidate":
        return _help_center_revalidate()
    if args.command == "channels-dispatch":
        return _channels_dispatch()
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
