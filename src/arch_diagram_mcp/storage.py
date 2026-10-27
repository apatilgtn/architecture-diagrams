"""Where rendered files go and how callers download them.

- local: files stay in the output directory. Over HTTP with ARCH_DIAGRAM_PUBLIC_BASE_URL set, the server
  hands out HMAC-signed, expiring /artifacts/... links that it serves itself; over stdio, file:// URIs.
- s3 / azure / gcs: files are uploaded under <prefix>/<yyyy>/<mm>/<dd>/<caller-hash>/ and returned as
  pre-signed / SAS / V4-signed URLs that expire after ARCH_DIAGRAM_URL_TTL_S.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .auth import Principal
from .config import Settings

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".dot": "text/vnd.graphviz; charset=utf-8",
}


class StorageError(RuntimeError):
    pass


@dataclass
class Published:
    backend: str
    urls: dict[str, str]  # format -> URL
    expires_at: datetime | None


def owner_segment(principal: Principal) -> str:
    """Stable, non-reversible folder per caller, so object keys don't leak user names."""
    return hashlib.sha256(principal.subject.encode()).hexdigest()[:16]


def object_key(settings: Settings, principal: Principal, path: Path) -> str:
    return f"{settings.storage_prefix}{datetime.now(timezone.utc):%Y/%m/%d}/{owner_segment(principal)}/{path.name}"


def _expiry(settings: Settings) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=settings.url_ttl_s)


def _fmt(path: Path) -> str:
    return path.suffix.lstrip(".")


class ArtifactStore(ABC):
    name: str

    @abstractmethod
    def publish(self, files: list[Path], principal: Principal) -> Published: ...


class LocalStore(ArtifactStore):
    name = "local"

    def __init__(self, settings: Settings, *, http: bool):
        self.settings = settings
        self.http = http
        self._key = (settings.url_signing_key or secrets.token_urlsafe(48)).encode()
        if http and not settings.url_signing_key:
            log.warning("url_signing_key_ephemeral", extra={"hint": "set ARCH_DIAGRAM_URL_SIGNING_KEY when running more than one replica"})

    def sign(self, name: str, expires: int) -> str:
        mac = hmac.new(self._key, f"{name}\n{expires}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def verify(self, name: str, expires: str, signature: str) -> bool:
        try:
            exp = int(expires)
        except ValueError:
            return False
        return exp >= int(time.time()) and hmac.compare_digest(self.sign(name, exp), signature)

    def publish(self, files: list[Path], principal: Principal) -> Published:
        base = self.settings.public_base_url
        if not (self.http and base):
            return Published(self.name, {_fmt(p): p.resolve().as_uri() for p in files}, None)
        expires = _expiry(self.settings)
        exp = int(expires.timestamp())
        urls = {_fmt(p): f"{base}/artifacts/{quote(p.name)}?exp={exp}&sig={self.sign(p.name, exp)}" for p in files}
        return Published(self.name, urls, expires)


class S3Store(ArtifactStore):
    name = "s3"

    def __init__(self, settings: Settings, client=None):
        import boto3
        from botocore.config import Config

        self.settings = settings
        # SigV4 is required for KMS-encrypted objects and in newer regions; boto3 may default to SigV2 links.
        self.client = client or boto3.client("s3", region_name=settings.s3_region, config=Config(signature_version="s3v4"))

    def publish(self, files: list[Path], principal: Principal) -> Published:
        bucket = self.settings.storage_bucket
        urls: dict[str, str] = {}
        for p in files:
            key = object_key(self.settings, principal, p)
            extra = {"ContentType": CONTENT_TYPES.get(p.suffix, "application/octet-stream")}
            if self.settings.s3_kms_key_id:
                extra |= {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.settings.s3_kms_key_id}
            else:
                extra["ServerSideEncryption"] = "AES256"
            try:
                self.client.upload_file(str(p), bucket, key, ExtraArgs=extra)
                urls[_fmt(p)] = self.client.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=self.settings.url_ttl_s
                )
            except Exception as exc:  # botocore raises many types; surface one clear error
                raise StorageError(f"S3 upload to s3://{bucket}/{key} failed: {exc}") from exc
        return Published(self.name, urls, _expiry(self.settings))


