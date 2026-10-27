"""Turn a validated DiagramSpec into image files.

The `diagrams` package builds the Graphviz source (with official icons); Graphviz itself runs as a
subprocess so it can be killed on timeout, which `diagrams`' own render() cannot do.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from diagrams import Cluster, Diagram, Edge, Node, setdiagram
from graphviz import nohtml

from . import catalog
from .config import Settings
from .models import ClusterSpec, DiagramSpec, NodeSpec

log = logging.getLogger(__name__)

THEMES: dict[str, dict[str, dict[str, str] | tuple[str, ...]]] = {
    "light": {
        "graph": {"bgcolor": "white", "fontcolor": "#16191F"},
        "node": {"fontcolor": "#16191F"},
        "edge": {"color": "#5F6B7A", "fontcolor": "#2E3A48"},
        "cluster": {"pencolor": "#9AA5B1", "fontcolor": "#16191F"},
        "cluster_fills": ("#E5F5FD", "#EBF3E7", "#ECE8F6", "#FDF7E3"),
    },
    "dark": {
        "graph": {"bgcolor": "#0F1419", "fontcolor": "#F2F4F7"},
        "node": {"fontcolor": "#F2F4F7"},
        "edge": {"color": "#9AA5B1", "fontcolor": "#E1E6EC"},
        "cluster": {"pencolor": "#4A5563", "fontcolor": "#F2F4F7"},
        "cluster_fills": ("#18202A", "#1F2833", "#26303C", "#2C3744"),
    },
}

# Point sizes. Labels sit under 1.4in icons, so 14pt stays legible when the diagram is scaled down.
FONT_SIZES = {"title": "26", "cluster": "15", "node": "14", "edge": "12"}
SVG_FONT_FALLBACK = "'Segoe UI', 'Helvetica Neue', Arial, 'DejaVu Sans', sans-serif"


@dataclass(frozen=True)
class Fonts:
    regular: str
    bold: str


def resolve_fonts(settings: Settings) -> Fonts:
    """Pick a font that exists on this OS; Pango silently substitutes a monospace font otherwise."""
    if settings.font_family:
        return Fonts(settings.font_family, f"{settings.font_family} Bold")
    if os.name == "nt":
        return Fonts("Segoe UI", "Segoe UI Semibold")
    if sys.platform == "darwin":
        return Fonts("Helvetica Neue", "Helvetica Neue Bold")
    return Fonts("DejaVu Sans", "DejaVu Sans Bold")


@lru_cache(maxsize=1)
def _graphviz_env() -> dict[str, str] | None:
    """On Windows, switch Pango from its Win32 backend (no anti-aliasing) to FreeType via fontconfig."""
    if os.name != "nt" or "FONTCONFIG_FILE" in os.environ:
        return None
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "arch-diagram-mcp" / "fontconfig"
    base.mkdir(parents=True, exist_ok=True)
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    dir_xml = "\n".join(f"  <dir>{html.escape(d.as_posix())}</dir>" for d in dirs if d.is_dir())
    conf = f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
{dir_xml}
  <cachedir>{html.escape((base / "cache").as_posix())}</cachedir>
  <match target="font">
    <edit name="antialias" mode="assign"><bool>true</bool></edit>
    <edit name="hinting" mode="assign"><bool>true</bool></edit>
    <edit name="hintstyle" mode="assign"><const>hintslight</const></edit>
    <edit name="rgba" mode="assign"><const>none</const></edit>
  </match>
</fontconfig>
"""
    path = base / "fonts.conf"
    if not path.exists() or path.read_text(encoding="utf-8") != conf:
        path.write_text(conf, encoding="utf-8")
    return {**os.environ, "FONTCONFIG_FILE": str(path), "PANGOCAIRO_BACKEND": "fc"}


def _plain(text: str) -> str:
    """User text as a literal DOT string: never an HTML label, no backslash escape sequences."""
    return nohtml(text.replace("\\", "/"))


def _edge_label(text: str, bgcolor: str) -> str:
    """HTML label on a background panel so edge lines don't run through the text."""
    body = "<br/>".join(html.escape(line.replace("\\", "/")) for line in text.split("\n"))
    return f'<<table border="0" cellborder="0" cellpadding="3" bgcolor="{bgcolor}"><tr><td>{body}</td></tr></table>>'

