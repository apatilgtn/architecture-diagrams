import dataclasses
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from arch_diagram_mcp import auth, storage
from arch_diagram_mcp.http_app import build_app
from arch_diagram_mcp.server import create_server

ISSUER = "https://login.example.com/tenant/v2.0"
AUDIENCE = "api://arch-diagrams"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKClient:
    """Stands in for the issuer's JWKS endpoint."""

    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        jwt.get_unverified_header(token)  # malformed tokens fail here like the real client
        return type("Key", (), {"key": self.public_key})()


def make_token(key=KEY, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "oid": "00000000-aaaa-bbbb-cccc-000000000001",
        "preferred_username": "alice@example.com",
        "roles": ["Diagrams.Author"],
        "scp": "diagrams.access",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def oidc(**kw):
    return auth.OIDCAuthenticator(ISSUER, (AUDIENCE,), jwk_client=FakeJWKClient(KEY.public_key()), **kw)


# --------------------------------------------------------------------------- authenticators


def test_oidc_accepts_valid_token_and_maps_identity():
    p = oidc(required_scopes=("diagrams.access",)).authenticate(make_token())
    assert p.method == "oidc" and p.name == "alice@example.com"
    assert p.subject == "00000000-aaaa-bbbb-cccc-000000000001"
    assert {"Diagrams.Author", "diagrams.access"} <= p.roles


@pytest.mark.parametrize(
    "token,reason",
    [
        (make_token(exp=int(time.time()) - 3600), "expired"),
        (make_token(aud="api://someone-else"), "invalid"),
        (make_token(iss="https://evil.example.com"), "invalid"),
        (make_token(key=OTHER_KEY), "invalid"),
        (make_token(exp=None), "invalid"),
        ("not-a-jwt", "invalid"),
    ],
)
def test_oidc_rejects_bad_tokens(token, reason):
    with pytest.raises(auth.AuthError, match=reason):
        oidc().authenticate(token)


def test_oidc_rejects_algorithm_confusion_and_none():
    from cryptography.hazmat.primitives import serialization

    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    claims = jwt.decode(make_token(), options={"verify_signature": False})
    # HS256 signed with the public key as the shared secret, the classic key-confusion attack.
    # Built by hand because PyJWT refuses to produce such a token.
    import base64
    import hashlib
    import hmac

    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")  # noqa: E731
    signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode()) + b"." + b64(json.dumps(claims).encode())
    forged = (signing_input + b"." + b64(hmac.new(pem, signing_input, hashlib.sha256).digest())).decode()
    with pytest.raises(auth.AuthError):
        oidc().authenticate(forged)
    unsigned = jwt.encode(claims, None, algorithm="none")
    with pytest.raises(auth.AuthError):
        oidc().authenticate(unsigned)


def test_oidc_required_scope():
    with pytest.raises(auth.AuthError, match="scope"):
        oidc(required_scopes=("diagrams.admin",)).authenticate(make_token())


def test_static_tokens_support_rotation():
    a = auth.StaticTokenAuthenticator({"old": "o" * 20, "new": "n" * 20})
    assert a.authenticate("o" * 20).name == "old"
    assert a.authenticate("n" * 20).name == "new"
    with pytest.raises(auth.AuthError):
        a.authenticate("x" * 20)


def test_rbac_rules(settings):
    s = dataclasses.replace(settings, reader_role="Diagrams.Reader", author_role="Diagrams.Author")
    reader = auth.Principal("r", "reader", "oidc", frozenset({"Diagrams.Reader"}))
    author = auth.Principal("a", "author", "oidc", frozenset({"Diagrams.Author"}))
    nobody = auth.Principal("n", "nobody", "oidc", frozenset())
    auth.authorize(reader, auth.Permission.READ, s)
    auth.authorize(author, auth.Permission.READ, s)  # author implies reader
    auth.authorize(author, auth.Permission.WRITE, s)
    with pytest.raises(auth.Forbidden):
        auth.authorize(reader, auth.Permission.WRITE, s)
    with pytest.raises(auth.Forbidden):
        auth.authorize(nobody, auth.Permission.READ, s)
    auth.authorize(auth.LOCAL_USER, auth.Permission.WRITE, s)  # stdio is always trusted


