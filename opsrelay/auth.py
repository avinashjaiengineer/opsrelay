"""Who is calling the coordinator API: authentication.

    OPSRELAY_AUTH_MODE=none   no login (development). Everyone is an anonymous admin-equivalent,
                              and a decision's approver is whatever name was typed: recorded as
                              *unverified* in the audit log.
    OPSRELAY_AUTH_MODE=dev    named users with random bearer tokens, from a YAML file (or a
                              Secrets Manager secret holding it). Tokens are stored only as SHA-256
                              hashes: `opsrelay users add` prints a new token once.
    OPSRELAY_AUTH_MODE=oidc   JWTs from Amazon Cognito or any OIDC provider, verified against the
                              provider's JWKS (signature, issuer, audience, expiry). Roles come from
                              a claim (default `cognito:groups`), the name from another (`email`).

In `dev` and `oidc` modes every API call needs `Authorization: Bearer <token>`, and a decision's
approver is the authenticated principal, never a name from the request. What each role may do is
in opsrelay.rbac.
"""

import hashlib
import hmac
import json
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from . import secrets
from .config import get_settings

ROLES = ("viewer", "operator", "sre", "incident_commander", "admin", "auditor")


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    name: str
    roles: frozenset[str]
    method: str  # none | dev | oidc
    verified: bool

    def public(self) -> dict:
        return {"name": self.name, "roles": sorted(self.roles), "method": self.method, "verified": self.verified}


ANONYMOUS = Principal("anonymous", "anonymous", frozenset(ROLES), "none", False)
_current: ContextVar[Principal] = ContextVar("opsrelay_principal", default=ANONYMOUS)


def current() -> Principal:
    return _current.get()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@lru_cache
def _dev_users(source: str) -> tuple[tuple[str, str, frozenset[str]], ...]:
    text = secrets.resolve(source) if secrets.is_reference(source) else Path(source).read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    users = []
    for u in doc.get("users") or []:
        digest = u.get("token_sha256") or (token_hash(u["token"]) if u.get("token") else None)
        if not digest:
            raise ValueError(f"dev user {u.get('name')} has no token_sha256")
        unknown = set(u.get("roles") or []) - set(ROLES)
        if unknown:
            raise ValueError(f"dev user {u.get('name')}: unknown roles {sorted(unknown)}")
        users.append((u["name"], digest, frozenset(u.get("roles") or [])))
    return tuple(users)


@lru_cache
def _jwks_client(url: str):  # noqa: ANN202
    import jwt

    return jwt.PyJWKClient(url, cache_keys=True)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    return authorization[7:].strip()


def authenticate(authorization: str | None) -> Principal:
    settings = get_settings()
    mode = settings.auth_mode
    if mode == "none":
        return ANONYMOUS
    token = _bearer(authorization)
    if mode == "dev":
        digest = token_hash(token)
        for name, expected, roles in _dev_users(settings.dev_users):
            if hmac.compare_digest(digest, expected):
                return Principal(f"dev:{name}", name, roles, "dev", True)
        raise AuthError("unknown token")
    return _verify_oidc(token)


def _verify_oidc(token: str) -> Principal:
    import jwt

    settings = get_settings()
    if not settings.oidc_issuer:
        raise AuthError("OIDC is not configured (OPSRELAY_OIDC_ISSUER)")
    jwks_url = settings.oidc_jwks_url or settings.oidc_issuer.rstrip("/") + "/.well-known/jwks.json"
    try:
        key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience or None,
            options={"verify_aud": bool(settings.oidc_audience), "require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}") from e
    raw_roles = claims.get(settings.oidc_roles_claim) or []
    if isinstance(raw_roles, str):
        raw_roles = raw_roles.replace(",", " ").split()
    roles = frozenset(r for r in raw_roles if r in ROLES)
    name = claims.get(settings.oidc_name_claim) or claims.get("cognito:username") or claims["sub"]
    return Principal(claims["sub"], str(name), roles, "oidc", True)


def clear_caches() -> None:
    _dev_users.cache_clear()
    _jwks_client.cache_clear()


# The alert webhook checks its own token (OPSRELAY_WEBHOOK_TOKEN), so sources need no user account.
# Routes that authenticate themselves: /alerts with its webhook token, Slack with its signature.
PUBLIC = {("GET", "/"), ("GET", "/ping"), ("POST", "/alerts"), ("POST", "/integrations/slack/actions")}


ORIGIN_HEADER = b"x-opsrelay-origin"
LOCAL = {"127.0.0.1", "::1", "localhost"}


def _through_front_door(scope) -> bool:  # noqa: ANN001
    expected = secrets.resolve(get_settings().origin_secret)
    if not expected:
        return True
    if (scope.get("client") or ("",))[0] in LOCAL:
        return True
    presented = dict(scope.get("headers") or []).get(ORIGIN_HEADER, b"")
    return hmac.compare_digest(presented, expected.encode())


async def _refuse(send, status: int, message: str) -> None:  # noqa: ANN001
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps({"error": message}).encode()})


class AuthMiddleware:
    """ASGI middleware for the coordinator: authenticates every API request and makes the principal
    available to the handler (opsrelay.auth.current). The dashboard page itself and /ping are public."""

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http" and not _through_front_door(scope):
            await _refuse(send, 403, "requests must come through the HTTPS endpoint")
            return
        if scope["type"] != "http" or (scope.get("method"), scope.get("path")) in PUBLIC:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        try:
            principal = authenticate((headers.get(b"authorization") or b"").decode() or None)
        except AuthError as e:
            body = json.dumps({"error": f"unauthorized: {e}"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = _current.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
