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
- ``relay reporting-metrics`` — run the reporting-metrics consumer (P0.9): consumes the outbox
                               Redis stream and projects per-conversation metrics.
- ``relay ai-dispatch``      — run the Neko turn dispatcher (P1.2): consumes the outbox Redis stream
                               and enqueues ``ai.run_turn`` on the ai.interactive queue for each
                               customer message.
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


def _reporting_metrics() -> int:
    from relay.modules.reporting.consumer import main as run_metrics

    run_metrics()
    return 0


def _knowledge_indexing() -> int:
    from relay.modules.knowledge.indexing_consumer import main as run_indexing

    run_indexing()
    return 0


def _webhook_dispatch() -> int:
    from relay.modules.webhooks.consumer import main as run_dispatch

    run_dispatch()
    return 0


def _ai_dispatch() -> int:
    from relay.modules.ai.consumer import main as run_dispatch

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
    sub.add_parser(
        "reporting-metrics", help="Run the reporting-metrics consumer (outbox → metrics)"
    )
    sub.add_parser(
        "knowledge-indexing",
        help="Run the knowledge-indexing consumer (outbox → re-index tasks)",
    )
    sub.add_parser(
        "webhook-dispatch", help="Run the webhook dispatch consumer (outbox → webhook deliveries)"
    )
    sub.add_parser(
        "ai-dispatch", help="Run the Neko turn dispatcher (outbox → ai.interactive queue)"
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
    if args.command == "reporting-metrics":
        return _reporting_metrics()
    if args.command == "knowledge-indexing":
        return _knowledge_indexing()
    if args.command == "webhook-dispatch":
        return _webhook_dispatch()
    if args.command == "ai-dispatch":
        return _ai_dispatch()
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
