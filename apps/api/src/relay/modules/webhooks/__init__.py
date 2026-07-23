"""The ``webhooks`` module (P0.11): signed, at-least-once webhook delivery fed from the outbox.

Owns two tables (``webhook_subscriptions``, partitioned ``webhook_deliveries``), a public API for
managing subscriptions + inspecting/redelivering deliveries, a dispatch consumer that turns outbox
events into per-subscription delivery rows, and a Celery ``webhooks.deliver`` task that signs
(HMAC) and POSTs them through an SSRF-guarded client with retries + a per-endpoint circuit breaker.
"""