# --------------------------------------------------------------------------- end to end over HTTP


@pytest.fixture
def oidc_client(settings):
    s = dataclasses.replace(
        settings,
        auth_mode="oidc",
        oidc_issuer=ISSUER,
        oidc_audiences=(AUDIENCE,),
        reader_role="Diagrams.Reader",
        author_role="Diagrams.Author",
        allowed_hosts=("testserver",),
        public_base_url="https://diagrams.example.com",
        url_signing_key="k" * 40,
    )
    store = storage.LocalStore(s, http=True)
    app = build_app(create_server(s, store, http=True), s, store, authenticator=oidc())
    with TestClient(app) as client:
        yield client


def rpc(client, token, method, params, rid=1):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
    )


def test_protected_resource_metadata_and_challenge(oidc_client):
    meta = oidc_client.get("/.well-known/oauth-protected-resource").json()
    assert meta["authorization_servers"] == [ISSUER]
    assert meta["resource"] == "https://diagrams.example.com/mcp"
    r = oidc_client.post("/mcp", json={})
    assert r.status_code == 401
    assert 'resource_metadata="https://diagrams.example.com/.well-known/oauth-protected-resource"' in r.headers["www-authenticate"]


def test_identity_reaches_tools_and_rbac_is_enforced(oidc_client, small_spec):
    author = make_token()
    info = rpc(oidc_client, author, "tools/call", {"name": "server_info", "arguments": {}}).json()
    caller = info["result"]["structuredContent"]["caller"]
    assert caller == {"name": "alice@example.com", "method": "oidc", "roles": ["Diagrams.Author", "diagrams.access"]}
    assert "output_dir" not in info["result"]["structuredContent"]  # no server paths over HTTP

    reader = make_token(roles=["Diagrams.Reader"], preferred_username="bob@example.com")
    ok = rpc(oidc_client, reader, "tools/call", {"name": "search_icons", "arguments": {"query": "s3"}}).json()
    assert not ok["result"].get("isError")
    denied = rpc(oidc_client, reader, "tools/call", {"name": "generate_architecture_diagram", "arguments": {"spec": small_spec.model_dump()}}).json()
    assert denied["result"]["isError"] and "Diagrams.Author" in denied["result"]["content"][0]["text"]

    nobody = make_token(roles=[], preferred_username="eve@example.com")
    blocked = rpc(oidc_client, nobody, "tools/call", {"name": "search_icons", "arguments": {"query": "s3"}}).json()
    assert blocked["result"]["isError"]


def test_render_returns_signed_download_links(oidc_client, small_spec):
    spec = small_spec.model_dump() | {"output_formats": ["png", "svg"]}
    res = rpc(oidc_client, make_token(), "tools/call", {"name": "generate_architecture_diagram", "arguments": {"spec": spec}}).json()
    summary = json.loads(res["result"]["content"][0]["text"])
    assert "files" not in summary and summary["storage"] == "local"
    svg_url = summary["urls"]["svg"]
    assert svg_url.startswith("https://diagrams.example.com/artifacts/")
    path = svg_url.removeprefix("https://diagrams.example.com")

    got = oidc_client.get(path)  # no bearer token needed: the signature is the credential
    assert got.status_code == 200 and got.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in got.headers["content-security-policy"] and got.headers["x-content-type-options"] == "nosniff"

    assert oidc_client.get(path.replace("sig=", "sig=x")).status_code == 403
    assert oidc_client.get(path.replace(".svg?", ".png?")).status_code == 403  # signature is per file
    assert oidc_client.get("/artifacts/..%2F..%2Fsecret.txt?exp=9999999999&sig=x").status_code == 403


def test_terraform_path_disabled_over_http(oidc_client):
    res = rpc(oidc_client, make_token(), "tools/call", {"name": "diagram_from_terraform", "arguments": {"title": "t", "path": "plan.json"}}).json()
    assert res["result"]["isError"] and "disabled over HTTP" in res["result"]["content"][0]["text"]
