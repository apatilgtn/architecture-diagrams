"""GCP Cloud Asset Inventory import.

tests/fixtures/gcp_inventory_shop.json is a real `gcloud asset list --content-type resource` export of a
test platform (Cloud Run, Cloud SQL, Memorystore, GKE Autopilot with a workload, Pub/Sub, a global load
balancer with a CDN bucket backend, Scheduler, a VM), with identifiers replaced by placeholders.
"""

import asyncio
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from arch_diagram_mcp import gcp_inventory
from arch_diagram_mcp.models import validate_spec
from arch_diagram_mcp.server import create_server

FIXTURE = Path(__file__).parent / "fixtures" / "gcp_inventory_shop.json"


@pytest.fixture(scope="module")
def shop() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _view(result):
    """(nodes by first label line -> (icon, cluster label), edges as (src, dst, label))."""
    clusters = {c.id: c.label for c in result.spec.clusters}
    nodes = {n.label.split("\n")[0]: (n.type, clusters.get(n.cluster)) for n in result.spec.nodes}
    names = {n.id: n.label.split("\n")[0] for n in result.spec.nodes}
    edges = {(names[e.source], names[e.target], e.label) for e in result.spec.edges}
    return nodes, edges


def test_real_export_keeps_names_and_places_resources(shop, settings):
    result = gcp_inventory.to_spec(shop, "Shop")
    nodes, _ = _view(result)
    assert nodes["orders-api"] == ("gcp.compute.Run", "Subnet: shop-app 10.10.0.0/24 (australia-southeast1)")
    assert nodes["shop-web"] == ("gcp.compute.Run", "Project: acme-shop-prod")
    assert nodes["shop-db"] == ("gcp.database.SQL", "VPC: shop-vpc")
    assert nodes["shop-cache"] == ("gcp.database.Memorystore", "VPC: shop-vpc")
    assert nodes["shop-conn"] == ("gcp.network.VirtualPrivateCloud", "VPC: shop-vpc")
    assert nodes["ops-bastion"] == ("gcp.compute.ComputeEngine", "Subnet: shop-app 10.10.0.0/24 (australia-southeast1)")
    assert nodes["recommendations (x2)"] == ("k8s.compute.Deploy", "Namespace: shop")
    assert nodes["catalog-sync"] == ("k8s.compute.Cronjob", "Namespace: shop")
    assert nodes["shop-assets-acme"][0] == "gcp.storage.Storage"
    assert nodes["shop-lb"][0] == "gcp.network.LoadBalancing"
    labels = [c.label for c in result.spec.clusters]
    assert "GKE: shop-gke (Autopilot)" in labels
    # kube-system workloads, routes, RBAC and secret versions are noise, not diagram content
    assert not any("kube-dns" in n or "metrics" in n for n in nodes)
    assert result.skipped_types == {}
    assert not [i for i in validate_spec(result.spec, settings) if i.level == "error"]


def test_real_export_edges_come_from_configuration(shop):
    _, edges = _view(gcp_inventory.to_spec(shop, "Shop"))
    assert edges == {
        ("Internet users", "shop-lb", "HTTP :80"),
        ("shop-lb", "shop-web", ""),  # URL map default service > backend service > serverless NEG
        ("shop-lb", "shop-assets-acme", "/static/* CDN"),  # path rule > CDN backend bucket
        ("shop-web", "orders-api", "HTTPS"),  # env var URL matched to the service that owns it
        ("orders-api", "shop-db", "SQL"),  # cloudsql-instances annotation
        ("orders-api", "db-password", "secret"),  # secretKeyRef
        ("orders-api", "orders", "publish"),  # topic path in an env var
        ("orders", "payments-worker", "push: orders-to-payments"),  # push subscription endpoint URL
        ("orders", "orders-dlq", "dead-letter"),
        ("payments-worker", "shop-conn", "VPC egress"),  # vpc-access-connector annotation
        ("nightly-report", "orders-api", "scheduled"),  # Scheduler HTTP target URL
        ("recommendations (x2)", "shop-cache", "cache"),  # REDIS_HOST matched to the instance IP
        ("recommendations (x2)", "orders", "publish"),
    }


