"""Bearer-token authentication for the Garmin MCP Streamable HTTP endpoint.

A minimal, dependency-free ASGI middleware. Every request to a protected path
must present the token one of two ways:

- Header:  ``Authorization: Bearer <MCP_ACCESS_TOKEN>``   (preferred)
- Query:   ``?token=<MCP_ACCESS_TOKEN>``                  (or ``?access_token=``)

The query-parameter form exists because some MCP clients (e.g. Claude's custom
connector) can only take a URL and have no field for a custom header. It is
slightly less private (URLs may appear in server logs), so prefer the header
when your client supports it. All comparisons are constant-time.

Design choices:
- Fail closed. If no token is configured the middleware rejects everything,
  but the server also refuses to start without a token (see ``http_server``),
  so the endpoint is never reachable unprotected — not even briefly.
- Exempt only ``/healthz``. Unknown paths get 401 before routing, so the
  route table is never revealed to unauthenticated callers.
"""

import hmac
from typing import Iterable
from urllib.parse import parse_qs

# Query-string keys accepted as an alternative to the Authorization header.
_QUERY_KEYS = ("token", "access_token")


class BearerAuthMiddleware:
    def __init__(self, app, token: str, exempt_paths: Iterable[str] = ("/healthz",)):
        self.app = app
        self._token = token or ""
        self._expected_header = f"Bearer {self._token}".encode("utf-8")
        self.exempt_paths = set(exempt_paths)

    def _authorized(self, scope) -> bool:
        if not self._token:
            return False

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        if hmac.compare_digest(provided, self._expected_header):
            return True

        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        for key in _QUERY_KEYS:
            for value in qs.get(key, ()):
                if hmac.compare_digest(value, self._token):
                    return True
        return False

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            # Lifespan / websocket events pass straight through.
            await self.app(scope, receive, send)
            return

        if scope.get("path", "") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        if self._authorized(scope):
            await self.app(scope, receive, send)
            return

        await self._unauthorized(send)

    @staticmethod
    async def _unauthorized(send):
        body = b'{"error":"unauthorized","detail":"missing or invalid bearer token"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="garmin-mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
