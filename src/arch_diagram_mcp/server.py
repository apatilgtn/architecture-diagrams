"""MCP tool, prompt and resource definitions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, auth, catalog, gcp_inventory, renderer, review, storage, template_library, terraform
from .config import Settings
from .logs import audit
from .models import DiagramSpec, validate_spec

Provider = Literal["aws", "azure", "gcp", "k8s", "onprem", "saas", "programming", "generic"]

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITES_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

INSTRUCTIONS = """\
Generate and review cloud architecture diagrams with official AWS, Azure and GCP icons
(plus Kubernetes, on-prem and SaaS services such as PostgreSQL, Redis, Kafka, Nginx).

Workflow:
1. Start from get_template when a reference architecture is close; otherwise build a spec.
2. Use search_icons to find exact icon ids (e.g. 'aws.compute.ECS', 'azure.compute.AKS', 'gcp.database.SQL').
3. Call validate_diagram, fix every error, then generate_architecture_diagram.
4. Call review_architecture and either fix findings or explain why they are accepted.
For existing infrastructure, use diagram_from_terraform with `terraform show -json` output, or, for
resources already running in Google Cloud (whatever created them), diagram_from_inventory with
`gcloud asset list --content-type resource --format json` output. Both keep real resource names.

Layout conventions: nest clusters the way providers draw boundaries
(Cloud/Account > Region > VPC/VNet > Subnet/AZ; Subscription > Resource Group; Org > Project > VPC).
Keep edges node-to-node; use labels for protocols and dashed/dotted styles for async or control-plane flows.
"""


def create_server(settings: Settings, store: storage.ArtifactStore | None = None, *, http: bool = False) -> MCPServer:
    mcp = MCPServer(name="architecture-diagrams", version=__version__, instructions=INSTRUCTIONS)
    store = store or storage.build_store(settings, http=http)

    def allow(permission: auth.Permission) -> None:
        try:
            auth.authorize(auth.current(), permission, settings)
        except auth.Forbidden as exc:
            raise ToolError(f"Forbidden: {exc}") from exc

    def check(spec: DiagramSpec) -> list[dict[str, str]]:
        issues = validate_spec(spec, settings)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ToolError("Diagram spec is invalid:\n" + "\n".join(f"- [{i.code}] {i.message}" for i in errors))
        return [i.model_dump() for i in issues]

    def render(spec: DiagramSpec, extra: dict[str, Any]) -> list[Any]:
        warnings = check(spec)
        try:
            result = renderer.render_spec(spec, settings)
        except renderer.RenderError as exc:
            raise ToolError(str(exc)) from exc
        to_publish = [*result.files.values(), *([result.dot_source] if result.dot_source else [])]
        try:
            published = store.publish(to_publish, auth.current())
        except storage.StorageError as exc:
            raise ToolError(str(exc)) from exc
        png = result.files.get("png")
        inline_png = png.read_bytes() if png and png.stat().st_size <= settings.inline_png_max_bytes else None
        if published.backend != "local" and settings.delete_local_after_upload:
            for p in to_publish:
                p.unlink(missing_ok=True)
        summary = {
            "urls": published.urls,
            "urls_expire_at": published.expires_at.isoformat() if published.expires_at else None,
            "storage": published.backend,
            # Local paths only help (and are only safe to reveal) when client and server share a machine.
            **({} if http else {"files": {fmt: str(p) for fmt, p in result.files.items()}}),
            "nodes": len(spec.nodes),
            "edges": len(spec.edges),
            "clusters": len(spec.clusters),
            "duration_ms": result.duration_ms,
            "warnings": warnings,
            **extra,
        }
        content: list[Any] = [json.dumps(summary, indent=2)]
        if inline_png:
            content.append(Image(data=inline_png, format="png"))
        return content

    @mcp.tool(annotations=READ_ONLY)
    def search_icons(query: str, provider: Provider | None = None, limit: int = 25) -> list[dict[str, object]]:
        """Search ~1,900 official icons by service name, alias or category.

        Examples: 'ecs', 's3', 'aks', 'cloud run', 'postgres', 'redis', 'kafka', 'load balancer'.
        Returns canonical ids (usable as node `type`) and their aliases.
        """
        allow(auth.Permission.READ)
        with audit("search_icons", query=query, provider=provider):
            return catalog.search(query, provider, limit)

    @mcp.tool(annotations=READ_ONLY)
    def list_icon_categories(provider: Provider) -> dict[str, list[str]]:
        """List every category of a provider and the service icons in each."""
        allow(auth.Permission.READ)
        with audit("list_icon_categories", provider=provider):
            return catalog.categories(provider)

    @mcp.tool(annotations=READ_ONLY)
    def list_templates(provider: Provider | None = None) -> list[dict[str, object]]:
        """List bundled reference architectures (AWS, Azure, GCP) that can be loaded with get_template."""
        allow(auth.Permission.READ)
        with audit("list_templates", provider=provider):
            return template_library.list_templates(provider)

    @mcp.tool(annotations=READ_ONLY)
    def get_template(template_id: str) -> dict[str, Any]:
        """Return a reference architecture spec to customise and pass to generate_architecture_diagram."""
        allow(auth.Permission.READ)
        with audit("get_template", template_id=template_id):
            try:
                return template_library.get_template(template_id).model_dump()
            except KeyError as exc:
                raise ToolError(str(exc.args[0])) from exc

    @mcp.tool(annotations=READ_ONLY)
    def validate_diagram(spec: DiagramSpec) -> dict[str, Any]:
        """Check a spec without rendering: icon ids (with suggestions), references, nesting, limits."""
        allow(auth.Permission.READ)
        with audit("validate_diagram", nodes=len(spec.nodes)):
            issues = validate_spec(spec, settings)
            return {
                "valid": not any(i.level == "error" for i in issues),
                "issues": [i.model_dump() for i in issues],
            }

    @mcp.tool(annotations=WRITES_FILES, structured_output=False)
    def generate_architecture_diagram(spec: DiagramSpec) -> list[Any]:
        """Render an architecture diagram with official provider icons.

        spec.nodes: services (`type` = icon id from search_icons).
        spec.clusters: boundaries (account, subscription, project, region, VPC/VNet, subnet, AZ,
          namespace), nested with `parent`.
        spec.edges: node-to-node connections with optional label, colour and style.
        spec.output_formats: any of png, svg (self-contained), pdf, jpg.
        Returns file paths and an inline PNG preview.
        """
        allow(auth.Permission.WRITE)
        with audit("generate_architecture_diagram", nodes=len(spec.nodes), edges=len(spec.edges)):
            return render(spec, {})

    @mcp.tool(annotations=READ_ONLY)
    def review_architecture(spec: DiagramSpec) -> dict[str, Any]:
        """Heuristic Well-Architected review of a spec (security, reliability, operations, performance).

        Returns a 0-100 score, grade, and findings with recommendations. Findings reflect only what
        the diagram shows.
        """
        allow(auth.Permission.READ)
        with audit("review_architecture", nodes=len(spec.nodes)) as extra:
            check(spec)
            report = review.review(spec)
            extra.update(score=report.score, findings=len(report.findings))
            return report.model_dump()

    @mcp.tool(annotations=WRITES_FILES, structured_output=False)
    def diagram_from_terraform(
        title: str,
        terraform_json: str | dict[str, Any] | None = None,
        path: str | None = None,
        render_diagram: bool = True,
        direction: Literal["LR", "TB", "RL", "BT"] = "LR",
        output_formats: list[Literal["png", "svg", "pdf", "jpg"]] | None = None,
    ) -> list[Any]:
        """Build a diagram from Terraform: `terraform show -json plan.out`, state JSON, or a .tfstate file.

        Pass the JSON text in `terraform_json`, or a file `path` inside the configured input roots.
        VPC/VNet/subnet/resource-group become nested boundaries; references become edges (dashed =
        inferred through listeners, NICs, role assignments, etc.). Returns the generated spec so it
        can be refined and re-rendered.
        """
        allow(auth.Permission.WRITE)
        if path is not None and http:
            raise ToolError("`path` is disabled over HTTP; send the JSON in `terraform_json`.")
        with audit("diagram_from_terraform", from_path=bool(path)) as extra:
            doc = _load_terraform(terraform_json, path, settings)
            try:
                imported = terraform.to_spec(doc, title, direction)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            spec = imported.spec
            if output_formats:
                spec.output_formats = output_formats
            stats = {
                "mapped_resources": imported.mapped,
                "boundaries": imported.containers,
                "inferred_edges": imported.inferred_edges,
                "unmapped_resource_types": imported.skipped_types,
            }
            extra.update(stats)
            if not render_diagram:
                return [json.dumps({**stats, "spec": spec.model_dump()}, indent=2)]
            return render(spec, {**stats, "spec": spec.model_dump()})

    @mcp.tool(annotations=WRITES_FILES, structured_output=False)
    def diagram_from_inventory(
        title: str,
        inventory_json: str | list[Any] | dict[str, Any] | None = None,
        path: str | None = None,
        provider: Literal["gcp"] = "gcp",
        projects: list[str] | None = None,
        regions: list[str] | None = None,
        labels: dict[str, str] | None = None,
        include_neighbors: bool = True,
        include_kubernetes: bool = True,
        render_diagram: bool = True,
        direction: Literal["LR", "TB", "RL", "BT"] = "LR",
        output_formats: list[Literal["png", "svg", "pdf", "jpg"]] | None = None,
    ) -> list[Any]:
        """Diagram infrastructure that is already running, from the cloud's own inventory, with real names.

        Google Cloud: pass the output of
        `gcloud asset list --project <id> --content-type resource --format json`
        (or `gcloud asset export` newline-delimited JSON) in `inventory_json`, or a file `path` inside
        the configured input roots. Works however the resources were created (console, gcloud, IaC).

        Layout: Project > VPC > Subnet > GKE cluster > namespace. Edges come from configuration
        (Cloud SQL/secret/Pub/Sub references, URLs matched to Cloud Run services, load balancer
        backends, push subscriptions, Scheduler targets, VPC connectors). Filter with `projects`,
        `regions` and `labels` (e.g. {"app": "payments"}); `include_neighbors` keeps resources that
        matched resources connect to. Calls made only in application code are not visible: add those
        edges to the returned spec and re-render with generate_architecture_diagram.
        """
        allow(auth.Permission.WRITE)
        if path is not None and http:
            raise ToolError("`path` is disabled over HTTP; send the export in `inventory_json`.")
        with audit("diagram_from_inventory", provider=provider, from_path=bool(path)) as extra:
            doc = _load_inventory(inventory_json, path, settings)
            try:
                imported = gcp_inventory.to_spec(
                    doc, title, direction, projects=projects, regions=regions, labels=labels,
                    include_neighbors=include_neighbors, include_kubernetes=include_kubernetes,
                )
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if imported.mapped > settings.max_nodes:
                raise ToolError(
                    f"The inventory maps to {imported.mapped} resources, above the {settings.max_nodes}-node limit. "
                    "Narrow it with `projects`, `regions` or `labels`, or export one project at a time."
                )
            spec = imported.spec
            if output_formats:
                spec.output_formats = output_formats
            stats = {
                "mapped_resources": imported.mapped,
                "boundaries": imported.boundaries,
                "edges": imported.edges,
                "filtered_out": imported.filtered_out,
                "unmapped_asset_types": imported.skipped_types,
                "external_hosts": imported.external_hosts,
            }
            extra.update({k: v for k, v in stats.items() if isinstance(v, int)})
            if not render_diagram:
                return [json.dumps({**stats, "spec": spec.model_dump()}, indent=2)]
            return render(spec, {**stats, "spec": spec.model_dump()})

    @mcp.tool(annotations=READ_ONLY)
    def server_info() -> dict[str, Any]:
        """Report version, Graphviz status, icon count, storage, auth mode, caller identity and limits."""
        allow(auth.Permission.READ)
        me = auth.current()
        return {
            "version": __version__,
            "graphviz": renderer.graphviz_version(),
            "icons": len(catalog.all_icons()),
            "templates": [t["id"] for t in template_library.list_templates()],
            "storage": store.name,
            "auth_mode": settings.auth_mode if http else "local (stdio)",
            "caller": {"name": me.name, "method": me.method, "roles": sorted(me.roles)},
            **({} if http else {"output_dir": str(settings.output_dir), "input_roots": [str(p) for p in settings.input_roots]}),
            "limits": {
                "max_nodes": settings.max_nodes,
                "max_edges": settings.max_edges,
                "max_clusters": settings.max_clusters,
                "render_timeout_s": settings.render_timeout_s,
                "max_terraform_bytes": settings.max_terraform_bytes,
            },
            "retention_days": settings.retention_days,
        }

    @mcp.prompt()
    def design_architecture(provider: str, workload: str, requirements: str = "") -> str:
        """Guide the model through designing, rendering and reviewing an architecture."""
        return (
            f"Design a production-grade {provider.upper()} architecture for: {workload}.\n"
            f"Requirements: {requirements or 'high availability, security best practices, observability'}.\n\n"
            "Steps:\n"
            "1. Call list_templates and start from the closest template with get_template, if any.\n"
            "2. Use search_icons for every service you add; use only returned ids.\n"
            "3. Draw provider boundaries as nested clusters (account/subscription/project > region > network > subnet).\n"
            "4. Call validate_diagram and fix all errors, then generate_architecture_diagram (png + svg).\n"
            "5. Call review_architecture; fix critical/high/medium findings and re-render, "
            "then summarise remaining trade-offs."
        )

    return mcp


def _read_input(text: Any, path: str | None, settings: Settings, *, arg: str, what: str, suffixes: set[str]) -> str:
    """Return JSON text from an inline argument or a file inside the input roots, enforcing the size limit."""
    if (text is None) == (path is None):
        raise ToolError(f"Provide exactly one of `{arg}` or `path`.")
    if isinstance(text, (dict, list)):  # clients may send the JSON value itself
        text = json.dumps(text)
    if path is not None:
        p = Path(path).expanduser().resolve()
        if not any(p.is_relative_to(root) for root in settings.input_roots):
            roots = ", ".join(str(r) for r in settings.input_roots)
            raise ToolError(f"Path is outside the allowed input roots ({roots}). Set ARCH_DIAGRAM_INPUT_ROOTS to allow it.")
        if p.suffix.lower() not in suffixes or not p.is_file():
            raise ToolError(f"'{p}' is not a {' or '.join(sorted(suffixes))} file.")
        if p.stat().st_size > settings.max_terraform_bytes:
            raise ToolError(f"'{p}' exceeds the {settings.max_terraform_bytes // (1024 * 1024)} MB limit.")
        text = p.read_text(encoding="utf-8-sig")
    assert isinstance(text, str)
    if len(text.encode()) > settings.max_terraform_bytes:
        raise ToolError(f"{what} exceeds the {settings.max_terraform_bytes // (1024 * 1024)} MB limit.")
    return text


def _load_inventory(text: Any, path: str | None, settings: Settings) -> Any:
    raw = _read_input(text, path, settings, arg="inventory_json", what="Inventory JSON",
                      suffixes={".json", ".ndjson", ".jsonl"})
    try:
        return gcp_inventory.parse_text(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON: {exc}") from exc


def _load_terraform(text: str | dict[str, Any] | None, path: str | None, settings: Settings) -> dict[str, Any]:
    text = _read_input(text, path, settings, arg="terraform_json", what="Terraform JSON", suffixes={".json", ".tfstate"})
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ToolError("Terraform JSON must be an object.")
    return doc