def test_label_filter_keeps_matches_and_their_neighbours(shop):
    tagged = gcp_inventory.to_spec(shop, "Shop", labels={"app": "shop"})
    nodes, _ = _view(tagged)
    assert "shop-db" in nodes  # unlabelled, but orders-api uses it
    assert "shop-gke" not in nodes  # unlabelled control plane with no edges
    strict = gcp_inventory.to_spec(shop, "Shop", labels={"app": "shop"}, include_neighbors=False)
    assert "shop-db" not in _view(strict)[0]
    assert strict.filtered_out > tagged.filtered_out


def test_region_and_kubernetes_switches(shop):
    elsewhere, _ = _view(gcp_inventory.to_spec(shop, "Shop", regions=["us-central1"], labels={"app": "shop"}))
    assert "orders" in elsewhere  # global resources are in every region
    assert not {"orders-api", "shop-db", "payments-worker", "recommendations (x2)"} & set(elsewhere)  # never via neighbours
    nodes, _ = _view(gcp_inventory.to_spec(shop, "Shop", include_kubernetes=False))
    assert "recommendations (x2)" not in nodes and "shop-gke" in nodes


def test_project_number_is_paired_with_id_without_a_project_asset(shop):
    without = [a for a in shop if a["assetType"] != "cloudresourcemanager.googleapis.com/Project"]
    nodes, edges = _view(gcp_inventory.to_spec(without, "Shop"))
    assert nodes["db-password"][1] == "Project: acme-shop-prod"  # secret names use the project number
    assert ("payments-worker", "shop-conn", "VPC egress") in edges  # connector name uses the number too


def test_export_ndjson_with_snake_case_keys(shop):
    lines = "\n".join(
        json.dumps({"name": a["name"], "asset_type": a["assetType"], "ancestors": a["ancestors"], "resource": a["resource"]})
        for a in shop
    )
    doc = gcp_inventory.parse_text(lines)
    assert gcp_inventory.to_spec(doc, "Shop").mapped == gcp_inventory.to_spec(shop, "Shop").mapped


def test_search_all_resources_format_with_versioned_resources(shop):
    results = [
        {"name": a["name"], "assetType": a["assetType"], "project": "projects/123456789012",
         "location": a["resource"].get("location"), "versionedResources": [{"version": "v1", "resource": a["resource"]["data"]}]}
        for a in shop
    ]
    _, edges = _view(gcp_inventory.to_spec({"results": results}, "Shop"))
    assert ("orders-api", "shop-db", "SQL") in edges


def _asset(asset_type, name, data, location="europe-west1"):
    return {"assetType": asset_type, "name": name, "ancestors": ["projects/42"],
            "resource": {"data": data, "location": location}}


P = "//{svc}.googleapis.com/projects/demo"


def test_functions_gen2_absorb_their_run_service_and_triggers():
    run_name = "projects/demo/locations/europe-west1/services/thumbnailer"
    doc = [
        _asset("cloudfunctions.googleapis.com/Function", P.format(svc="cloudfunctions") + "/locations/europe-west1/functions/thumbnailer", {
            "serviceConfig": {"service": run_name, "uri": "https://thumbnailer-x.a.run.app",
                              "environmentVariables": {"OUT": "gs://thumbs"}},
            "eventTrigger": {"eventType": "google.cloud.storage.object.v1.finalized",
                             "eventFilters": [{"attribute": "bucket", "value": "uploads"}]},
            "buildConfig": {"runtime": "python312"}}),
        _asset("run.googleapis.com/Service", "//run.googleapis.com/" + run_name, {
            "metadata": {"name": "thumbnailer", "labels": {"goog-managed-by": "cloudfunctions"}}}),
        _asset("storage.googleapis.com/Bucket", "//storage.googleapis.com/uploads", {"name": "uploads"}),
        _asset("storage.googleapis.com/Bucket", "//storage.googleapis.com/thumbs", {"name": "thumbs"}),
        _asset("storage.googleapis.com/Bucket", "//storage.googleapis.com/demo_cloudbuild", {"name": "demo_cloudbuild"}),
    ]
    nodes, edges = _view(gcp_inventory.to_spec(doc, "Fn"))
    assert set(nodes) == {"thumbnailer", "uploads", "thumbs"}  # one function node, no build bucket
    assert nodes["thumbnailer"][0] == "gcp.compute.Functions"
    assert ("uploads", "thumbnailer", "finalized") in edges
    assert ("thumbnailer", "thumbs", "objects") in edges


