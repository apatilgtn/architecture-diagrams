"""Reference architectures bundled as JSON under templates/."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from .models import DiagramSpec


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in resources.files(__package__).joinpath("templates").iterdir():
        if entry.name.endswith(".json"):
            doc = json.loads(entry.read_text(encoding="utf-8"))
            DiagramSpec.model_validate(doc["spec"])  # fail fast on a broken template
            out[doc["id"]] = doc
    return dict(sorted(out.items()))


def list_templates(provider: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "id": t["id"],
            "provider": t["provider"],
            "title": t["spec"]["title"],
            "description": t["description"],
            "nodes": len(t["spec"]["nodes"]),
        }
        for t in _load().values()
        if provider is None or t["provider"] == provider
    ]


def get_template(template_id: str) -> DiagramSpec:
    doc = _load().get(template_id)
    if doc is None:
        raise KeyError(f"Unknown template '{template_id}'. Available: {', '.join(_load())}.")
    return DiagramSpec.model_validate(doc["spec"])
