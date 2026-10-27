"""Heuristic Well-Architected review of a DiagramSpec.

Rules look only at what the diagram shows (service types, edges and boundaries), so findings
are prompts for a human reviewer, not proof of a misconfiguration.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel

from . import catalog
from .models import DiagramSpec

Severity = Literal["critical", "high", "medium", "low", "info"]
Pillar = Literal["Security", "Reliability", "Operational Excellence", "Performance Efficiency", "Cost Optimization"]

PENALTY: dict[str, int] = {"critical": 25, "high": 15, "medium": 8, "low": 3, "info": 0}

# Roles are matched against the lower-cased canonical icon id plus its aliases.
ROLES: dict[str, str] = {
    "public": r"^onprem\.client\.|^aws\.general\.(users|user|client|mobileclient)\b|^onprem\.network\.internet$|^generic\.device\.",
    "waf": r"aws\.security\.(waf|shield)|azure\.\w+\.(frontdoor|applicationgateway|webapplicationfirewall)|gcp\.network\.armor|cloudflare",
    "cdn": r"cloudfront|\.cdn|frontdoor|cloudflare|akamai|fastly",
    "edge": (
        r"loadbalanc|\belb|\balb\b|\bnlb\b|applicationgateway|frontdoor|cloudfront|\.cdn|apigateway|apimanagement|"
        r"\.network\.ingress|onprem\.network\.(nginx|traefik|haproxy|envoy|istio|kong)|cloudflare"
    ),
    "compute": (
        r"\.compute\.|\.containers\.|^onprem\.compute\.|\.appservices\.appservices|\.web\.appservices|"
        r"^k8s\.compute\.|^programming\.framework\."
    ),
    "vm": r"^aws\.compute\.(ec2|ec2instance)\b|^azure\.compute\.virtualmachine\b|^gcp\.compute\.computeengine\b|^onprem\.compute\.server\b",
    "data": r"\.(database|databases|storage)\.|^onprem\.(database|inmemory)\.|analytics\.(bigquery|redshift|synapse)|^k8s\.storage\.",
    "queue": r"sqs|\bsns\b|simplenotification|servicebus|eventhub|eventgrid|pubsub|kafka|rabbitmq|kinesis|eventbridge|activemq|nats",
    "monitoring": (
        r"cloudwatch|\.monitor\.|applicationinsights|loganalytics|^gcp\.operations\.|^onprem\.(monitoring|logging)\.|"
        r"datadog|newrelic|grafana|prometheus|xray|cloudtrail|splunk|elastic"
    ),
    "secrets": r"secretsmanager|\bkms\b|keymanagement|keyvault|secretmanager|\.vault\b|hashicorp",
    "identity": r"cognito|\biam\b|identityandaccess|\.identity\.|activedirectory|adb2c|entra|gcp\.security\.iam|okta|auth0|keycloak",
    "egress": r"natgateway|\.nat\b|networkfirewall|\.firewall|firewallrules",
    "backup": r"backup|recoveryservices",
    "cicd": r"^onprem\.ci\.|^onprem\.gitops\.|devops\.(pipelines|repos)|codepipeline|codebuild|cloudbuild|githubactions|gitlab|jenkins|argo",
}
_ROLE_RE = {k: re.compile(v) for k, v in ROLES.items()}
_NETWORK_LABEL = re.compile(r"vpc|vnet|virtual network|subnet|private|network|vpn|\bnet\b", re.I)
_REGION_LABEL = re.compile(r"region|\b(us|eu|ap|sa|ca|me|af)-[a-z]+-\d\b|east|west|central|north|south", re.I)


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    pillar: Pillar
    title: str
    detail: str
    recommendation: str
    nodes: list[str] = []


class ReviewReport(BaseModel):
    title: str
    score: int
    grade: str
    summary: dict[str, int]
    by_pillar: dict[str, int]
    findings: list[Finding]
    note: str = "Heuristic review of what the diagram shows; confirm each finding against the real configuration."


def _roles(node_type: str) -> set[str]:
    try:
        icon = catalog.resolve(node_type)
        hay = " ".join([icon.id.lower(), *(f"{icon.provider}.{icon.category}.{a}".lower() for a in icon.aliases)])
    except catalog.UnknownIconError:
        hay = node_type.lower()
    return {role for role, rx in _ROLE_RE.items() if any(rx.search(part) for part in hay.split())}


def review(spec: DiagramSpec) -> ReviewReport:
    roles = {n.id: _roles(n.type) for n in spec.nodes}
    labels = {n.id: n.label.replace("\n", " ") or n.id for n in spec.nodes}
    types = {n.id: n.type.lower() for n in spec.nodes}
    has = lambda role: any(role in r for r in roles.values())  # noqa: E731
    with_role = lambda role: [nid for nid, r in roles.items() if role in r]  # noqa: E731

    succ: dict[str, set[str]] = defaultdict(set)
    pred: dict[str, set[str]] = defaultdict(set)
    for e in spec.edges:
        succ[e.source].add(e.target)
        pred[e.target].add(e.source)
        if e.bidirectional:
            succ[e.target].add(e.source)
            pred[e.source].add(e.target)

    parents = {c.id: c.parent for c in spec.clusters}
    cluster_labels = {c.id: c.label for c in spec.clusters}

    def ancestry(node_cluster: str | None) -> list[str]:
        chain, cur = [], node_cluster
        while cur is not None and cur in cluster_labels and cur not in chain:
            chain.append(cur)
            cur = parents.get(cur)
        return chain

    node_cluster = {n.id: n.cluster for n in spec.nodes}
    findings: list[Finding] = []

    def add(rule: str, sev: Severity, pillar: Pillar, title: str, detail: str, rec: str, nodes: list[str] | None = None) -> None:
        findings.append(Finding(rule_id=rule, severity=sev, pillar=pillar, title=title, detail=detail, recommendation=rec, nodes=nodes or []))

    public = with_role("public")
    public_targets = {t for p in public for t in succ[p]}

    exposed_data = sorted(t for t in public_targets if "data" in roles.get(t, set()) and "edge" not in roles.get(t, set()))
    if exposed_data:
        add("SEC-001", "critical", "Security", "Data store reachable directly from the internet",
            f"Public clients connect straight to: {', '.join(labels[n] for n in exposed_data)}.",
            "Put data stores behind private endpoints/subnets and reach them only through an application tier.", exposed_data)

    exposed_compute = sorted(
        t for t in public_targets
        if "compute" in roles.get(t, set()) and not roles.get(t, set()) & {"edge", "waf"}
    )
    if exposed_compute:
        add("SEC-002", "high", "Security", "Compute exposed without a gateway or load balancer",
            f"Public clients connect straight to: {', '.join(labels[n] for n in exposed_compute)}.",
            "Front compute with a load balancer / API gateway / application gateway and a WAF.", exposed_compute)

    if public and public_targets and not has("waf"):
        add("SEC-003", "medium", "Security", "No web application firewall on the public entry point",
            "The diagram has internet-facing traffic but no WAF, Shield, Front Door, Application Gateway WAF or Cloud Armor.",
            "Add a WAF (AWS WAF, Azure Front Door/App Gateway WAF, Cloud Armor) with managed OWASP rule sets.")

    if has("compute") and has("data") and not has("secrets"):
        add("SEC-004", "medium", "Security", "No secrets or key management service",
            "Workloads use data stores but no Secrets Manager / KMS / Key Vault / Secret Manager is shown.",
            "Store credentials and keys in a managed secret store and access it with workload identity.")

    if (public or has("compute")) and not has("identity"):
        add("SEC-005", "medium", "Security", "No identity provider shown",
            "No Cognito, IAM, Entra ID or Cloud IAM component appears in the diagram.",
            "Show how users and workloads authenticate (OIDC provider, managed identities / IAM roles).")

    has_network_boundary = any(_NETWORK_LABEL.search(lbl) for lbl in cluster_labels.values())
    loose_data = sorted(
        n for n in with_role("data")
        if not any(_NETWORK_LABEL.search(cluster_labels[c]) for c in ancestry(node_cluster[n]))
        and not re.search(r"s3|storage\.(gcs|storage)|blob", types[n])
    )
    if loose_data and (has_network_boundary or len(loose_data) == len(with_role("data"))):
        add("SEC-006", "low", "Security", "Data stores outside any network boundary",
            f"Not inside a VPC/VNet/subnet/private boundary: {', '.join(labels[n] for n in loose_data)}.",
            "Place databases and caches in private subnets or behind private endpoints and draw that boundary.", loose_data)

    if has_network_boundary and has("compute") and not has("egress"):
        add("SEC-007", "info", "Security", "No egress control shown",
            "Private networks are shown without a NAT gateway or firewall for outbound traffic.",
            "Route outbound traffic through a NAT gateway or firewall with an allow-list.")

    lb_like = set(with_role("edge"))
    single_vms = sorted(
        n for n in with_role("vm")
        if pred[n] and not (pred[n] & lb_like) and not re.search(r"auto ?scal|scale set|\bmig\b", " ".join(cluster_labels[c] for c in ancestry(node_cluster[n])), re.I)
    )
    if single_vms:
        add("REL-001", "medium", "Reliability", "Virtual machine without a load balancer",
            f"Traffic reaches these VMs without a load balancer: {', '.join(labels[n] for n in single_vms)}.",
            "Run at least two instances across availability zones behind a load balancer, or use an autoscaling group.", single_vms)

    region_clusters = [c for c, lbl in cluster_labels.items() if _REGION_LABEL.search(lbl)]
    if len(region_clusters) <= 1 and len(spec.nodes) >= 5:
        add("REL-002", "info", "Reliability", "Single region",
            "Only one region (or none) is shown, so a regional outage has no documented recovery path.",
            "State the RTO/RPO; if needed add a DR region (pilot light, warm standby or active-active).")

    if has("data") and not has("backup"):
        add("REL-003", "low", "Reliability", "No backup service shown",
            "Data stores appear without AWS Backup, Recovery Services / Backup vault or equivalent.",
            "Show backup and point-in-time-restore strategy for every stateful service.")

    compute_ids = set(with_role("compute"))
    longest = _longest_chain(compute_ids, succ)
    if len(longest) >= 4 and not has("queue"):
        add("REL-004", "low", "Reliability", "Deep synchronous call chain",
            f"{len(longest)} services call each other in sequence: {' -> '.join(labels[n] for n in longest)}.",
            "Decouple with a queue or event bus, and add timeouts, retries and circuit breakers.", longest)

    if len(spec.nodes) >= 3 and not has("monitoring"):
        add("OPS-001", "medium", "Operational Excellence", "No monitoring or logging",
            "No CloudWatch, Azure Monitor/App Insights/Log Analytics, Cloud Monitoring or similar is shown.",
            "Add centralised logs, metrics, traces and alerting.")

    if len(spec.nodes) >= 5 and not has("cicd"):
        add("OPS-002", "info", "Operational Excellence", "No deployment pipeline shown",
            "The diagram does not show how changes reach production.",
            "Show the CI/CD pipeline and infrastructure-as-code path.")

    connected = set(succ) | set(pred)
    orphans = sorted(n.id for n in spec.nodes if spec.edges and n.id not in connected)
    if orphans:
        add("OPS-003", "low", "Operational Excellence", "Unconnected components",
            f"These components have no connections: {', '.join(labels[n] for n in orphans)}.",
            "Connect them or remove them so the diagram reflects real dependencies.", orphans)

    if public and has("compute") and not has("cdn"):
        add("PERF-001", "info", "Performance Efficiency", "No CDN for public traffic",
            "Internet traffic reaches the application without a CDN / edge cache.",
            "Serve static content and cacheable APIs through CloudFront, Front Door or Cloud CDN.")

    data_ids = set(with_role("data"))
    cache_like = {n for n in data_ids if re.search(r"redis|cache|memcache|memorystore|dax", types[n])}
    if len(compute_ids) >= 2 and data_ids and not cache_like:
        add("PERF-002", "info", "Performance Efficiency", "No caching layer",
            "Several compute services read from data stores but no cache is shown.",
            "Consider ElastiCache / Azure Managed Redis / Memorystore for hot reads and sessions.")

    order = list(PENALTY)
    findings.sort(key=lambda f: (order.index(f.severity), f.rule_id))
    score = max(0, 100 - sum(PENALTY[f.severity] for f in findings))
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"
    summary = {sev: sum(1 for f in findings if f.severity == sev) for sev in order}
    by_pillar: dict[str, int] = defaultdict(int)
    for f in findings:
        by_pillar[f.pillar] += 1
    return ReviewReport(title=spec.title, score=score, grade=grade, summary=summary, by_pillar=dict(by_pillar), findings=findings)


def _longest_chain(nodes: set[str], succ: dict[str, set[str]], budget: int = 20_000) -> list[str]:
    """Longest simple path through compute nodes; the step budget bounds work on dense graphs."""
    best: list[str] = []
    steps = 0

    def walk(path: list[str]) -> None:
        nonlocal best, steps
        steps += 1
        if len(path) > len(best):
            best = list(path)
        if len(path) >= 12 or steps > budget:
            return
        for nxt in succ[path[-1]]:
            if nxt in nodes and nxt not in path:
                path.append(nxt)
                walk(path)
                path.pop()

    for start in nodes:
        walk([start])
    return best
