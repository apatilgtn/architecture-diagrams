"""Streamable-HTTP deployment.

Routes handled before the MCP app:
- GET /healthz                                   unauthenticated liveness probe
- GET /.well-known/oauth-protected-resource[/mcp] RFC 9728 metadata (OIDC mode) so MCP clients can
                                                  discover the authorization server
- GET /artifacts/<file>?exp=..&sig=..            signed, expiring downloads (local storage)
Everything else requires a valid bearer token (static or OIDC) when auth is enabled.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, auth
from .config import Settings
from .storage import CONTENT_TYPES, ArtifactStore, LocalStore

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

METADATA_PATH = "/.well-known/oauth-protected-resource"
_ARTIFACT_NAME = re.compile(r"^[a-z0-9_-]{1,80}_\d{8}_\d{6}_[0-9a-f]{6}\.(png|svg|pdf|jpg|dot)$")
_ARTIFACT_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    # SVGs are served as documents; forbid scripts and remote loads even though Graphviz output has none.
    (b"content-security-policy", b"default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"),
    (b"cache-control", b"private, max-age=300"),
]


class AuthMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, authenticator: auth.Authenticator | None, store: ArtifactStore):
        self.app = app
        self.settings = settings
        self.authenticator = authenticator
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan etc.
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path == "/healthz":
            await _json(send, 200, {"status": "ok", "version": __version__})
            return
        if path.startswith(METADATA_PATH) and self.settings.auth_mode == "oidc":
            await _json(send, 200, self._resource_metadata())
            return
        if path.startswith("/artifacts/"):
            await self._serve_artifact(scope, send)
            return
        if self.authenticator is None:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        scheme, _, value = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            await self._unauthorized(send, "missing bearer token")
            return
        try:
            principal = await anyio.to_thread.run_sync(self.authenticator.authenticate, value.strip())
        except auth.AuthError as exc:
            await self._unauthorized(send, str(exc))
            return
        token = auth.set_current(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.reset_current(token)

    def _resource_metadata(self) -> dict[str, Any]:
        base = self.settings.public_base_url or f"http://{self.settings.http_host}:{self.settings.http_port}"
        return {
            "resource": f"{base}/mcp",
            "authorization_servers": [self.settings.oidc_issuer],
            "scopes_supported": list(self.settings.oidc_required_scopes),
            "bearer_methods_supported": ["header"],
            "resource_name": "Architecture Diagrams MCP",
        }

    async def _unauthorized(self, send: Send, reason: str) -> None:
        challenge = f'Bearer realm="mcp", error="invalid_token", error_description="{reason}"'
        if self.settings.auth_mode == "oidc" and self.settings.public_base_url:
            challenge += f', resource_metadata="{self.settings.public_base_url}{METADATA_PATH}"'
        await _json(send, 401, {"error": "unauthorized", "detail": reason}, {b"www-authenticate": challenge.encode()})

    async def _serve_artifact(self, scope: Scope, send: Send) -> None:
        if not isinstance(self.store, LocalStore) or scope.get("method") != "GET":
            await _json(send, 404, {"error": "not found"})
            return
        name = unquote(scope["path"][len("/artifacts/"):])
        query = parse_qs(scope.get("query_string", b"").decode())
        exp, sig = (query.get("exp") or [""])[0], (query.get("sig") or [""])[0]
        path = self.settings.output_dir / name
        if not _ARTIFACT_NAME.match(name) or not self.store.verify(name, exp, sig) or not path.is_file():
            await _json(send, 403, {"error": "invalid or expired link"})
            return
        body = await anyio.Path(path).read_bytes()
        headers = [
            (b"content-type", CONTENT_TYPES.get(path.suffix, "application/octet-stream").encode()),
            (b"content-length", str(len(body)).encode()),
            *_ARTIFACT_HEADERS,
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})


async def _json(send: Send, status: int, body: dict[str, Any], headers: dict[bytes, bytes] | None = None) -> None:
    payload = json.dumps(body).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    hdrs += list((headers or {}).items())
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": payload})


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_app(mcp: MCPServer, settings: Settings, store: ArtifactStore, authenticator: auth.Authenticator | None = None) -> ASGIApp:
    if authenticator is None and settings.auth_mode != "none":
        authenticator = auth.build_authenticator(settings)
    if authenticator is None and not _is_loopback(settings.http_host) and not settings.allow_unauthenticated:
        raise SystemExit(
            f"Refusing to serve on {settings.http_host} without authentication. Configure ARCH_DIAGRAM_AUTH_TOKENS "
            "or ARCH_DIAGRAM_OIDC_ISSUER, bind to 127.0.0.1, or set ARCH_DIAGRAM_ALLOW_UNAUTHENTICATED=true "
            "behind a trusted authenticating proxy."
        )
    security = None
    if settings.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        )
    # Without an allow-list the SDK protects loopback binds automatically and accepts any Host otherwise.
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host=settings.http_host, transport_security=security)
    return AuthMiddleware(app, settings, authenticator, store)