_GENERATED = re.compile(r"_\d{8}_\d{6}_[0-9a-f]{6}\.(png|svg|pdf|jpg|dot)$")


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    files: dict[str, Path]
    dot_source: Path | None
    duration_ms: int
    warnings: list[str] = field(default_factory=list)


def ensure_graphviz_on_path() -> str | None:
    """Return the `dot` executable, adding the default Windows install dir to PATH if needed."""
    if shutil.which("dot") is None:
        for candidate in (r"C:\Program Files\Graphviz\bin", r"C:\Program Files (x86)\Graphviz\bin"):
            if Path(candidate, "dot.exe").exists():
                os.environ["PATH"] = candidate + os.pathsep + os.environ.get("PATH", "")
                break
    return shutil.which("dot")


@lru_cache(maxsize=1)
def graphviz_version() -> str | None:
    dot = ensure_graphviz_on_path()
    if not dot:
        return None
    out = subprocess.run([dot, "-V"], capture_output=True, text=True, timeout=15)
    return (out.stderr or out.stdout).strip()


class _SourceOnlyDiagram(Diagram):
    """Collects the Graphviz source without letting `diagrams` invoke Graphviz or touch the disk."""

    def render(self) -> None:  # noqa: D401 - rendering happens in render_spec
        pass

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        setdiagram(None)


def _stem(spec: DiagramSpec) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", spec.filename or spec.title).strip("_").lower()[:60] or "diagram"
    return f"{base}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


def build_dot_source(spec: DiagramSpec, fonts: Fonts = Fonts("DejaVu Sans", "DejaVu Sans Bold")) -> str:
    theme = THEMES[spec.theme]
    bg = theme["graph"]["bgcolor"]  # type: ignore[index]
    edge_font = {"fontname": fonts.regular, "fontsize": FONT_SIZES["edge"], **theme["edge"]}  # type: ignore[dict-item]
    fills: tuple[str, ...] = theme["cluster_fills"]  # type: ignore[assignment]
    node_classes: dict[str, type[Node]] = {n.id: catalog.resolve(n.type).cls for n in spec.nodes}

    children: dict[str | None, list[ClusterSpec]] = {}
    for c in spec.clusters:
        children.setdefault(c.parent, []).append(c)
    members: dict[str | None, list[NodeSpec]] = {}
    for n in spec.nodes:
        members.setdefault(n.cluster, []).append(n)

    rendered: dict[str, Node] = {}

    def place(cluster_id: str | None, depth: int) -> None:
        for n in members.get(cluster_id, []):
            label = n.label or n.id
            # The icon is centred in the node box and the label hangs off the bottom, so every extra
            # line at 14pt needs ~0.3in below and above the icon to keep text clear of it.
            height = f"{1.9 + 0.6 * label.count(chr(10)):.2f}"
            rendered[n.id] = node_classes[n.id](_plain(label), height=height)
        for child in children.get(cluster_id, []):
            attrs = {
                "fontsize": FONT_SIZES["cluster"],
                "fontname": fonts.bold,
                "bgcolor": fills[depth % len(fills)],
                "margin": "14",
            }
            attrs.update(theme["cluster"])  # type: ignore[arg-type]
            if child.bgcolor and spec.theme == "light":
                attrs["bgcolor"] = child.bgcolor
            if child.style:
                attrs["style"] = child.style
            with Cluster(_plain(child.label), graph_attr=attrs):
                place(child.id, depth + 1)

    graph_attr = {
        "fontsize": FONT_SIZES["title"],
        "fontname": fonts.bold,
        "labelloc": "t",
        "pad": "0.6",
        "nodesep": "0.8",
        "ranksep": "1.0",
        # `diagrams` only validates ortho/curved, but graph_attr is applied afterwards.
        "splines": spec.curve_style,
        **theme["graph"],  # type: ignore[dict-item]
    }
    with _SourceOnlyDiagram(
        _plain(spec.title),
        filename="unused",
        direction=spec.direction,
        show=False,
        graph_attr=graph_attr,
        node_attr={"fontsize": FONT_SIZES["node"], "fontname": fonts.regular, **theme["node"]},  # type: ignore[dict-item]
        edge_attr=dict(theme["edge"]),  # type: ignore[arg-type]
    ) as diagram:
        place(None, 0)
        for e in spec.edges:
            # `diagrams` stamps its own font on every Edge, so the font must be set per edge.
            attrs = {"style": e.style, "forward": True, "reverse": e.bidirectional, **edge_font}
            if e.label:
                attrs["label"] = _edge_label(e.label, bg)
            if e.color:
                attrs["color"] = e.color
            rendered[e.source] >> Edge(**attrs) >> rendered[e.target]
        return diagram.dot.source