class AzureBlobStore(ArtifactStore):
    """Signs with the account key when using a connection string, otherwise with a user delegation key
    obtained through Entra ID (managed identity / workload identity / az login)."""

    name = "azure"

    def __init__(self, settings: Settings, service_client=None):
        import os

        from azure.storage.blob import BlobServiceClient

        self.settings = settings
        if service_client is not None:
            self.service = service_client
        elif os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
            self.service = BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
        elif settings.azure_account_url:
            from azure.identity import DefaultAzureCredential

            self.service = BlobServiceClient(settings.azure_account_url, credential=DefaultAzureCredential())
        else:
            raise StorageError("Azure storage needs AZURE_STORAGE_CONNECTION_STRING or ARCH_DIAGRAM_AZURE_ACCOUNT_URL.")
        self._udk = None
        self._udk_expiry = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def _delegation_key(self, needed_until: datetime):
        with self._lock:
            if self._udk is None or self._udk_expiry < needed_until:
                start = datetime.now(timezone.utc) - timedelta(minutes=5)
                self._udk_expiry = needed_until + timedelta(hours=1)
                self._udk = self.service.get_user_delegation_key(start, self._udk_expiry)
            return self._udk

    def _sas(self, container: str, blob: str, expiry: datetime) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        common = dict(
            account_name=self.service.account_name,
            container_name=container,
            blob_name=blob,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
            start=datetime.now(timezone.utc) - timedelta(minutes=5),
            protocol="https",
        )
        account_key = getattr(self.service.credential, "account_key", None)
        if account_key:
            return generate_blob_sas(account_key=account_key, **common)
        return generate_blob_sas(user_delegation_key=self._delegation_key(expiry), **common)

    def publish(self, files: list[Path], principal: Principal) -> Published:
        from azure.storage.blob import ContentSettings

        container = self.settings.storage_bucket or ""
        expiry = _expiry(self.settings)
        urls: dict[str, str] = {}
        for p in files:
            key = object_key(self.settings, principal, p)
            try:
                blob = self.service.get_blob_client(container, key)
                with p.open("rb") as fh:
                    blob.upload_blob(fh, overwrite=True, content_settings=ContentSettings(content_type=CONTENT_TYPES.get(p.suffix)))
                urls[_fmt(p)] = f"{blob.url}?{self._sas(container, key, expiry)}"
            except Exception as exc:
                raise StorageError(f"Azure Blob upload to {container}/{key} failed: {exc}") from exc
        return Published(self.name, urls, expiry)


class GCSStore(ArtifactStore):
    """V4 signed URLs: signs locally with a service-account key, or through the IAM signBlob API when
    running on GKE/Cloud Run with workload identity (needs roles/iam.serviceAccountTokenCreator)."""

    name = "gcs"
    # storage.Client() alone requests storage-only scopes; on Cloud Run/GKE the metadata token then lacks the
    # scope the IAM signBlob call needs, so uploads succeed but signing fails with ACCESS_TOKEN_SCOPE_INSUFFICIENT.
    SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

    def __init__(self, settings: Settings, client=None):
        from google.cloud import storage

        self.settings = settings
        if client is None:
            import google.auth

            creds, project = google.auth.default(scopes=list(self.SCOPES))
            client = storage.Client(project=project, credentials=creds)
        self.client = client

    def _signing_kwargs(self) -> dict[str, object]:
        creds = self.client._credentials  # noqa: SLF001 - the public client exposes no accessor
        if hasattr(creds, "sign_bytes") and getattr(creds, "signer", None) is not None:
            return {}
        import google.auth.transport.requests

        creds.refresh(google.auth.transport.requests.Request())
        return {"service_account_email": creds.service_account_email, "access_token": creds.token}

    def publish(self, files: list[Path], principal: Principal) -> Published:
        bucket = self.client.bucket(self.settings.storage_bucket)
        urls: dict[str, str] = {}
        for p in files:
            key = object_key(self.settings, principal, p)
            try:
                blob = bucket.blob(key)
                blob.upload_from_filename(str(p), content_type=CONTENT_TYPES.get(p.suffix))
                urls[_fmt(p)] = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(seconds=self.settings.url_ttl_s),
                    method="GET",
                    **self._signing_kwargs(),
                )
            except Exception as exc:
                raise StorageError(f"GCS upload to gs://{self.settings.storage_bucket}/{key} failed: {exc}") from exc
        return Published(self.name, urls, _expiry(self.settings))


def build_store(settings: Settings, *, http: bool) -> ArtifactStore:
    if settings.storage_backend == "s3":
        return S3Store(settings)
    if settings.storage_backend == "azure":
        return AzureBlobStore(settings)
    if settings.storage_backend == "gcs":
        return GCSStore(settings)
    return LocalStore(settings, http=http)
