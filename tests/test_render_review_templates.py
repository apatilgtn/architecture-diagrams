import dataclasses
import re

import pytest

from arch_diagram_mcp import renderer, review, template_library
from arch_diagram_mcp.models import DiagramSpec, validate_spec


def test_render_png_svg_and_dot(small_spec, settings):
    small_spec.output_formats = ["png", "svg"]
    result = renderer.render_spec(small_spec, settings)
    assert result.files["png"].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    svg = result.files["svg"].read_text(encoding="utf-8")
    assert "data:image/png;base64," in svg
    assert not re.search(r'href="[^"]+\.png"', svg), "SVG must not reference local icon files"
    assert result.dot_source and result.dot_source.exists()


@pytest.mark.parametrize(
    "field",
    ["title", "node", "cluster", "edge"],
)
def test_user_text_cannot_inject_dot(field, small_spec):
    evil = '<x>]; pwn [image="C:/Windows/win.ini" label=""]; z [label=<y>'
    if field == "title":
        small_spec.title = evil
    elif field == "node":
        small_spec.nodes[0].label = evil
    elif field == "cluster":
        small_spec.clusters[0].label = evil
    else:
        small_spec.edges[0].label = evil + "\\"
    src = renderer.build_dot_source(small_spec)
    assert 'image="C:/Windows' not in src  # only ever appears escaped inside a string
    import json
    import subprocess

    dot = renderer.ensure_graphviz_on_path()
    graph = json.loads(subprocess.run([dot, "-Tjson0"], input=src, capture_output=True, text=True, check=True).stdout)
    assert not any(o.get("name") in {"pwn", "z"} for o in graph.get("objects", []))


def test_svg_has_font_fallbacks_and_edge_label_panels(small_spec, settings):
    small_spec.output_formats = ["svg"]
    svg = renderer.render_spec(small_spec, settings).files["svg"].read_text(encoding="utf-8")
    fonts = renderer.resolve_fonts(settings)
    assert f'font-family="{fonts.regular}, ' in svg
    assert "HTTPS" in svg


def test_png_uses_configured_dpi(small_spec, settings):
    import struct

    def width(dpi):
        png = renderer.render_spec(small_spec, dataclasses.replace(settings, png_dpi=dpi)).files["png"].read_bytes()
        return struct.unpack(">I", png[16:20])[0]

    assert width(144) > width(72) * 1.8


def test_dark_theme_renders(small_spec, settings):
    small_spec.theme = "dark"
    result = renderer.render_spec(small_spec, settings)
    assert "#0F1419" in result.dot_source.read_text(encoding="utf-8")


def test_render_timeout_cleans_up(small_spec, settings):
    fast = dataclasses.replace(settings, render_timeout_s=0.000001)
    with pytest.raises(renderer.RenderError, match="exceeded"):
        renderer.render_spec(small_spec, fast)
    assert not list(fast.output_dir.glob("*"))


def test_retention_purges_only_generated_files(settings, small_spec):
    import os
    import time

    result = renderer.render_spec(small_spec, settings)
    keep = settings.output_dir / "notes.txt"
    keep.write_text("mine")
    old = time.time() - 10 * 86400
    for p in [*result.files.values(), keep]:
        os.utime(p, (old, old))
    assert renderer.purge_old_outputs(dataclasses.replace(settings, retention_days=7)) >= 1
    assert keep.exists() and not result.files["png"].exists()


@pytest.mark.parametrize("template_id", [t["id"] for t in template_library.list_templates()])
def test_every_template_is_valid_and_renders(template_id, settings):
    spec = template_library.get_template(template_id)
    issues = validate_spec(spec, settings)
    assert not [i for i in issues if i.level == "error"], issues
    assert renderer.render_spec(spec, settings).files["png"].exists()


@pytest.mark.parametrize("template_id", [t["id"] for t in template_library.list_templates()])
def test_templates_have_no_critical_or_high_findings(template_id):
    report = review.review(template_library.get_template(template_id))
    bad = [f.rule_id for f in report.findings if f.severity in {"critical", "high"}]
    assert not bad, bad


def test_review_flags_insecure_design():
    spec = DiagramSpec.model_validate(
        {
            "title": "Insecure",
            "nodes": [
                {"id": "u", "type": "onprem.client.Users"},
                {"id": "vm", "type": "aws.compute.EC2"},
                {"id": "db", "type": "aws.database.RDS"},
                {"id": "lonely", "type": "aws.storage.S3"},
            ],
            "edges": [{"source": "u", "target": "vm"}, {"source": "u", "target": "db"}, {"source": "vm", "target": "db"}],
        }
    )
    report = review.review(spec)
    ids = {f.rule_id for f in report.findings}
    assert {"SEC-001", "SEC-002", "SEC-003", "SEC-004", "SEC-005", "REL-001", "OPS-001", "OPS-003"} <= ids
    assert report.findings[0].severity == "critical"
    assert report.score < 40 and report.grade in {"D", "F"}
