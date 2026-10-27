"""Build a DiagramSpec from Terraform JSON.

Accepts `terraform show -json <planfile>` (best: has configuration references),
`terraform show -json` of state, or a raw `.tfstate` file (v4).

- Mapped resource types become nodes with official icons.
- Network/grouping resources (VPC, VNet, subnet, resource group, ...) become nested clusters.
- Edges come from resource references. References that pass through "glue" resources
  (listeners, target groups, NICs, role assignments, ...) are collapsed into a direct edge,
  oriented by tier: edge -> compute -> messaging -> data.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import ClusterSpec, DiagramSpec, EdgeSpec, NodeSpec

Values = dict[str, Any]

# --------------------------------------------------------------------------- mappings

def _lb_icon(v: Values) -> str:
    return "aws.network.NLB" if v.get("load_balancer_type") == "network" else "aws.network.ALB"


def _ecs_service_icon(v: Values) -> str:
    return "aws.compute.Fargate" if v.get("launch_type") == "FARGATE" else "aws.compute.ECS"


def _cognitive_icon(v: Values) -> str:
    return "azure.ml.AzureOpenAI" if str(v.get("kind", "")).lower() == "openai" else "azure.aimachinelearning.CognitiveServices"


NODE_TYPES: dict[str, str | Callable[[Values], str]] = {
    # AWS
    "aws_instance": "aws.compute.EC2",
    "aws_autoscaling_group": "aws.compute.EC2AutoScaling",
    "aws_lb": _lb_icon,
    "aws_alb": "aws.network.ALB",
    "aws_elb": "aws.network.CLB",
    "aws_lambda_function": "aws.compute.Lambda",
    "aws_ecs_cluster": "aws.compute.ECS",
    "aws_ecs_service": _ecs_service_icon,
    "aws_eks_cluster": "aws.compute.EKS",
    "aws_eks_node_group": "aws.compute.EC2AutoScaling",
    "aws_ecr_repository": "aws.compute.ECR",
    "aws_db_instance": "aws.database.RDS",
    "aws_rds_cluster": "aws.database.Aurora",
    "aws_dynamodb_table": "aws.database.Dynamodb",
    "aws_elasticache_cluster": "aws.database.ElastiCache",
    "aws_elasticache_replication_group": "aws.database.ElastiCache",
    "aws_s3_bucket": "aws.storage.S3",
    "aws_efs_file_system": "aws.storage.EFS",
    "aws_sqs_queue": "aws.integration.SQS",
    "aws_sns_topic": "aws.integration.SNS",
    "aws_cloudwatch_event_bus": "aws.integration.Eventbridge",
    "aws_cloudwatch_event_rule": "aws.integration.EventbridgeRule",
    "aws_kinesis_stream": "aws.analytics.KinesisDataStreams",
    "aws_msk_cluster": "aws.analytics.ManagedStreamingForKafka",
    "aws_api_gateway_rest_api": "aws.network.APIGateway",
    "aws_apigatewayv2_api": "aws.network.APIGateway",
    "aws_cloudfront_distribution": "aws.network.CloudFront",
    "aws_route53_zone": "aws.network.Route53",
    "aws_nat_gateway": "aws.network.NATGateway",
    "aws_internet_gateway": "aws.network.InternetGateway",
    "aws_ec2_transit_gateway": "aws.network.TransitGateway",
    "aws_networkfirewall_firewall": "aws.network.NetworkFirewall",
    "aws_wafv2_web_acl": "aws.security.WAF",
    "aws_secretsmanager_secret": "aws.security.SecretsManager",
    "aws_kms_key": "aws.security.KMS",
    "aws_cognito_user_pool": "aws.security.Cognito",
    "aws_guardduty_detector": "aws.security.Guardduty",
    "aws_cloudwatch_log_group": "aws.management.CloudwatchLogs",
    "aws_cloudtrail": "aws.management.Cloudtrail",
    "aws_sfn_state_machine": "aws.integration.StepFunctions",
    "aws_opensearch_domain": "aws.analytics.AmazonOpensearchService",
    "aws_redshift_cluster": "aws.analytics.Redshift",
    "aws_backup_vault": "aws.storage.Backup",
    "aws_sagemaker_endpoint": "aws.ml.Sagemaker",
    # Azure
    "azurerm_linux_virtual_machine": "azure.compute.VirtualMachine",
    "azurerm_windows_virtual_machine": "azure.compute.VirtualMachine",
    "azurerm_virtual_machine": "azure.compute.VirtualMachine",
    "azurerm_linux_virtual_machine_scale_set": "azure.compute.VMScaleSets",
    "azurerm_windows_virtual_machine_scale_set": "azure.compute.VMScaleSets",
    "azurerm_orchestrated_virtual_machine_scale_set": "azure.compute.VMScaleSets",
    "azurerm_kubernetes_cluster": "azure.compute.AKS",
    "azurerm_linux_web_app": "azure.compute.AppServices",
    "azurerm_windows_web_app": "azure.compute.AppServices",
    "azurerm_app_service": "azure.compute.AppServices",
    "azurerm_linux_function_app": "azure.compute.FunctionApps",
    "azurerm_windows_function_app": "azure.compute.FunctionApps",
    "azurerm_function_app": "azure.compute.FunctionApps",
    "azurerm_container_app": "azure.compute.ContainerApps",
    "azurerm_container_group": "azure.compute.ContainerInstances",
    "azurerm_container_registry": "azure.compute.ContainerRegistries",
    "azurerm_mssql_server": "azure.database.SQLServers",
    "azurerm_mssql_database": "azure.database.SQLDatabases",
    "azurerm_mssql_managed_instance": "azure.database.SQLManagedInstances",
    "azurerm_postgresql_flexible_server": "azure.database.DatabaseForPostgresqlServers",
    "azurerm_mysql_flexible_server": "azure.database.DatabaseForMysqlServers",
    "azurerm_cosmosdb_account": "azure.database.CosmosDb",
    "azurerm_redis_cache": "azure.database.CacheForRedis",
    "azurerm_managed_redis": "azure.database.CacheForRedis",
    "azurerm_storage_account": "azure.storage.StorageAccounts",
    "azurerm_servicebus_namespace": "azure.integration.ServiceBus",
    "azurerm_eventhub_namespace": "azure.analytics.EventHubs",
    "azurerm_eventgrid_topic": "azure.integration.EventGridTopics",
    "azurerm_api_management": "azure.integration.APIManagement",
    "azurerm_cdn_frontdoor_profile": "azure.network.FrontDoors",
    "azurerm_frontdoor": "azure.network.FrontDoors",
    "azurerm_cdn_frontdoor_firewall_policy": "azure.networking.WebApplicationFirewallPolicieswaf",
    "azurerm_web_application_firewall_policy": "azure.networking.WebApplicationFirewallPolicieswaf",
    "azurerm_application_gateway": "azure.network.ApplicationGateway",
    "azurerm_lb": "azure.network.LoadBalancers",
    "azurerm_firewall": "azure.network.Firewall",
    "azurerm_key_vault": "azure.security.KeyVaults",
    "azurerm_log_analytics_workspace": "azure.monitor.LogAnalyticsWorkspaces",
    "azurerm_application_insights": "azure.devops.ApplicationInsights",
    "azurerm_dns_zone": "azure.network.DNSZones",
    "azurerm_private_dns_zone": "azure.network.DNSPrivateZones",
    "azurerm_public_ip": "azure.network.PublicIpAddresses",
    "azurerm_nat_gateway": "azure.networking.Nat",
    "azurerm_virtual_network_gateway": "azure.network.VirtualNetworkGateways",
    "azurerm_bastion_host": "azure.networking.Bastions",
    "azurerm_private_endpoint": "azure.network.PrivateEndpoint",
    "azurerm_user_assigned_identity": "azure.identity.ManagedIdentities",
    "azurerm_cognitive_account": _cognitive_icon,
    "azurerm_search_service": "azure.web.Search",
    "azurerm_logic_app_workflow": "azure.integration.LogicApps",
    "azurerm_data_factory": "azure.database.DataFactory",
    "azurerm_synapse_workspace": "azure.analytics.SynapseAnalytics",
    "azurerm_recovery_services_vault": "azure.storage.RecoveryServicesVaults",
    "azurerm_data_protection_backup_vault": "azure.other.BackupVault",
    # Google Cloud
    "google_compute_instance": "gcp.compute.ComputeEngine",
    "google_compute_instance_group_manager": "gcp.compute.ComputeEngine",
    "google_compute_region_instance_group_manager": "gcp.compute.ComputeEngine",
    "google_container_cluster": "gcp.compute.GKE",
    "google_cloud_run_v2_service": "gcp.compute.Run",
    "google_cloud_run_service": "gcp.compute.Run",
    "google_cloudfunctions2_function": "gcp.compute.Functions",
    "google_cloudfunctions_function": "gcp.compute.Functions",
    "google_app_engine_application": "gcp.compute.AppEngine",
    "google_sql_database_instance": "gcp.database.SQL",
    "google_spanner_instance": "gcp.database.Spanner",
    "google_bigtable_instance": "gcp.database.Bigtable",
    "google_firestore_database": "gcp.database.Firestore",
    "google_redis_instance": "gcp.database.Memorystore",
    "google_storage_bucket": "gcp.storage.GCS",
    "google_pubsub_topic": "gcp.analytics.Pubsub",
    "google_bigquery_dataset": "gcp.analytics.Bigquery",
    "google_dataflow_job": "gcp.analytics.Dataflow",
    "google_compute_global_forwarding_rule": "gcp.network.LoadBalancing",
    "google_compute_forwarding_rule": "gcp.network.LoadBalancing",
    "google_compute_security_policy": "gcp.network.Armor",
    "google_compute_router_nat": "gcp.network.NAT",
    "google_compute_vpn_gateway": "gcp.network.VPN",
    "google_compute_ha_vpn_gateway": "gcp.network.VPN",
    "google_dns_managed_zone": "gcp.network.DNS",
    "google_kms_crypto_key": "gcp.security.KMS",
    "google_secret_manager_secret": "gcp.security.SecretManager",
    "google_artifact_registry_repository": "gcp.devtools.GCR",
    "google_cloud_tasks_queue": "gcp.devtools.Tasks",
    "google_cloud_scheduler_job": "gcp.devtools.Scheduler",
    "google_vertex_ai_endpoint": "gcp.ml.VertexAI",
    "google_composer_environment": "gcp.analytics.Composer",
}

# Grouping resources -> (rank, label prefix). Higher rank = more specific boundary.
CONTAINER_TYPES: dict[str, tuple[int, str]] = {
    "azurerm_resource_group": (1, "Resource Group"),
    "aws_vpc": (2, "VPC"),
    "azurerm_virtual_network": (2, "VNet"),
    "google_compute_network": (2, "VPC"),
    "aws_subnet": (3, "Subnet"),
    "azurerm_subnet": (3, "Subnet"),
    "google_compute_subnetwork": (3, "Subnet"),
    "azurerm_container_app_environment": (4, "Container Apps Env"),
}

# Plumbing resources whose references are collapsed into direct edges between mapped resources.
GLUE_TYPES = {
    "aws_lb_listener", "aws_lb_listener_rule", "aws_lb_target_group", "aws_lb_target_group_attachment",
    "aws_alb_listener", "aws_alb_target_group", "aws_alb_target_group_attachment", "aws_autoscaling_attachment",
    "aws_lambda_permission", "aws_lambda_event_source_mapping", "aws_sns_topic_subscription",
    "aws_api_gateway_integration", "aws_apigatewayv2_integration", "aws_apigatewayv2_route",
    "aws_cloudwatch_event_target", "aws_ecs_task_definition", "aws_s3_bucket_notification",
    "aws_wafv2_web_acl_association", "aws_iam_role", "aws_iam_role_policy_attachment", "aws_iam_instance_profile",
    "azurerm_network_interface", "azurerm_lb_backend_address_pool", "azurerm_lb_rule",
    "azurerm_cdn_frontdoor_endpoint", "azurerm_cdn_frontdoor_origin_group", "azurerm_cdn_frontdoor_origin",
    "azurerm_cdn_frontdoor_route", "azurerm_cdn_frontdoor_security_policy", "azurerm_api_management_api",
    "azurerm_api_management_backend", "azurerm_servicebus_queue", "azurerm_servicebus_topic",
    "azurerm_servicebus_subscription", "azurerm_eventgrid_event_subscription", "azurerm_eventhub",
    "azurerm_storage_container", "azurerm_key_vault_secret", "azurerm_role_assignment",
    "azurerm_service_plan", "azurerm_mssql_firewall_rule", "azurerm_private_dns_zone_virtual_network_link",
    "google_compute_backend_service", "google_compute_url_map", "google_compute_target_https_proxy",
    "google_compute_target_http_proxy", "google_compute_region_network_endpoint_group",
    "google_compute_instance_template", "google_pubsub_subscription", "google_service_account",
    "google_compute_router", "google_sql_database", "google_cloud_run_v2_service_iam_member",
}

_TIER_PATTERNS = (
    (0, r"network\.(alb|nlb|clb|elb|apigateway|cloudfront|route53|frontdoors|applicationgateway|loadbalanc|dns|cdn)|waf|"
        r"gcp\.network\.(loadbalancing|armor|dns)|networking\.webapplicationfirewall"),
    (1, r"\.compute\.|containerapps|appservices"),
    (2, r"\.integration\.|pubsub|eventhub|kinesis|kafka"),
    (3, r"\.database\.|\.storage\.|bigquery|redshift|opensearch|synapse"),
)

PROVIDER_CLUSTERS = {"aws": "AWS Cloud", "azurerm": "Microsoft Azure", "google": "Google Cloud"}


def _tier(icon: str) -> int | None:
    low = icon.lower()
    for tier, rx in _TIER_PATTERNS:
        if re.search(rx, low):
            return tier
    return None


# --------------------------------------------------------------------------- parsing


@dataclass
class _Resource:
    address: str  # config address without instance keys, e.g. module.net.aws_subnet.private
    type: str
    name: str
    values: Values = field(default_factory=dict)
    count: int = 0
    refs: set[str] = field(default_factory=set)


_INDEX = re.compile(r"\[[^\]]*\]")


def _strip_index(address: str) -> str:
    return _INDEX.sub("", address)


def _collect_refs(expr: Any, out: set[str]) -> None:
    if isinstance(expr, dict):
        refs = expr.get("references")
        if isinstance(refs, list):
            out.update(r for r in refs if isinstance(r, str))
        for v in expr.values():
            if isinstance(v, (dict, list)):
                _collect_refs(v, out)
    elif isinstance(expr, list):
        for item in expr:
            _collect_refs(item, out)


def _normalise_ref(ref: str, prefix: str, outputs: dict[str, set[str]]) -> set[str]:
    """Turn 'aws_vpc.main.id' / 'module.net.vpc_id' into resource addresses."""
    ref = _strip_index(ref)
    parts = ref.split(".")
    if parts[0] in {"var", "local", "each", "count", "path", "terraform", "self", "data"}:
        return set()
    if parts[0] == "module" and len(parts) >= 3:
        return set(outputs.get(f"{prefix}module.{parts[1]}.{parts[2]}", set()))
    if len(parts) >= 2:
        return {f"{prefix}{parts[0]}.{parts[1]}"}
    return set()


def _walk_config(module: Values, prefix: str, refs_by_addr: dict[str, set[str]], outputs: dict[str, set[str]]) -> None:
    # Child modules first so their outputs can be resolved by the parent.
    for call_name, call in (module.get("module_calls") or {}).items():
        child_prefix = f"{prefix}module.{call_name}."
        child = call.get("module") or {}
        _walk_config(child, child_prefix, refs_by_addr, outputs)
        for out_name, out in (child.get("outputs") or {}).items():
            raw: set[str] = set()
            _collect_refs(out.get("expression", {}), raw)
            resolved: set[str] = set()
            for r in raw:
                resolved |= _normalise_ref(r, child_prefix, outputs)
            outputs[f"{prefix}module.{call_name}.{out_name}"] = resolved
    for res in module.get("resources") or []:
        if res.get("mode", "managed") != "managed":
            continue
        raw = set()
        _collect_refs(res.get("expressions", {}), raw)
        raw.update(res.get("depends_on") or [])
        addr = prefix + res["address"]
        bucket = refs_by_addr.setdefault(addr, set())
        for r in raw:
            bucket |= _normalise_ref(r, prefix, outputs)


def _walk_values(module: Values, resources: dict[str, _Resource]) -> None:
    for res in module.get("resources") or []:
        if res.get("mode", "managed") != "managed":
            continue
        addr = _strip_index(res["address"])
        r = resources.get(addr)
        if r is None:
            r = resources[addr] = _Resource(addr, res["type"], res["name"], res.get("values") or {})
        r.count += 1
        r.refs.update(_strip_index(d) for d in res.get("depends_on") or [])
    for child in module.get("child_modules") or []:
        _walk_values(child, resources)


def _parse(doc: Values) -> dict[str, _Resource]:
    resources: dict[str, _Resource] = {}
    if "planned_values" in doc or "values" in doc:
        root = (doc.get("planned_values") or doc.get("values") or {}).get("root_module") or {}
        _walk_values(root, resources)
        # Resources being destroyed are absent from planned_values; prior_state fills nothing in that case.
        refs_by_addr: dict[str, set[str]] = {}
        outputs: dict[str, set[str]] = {}
        if "configuration" in doc:
            _walk_config(doc["configuration"].get("root_module") or {}, "", refs_by_addr, outputs)
        for addr, refs in refs_by_addr.items():
            if addr in resources:
                resources[addr].refs |= refs
    elif "resources" in doc:  # raw tfstate v4
        for res in doc["resources"]:
            if res.get("mode") != "managed":
                continue
            addr = (res["module"] + "." if res.get("module") else "") + f"{res['type']}.{res['name']}"
            addr = _strip_index(addr)
            instances = res.get("instances") or [{}]
            r = resources.setdefault(addr, _Resource(addr, res["type"], res["name"], instances[0].get("attributes") or {}))
            r.count += len(instances)
            for inst in instances:
                r.refs.update(_strip_index(d) for d in inst.get("dependencies") or [])
    else:
        raise ValueError("Unrecognised Terraform JSON: expected a plan (`terraform show -json plan`), state, or .tfstate file.")
    for r in resources.values():
        r.refs.discard(r.address)
    return resources


# --------------------------------------------------------------------------- spec building


def _safe_id(address: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", address)[-64:]


def _display_name(r: _Resource) -> str:
    v = r.values
    tags = v.get("tags") if isinstance(v.get("tags"), dict) else {}
    name = tags.get("Name") or v.get("name") or r.name
    return str(name)[:60]


def _container_label(r: _Resource) -> str:
    _, prefix = CONTAINER_TYPES[r.type]
    v = r.values
    cidr = v.get("cidr_block") or v.get("ip_cidr_range") or v.get("address_space") or v.get("address_prefixes")
    if isinstance(cidr, list):
        cidr = ", ".join(map(str, cidr[:2]))
    location = v.get("location") or v.get("availability_zone") or v.get("region")
    extras = " ".join(str(x) for x in (cidr, f"({location})" if location else None) if x)
    return f"{prefix}: {_display_name(r)}" + (f" {extras}" if extras else "")


@dataclass
class TerraformImport:
    spec: DiagramSpec
    mapped: int
    containers: int
    skipped_types: dict[str, int]
    inferred_edges: int


def to_spec(doc: Values, title: str, direction: str = "LR") -> TerraformImport:
    resources = _parse(doc)
    nodes = {a: r for a, r in resources.items() if r.type in NODE_TYPES}
    containers = {a: r for a, r in resources.items() if r.type in CONTAINER_TYPES}
    glue = {a for a, r in resources.items() if r.type in GLUE_TYPES}
    skipped: dict[str, int] = defaultdict(int)
    for r in resources.values():
        if r.address not in nodes and r.address not in containers and r.address not in glue:
            skipped[r.type] += 1

    icons: dict[str, str] = {}
    for a, r in nodes.items():
        m = NODE_TYPES[r.type]
        icons[a] = m(r.values) if callable(m) else m

    # Undirected adjacency over all resources, used for glue traversal and container lookup.
    adj: dict[str, set[str]] = defaultdict(set)
    for a, r in resources.items():
        for ref in r.refs:
            if ref in resources:
                adj[a].add(ref)
                adj[ref].add(a)

    def best_container(addr: str, _stack: frozenset[str] = frozenset()) -> str | None:
        candidates = {ref for ref in resources[addr].refs if ref in containers}
        for g in resources[addr].refs & glue:  # e.g. VM -> NIC -> subnet
            candidates |= {ref for ref in resources[g].refs if ref in containers}
        candidates -= _stack | {addr}
        if not candidates:
            return None
        top_rank = max(CONTAINER_TYPES[containers[c].type][0] for c in candidates)
        top = sorted(c for c in candidates if CONTAINER_TYPES[containers[c].type][0] == top_rank)
        if len(top) == 1:
            return top[0]
        # Spans several subnets (e.g. a load balancer): use their common parent if there is one.
        parents_of = {best_container(c, _stack | {addr}) for c in top}
        return parents_of.pop() if len(parents_of) == 1 and None not in parents_of else top[0]

    container_parent = {a: best_container(a) for a in containers}
    for a, parent in list(container_parent.items()):  # break accidental cycles
        seen, cur = {a}, parent
        while cur is not None:
            if cur in seen:
                container_parent[a] = None
                break
            seen.add(cur)
            cur = container_parent.get(cur)

    providers = {r.type.split("_", 1)[0] for r in [*nodes.values(), *containers.values()]}
    provider_roots = {p: f"provider_{p}" for p in providers if p in PROVIDER_CLUSTERS}

    clusters: list[ClusterSpec] = [
        ClusterSpec(id=cid, label=PROVIDER_CLUSTERS[p], style="rounded") for p, cid in sorted(provider_roots.items())
    ]
    for a, r in sorted(containers.items(), key=lambda kv: CONTAINER_TYPES[kv[1].type][0]):
        parent = container_parent[a]
        parent_id = _safe_id(parent) if parent else provider_roots.get(r.type.split("_", 1)[0])
        clusters.append(ClusterSpec(id=_safe_id(a), label=_container_label(r)[:200], parent=parent_id))

    spec_nodes: list[NodeSpec] = []
    for a, r in sorted(nodes.items()):
        label = _display_name(r) + (f" (x{r.count})" if r.count > 1 else "")
        cont = best_container(a)
        cluster = _safe_id(cont) if cont else provider_roots.get(r.type.split("_", 1)[0])
        spec_nodes.append(NodeSpec(id=_safe_id(a), type=icons[a], label=label, cluster=cluster))

    edges: dict[tuple[str, str], bool] = {}  # (src, dst) -> inferred

    def orient(a: str, b: str, referencing: str | None) -> tuple[str, str]:
        ta, tb = _tier(icons[a]), _tier(icons[b])
        if ta is not None and tb is not None and ta != tb:
            return (a, b) if ta < tb else (b, a)
        if "identity" in icons[b].lower() and "identity" not in icons[a].lower():
            return (b, a)
        if referencing == b:
            return (b, a)
        return (a, b)

    for a, r in nodes.items():
        for ref in r.refs:
            if ref in nodes and ref != a:
                edges.setdefault(orient(a, ref, a), False)

    # Collapse each connected component of glue resources into edges between its mapped neighbours.
    visited: set[str] = set()
    for start in glue:
        if start in visited:
            continue
        stack, component = [start], set()
        while stack:
            g = stack.pop()
            if g in component:
                continue
            component.add(g)
            stack.extend(n for n in adj[g] if n in glue and n not in component)
        visited |= component
        touching = sorted({n for g in component for n in adj[g] if n in nodes})
        added = 0
        for i, a in enumerate(touching):
            for b in touching[i + 1:]:
                ta, tb = _tier(icons[a]), _tier(icons[b])
                identity_pair = "identity" in icons[a].lower() or "identity" in icons[b].lower()
                if (ta is not None and tb is not None and ta != tb) or identity_pair:
                    pair = orient(a, b, None)
                    if pair not in edges and (pair[1], pair[0]) not in edges and added < 50:
                        edges[pair] = True
                        added += 1

    spec_edges = [
        EdgeSpec(source=_safe_id(s), target=_safe_id(t), style="dashed" if inferred else "")
        for (s, t), inferred in sorted(edges.items())
    ]
    # Drop clusters that ended up empty so Graphviz doesn't draw blank boxes.
    used = {n.cluster for n in spec_nodes}
    parent_of = {c.id: c.parent for c in clusters}
    keep: set[str] = set()
    for cid in used:
        while cid is not None and cid not in keep:
            keep.add(cid)
            cid = parent_of.get(cid)
    clusters = [c for c in clusters if c.id in keep]

    spec = DiagramSpec(
        title=title,
        nodes=spec_nodes or [NodeSpec(id="empty", type="generic.blank.Blank", label="No mapped resources")],
        edges=spec_edges,
        clusters=clusters,
        direction=direction,  # type: ignore[arg-type]
        filename=re.sub(r"\W+", "_", title)[:60],
    )
    return TerraformImport(
        spec=spec,
        mapped=len(spec_nodes),
        containers=len(clusters),
        skipped_types=dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
        inferred_edges=sum(1 for v in edges.values() if v),
    )
