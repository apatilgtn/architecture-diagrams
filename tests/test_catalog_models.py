import pytest

from arch_diagram_mcp import catalog
from arch_diagram_mcp.models import DiagramSpec, validate_spec


@pytest.mark.parametrize(
    "query,expected",
    [
        ("ecs", "aws.compute.ElasticContainerService"),
        ("s3", "aws.storage.SimpleStorageServiceS3"),
        ("eks", "aws.compute.ElasticKubernetesService"),
        ("cloud run", "gcp.compute.Run"),
    ],
)
def test_search_ranks_exact_alias_first(query, expected):
    assert catalog.search(query, limit=3)[0]["id"] == expected


def test_resolve_alias_and_case_insensitive():
    assert catalog.resolve("aws.compute.ecs").id == "aws.compute.ElasticContainerService"
    assert catalog.resolve("AWS.Network.ALB").name == "ElbApplicationLoadBalancer"


def test_unknown_icon_suggests():
    with pytest.raises(catalog.UnknownIconError) as exc:
        catalog.resolve("aws.compute.Lamda")
    assert "aws.compute.Lambda" in exc.value.suggestions


def test_valid_spec_has_no_errors(small_spec, settings):
    assert not [i for i in validate_spec(small_spec, settings) if i.level == "error"]


def test_validation_reports_every_problem(settings):
    spec = DiagramSpec.model_validate(
        {
            "title": "Broken",
            "clusters": [
                {"id": "a", "label": "A", "parent": "b"},
                {"id": "b", "label": "B", "parent": "a"},
                {"id": "x", "label": "X", "parent": "missing"},
            ],
            "nodes": [
                {"id": "n1", "type": "aws.compute.Lamda", "cluster": "nope"},
                {"id": "n1", "type": "aws.compute.EC2"},
                {"id": "a", "type": "aws.compute.EC2"},
            ],
            "edges": [{"source": "n1", "target": "b"}, {"source": "n1", "target": "ghost"}],
        }
    )
    codes = {i.code for i in validate_spec(spec, settings)}
    assert {
        "UNKNOWN_PARENT",
        "CLUSTER_CYCLE",
        "UNKNOWN_CLUSTER",
        "UNKNOWN_ICON",
        "DUPLICATE_NODE",
        "ID_COLLISION",
        "UNKNOWN_EDGE_END",
    } <= codes


def test_limits_enforced(settings, small_spec):
    import dataclasses

    tight = dataclasses.replace(settings, max_nodes=2)
    assert any(i.code == "LIMIT_NODES" for i in validate_spec(small_spec, tight))


def test_schema_rejects_injection_in_ids_and_colours():
    with pytest.raises(ValueError):
        DiagramSpec.model_validate({"title": "t", "nodes": [{"id": 'a"; x', "type": "aws.compute.EC2"}]})
    with pytest.raises(ValueError):
        DiagramSpec.model_validate(
            {"title": "t", "nodes": [{"id": "a", "type": "aws.compute.EC2"}], "edges": [{"source": "a", "target": "a", "color": "red];x"}]}
        )