def test_cloud_armor_mig_and_internal_users_edge():
    g = "https://www.googleapis.com/compute/v1/projects/demo"
    doc = [
        _asset("compute.googleapis.com/Network", P.format(svc="compute") + "/global/networks/prod", {"name": "prod"}, "global"),
        _asset("compute.googleapis.com/Subnetwork", P.format(svc="compute") + "/regions/europe-west1/subnetworks/web",
               {"name": "web", "network": f"{g}/global/networks/prod", "ipCidrRange": "10.0.0.0/24"}),
        _asset("compute.googleapis.com/InstanceTemplate", P.format(svc="compute") + "/global/instanceTemplates/web-t", {
            "properties": {"machineType": "n2-standard-2", "networkInterfaces": [{"subnetwork": f"{g}/regions/europe-west1/subnetworks/web"}]}}, "global"),
        _asset("compute.googleapis.com/InstanceGroupManager", P.format(svc="compute") + "/zones/europe-west1-b/instanceGroupManagers/web-mig", {
            "instanceTemplate": f"{g}/global/instanceTemplates/web-t", "targetSize": 3}, "europe-west1-b"),
        _asset("compute.googleapis.com/Instance", P.format(svc="compute") + "/zones/europe-west1-b/instances/web-mig-abcd", {
            "metadata": {"items": [{"key": "created-by", "value": "projects/42/zones/europe-west1-b/instanceGroupManagers/web-mig"}]}},
            "europe-west1-b"),
        _asset("compute.googleapis.com/InstanceGroup", P.format(svc="compute") + "/zones/europe-west1-b/instanceGroups/web-mig", {}, "europe-west1-b"),
        _asset("compute.googleapis.com/SecurityPolicy", P.format(svc="compute") + "/global/securityPolicies/edge-waf", {}, "global"),
        _asset("compute.googleapis.com/BackendService", P.format(svc="compute") + "/global/backendServices/web-be", {
            "securityPolicy": f"{g}/global/securityPolicies/edge-waf", "enableCDN": True,
            "backends": [{"group": f"{g}/zones/europe-west1-b/instanceGroups/web-mig"}]}, "global"),
        _asset("compute.googleapis.com/UrlMap", P.format(svc="compute") + "/global/urlMaps/web-lb",
               {"defaultService": f"{g}/global/backendServices/web-be"}, "global"),
        _asset("compute.googleapis.com/TargetHttpsProxy", P.format(svc="compute") + "/global/targetHttpsProxies/web-px",
               {"urlMap": f"{g}/global/urlMaps/web-lb"}, "global"),
        _asset("compute.googleapis.com/GlobalForwardingRule", P.format(svc="compute") + "/global/forwardingRules/web-fr", {
            "target": f"{g}/global/targetHttpsProxies/web-px", "portRange": "443-443",
            "loadBalancingScheme": "EXTERNAL_MANAGED"}, "global"),
        _asset("example.googleapis.com/Widget", P.format(svc="example") + "/widgets/w1", {}),
    ]
    result = gcp_inventory.to_spec(doc, "LB")
    nodes, edges = _view(result)
    assert nodes["web-mig (x3)"] == ("gcp.compute.ComputeEngine", "Subnet: web 10.0.0.0/24 (europe-west1)")
    assert "web-mig-abcd" not in nodes  # member VM collapsed into its MIG
    assert ("Internet users", "web-lb", "HTTPS :443") in edges
    assert ("web-lb", "web-mig (x3)", "CDN") in edges
    assert ("edge-waf", "web-lb", "WAF policy") in edges
    assert result.skipped_types == {"example.googleapis.com/Widget": 1}


