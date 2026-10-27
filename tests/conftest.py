import dataclasses
from pathlib import Path

import pytest

from arch_diagram_mcp.config import Settings
from arch_diagram_mcp.models import DiagramSpec


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        output_dir=tmp_path / "out",
        input_roots=(tmp_path,),
        auth_mode="none",
        auth_tokens=(),
        storage_backend="local",
        public_base_url=None,
    )


@pytest.fixture
def small_spec() -> DiagramSpec:
    return DiagramSpec.model_validate(
        {
            "title": "Small",
            "clusters": [{"id": "vpc", "label": "VPC 10.0.0.0/16"}],
            "nodes": [
                {"id": "users", "type": "onprem.client.Users"},
                {"id": "alb", "type": "aws.network.ALB", "cluster": "vpc"},
                {"id": "app", "type": "aws.compute.ECS", "cluster": "vpc"},
                {"id": "db", "type": "aws.database.RDS", "cluster": "vpc"},
            ],
            "edges": [
                {"source": "users", "target": "alb", "label": "HTTPS"},
                {"source": "alb", "target": "app"},
                {"source": "app", "target": "db"},
            ],
        }
    )
