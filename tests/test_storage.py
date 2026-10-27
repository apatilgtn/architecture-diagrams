import dataclasses
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from arch_diagram_mcp import auth, storage

ALICE = auth.Principal("00000000-aaaa", "alice@example.com", "oidc", frozenset())


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    out = []
    for ext, data in [("png", b"\x89PNG\r\n\x1a\nfake"), ("svg", b"<svg/>")]:
        p = tmp_path / f"demo_20260101_000000_abcdef.{ext}"
        p.write_bytes(data)
        out.append(p)
    return out


def test_object_keys_are_per_caller_and_do_not_leak_names(settings, files):
    key = storage.object_key(settings, ALICE, files[0])
    assert key.startswith("diagrams/") and key.endswith("/demo_20260101_000000_abcdef.png")
    assert "alice" not in key
    bob = auth.Principal("00000000-bbbb", "bob", "oidc")
    assert storage.object_key(settings, bob, files[0]) != key


def test_local_store_stdio_returns_file_uris(settings, files):
    pub = storage.LocalStore(settings, http=False).publish(files, auth.LOCAL_USER)
    assert pub.urls["png"].startswith("file:") and pub.expires_at is None


def test_local_signatures_expire_and_bind_to_file(settings):
    s = dataclasses.replace(settings, url_signing_key="k" * 40)
    store = storage.LocalStore(s, http=True)
    exp = int(time.time()) + 60
    sig = store.sign("a.png", exp)
    assert store.verify("a.png", str(exp), sig)
    assert not store.verify("b.png", str(exp), sig)
    assert not store.verify("a.png", str(exp + 1), sig)
    assert not store.verify("a.png", str(int(time.time()) - 1), store.sign("a.png", int(time.time()) - 1))
    # Same key on another replica validates the same link.
    assert storage.LocalStore(s, http=True).verify("a.png", str(exp), sig)


def test_s3_upload_encrypts_and_presigns(settings, files):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        s = dataclasses.replace(settings, storage_backend="s3", storage_bucket="diagrams-bucket", url_ttl_s=900, s3_region="us-east-1")
        store = storage.S3Store(s)  # the production client construction, incl. SigV4
        client = store.client
        client.create_bucket(Bucket="diagrams-bucket")
        pub = store.publish(files, ALICE)

        key = storage.object_key(s, ALICE, files[1])
        head = client.head_object(Bucket="diagrams-bucket", Key=key)
        assert head["ContentType"] == "image/svg+xml" and head["ServerSideEncryption"] == "AES256"
        q = parse_qs(urlparse(pub.urls["svg"]).query)
        assert q["X-Amz-Expires"] == ["900"] and "X-Amz-Signature" in q


def test_s3_failure_is_a_clean_storage_error(settings, files):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        s = dataclasses.replace(settings, storage_backend="s3", storage_bucket="missing-bucket")
        with pytest.raises(storage.StorageError, match="s3://missing-bucket"):
            storage.S3Store(s, client=client).publish(files, ALICE)


def test_azure_upload_with_read_only_https_sas(settings, files, monkeypatch):
    pytest.importorskip("azure.storage.blob")
    from azure.storage.blob import BlobClient

    conn = "DefaultEndpointsProtocol=https;AccountName=diagacct;AccountKey=" + "a" * 86 + "==;EndpointSuffix=core.windows.net"
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", conn)
    uploaded = {}
    monkeypatch.setattr(BlobClient, "upload_blob", lambda self, data, **kw: uploaded.__setitem__(self.blob_name, kw["content_settings"].content_type))

    s = dataclasses.replace(settings, storage_backend="azure", storage_bucket="diagrams")
    pub = storage.AzureBlobStore(s).publish(files, ALICE)
    url = urlparse(pub.urls["png"])
    q = parse_qs(url.query)
    assert url.netloc == "diagacct.blob.core.windows.net" and url.path.startswith("/diagrams/diagrams/")
    assert q["sp"] == ["r"] and q["spr"] == ["https"] and "sig" in q
    assert set(uploaded.values()) == {"image/png", "image/svg+xml"}


def test_gcs_upload_with_v4_signed_url(settings, files, monkeypatch):
    pytest.importorskip("google.cloud.storage")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.cloud import storage as gcs
    from google.oauth2 import service_account

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    creds = service_account.Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": "diagrams@proj.iam.gserviceaccount.com",
            "private_key": pem.decode(),
            "private_key_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
            "project_id": "proj",
        }
    )
    uploaded = []
    monkeypatch.setattr(gcs.Blob, "upload_from_filename", lambda self, f, content_type=None: uploaded.append((self.name, content_type)))
    s = dataclasses.replace(settings, storage_backend="gcs", storage_bucket="diagrams-bucket", url_ttl_s=600)
    pub = storage.GCSStore(s, client=gcs.Client(project="proj", credentials=creds)).publish(files, ALICE)
    q = parse_qs(urlparse(pub.urls["png"]).query)
    assert q["X-Goog-Algorithm"] == ["GOOG4-RSA-SHA256"] and q["X-Goog-Expires"] == ["600"]
    assert {ct for _, ct in uploaded} == {"image/png", "image/svg+xml"}


def test_gcs_default_credentials_request_cloud_platform_scope(settings, monkeypatch):
    # Regression: found on Cloud Run, where storage-only scopes made the signBlob call fail.
    pytest.importorskip("google.cloud.storage")
    import google.auth
    from google.auth.credentials import AnonymousCredentials

    requested = {}

    def fake_default(scopes=None, **_):
        requested["scopes"] = scopes
        return AnonymousCredentials(), "proj"

    monkeypatch.setattr(google.auth, "default", fake_default)
    s = dataclasses.replace(settings, storage_backend="gcs", storage_bucket="diagrams-bucket")
    store = storage.GCSStore(s)
    assert requested["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
    assert store.client.project == "proj"


def test_config_validation(monkeypatch):
    from arch_diagram_mcp.config import Settings

    monkeypatch.setenv("ARCH_DIAGRAM_AUTH_TOKENS", "ci=short")
    with pytest.raises(ValueError, match="16 characters"):
        Settings.from_env()
    monkeypatch.setenv("ARCH_DIAGRAM_AUTH_TOKENS", "ci=" + "x" * 20)
    monkeypatch.setenv("ARCH_DIAGRAM_STORAGE", "s3")
    with pytest.raises(ValueError, match="STORAGE_BUCKET"):
        Settings.from_env()
    monkeypatch.setenv("ARCH_DIAGRAM_STORAGE_BUCKET", "b")
    s = Settings.from_env()
    assert s.auth_mode == "token" and s.auth_tokens == (("ci", "x" * 20),)
    assert "x" * 20 not in repr(s)  # secrets never appear in logs or tracebacks
