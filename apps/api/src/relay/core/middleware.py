"""Cross-cutting ASGI middleware.

``RequestContextMiddleware`` assigns a correlation id per request (honouring an inbound
``X-Request-ID``), stashes it in a contextvar for structured logging, and echoes it back
on the response. The tenant/session middleware that sets ``app.ws`` lives in the identity
module (P0.1), because it needs the authenticated principal.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from relay.core.context import request_id_var

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
