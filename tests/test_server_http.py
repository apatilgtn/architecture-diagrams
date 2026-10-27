import asyncio
import dataclasses
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from arch_diagram_mcp import storage
from arch_diagram_mcp.http_app import build_app
from arch_diagram_mcp.server import create_server


def call(mcp, tool, args):
    return asyncio.run(mcp.call_tool(tool, args))


def test_all_tools_registered(settings):
    mcp = create_server(settings)
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "search_icons",
        "list_icon_categories",
        "list_templates",
        "get_template",
        "validate_diagram",
        "generate_architecture_diagram",
        "review_architecture",
        "diagram_from_terraform",
        "diagram_from_inventory",
        "server_info",
    }


def test_generate_returns_summary_and_inline_png(settings, small_spec):
    mcp = create_server(settings)
    result = call(mcp, "generate_architecture_diagram", {"spec": small_spec.model_dump()})
    summary = json.loads(result.content[0].text)
    assert summary["files"]["png"].endswith(".png")
    assert result.content[1].type == "image"


def test_invalid_spec_error_reaches_client_with_suggestions(settings, small_spec):
    mcp = create_server(settings)
    spec = small_spec.model_dump()
    spec["nodes"][2]["type"] = "aws.compute.Lamda"
    with pytest.raises(ToolError, match="aws.compute.Lambda"):
        call(mcp, "generate_architecture_diagram", {"spec": spec})


def test_template_round_trip_through_tools(settings):
    mcp = create_server(settings)
    spec = call(mcp, "get_template", {"template_id": "aws_three_tier"}).structured_content
    report = call(mcp, "review_architecture", {"spec": spec}).structured_content
    assert 0 <= report["score"] <= 100
    with pytest.raises(ToolError, match="Unknown template"):
        call(mcp, "get_template", {"template_id": "nope"})


def test_terraform_path_must_be_inside_input_roots(settings, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "plan.json"
    outside.write_text("{}")
    mcp = create_server(settings)
    with pytest.raises(ToolError, match="outside the allowed input roots"):
        call(mcp, "diagram_from_terraform", {"title": "t", "path": str(outside)})


def test_terraform_tool_from_text(settings):
    from tests.test_terraform import AWS_PLAN

    mcp = create_server(settings)
    result = call(mcp, "diagram_from_terraform", {"title": "Plan", "terraform_json": json.dumps(AWS_PLAN), "render_diagram": False})
    payload = json.loads(result.content[0].text)
    assert payload["mapped_resources"] == 4 and payload["spec"]["nodes"]


def _client(settings, token):
    s = dataclasses.replace(settings, auth_mode="token", auth_tokens=(("tester", token),), allowed_hosts=("testserver",))
    store = storage.LocalStore(s, http=True)
    return TestClient(build_app(create_server(s, store, http=True), s, store))


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def test_http_health_and_auth(settings):
    token = "s3cret-token-0123456789"
    with _client(settings, token) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.post("/mcp", json={}).status_code == 401
        assert client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {token}", **ACCEPT})
        assert ok.status_code == 200, ok.text
        assert ok.json()["result"]["serverInfo"]["name"] == "architecture-diagrams"
        spoofed = client.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {token}", **ACCEPT, "Host": "evil.example"})
        assert spoofed.status_code == 421


def test_http_refuses_public_bind_without_auth(settings):
    s = dataclasses.replace(settings, http_host="0.0.0.0", auth_mode="none")
    store = storage.LocalStore(s, http=True)
    with pytest.raises(SystemExit, match="Refusing"):
        build_app(create_server(s, store, http=True), s, store)
