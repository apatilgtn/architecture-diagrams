"""Index of every icon shipped with the `diagrams` package, addressable as 'provider.category.Name'."""

from __future__ import annotations

import difflib
import importlib
import inspect
import pkgutil
import re
from dataclasses import dataclass
from functools import lru_cache

from diagrams import Node

PROVIDERS = ("aws", "azure", "gcp", "k8s", "onprem", "saas", "programming", "generic")


class UnknownIconError(ValueError):
    def __init__(self, node_type: str, suggestions: list[str]):
        self.node_type = node_type
        self.suggestions = suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else " Use search_icons to find valid ids."
        super().__init__(f"Unknown icon type '{node_type}'.{hint}")


@dataclass(frozen=True)
class Icon:
    id: str
    provider: str
    category: str
    name: str
    aliases: tuple[str, ...]
    cls: type[Node]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider": self.provider,
            "category": self.category,
            "name": self.name,
            "aliases": [f"{self.provider}.{self.category}.{a}" for a in self.aliases],
        }


@lru_cache(maxsize=1)
def _build() -> tuple[tuple[Icon, ...], dict[str, Icon]]:
    icons: list[Icon] = []
    index: dict[str, Icon] = {}
    for provider in PROVIDERS:
        pkg = importlib.import_module(f"diagrams.{provider}")
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            module = importlib.import_module(f"diagrams.{provider}.{mod_info.name}")
            names_by_cls: dict[type[Node], list[str]] = {}
            for name, obj in vars(module).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, Node)
                    and obj.__module__ == module.__name__
                    and not name.startswith("_")
                    and getattr(obj, "_icon", None)
                ):
                    names_by_cls.setdefault(obj, []).append(name)
            for cls, names in names_by_cls.items():
                aliases = tuple(sorted(n for n in names if n != cls.__name__))
                icon = Icon(f"{provider}.{mod_info.name}.{cls.__name__}", provider, mod_info.name, cls.__name__, aliases, cls)
                icons.append(icon)
                for n in names:
                    index[f"{provider}.{mod_info.name}.{n}".lower()] = icon
    return tuple(icons), index


def all_icons() -> tuple[Icon, ...]:
    return _build()[0]


def resolve(node_type: str) -> Icon:
    """Return the icon for an id or alias (case-insensitive), or raise UnknownIconError."""
    icon = _build()[1].get(node_type.strip().lower())
    if icon:
        return icon
    raise UnknownIconError(node_type, suggest(node_type))


def suggest(node_type: str) -> list[str]:
    index = _build()[1]
    close = difflib.get_close_matches(node_type.lower(), index.keys(), n=5, cutoff=0.55)
    suggestions = list(dict.fromkeys(index[c].id for c in close))
    if not suggestions:
        # Fall back to a name search, e.g. 'aws.compute.Kubernetes' -> EKS candidates.
        tail = node_type.rsplit(".", 1)[-1]
        provider = node_type.split(".", 1)[0] if "." in node_type else None
        suggestions = [str(r["id"]) for r in search(tail, provider if provider in PROVIDERS else None, 5)]
    return suggestions


def search(query: str, provider: str | None = None, limit: int = 30) -> list[dict[str, object]]:
    """Rank icons whose id or aliases contain every query term."""
    terms = [t for t in re.split(r"[\s._/-]+", query.lower()) if t]
    if not terms:
        return []
    scored: list[tuple[int, Icon]] = []
    for icon in all_icons():
        if provider and icon.provider != provider.lower():
            continue
        names = [icon.name.lower(), *(a.lower() for a in icon.aliases)]
        hay = " ".join([icon.id.lower(), *names])
        if not all(t in hay for t in terms):
            continue
        score = 0
        for t in terms:
            if t in names:
                score += 60  # exact service name or alias, e.g. 'ecs', 's3'
            elif any(n.startswith(t) for n in names):
                score += 20
            elif any(t in n for n in names):
                score += 10
            elif t in (icon.provider, icon.category):
                score += 5
        score -= len(icon.name) // 4
        scored.append((score, icon))
    scored.sort(key=lambda s: (-s[0], s[1].id))
    return [icon.as_dict() for _, icon in scored[: max(1, min(limit, 200))]]


def categories(provider: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for icon in all_icons():
        if icon.provider == provider:
            out.setdefault(icon.category, []).append(icon.name)
    return {c: sorted(v) for c, v in sorted(out.items())}
