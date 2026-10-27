"""Render the README sample gallery into docs/samples/: every bundled template plus the Terraform example.

usage: python scripts/render_samples.py
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
from pathlib import Path

from arch_diagram_mcp import renderer, review, template_library, terraform
from arch_diagram_mcp.config import Settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "samples"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # 110 dpi keeps the committed PNGs small while staying sharp at the width GitHub displays them.
        settings = dataclasses.replace(
            Settings.from_env(), output_dir=Path(tmp), keep_dot_source=False, retention_days=0, png_dpi=110
        )
        specs = {t["id"]: template_library.get_template(t["id"]) for t in template_library.list_templates()}
        plan = json.loads((ROOT / "examples" / "aws_plan.json").read_text(encoding="utf-8"))
        specs["terraform_aws_plan"] = terraform.to_spec(plan, "AWS (from terraform show -json)").spec

        for name, spec in specs.items():
            spec = spec.model_copy(update={"output_formats": ["png"]})
            result = renderer.render_spec(spec, settings)
            target = OUT / f"{name}.png"
            shutil.copyfile(result.files["png"], target)
            report = review.review(spec)
            print(f"{name}: {target.stat().st_size // 1024} KB, review {report.score}/100 ({report.grade})")


if __name__ == "__main__":
    main()
