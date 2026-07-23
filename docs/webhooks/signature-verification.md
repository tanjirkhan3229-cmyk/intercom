# Verifying Relay webhook signatures

Every webhook Relay delivers is signed so you can confirm it genuinely came from Relay and was not
tampered with or replayed. **Always verify the signature before trusting a webhook.**

## What Relay sends

Each delivery is an HTTP `POST` with a JSON body and these headers:

| Header | Example | Meaning |
|---|---|---|
| `Relay-Signature` | `v1=9f86d081...` | `v1=` + hex HMAC-SHA256 of the signed payload |
| `Relay-Timestamp` | `1753272000` | Unix seconds when the request was signed |
| `Relay-Event-Id` | `evt_2Yx...` | Stable id for this event — **dedupe on it** (delivery is at-least-once) |
| `Relay-Topic` | `conversation.created` | The event topic |

The JSON body looks like:

```json
{"created_at":"2026-07-23T12:00:00+00:00","data":{...},"id":"evt_2Yx...","topic":"conversation.created"}
```

## How the signature is computed

1. Take the **exact raw request body bytes** (do not re-serialize the JSON — whitespace matters).
2. Build the signed content by prefixing the timestamp: `signed = f"{timestamp}." + body`.
3. Compute `HMAC-SHA256(secret, signed)` where `secret` is the subscription's signing secret
   (shown once when you create or rotate the subscription).
4. The header value is `v1=` followed by the lowercase hex digest.

Reject the request if the signature does not match, or if `Relay-Timestamp` is more than **5
minutes** from your current time (this bounds replay attacks). Compare in constant time.

## Python

```python
import hashlib
import hmac


def verify(secret, payload, signature_header, timestamp_header, now, tolerance=300):
    """Return True iff `payload` (raw request body bytes) carries a valid, fresh Relay signature.

    secret            -- your subscription signing secret (str)
    payload           -- the raw request body (bytes)
    signature_header  -- the `Relay-Signature` header value (str)
    timestamp_header  -- the `Relay-Timestamp` header value (str)
    now               -- current time, unix seconds (int)
    tolerance         -- max allowed age in seconds (default 300 = 5 min)
    """
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    signed = f"{timestamp}.".encode() + payload
    expected = "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

Flask example:

```python
import time
from flask import request, abort

@app.post("/relay/webhooks")
def receive():
    ok = verify(
        secret=MY_WEBHOOK_SECRET,
        payload=request.get_data(),  # raw bytes, not request.json
        signature_header=request.headers.get("Relay-Signature", ""),
        timestamp_header=request.headers.get("Relay-Timestamp", ""),
        now=int(time.time()),
    )
    if not ok:
        abort(400)
    ...  # dedupe on request.headers["Relay-Event-Id"], then process
```

## Node.js

```js
const crypto = require("crypto");

function verify(secret, payload, signatureHeader, timestampHeader, now, tolerance = 300) {
  const timestamp = parseInt(timestampHeader, 10);
  if (!Number.isFinite(timestamp) || Math.abs(now - timestamp) > tolerance) return false;
  const signed = Buffer.concat([Buffer.from(`${timestamp}.`), payload]); // payload = raw body Buffer
  const expected = "v1=" + crypto.createHmac("sha256", secret).update(signed).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signatureHeader || "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

## Notes

- **Delivery is at-least-once.** A single event may arrive more than once (retries, redelivery).
  Dedupe on `Relay-Event-Id`.
- Relay retries failed deliveries with exponential backoff + jitter for up to 72 hours. Respond
  `2xx` quickly (within 10 seconds) to acknowledge receipt; do heavy work asynchronously.
- After sustained failures a subscription is automatically disabled and the workspace is notified.
- Rotating the secret takes effect immediately; deliveries in flight during a rotation may be
  signed with the new secret, so accept either briefly if you rotate.
