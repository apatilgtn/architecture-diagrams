"""Diagram spec schema and semantic validation (references, nesting, limits, icon ids)."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import catalog
from .config import Settings

def _id() -> Any:
    return Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$", description="Unique id: letters, digits, _ . : - (max 64).")


def _color() -> Any:
    return Field("", pattern=r"^(#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?|[A-Za-z]{0,30})$", description="Hex '#RRGGBB' or a Graphviz colour name.")


class NodeSpec(BaseModel):
    id: str = _id()
    type: str = Field(max_length=120, description="Icon id from search_icons, e.g. 'aws.compute.EC2'.")
    label: str = Field("", max_length=200, description="Text under the icon; defaults to the id. Use \\n for line breaks.")
    cluster: str | None = Field(None, description="Id of the cluster (boundary) this node sits in.")


class ClusterSpec(BaseModel):
    id: str = _id()
    label: str = Field(max_length=200, description="Boundary title, e.g. 'VPC 10.0.0.0/16' or 'Resource Group: rg-prod'.")
    parent: str | None = Field(None, description="Id of the enclosing cluster, for nesting.")
    bgcolor: str = _color()
    style: Literal["", "rounded", "dashed", "dotted", "bold", "solid"] = ""


class EdgeSpec(BaseModel):
    source: str = Field(description="Node id (clusters are not valid endpoints).")
    target: str = Field(description="Node id.")
    label: str = Field("", max_length=120)
    color: str = _color()
    style: Literal["", "solid", "dashed", "dotted", "bold"] = ""
    bidirectional: bool = False


class DiagramSpec(BaseModel):
    title: str = Field(max_length=200)
    nodes: list[NodeSpec] = Field(min_length=1)
    edges: list[EdgeSpec] = Field(default_factory=list)
    clusters: list[ClusterSpec] = Field(default_factory=list)
    direction: Literal["LR", "TB", "RL", "BT"] = "LR"
    curve_style: Literal["spline", "ortho", "polyline", "curved"] = Field(
        "spline", description="spline keeps edge labels attached; ortho gives right angles but floats labels."
    )
    theme: Literal["light", "dark"] = "light"
    output_formats: list[Literal["png", "svg", "pdf", "jpg"]] = Field(default_factory=lambda: ["png"], min_length=1, max_length=4)
    filename: str | None = Field(None, max_length=80, description="Base file name; sanitised and timestamped.")


class Issue(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str


def validate_spec(spec: DiagramSpec, settings: Settings) -> list[Issue]:
    """Return every problem found; any 'error' blocks rendering."""
    issues: list[Issue] = []

    def err(code: str, msg: str) -> None:
        issues.append(Issue(level="error", code=code, message=msg))

    def warn(code: str, msg: str) -> None:
        issues.append(Issue(level="warning", code=code, message=msg))

    if len(spec.nodes) > settings.max_nodes:
        err("LIMIT_NODES", f"{len(spec.nodes)} nodes exceeds the limit of {settings.max_nodes}.")
    if len(spec.edges) > settings.max_edges:
        err("LIMIT_EDGES", f"{len(spec.edges)} edges exceeds the limit of {settings.max_edges}.")
    if len(spec.clusters) > settings.max_clusters:
        err("LIMIT_CLUSTERS", f"{len(spec.clusters)} clusters exceeds the limit of {settings.max_clusters}.")

    node_ids = Counter(n.id for n in spec.nodes)
    cluster_ids = Counter(c.id for c in spec.clusters)
    for dup in [i for i, n in node_ids.items() if n > 1]:
        err("DUPLICATE_NODE", f"Node id '{dup}' is used more than once.")
    for dup in [i for i, n in cluster_ids.items() if n > 1]:
        err("DUPLICATE_CLUSTER", f"Cluster id '{dup}' is used more than once.")
    for both in set(node_ids) & set(cluster_ids):
        err("ID_COLLISION", f"'{both}' is used as both a node id and a cluster id.")

    for c in spec.clusters:
        if c.parent is not None and c.parent not in cluster_ids:
            err("UNKNOWN_PARENT", f"Cluster '{c.id}' has unknown parent '{c.parent}'.")
    parents = {c.id: c.parent for c in spec.clusters}
    for start in parents:
        seen: set[str] = set()
        cur: str | None = start
        while cur is not None and cur in parents:
            if cur in seen:
                err("CLUSTER_CYCLE", f"Cluster nesting cycle involving '{start}'.")
                break
            seen.add(cur)
            cur = parents[cur]

    for n in spec.nodes:
        if n.cluster is not None and n.cluster not in cluster_ids:
            err("UNKNOWN_CLUSTER", f"Node '{n.id}' references unknown cluster '{n.cluster}'.")
        try:
            catalog.resolve(n.type)
        except catalog.UnknownIconError as exc:
            err("UNKNOWN_ICON", f"Node '{n.id}': {exc}")

    connected: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    for e in spec.edges:
        for end in (e.source, e.target):
            if end not in node_ids:
                hint = " (that is a cluster id; edges must join nodes)" if end in cluster_ids else ""
                err("UNKNOWN_EDGE_END", f"Edge {e.source} -> {e.target}: '{end}' is not a node id{hint}.")
        if e.source == e.target:
            warn("SELF_LOOP", f"Edge on '{e.source}' points to itself.")
        if (e.source, e.target) in seen_edges:
            warn("DUPLICATE_EDGE", f"Edge {e.source} -> {e.target} appears more than once.")
        seen_edges.add((e.source, e.target))
        connected.update((e.source, e.target))

    if spec.edges:
        for orphan in sorted(set(node_ids) - connected):
            warn("ORPHAN_NODE", f"Node '{orphan}' has no connections.")
    occupied = {n.cluster for n in spec.nodes} | {c.parent for c in spec.clusters}
    for c in spec.clusters:
        if c.id not in occupied:
            warn("EMPTY_CLUSTER", f"Cluster '{c.id}' contains no nodes or clusters and will not be drawn.")
    return issues
