"""Small operational CLI.

- ``relay openapi``       — dump the spec used to generate the TS SDK.
- ``relay outbox-relay``  — run the transactional-outbox relay (RFC-001 §6.5): a dedicated,
                            single-instance process that drains ``outbox`` to Redis at-least-once
                            (LISTEN/NOTIFY-woken, poll fallback). Run one per deployment.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("openapi", help="Print the OpenAPI spec to stdout")
    sub.add_parser("outbox-relay", help="Run the transactional-outbox relay")

    args = parser.parse_args(argv)
    if args.command == "openapi":
        return _openapi()
    if args.command == "outbox-relay":
        return _outbox_relay()
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
