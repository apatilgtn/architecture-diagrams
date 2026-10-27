"""Entry point: `arch-diagram-mcp [--transport stdio|http]` or `python -m arch_diagram_mcp`."""

from __future__ import annotations

import argparse
import dataclasses
import logging

from . import __version__, logs, renderer, storage
from .config import Settings
from .server import create_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="arch-diagram-mcp", description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", help="HTTP bind address (default ARCH_DIAGRAM_HTTP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP port (default ARCH_DIAGRAM_HTTP_PORT or 8000)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.host:
        settings = dataclasses.replace(settings, http_host=args.host)
    if args.port:
        settings = dataclasses.replace(settings, http_port=args.port)

    logs.configure(settings.log_level)
    log = logging.getLogger("arch_diagram_mcp")
    gv = renderer.graphviz_version()
    if gv is None:
        log.warning("graphviz_missing", extra={"hint": "install Graphviz; rendering tools will fail until it is on PATH"})
    http = args.transport == "http"
    store = storage.build_store(settings, http=http)
    log.info(
        "starting",
        extra={
            "version": __version__,
            "transport": args.transport,
            "graphviz": gv,
            "output_dir": str(settings.output_dir),
            "storage": store.name,
            "auth_mode": settings.auth_mode if http else "local",
        },
    )

    mcp = create_server(settings, store, http=http)
    if not http:
        mcp.run("stdio")
        return

    import uvicorn

    from .http_app import build_app

    uvicorn.run(build_app(mcp, settings, store), host=settings.http_host, port=settings.http_port, log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
