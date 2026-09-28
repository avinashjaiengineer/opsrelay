"""Authenticates the coordinator to specialist A2A servers that aren't behind AgentCore.

On AgentCore, agent-to-agent calls are SigV4-signed and authorized by IAM. Elsewhere (`opsrelay
up`, EC2, Docker Compose) a specialist would otherwise accept JSON-RPC from anyone who can reach
its port, so with OPSRELAY_A2A_TOKEN set it requires `Authorization: Bearer <token>` on every
request except the public agent card and /ping. Comparison is constant-time.
"""

import hmac
import json

from . import secrets
from .config import get_settings

PUBLIC_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json", "/ping")


class RequireBearerToken:
    """ASGI middleware: 401 unless the request carries the shared A2A token."""

    def __init__(self, app, token: str):  # noqa: ANN001
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http" or scope.get("path", "") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        presented = dict(scope.get("headers") or []).get(b"authorization", b"")
        if hmac.compare_digest(presented, self.expected):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "missing or invalid A2A bearer token"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def protect(app):  # noqa: ANN001, ANN201
    """Wrap a specialist app when a token is configured."""
    token = secrets.resolve(get_settings().a2a_token)
    return RequireBearerToken(app, token) if token else app