def test_shared_vpc_network_from_another_project_is_synthesised():
    doc = [_asset("redis.googleapis.com/Instance", P.format(svc="redis") + "/locations/europe-west1/instances/cache",
                  {"authorizedNetwork": "projects/net-host/global/networks/shared", "redisVersion": "REDIS_7_2"})]
    result = gcp_inventory.to_spec(doc, "Shared VPC")
    clusters = {c.id: c for c in result.spec.clusters}
    node = result.spec.nodes[0]
    assert clusters[node.cluster].label == "VPC: shared"
    assert clusters[clusters[node.cluster].parent].label == "Project: net-host"


def test_long_names_produce_valid_ids(settings):
    long = "x" * 120
    doc = [_asset("pubsub.googleapis.com/Topic", P.format(svc="pubsub") + f"/topics/{long}", {})]
    result = gcp_inventory.to_spec(doc, "Long")
    assert all(len(n.id) <= 64 for n in result.spec.nodes)
    assert not [i for i in validate_spec(result.spec, settings) if i.level == "error"]


def test_every_supported_type_is_handled_and_printable(capsys):
    from arch_diagram_mcp.__main__ import main

    containers = {"compute.googleapis.com/Network", "compute.googleapis.com/Subnetwork",
                  "cloudresourcemanager.googleapis.com/Project"}
    handled = set(gcp_inventory.HANDLERS) | gcp_inventory._LB_TYPES | containers
    assert set(gcp_inventory.SUPPORTED_ASSET_TYPES) <= handled
    # Each name was checked against the live Cloud Asset API, which rejects unknown types outright.
    assert not [t for t in gcp_inventory.SUPPORTED_ASSET_TYPES if "/Region" in t and "BackendService" not in t]
    main(["--gcp-asset-types"])
    assert capsys.readouterr().out.strip().split(",") == list(gcp_inventory.SUPPORTED_ASSET_TYPES)


def test_rejects_non_inventory_input():
    with pytest.raises(ValueError, match="Cloud Asset Inventory"):
        gcp_inventory.to_spec("nope", "x")


def call(mcp, tool, args):
    return asyncio.run(mcp.call_tool(tool, args))


def test_tool_renders_from_path_and_reports_stats(settings, tmp_path):
    export = tmp_path / "assets.json"
    export.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    result = call(create_server(settings), "diagram_from_inventory",
                  {"title": "Shop", "path": str(export), "labels": {"app": "shop"}})
    summary = json.loads(result.content[0].text)
    assert summary["mapped_resources"] == 18 and summary["edges"] == 13
    assert summary["files"]["png"].endswith(".png")
    assert result.content[1].type == "image"


def test_tool_spec_only_and_http_path_guard(settings):
    mcp = create_server(settings)
    out = call(mcp, "diagram_from_inventory",
               {"title": "Shop", "inventory_json": FIXTURE.read_text(encoding="utf-8"), "render_diagram": False})
    assert json.loads(out.content[0].text)["spec"]["title"] == "Shop"
    with pytest.raises(ToolError, match="disabled over HTTP"):
        call(create_server(settings, http=True), "diagram_from_inventory", {"title": "t", "path": "x.json"})
    with pytest.raises(ToolError, match="exactly one"):
        call(mcp, "diagram_from_inventory", {"title": "t"})