def _finalize_svg(svg_path: Path, fonts: Fonts) -> None:
    """Embed icon PNGs as data URIs and add font fallbacks so the SVG renders the same anywhere."""
    text = svg_path.read_text(encoding="utf-8")
    for family in {fonts.regular, fonts.bold}:
        text = text.replace(f'font-family="{family}"', f"font-family=\"{family}, {SVG_FONT_FALLBACK}\"")
    cache: dict[str, str] = {}

    def repl(m: re.Match[str]) -> str:
        src = m.group(2)
        if src not in cache:
            p = Path(src)
            cache[src] = (
                "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode() if p.is_file() else src
            )
        return f'{m.group(1)}="{cache[src]}"'

    svg_path.write_text(re.sub(r'((?:xlink:)?href)="([^"]+\.png)"', repl, text), encoding="utf-8")


def purge_old_outputs(settings: Settings) -> int:
    if settings.retention_days <= 0 or not settings.output_dir.exists():
        return 0
    cutoff = time.time() - timedelta(days=settings.retention_days).total_seconds()
    removed = 0
    for p in settings.output_dir.iterdir():
        if p.is_file() and _GENERATED.search(p.name) and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def render_spec(spec: DiagramSpec, settings: Settings) -> RenderResult:
    dot = ensure_graphviz_on_path()
    if not dot:
        raise RenderError("Graphviz 'dot' was not found. Install it (winget install Graphviz.Graphviz / apt install graphviz).")

    started = time.perf_counter()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    purge_old_outputs(settings)
    stem = _stem(spec)
    fonts = resolve_fonts(settings)
    env = _graphviz_env()
    dot_path = settings.output_dir / f"{stem}.dot"
    dot_path.write_text(build_dot_source(spec, fonts), encoding="utf-8")

    files: dict[str, Path] = {}
    deadline = started + settings.render_timeout_s
    try:
        for fmt in dict.fromkeys(spec.output_formats):
            out = settings.output_dir / f"{stem}.{fmt}"
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise RenderError(f"Rendering exceeded {settings.render_timeout_s:.0f}s; simplify the diagram.")
            # Raster output: render through Cairo at a higher DPI for sharp text and icons.
            cmd = [dot, "-Tpng:cairo" if fmt == "png" else f"-T{fmt}"]
            if fmt in {"png", "jpg"}:
                cmd.append(f"-Gdpi={settings.png_dpi}")
            try:
                proc = subprocess.run(
                    [*cmd, "-o", str(out), str(dot_path)],
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise RenderError(f"Rendering exceeded {settings.render_timeout_s:.0f}s; simplify the diagram.") from exc
            if proc.returncode != 0 or not out.exists():
                raise RenderError(f"Graphviz failed ({fmt}): {proc.stderr.strip()[:500]}")
            if fmt == "svg":
                _finalize_svg(out, fonts)
            files[fmt] = out
    except Exception:
        for p in files.values():
            p.unlink(missing_ok=True)
        dot_path.unlink(missing_ok=True)
        raise

    if not settings.keep_dot_source:
        dot_path.unlink(missing_ok=True)
    duration_ms = int((time.perf_counter() - started) * 1000)
    log.info("rendered", extra={"stem": stem, "formats": list(files), "duration_ms": duration_ms, "nodes": len(spec.nodes)})
    return RenderResult(files=files, dot_source=dot_path if settings.keep_dot_source else None, duration_ms=duration_ms)
