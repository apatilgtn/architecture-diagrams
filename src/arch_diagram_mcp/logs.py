"""JSON logging to stderr (stdout carries the MCP stdio protocol and must stay clean)."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


@contextmanager
def audit(tool: str, **fields: object) -> Iterator[dict[str, object]]:
    """Log one line per tool call with outcome and duration; callers may add fields to the dict."""
    from . import auth  # local import: auth imports config, logs must stay importable on its own

    log = logging.getLogger("arch_diagram_mcp.audit")
    who = auth.current()
    extra: dict[str, object] = {"tool": tool, "principal": who.name, "subject": who.subject, "auth": who.method, **fields}
    started = time.perf_counter()
    try:
        yield extra
    except Exception as exc:
        extra.update(outcome="error", error=str(exc)[:300], duration_ms=int((time.perf_counter() - started) * 1000))
        log.warning("tool_call", extra=extra)
        raise
    extra.update(outcome="ok", duration_ms=int((time.perf_counter() - started) * 1000))
    log.info("tool_call", extra=extra)
