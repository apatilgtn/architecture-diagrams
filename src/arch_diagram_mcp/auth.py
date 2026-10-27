"""Caller identity for HTTP mode: named static tokens or OIDC JWTs (Entra ID, Okta, Auth0, Keycloak...).

The authenticated Principal is stored in a context variable for the duration of the request so tools
can authorize actions and audit logs can record who did what. stdio mode runs as the local user.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import urllib.request
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import jwt

from .config import Settings

log = logging.getLogger(__name__)

# Asymmetric only: HS* would let anyone holding the (public) JWKS forge tokens, and "none" is never valid.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")


class AuthError(Exception):
    """Authentication failed; the message is safe to return to the client."""


class Forbidden(Exception):
    """Authenticated, but not allowed to perform the action."""


@dataclass(frozen=True)
class Principal:
    subject: str
    name: str
    method: str  # local | token | oidc
    roles: frozenset[str] = field(default_factory=frozenset)


LOCAL_USER = Principal(subject="local", name="local-user", method="local")
_current: ContextVar[Principal] = ContextVar("arch_diagram_principal", default=LOCAL_USER)


def current() -> Principal:
    return _current.get()


def set_current(principal: Principal) -> Token[Principal]:
    return _current.set(principal)


def reset_current(token: Token[Principal]) -> None:
    _current.reset(token)


class Permission(str, Enum):
    READ = "read"  # search, templates, validate, review, server_info
    WRITE = "write"  # render files, import Terraform


def authorize(principal: Principal, permission: Permission, settings: Settings) -> None:
    """Role checks apply to OIDC callers; local and static-token callers are trusted operators."""
    if principal.method != "oidc":
        return
    roles = principal.roles
    if permission is Permission.WRITE and settings.author_role and settings.author_role not in roles:
        raise Forbidden(f"'{principal.name}' needs the '{settings.author_role}' role to render or import diagrams.")
    if permission is Permission.READ and settings.reader_role:
        allowed = {settings.reader_role} | ({settings.author_role} if settings.author_role else set())
        if not allowed & roles:
            raise Forbidden(f"'{principal.name}' needs the '{settings.reader_role}' role.")


class Authenticator(Protocol):
    method: str

    def authenticate(self, token: str) -> Principal: ...


class StaticTokenAuthenticator:
    """Named shared secrets; several may be active at once so tokens can be rotated without downtime."""

    method = "token"

    def __init__(self, tokens: dict[str, str]):
        if not tokens:
            raise ValueError("At least one token is required.")
        self._tokens = [(name, secret.encode()) for name, secret in tokens.items()]

    def authenticate(self, token: str) -> Principal:
        supplied = token.encode()
        match: str | None = None
        for name, secret in self._tokens:  # compare against every token: no early exit timing signal
            if hmac.compare_digest(supplied, secret):
                match = name
        if match is None:
            raise AuthError("invalid token")
        return Principal(subject=f"token:{match}", name=match, method="token")


class OIDCAuthenticator:
    """Validates bearer JWTs against an OIDC issuer's published signing keys."""

    method = "oidc"

    def __init__(
        self,
        issuer: str,
        audiences: tuple[str, ...],
        *,
        jwks_uri: str | None = None,
        required_scopes: tuple[str, ...] = (),
        leeway_s: int = 60,
        jwk_client: jwt.PyJWKClient | None = None,
    ):
        if not issuer or not audiences:
            raise ValueError("OIDC needs an issuer and at least one audience.")
        self.issuer = issuer
        self.audiences = list(audiences)
        self.required_scopes = set(required_scopes)
        self.leeway_s = leeway_s
        self._jwks_uri = jwks_uri
        self._client = jwk_client
        self._lock = threading.Lock()

    def _jwk_client(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._client is None:
                uri = self._jwks_uri or self._discover_jwks_uri()
                self._client = jwt.PyJWKClient(uri, cache_jwk_set=True, lifespan=3600, timeout=10)
            return self._client

    def _discover_jwks_uri(self) -> str:
        url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - https issuer from operator config
            doc: dict[str, Any] = json.load(resp)
        if doc.get("issuer", "").rstrip("/") != self.issuer.rstrip("/"):
            raise AuthError("OIDC discovery issuer mismatch")
        return str(doc["jwks_uri"])

    def authenticate(self, token: str) -> Principal:
        try:
            key = self._jwk_client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.audiences,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            log.info("jwt_rejected", extra={"reason": type(exc).__name__})
            raise AuthError("invalid token") from exc

        scopes = set(str(claims.get("scp") or claims.get("scope") or "").split())
        roles = {str(r) for r in claims.get("roles") or []}
        if self.required_scopes and not self.required_scopes <= (scopes | roles):
            raise AuthError("token lacks required scope")
        subject = str(claims.get("oid") or claims.get("sub") or "")
        if not subject:
            raise AuthError("token has no subject")
        name = str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or subject)
        return Principal(subject=subject, name=name, method="oidc", roles=frozenset(roles | scopes))


def build_authenticator(settings: Settings) -> Authenticator | None:
    if settings.auth_mode == "none":
        return None
    if settings.auth_mode == "token":
        return StaticTokenAuthenticator(dict(settings.auth_tokens))
    if settings.auth_mode == "oidc":
        return OIDCAuthenticator(
            settings.oidc_issuer or "",
            settings.oidc_audiences,
            jwks_uri=settings.oidc_jwks_uri,
            required_scopes=settings.oidc_required_scopes,
        )
    raise ValueError(f"Unknown auth mode '{settings.auth_mode}'.")
