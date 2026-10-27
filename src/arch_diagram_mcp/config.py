"""Runtime settings, read once from ARCH_DIAGRAM_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "ARCH_DIAGRAM_"


def _env(name: str, default: str) -> str:
    return os.environ.get(PREFIX + name, default)


def _bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in _env(name, "").split(",") if v.strip())


def _tokens() -> tuple[tuple[str, str], ...]:
    """ARCH_DIAGRAM_AUTH_TOKENS="ci=abc,alice=def" plus the legacy single ARCH_DIAGRAM_AUTH_TOKEN."""
    pairs: list[tuple[str, str]] = []
    for entry in _csv("AUTH_TOKENS"):
        name, sep, secret = entry.partition("=")
        if not sep or not name.strip() or not secret.strip():
            raise ValueError("ARCH_DIAGRAM_AUTH_TOKENS entries must look like name=token")
        pairs.append((name.strip(), secret.strip()))
    legacy = os.environ.get(PREFIX + "AUTH_TOKEN")
    if legacy:
        pairs.append(("default", legacy))
    for _, secret in pairs:
        if len(secret) < 16:
            raise ValueError("Auth tokens must be at least 16 characters.")
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    input_roots: tuple[Path, ...]
    max_nodes: int = 300
    max_edges: int = 800
    max_clusters: int = 100
    max_terraform_bytes: int = 25 * 1024 * 1024
    render_timeout_s: float = 60.0
    retention_days: int = 0
    keep_dot_source: bool = True
    inline_png_max_bytes: int = 4 * 1024 * 1024
    font_family: str | None = None  # None = best installed font for the OS
    png_dpi: int = 144
    log_level: str = "INFO"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    allow_unauthenticated: bool = False
    # Host/Origin allow-lists for DNS-rebinding protection, e.g. "diagrams.internal.example.com:*".
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    # Public URL of this service (e.g. https://diagrams.example.com); used for signed download links
    # and OAuth protected-resource metadata.
    public_base_url: str | None = None

    # --- authentication (HTTP only) -----------------------------------------------------------
    auth_mode: str = "none"  # none | token | oidc
    auth_tokens: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    oidc_issuer: str | None = None
    oidc_audiences: tuple[str, ...] = ()
    oidc_jwks_uri: str | None = None
    oidc_required_scopes: tuple[str, ...] = ()
    reader_role: str | None = None
    author_role: str | None = None

    # --- artifact storage ----------------------------------------------------------------------
    storage_backend: str = "local"  # local | s3 | azure | gcs
    storage_bucket: str | None = None  # S3/GCS bucket or Azure container
    storage_prefix: str = "diagrams/"
    s3_region: str | None = None
    s3_kms_key_id: str | None = None
    azure_account_url: str | None = None
    url_ttl_s: int = 3600
    url_signing_key: str | None = field(default=None, repr=False)
    delete_local_after_upload: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        output_dir = Path(_env("OUTPUT_DIR", str(Path.home() / "architecture-diagrams"))).expanduser().resolve()
        roots_raw = _env("INPUT_ROOTS", "")
        roots = tuple(Path(p).expanduser().resolve() for p in roots_raw.split(os.pathsep) if p.strip())
        tokens = _tokens()
        issuer = _env("OIDC_ISSUER", "").strip() or None
        mode = _env("AUTH_MODE", "").strip().lower() or ("oidc" if issuer else "token" if tokens else "none")
        settings = cls(
            output_dir=output_dir,
            input_roots=roots or (Path.cwd().resolve(), output_dir),
            max_nodes=int(_env("MAX_NODES", "300")),
            max_edges=int(_env("MAX_EDGES", "800")),
            max_clusters=int(_env("MAX_CLUSTERS", "100")),
            max_terraform_bytes=int(_env("MAX_TERRAFORM_BYTES", str(25 * 1024 * 1024))),
            render_timeout_s=float(_env("RENDER_TIMEOUT_S", "60")),
            retention_days=int(_env("RETENTION_DAYS", "0")),
            keep_dot_source=_bool("KEEP_DOT_SOURCE", True),
            inline_png_max_bytes=int(_env("INLINE_PNG_MAX_BYTES", str(4 * 1024 * 1024))),
            font_family=_env("FONT", "").strip() or None,
            png_dpi=max(72, min(300, int(_env("PNG_DPI", "144")))),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            http_host=_env("HTTP_HOST", "127.0.0.1"),
            http_port=int(_env("HTTP_PORT", "8000")),
            allow_unauthenticated=_bool("ALLOW_UNAUTHENTICATED", False),
            allowed_hosts=_csv("ALLOWED_HOSTS"),
            allowed_origins=_csv("ALLOWED_ORIGINS"),
            public_base_url=(_env("PUBLIC_BASE_URL", "").strip().rstrip("/") or None),
            auth_mode=mode,
            auth_tokens=tokens,
            oidc_issuer=issuer,
            oidc_audiences=_csv("OIDC_AUDIENCE"),
            oidc_jwks_uri=_env("OIDC_JWKS_URI", "").strip() or None,
            oidc_required_scopes=_csv("OIDC_REQUIRED_SCOPES"),
            reader_role=_env("READER_ROLE", "").strip() or None,
            author_role=_env("AUTHOR_ROLE", "").strip() or None,
            storage_backend=_env("STORAGE", "local").strip().lower(),
            storage_bucket=_env("STORAGE_BUCKET", "").strip() or None,
            storage_prefix=_env("STORAGE_PREFIX", "diagrams/").strip().strip("/") + "/",
            s3_region=_env("S3_REGION", "").strip() or None,
            s3_kms_key_id=_env("S3_KMS_KEY_ID", "").strip() or None,
            azure_account_url=_env("AZURE_ACCOUNT_URL", "").strip() or None,
            url_ttl_s=max(60, min(7 * 86400, int(_env("URL_TTL_S", "3600")))),
            url_signing_key=os.environ.get(PREFIX + "URL_SIGNING_KEY") or None,
            delete_local_after_upload=_bool("DELETE_LOCAL_AFTER_UPLOAD", False),
        )
        settings.check()
        return settings

    def check(self) -> None:
        """Fail fast on inconsistent configuration instead of at the first request."""
        if self.auth_mode not in {"none", "token", "oidc"}:
            raise ValueError(f"ARCH_DIAGRAM_AUTH_MODE must be none, token or oidc (got '{self.auth_mode}').")
        if self.auth_mode == "token" and not self.auth_tokens:
            raise ValueError("Token auth needs ARCH_DIAGRAM_AUTH_TOKENS or ARCH_DIAGRAM_AUTH_TOKEN.")
        if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_audiences):
            raise ValueError("OIDC auth needs ARCH_DIAGRAM_OIDC_ISSUER and ARCH_DIAGRAM_OIDC_AUDIENCE.")
        if self.storage_backend not in {"local", "s3", "azure", "gcs"}:
            raise ValueError(f"ARCH_DIAGRAM_STORAGE must be local, s3, azure or gcs (got '{self.storage_backend}').")
        if self.storage_backend in {"s3", "gcs", "azure"} and not self.storage_bucket:
            raise ValueError("Cloud storage needs ARCH_DIAGRAM_STORAGE_BUCKET (bucket or Azure container).")
        if self.url_signing_key is not None and len(self.url_signing_key) < 32:
            raise ValueError("ARCH_DIAGRAM_URL_SIGNING_KEY must be at least 32 characters.")
