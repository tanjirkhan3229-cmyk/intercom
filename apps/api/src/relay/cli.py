"""Small operational CLI. ``relay openapi`` dumps the spec used to generate the TS SDK."""

from __future__ import annotations

import argparse
import json
import sys


def _openapi() -> int:
    from relay.main import app

    json.dump(app.openapi(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("openapi", help="Print the OpenAPI spec to stdout")

    args = parser.parse_args(argv)
    if args.command == "openapi":
        return _openapi()
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
