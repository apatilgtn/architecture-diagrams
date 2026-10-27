"""Build a DiagramSpec from a Google Cloud Asset Inventory export of running infrastructure.

Accepted input:
- `gcloud asset list --project P --content-type resource --format json` (recommended),
- `gcloud asset export --content-type resource` output (newline-delimited JSON, snake_case keys),
- `gcloud asset search-all-resources --read-mask '*' --format json` (config in versionedResources).

Resources keep their real names. Layout follows Google's own diagrams: Project > VPC > Subnet, with
GKE clusters as boxes holding their namespaces and workloads. Edges come from configuration only:
- Cloud SQL / secret / KMS / Pub/Sub / bucket / Redis references in annotations and env vars,
- URLs matched to the Cloud Run services and functions that own them (push subscriptions,
  Scheduler targets, env vars),
- the load balancer chain (forwarding rule > proxy > URL map > backend service > NEG or bucket),
- Eventarc triggers, Pub/Sub dead-letter and BigQuery/Cloud Storage subscriptions,
- container images pulled from Artifact Registry repositories.

Calls that exist only in application code are invisible here; add those edges to the returned spec.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .models import ClusterSpec, DiagramSpec, EdgeSpec, NodeSpec

Values = dict[str, Any]

# Asset types this importer understands; usable as `gcloud asset list --asset-types` filter.
SUPPORTED_ASSET_TYPES = (
    "cloudresourcemanager.googleapis.com/Project",
    "compute.googleapis.com/Network", "compute.googleapis.com/Subnetwork", "compute.googleapis.com/Instance",
    "compute.googleapis.com/InstanceGroupManager",
    "compute.googleapis.com/InstanceTemplate",
    "compute.googleapis.com/GlobalForwardingRule", "compute.googleapis.com/ForwardingRule",
    "compute.googleapis.com/TargetHttpProxy", "compute.googleapis.com/TargetHttpsProxy",
    "compute.googleapis.com/TargetTcpProxy", "compute.googleapis.com/TargetSslProxy",
    "compute.googleapis.com/UrlMap",
    "compute.googleapis.com/BackendService", "compute.googleapis.com/RegionBackendService",
    "compute.googleapis.com/BackendBucket", "compute.googleapis.com/NetworkEndpointGroup",
    "compute.googleapis.com/SecurityPolicy", "compute.googleapis.com/Router", "compute.googleapis.com/VpnGateway",
    "container.googleapis.com/Cluster", "apps.k8s.io/Deployment", "apps.k8s.io/StatefulSet",
    "batch.k8s.io/CronJob",
    "run.googleapis.com/Service", "run.googleapis.com/Job", "cloudfunctions.googleapis.com/Function",
    "cloudfunctions.googleapis.com/CloudFunction", "appengine.googleapis.com/Application",
    "sqladmin.googleapis.com/Instance", "alloydb.googleapis.com/Cluster", "redis.googleapis.com/Instance",
    "redis.googleapis.com/Cluster", "spanner.googleapis.com/Instance", "bigtableadmin.googleapis.com/Instance",
    "firestore.googleapis.com/Database", "file.googleapis.com/Instance", "storage.googleapis.com/Bucket",
    "bigquery.googleapis.com/Dataset", "pubsub.googleapis.com/Topic", "pubsub.googleapis.com/Subscription",
    "eventarc.googleapis.com/Trigger", "cloudscheduler.googleapis.com/Job", "cloudtasks.googleapis.com/Queue",
    "dataflow.googleapis.com/Job", "dataproc.googleapis.com/Cluster", "composer.googleapis.com/Environment",
    "aiplatform.googleapis.com/Endpoint", "apigateway.googleapis.com/Gateway",
    "secretmanager.googleapis.com/Secret", "cloudkms.googleapis.com/CryptoKey",
    "artifactregistry.googleapis.com/Repository", "dns.googleapis.com/ManagedZone",
    "vpcaccess.googleapis.com/Connector",
)

# Plumbing and per-object noise that is expected and never reported as "unmapped".
_IGNORED = re.compile(
    r"^(rbac\.authorization|admissionregistration|policy|storage|gateway\.networking|networking|"
    r"apiextensions|autoscaling|scheduling|discovery|certificates|coordination|node|metrics|monitoring|"
    r"snapshot\.storage|hub\.gke|net\.gke|networking\.gke|cloud\.google)\.k8s\.io/|^(apps|batch)\.k8s\.io/(ReplicaSet|Job|DaemonSet)$|"
    r"^k8s\.io/|^iam\.|^serviceusage\.|^cloudbilling\.|^logging\.|^monitoring\.|^servicedirectory\.|"
    r"^cloudresourcemanager\.|^container\.googleapis\.com/NodePool$|"
    r"^servicenetworking\.|^networkconnectivity\.|^orgpolicy\.|^osconfig\.|^securitycenter\.|"
    r"^dns\.googleapis\.com/(ResourceRecordSet|ResponsePolicy|ResponsePolicyRule|Policy)$|"
    r"^secretmanager\.googleapis\.com/SecretVersion$|^run\.googleapis\.com/(Revision|Execution|DomainMapping)$|"
    r"^cloudkms\.googleapis\.com/(KeyRing|CryptoKeyVersion|ImportJob)$|^bigquery\.googleapis\.com/Table$|"
    r"^artifactregistry\.googleapis\.com/DockerImage$|^sqladmin\.googleapis\.com/(Database|User)$|"
    r"^compute\.googleapis\.com/(Route|Firewall|FirewallPolicy|Disk|RegionDisk|Address|GlobalAddress|Image|"
    r"Snapshot|ResourcePolicy|InstanceSettings|Project|HealthCheck|RegionHealthCheck|HttpHealthCheck|"
    r"SslCertificate|RegionSslCertificate|SslPolicy|Autoscaler|RegionAutoscaler|InstanceGroup|"
    r"TargetPool|TargetInstance|VpnTunnel|ExternalVpnGateway|Interconnect|InterconnectAttachment|"
    r"ServiceAttachment|NetworkAttachment|PacketMirroring|Commitment|Reservation)$"
)

_K8S_SYSTEM_NS = re.compile(
    r"^(kube-|gke-|gmp-|config-management-|cnrm-|asm-|istio-|anthos-|gatekeeper-|resource-group-)|"
    r"^(kube-system|default|cert-manager|knative-serving|gke-connect|managed-prometheus)$"
)
_K8S_PATH = re.compile(r"projects/([^/]+)/(?:locations|zones)/([^/]+)/clusters/([^/]+)/k8s/namespaces/([^/]+)")
_PROJECT_SEGMENT = re.compile(r"(^|/)projects/([^/]+)")
_URL_PREFIXES = re.compile(
    r"^https://(www|compute|container|sqladmin|storage|run|cloudfunctions|pubsub)\.googleapis\.com/"
    r"(compute/(v1|beta|alpha)/|storage/v1/|sql/v1beta4/|v1beta1/|v1beta/|v1/|v2/)?"
)
_FULL_NAME = re.compile(r"^//[a-z0-9.-]+\.googleapis\.com/")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_SYSTEM_BUCKET = re.compile(
    r"(_cloudbuild$|^artifacts\..+\.appspot\.com$|\.appspot\.com$|^gcf-(v2-)?(sources|uploads)-|"
    r"^run-sources-|^cloud-ai-platform-|^dataproc-(staging|temp)-|^[a-z0-9-]+_cloudbuild$|^gke-.+-backups?$)"
)


@dataclass
class _Asset:
    type: str
    path: str  # canonical path: projects/<project-id>/..., the primary key
    project: str
    location: str
    data: Values
    labels: dict[str, str]
    raw_labels: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1].removeprefix("bucket:")


@dataclass
class _Node:
    key: str
    icon: str
    label: str
    project: str
    location: str
    labels: dict[str, str]
    place: str | None = None  # container key (subnet / network / GKE cluster / namespace)
    count: int = 1


@dataclass
class _Container:
    key: str
    rank: int  # 0 project, 1 VPC, 2 subnet, 3 GKE cluster, 4 namespace
    label: str
    parent: str | None


@dataclass
class InventoryImport:
    spec: DiagramSpec
    mapped: int
    boundaries: int
    edges: int
    skipped_types: dict[str, int]
    filtered_out: int
    external_hosts: list[str]


# --------------------------------------------------------------------------- input


def parse_text(text: str) -> Any:
    """JSON document, or newline-delimited JSON as written by `gcloud asset export`."""
    text = text.strip().lstrip("﻿")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
        return items


def _items(doc: Any) -> list[Values]:
    if isinstance(doc, dict):
        for key in ("assets", "results", "resources"):
            if isinstance(doc.get(key), list):
                return doc[key]
        return [doc]
    if isinstance(doc, list):
        return [i for i in doc if isinstance(i, dict)]
    raise ValueError("Expected a Cloud Asset Inventory export: a JSON array of assets or newline-delimited JSON.")


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _str_dict(v: Any) -> dict[str, str]:
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def _labels(data: Values, raw: Values) -> dict[str, str]:
    for candidate in (
        raw.get("labels"), data.get("labels"), _get(data, "metadata", "labels"), data.get("resourceLabels"),
        _get(data, "settings", "userLabels"), data.get("userLabels"),
    ):
        if isinstance(candidate, dict) and candidate:
            return _str_dict(candidate)
    return {}


# --------------------------------------------------------------------------- the importer


class _Importer:
    def __init__(self, items: list[Values], include_kubernetes: bool):
        self.include_kubernetes = include_kubernetes
        self.num2id: dict[str, str] = {}
        self.assets: list[_Asset] = []
        self.index: dict[str, _Asset] = {}
        self.hosts: dict[str, _Asset] = {}
        self.ips: dict[str, _Asset] = {}
        self.nodes: dict[str, _Node] = {}
        self.alias: dict[str, str] = {}  # asset path -> node key it is drawn as
        self.containers: dict[str, _Container] = {}
        self.pending: list[tuple[str, str, str, str, str, str, str]] = []  # src, ref, hint, label, style, project, region
        self.edges: dict[tuple[str, str], tuple[str, str]] = {}
        self.skipped: dict[str, int] = defaultdict(int)
        self.external_hosts: set[str] = set()
        self._load(items)

    # ---- loading and indexing

    def _load(self, items: list[Values]) -> None:
        raw_assets: list[tuple[str, str, str, Values, Values, str]] = []
        for raw in items:
            atype = raw.get("assetType") or raw.get("asset_type")
            name = raw.get("name")
            if not atype or not name:
                continue
            res = raw.get("resource") or {}
            data = res.get("data")
            if data is None:
                versioned = raw.get("versionedResources") or raw.get("versioned_resources") or []
                data = (versioned[0] or {}).get("resource") if versioned else None
            if data is None:
                data = raw.get("additionalAttributes") or raw.get("additional_attributes") or {}
            location = str(res.get("location") or raw.get("location") or data.get("location") or "")
            hint = " ".join(str(x) for x in (raw.get("project"), res.get("parent"), *(raw.get("ancestors") or [])) if x)
            raw_assets.append((atype, name, location, data, raw, hint))
            if atype == "cloudresourcemanager.googleapis.com/Project" and data.get("projectNumber"):
                self.num2id[str(data["projectNumber"])] = str(data.get("projectId") or data["projectNumber"])
        # Pair the project id in names with the project number in ancestors when no Project asset was exported.
        for _, name, _, _, _, hint in raw_assets:
            named = _PROJECT_SEGMENT.search(name)
            numbered = re.search(r"projects/(\d+)", hint)
            if named and numbered and not named.group(2).isdigit():
                self.num2id.setdefault(numbered.group(1), named.group(2))

        for atype, name, location, data, raw, hint in raw_assets:
            path = self.canon(name)
            m = _PROJECT_SEGMENT.search(path) or re.search(r"projects/([^/\s]+)", hint)
            project = self.num2id.get(m.group(m.lastindex or 1), m.group(m.lastindex or 1)) if m else ""
            if atype == "storage.googleapis.com/Bucket":
                path = f"bucket:{data.get('name') or name.rsplit('/', 1)[-1]}"
            labels = _labels(data, raw)
            asset = _Asset(atype, path, project, location.lower(), data, labels)
            self.assets.append(asset)
            self._index(asset)

    def canon(self, ref: str) -> str:
        ref = _URL_PREFIXES.sub("", _FULL_NAME.sub("", ref.strip()))
        if ref.startswith("gs://"):
            return f"bucket:{ref[5:].split('/', 1)[0]}"
        ref = re.sub(r"(^|/)(zones)/", r"\1zones/", ref)
        return _PROJECT_SEGMENT.sub(lambda m: f"{m.group(1)}projects/{self.num2id.get(m.group(2), m.group(2))}", ref)

    def _index(self, a: _Asset) -> None:
        keys = {a.path}
        for field_name in ("selfLink", "name", "id"):
            v = a.data.get(field_name)
            if isinstance(v, str) and ("/" in v or ":" in v):
                keys.add(self.canon(v))
        if a.type.startswith("container.googleapis.com/Cluster"):
            keys.add(a.path.replace("/zones/", "/locations/"))
        if a.type == "sqladmin.googleapis.com/Instance":
            if a.data.get("connectionName"):
                keys.add(f"sql:{a.data['connectionName']}")
            for ip in a.data.get("ipAddresses") or []:
                if ip.get("ipAddress"):
                    self.ips[ip["ipAddress"]] = a
        if a.type == "redis.googleapis.com/Instance":
            for ip_field in ("host", "readEndpoint"):
                if a.data.get(ip_field):
                    self.ips[a.data[ip_field]] = a
        if a.type == "run.googleapis.com/Service":
            urls = _get(a.data, "metadata", "annotations", "run.googleapis.com/urls", default="[]")
            try:
                url_list = json.loads(urls) if isinstance(urls, str) else []
            except json.JSONDecodeError:
                url_list = []
            url_list += [_get(a.data, "status", "url", default=""), _get(a.data, "status", "address", "url", default=""),
                         a.data.get("uri", "")] + list(a.data.get("urls") or [])
            for u in url_list:
                if u:
                    self.hosts[urlsplit(u).hostname or ""] = a
        if a.type.startswith("cloudfunctions.googleapis.com/"):
            for u in (a.data.get("url"), _get(a.data, "serviceConfig", "uri"), _get(a.data, "httpsTrigger", "url")):
                if u:
                    self.hosts[urlsplit(u).hostname or ""] = a
        if a.type == "artifactregistry.googleapis.com/Repository":
            loc, repo = a.location, a.name
            if loc and a.project:
                keys.add(f"image:{loc}-docker.pkg.dev/{a.project}/{repo}")
        for k in keys:
            self.index.setdefault(k, a)

    # ---- resolution

    _TEMPLATES = {
        "network": "projects/{p}/global/networks/{n}",
        "subnetwork": "projects/{p}/regions/{r}/subnetworks/{n}",
        "connector": "projects/{p}/locations/{r}/connectors/{n}",
        "secret": "projects/{p}/secrets/{n}",
        "topic": "projects/{p}/topics/{n}",
        "subscription": "projects/{p}/subscriptions/{n}",
        "run": "projects/{p}/locations/{r}/services/{n}",
        "function": "projects/{p}/locations/{r}/functions/{n}",
        "sql": "projects/{p}/instances/{n}",
        "bucket": "bucket:{n}",
        "kms": "{n}",
    }

    def resolve(self, ref: Any, project: str, region: str, hint: str = "") -> _Asset | None:
        if not isinstance(ref, str) or not ref.strip():
            return None
        ref = ref.strip()
        if ref.startswith(("http://", "https://")) and not _URL_PREFIXES.match(ref):
            host = urlsplit(ref).hostname or ""
            if host in self.hosts:
                return self.hosts[host]
            if host.endswith(".run.app") or host.endswith(".cloudfunctions.net"):
                return None
            if host and not host.endswith(("googleapis.com", "google.com", "localhost")) and "." in host:
                self.external_hosts.add(host)
            return None
        if _IPV4.match(ref):
            return self.ips.get(ref)
        if hint == "sql" and ref.count(":") == 2:
            return self.index.get(f"sql:{ref}")
        if re.match(r"^[a-z0-9-]+-docker\.pkg\.dev/", ref):
            parts = ref.split("/")
            return self.index.get(f"image:{'/'.join(parts[:3])}") if len(parts) >= 3 else None
        key = self.canon(ref)
        if key in self.index:
            return self.index[key]
        if "/" not in ref and hint in self._TEMPLATES:
            return self.index.get(self.canon(self._TEMPLATES[hint].format(p=project, r=region, n=ref)))
        if hint == "bigquery":  # project:dataset.table or project.dataset.table
            m = re.match(r"^([^:.]+)[:.]([^.]+)", ref)
            if m:
                return self.index.get(f"projects/{m.group(1)}/datasets/{m.group(2)}")
        return None

    def node_of(self, a: _Asset | None) -> str | None:
        if a is None:
            return None
        if a.path in self.nodes:
            return a.path
        if a.path in self.alias:
            return self.alias[a.path]
        if a.type == "compute.googleapis.com/NetworkEndpointGroup":
            target = (_get(a.data, "cloudRun", "service"), "run") if a.data.get("cloudRun") else (
                _get(a.data, "cloudFunction", "function"), "function")
            region = (a.data.get("region") or "").rsplit("/", 1)[-1]
            return self.node_of(self.resolve(target[0], a.project, region, target[1]))
        return None

    # ---- registration helpers

    def add_node(self, a: _Asset, icon: str, detail: str = "", place: str | None = None, label: str | None = None,
                 count: int = 1) -> _Node:
        text = (label or a.name)[:60] + (f" (x{count})" if count > 1 else "")
        node = _Node(a.path, icon, text + (f"\n{detail[:60]}" if detail else ""), a.project, a.location, a.labels,
                     place, count)
        self.nodes[a.path] = node
        return node

    def link(self, src: str, ref: Any, hint: str, label: str, style: str, a: _Asset, region: str = "") -> None:
        if isinstance(ref, str) and ref:
            self.pending.append((src, ref, hint, label, style, a.project, region or _region(a.location)))

    def edge(self, src: str | None, dst: str | None, label: str = "", style: str = "") -> None:
        if src and dst and src != dst and (src, dst) not in self.edges and (dst, src) not in self.edges:
            self.edges[(src, dst)] = (label, style)

    def container_for(self, ref: Any, project: str, region: str, hint: str) -> str | None:
        """Container key for a network/subnetwork reference, synthesising boxes for unexported networks."""
        a = self.resolve(ref, project, region, hint)
        if a is not None:
            return a.path
        if not isinstance(ref, str) or not ref:
            return None
        key = self.canon(self._TEMPLATES[hint].format(p=project, r=region, n=ref) if "/" not in ref else ref)
        if key not in self.containers:
            m = _PROJECT_SEGMENT.search(key)
            proj = m.group(2) if m else project
            rank = 2 if "/subnetworks/" in key else 1
            prefix = "Subnet" if rank == 2 else "VPC"
            self.containers[key] = _Container(key, rank, f"{prefix}: {key.rsplit('/', 1)[-1]}", self.project_box(proj))
        return key

    def project_box(self, project: str) -> str | None:
        if not project:
            return None
        key = f"project:{project}"
        if key not in self.containers:
            self.containers[key] = _Container(key, 0, f"Project: {project}", None)
        return key

    # ---- build

    def build(self) -> None:
        by_type: dict[str, list[_Asset]] = defaultdict(list)
        for a in self.assets:
            by_type[a.type].append(a)
        for a in by_type.get("compute.googleapis.com/Network", []):
            self.containers[a.path] = _Container(a.path, 1, f"VPC: {a.name}", self.project_box(a.project))
        for a in by_type.get("compute.googleapis.com/Subnetwork", []):
            region = _region(a.location) or (a.data.get("region") or "").rsplit("/", 1)[-1]
            extras = " ".join(x for x in (a.data.get("ipCidrRange") or "", f"({region})" if region else "") if x)
            parent = self.container_for(a.data.get("network"), a.project, region, "network") or self.project_box(a.project)
            self.containers[a.path] = _Container(a.path, 2, f"Subnet: {a.name} {extras}".strip(), parent)

        handled = {"compute.googleapis.com/Network", "compute.googleapis.com/Subnetwork"}
        for atype, handler in HANDLERS.items():
            for a in by_type.get(atype, []):
                handler(self, a)
            handled.add(atype)
        self._load_balancers(by_type)
        handled |= _LB_TYPES
        for atype, assets in by_type.items():
            if atype not in handled and not _IGNORED.search(atype):
                self.skipped[atype] += len(assets)

        for src, ref, hint, label, style, project, region in self.pending:
            target = self.resolve(ref, project, region, hint)
            if target is not None and target.type == "pubsub.googleapis.com/Subscription":
                # A workload that names a subscription consumes its topic.
                self.edge(self.node_of(self.resolve(target.data.get("topic"), target.project, "", "topic")), src,
                          f"pull: {target.name}", "dashed")
                continue
            dst = self.node_of(target)
            if label.startswith("<-"):
                self.edge(dst, src, label[2:], style)
            else:
                self.edge(src, dst, label or _edge_label(self.nodes[dst].icon if dst in self.nodes else ""), style)

    # ---- load balancers: forwarding rule > target proxy > URL map > backend service > NEG / bucket / MIG

    def _load_balancers(self, by_type: dict[str, list[_Asset]]) -> None:
        users_needed = False
        for fr in by_type.get("compute.googleapis.com/GlobalForwardingRule", []) + by_type.get(
                "compute.googleapis.com/ForwardingRule", []):
            if fr.data.get("pscConnectionId") or str(fr.data.get("target", "")).endswith(("all-apis", "vpc-sc")):
                continue  # Private Service Connect endpoint, not a load balancer
            frontend = self._lb_frontend(fr)
            if frontend is None:
                continue
            lb_asset, backends, armor = frontend
            scheme = str(fr.data.get("loadBalancingScheme") or "")
            ports = str(fr.data.get("portRange") or ",".join(fr.data.get("ports") or [])).split("-")[0]
            proto = "HTTPS" if "Https" in self._target_type(fr) or ports == "443" else (
                "HTTP" if "Http" in self._target_type(fr) or ports == "80" else str(fr.data.get("IPProtocol") or "TCP"))
            external = scheme.startswith("EXTERNAL")
            if lb_asset.path not in self.nodes:
                kind = "external" if external else "internal"
                self.add_node(lb_asset, "gcp.network.LoadBalancing", f"{kind} {proto} LB")
                if not external:
                    self.nodes[lb_asset.path].place = self.container_for(
                        fr.data.get("subnetwork") or fr.data.get("network"), fr.project, _region(fr.location),
                        "subnetwork" if fr.data.get("subnetwork") else "network")
            if external:
                users_needed = True
                self.edge("users", lb_asset.path, f"{proto} :{ports}" if ports else proto)
            for svc_ref, path_label in backends:
                self._lb_backend(lb_asset.path, svc_ref, path_label, fr)
            for policy in armor:
                pa = self.resolve(policy, fr.project, "", "")
                if pa is not None and pa.path not in self.nodes:
                    self.add_node(pa, "gcp.network.Armor", "Cloud Armor")
                if pa is not None:
                    self.edge(pa.path, lb_asset.path, "WAF policy", "dotted")
        if users_needed:
            self.nodes["users"] = _Node("users", "onprem.client.Users", "Internet users", "", "", {})

    def _target_type(self, fr: _Asset) -> str:
        t = self.resolve(fr.data.get("target"), fr.project, "", "")
        return t.type if t else ""

    def _lb_frontend(self, fr: _Asset) -> tuple[_Asset, list[tuple[str, str]], list[str]] | None:
        target = self.resolve(fr.data.get("target"), fr.project, _region(fr.location), "")
        if fr.data.get("backendService"):  # passthrough L4 load balancer
            svc = self.resolve(fr.data["backendService"], fr.project, _region(fr.location), "")
            return (fr, [(fr.data["backendService"], "")], [svc.data.get("securityPolicy")] if svc and
                    svc.data.get("securityPolicy") else [])
        if target is None:
            return None
        if target.data.get("urlMap"):
            urlmap = self.resolve(target.data["urlMap"], fr.project, _region(fr.location), "")
            if urlmap is None:
                return None
            backends: list[tuple[str, str]] = []
            if urlmap.data.get("defaultService"):
                backends.append((urlmap.data["defaultService"], ""))
            for pm in urlmap.data.get("pathMatchers") or []:
                if pm.get("defaultService"):
                    backends.append((pm["defaultService"], ""))
                for rule in pm.get("pathRules") or []:
                    if rule.get("service"):
                        backends.append((rule["service"], ", ".join(rule.get("paths") or [])[:40]))
                for rule in pm.get("routeRules") or []:
                    for ws in _get(rule, "routeAction", "weightedBackendServices", default=[]):
                        backends.append((ws.get("backendService"), ""))
                    if rule.get("service"):
                        backends.append((rule["service"], ""))
            armor = []
            for ref, _ in backends:
                svc = self.resolve(ref, fr.project, _region(fr.location), "")
                if svc is not None and svc.data.get("securityPolicy"):
                    armor.append(svc.data["securityPolicy"])
            for a in (fr, target, urlmap):
                self.alias.setdefault(a.path, urlmap.path)
            return urlmap, backends, sorted(set(armor))
        if target.data.get("service"):  # TCP / SSL proxy
            self.alias.setdefault(fr.path, target.path)
            return target, [(target.data["service"], "")], []
        return None

    def _lb_backend(self, lb: str, ref: str, path_label: str, fr: _Asset) -> None:
        svc = self.resolve(ref, fr.project, _region(fr.location), "")
        if svc is None:
            return
        if svc.type == "compute.googleapis.com/BackendBucket":
            bucket = self.resolve(svc.data.get("bucketName"), svc.project, "", "bucket")
            label = " ".join(x for x in (path_label, "CDN" if svc.data.get("enableCdn") else "") if x)
            self.edge(lb, self.node_of(bucket), label or "static")
            return
        cdn = "CDN" if svc.data.get("enableCDN") else ""
        label = " ".join(x for x in (path_label, cdn) if x)
        for backend in svc.data.get("backends") or []:
            group = self.resolve(backend.get("group"), svc.project, "", "")
            dst = self.node_of(group)
            if dst is None and group is not None and group.type.endswith("InstanceGroup"):
                mig = self.index.get(group.path.replace("/instanceGroups/", "/instanceGroupManagers/"))
                dst = self.node_of(mig)
            if dst is None and group is not None and group.type == "compute.googleapis.com/NetworkEndpointGroup":
                dst = self._gke_service_for_neg(group)
            self.edge(lb, dst, label)

    def _gke_service_for_neg(self, neg: _Asset) -> str | None:
        # GKE NEGs are named k8s1-<cluster-uid>-<namespace>-<service>-<port>-<hash>.
        m = re.match(r"^k8s1-[0-9a-f]+-(.+)-\d+-[0-9a-f]+$", neg.name)
        if not m:
            return None
        middle = m.group(1)
        for key, node in self.nodes.items():
            km = _K8S_PATH.search(key)
            if km and middle.startswith(km.group(4) + "-") and middle.endswith(node.label.split("\n")[0].split(" (x")[0]):
                return key
        return None


_LB_TYPES = {
    "compute.googleapis.com/GlobalForwardingRule", "compute.googleapis.com/ForwardingRule",
    "compute.googleapis.com/TargetHttpProxy", "compute.googleapis.com/TargetHttpsProxy",
    "compute.googleapis.com/TargetTcpProxy", "compute.googleapis.com/TargetSslProxy",
    "compute.googleapis.com/UrlMap",
    "compute.googleapis.com/BackendService", "compute.googleapis.com/RegionBackendService",
    "compute.googleapis.com/BackendBucket", "compute.googleapis.com/NetworkEndpointGroup",
    "compute.googleapis.com/SecurityPolicy", "compute.googleapis.com/InstanceTemplate",
    "pubsub.googleapis.com/Subscription",
    "eventarc.googleapis.com/Trigger",
}


# --------------------------------------------------------------------------- helpers


def _region(location: str) -> str:
    loc = (location or "").lower()
    m = re.match(r"^([a-z]+-[a-z]+\d+)(-[a-z])?$", loc)
    return m.group(1) if m else ""


def _edge_label(icon: str) -> str:
    low = icon.lower()
    for needle, label in (
        ("sql", "SQL"), ("spanner", "SQL"), ("memorystore", "cache"), ("pubsub", "publish"),
        ("secretmanager", "secret"), ("keymanagement", "CMEK"), ("storage.storage", "objects"),
        ("bigquery", "query"), ("firestore", "documents"), ("bigtable", "rows"), ("filestore", "NFS"),
        ("compute.run", "HTTPS"), ("compute.functions", "HTTPS"), ("containerregistry", "image"),
    ):
        if needle in low:
            return label
    return ""


def _containers(spec: Any) -> list[Values]:
    out: list[Values] = []
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k in ("containers", "initContainers") and isinstance(v, list):
                out.extend(c for c in v if isinstance(c, dict))
            elif isinstance(v, (dict, list)):
                out.extend(_containers(v))
    elif isinstance(spec, list):
        for v in spec:
            out.extend(_containers(v))
    return out


def _annotations(data: Values) -> dict[str, str]:
    ann = dict(_str_dict(_get(data, "metadata", "annotations")))
    ann.update(_str_dict(_get(data, "spec", "template", "metadata", "annotations")))
    ann.update(_str_dict(_get(data, "spec", "template", "spec", "template", "metadata", "annotations")))
    return ann


def _workload_links(imp: _Importer, key: str, a: _Asset, containers: list[Values], region: str) -> None:
    for c in containers:
        imp.link(key, c.get("image"), "image", "image", "dotted", a, region)
        for env in c.get("env") or []:
            ref = _get(env, "valueFrom", "secretKeyRef", "name")
            if ref:
                imp.link(key, ref, "secret", "secret", "dotted", a, region)
            elif isinstance(env.get("value"), str):
                for part in re.split(r"[,\s;]+", env["value"]):
                    imp.link(key, part.strip(), "", "", "", a, region)
        for vol_secret in _get(c, "volumeMounts", default=[]) or []:
            _ = vol_secret  # secret volumes are listed under spec.volumes; handled by the caller


# --------------------------------------------------------------------------- per-type handlers


def _instance(imp: _Importer, a: _Asset) -> None:
    labels = a.data.get("labels") or {}
    if any(k.startswith(("goog-gke-node", "goog-dataproc", "goog-dataflow", "goog-composer")) for k in labels):
        return  # belongs to a GKE / Dataproc / Dataflow / Composer resource drawn elsewhere
    created_by = next((i.get("value") for i in _get(a.data, "metadata", "items", default=[]) or []
                       if i.get("key") == "created-by"), None)
    if created_by:
        mig = imp.resolve(created_by, a.project, _region(a.location), "")
        if mig is not None:
            imp.alias[a.path] = mig.path
            return
    nic = (a.data.get("networkInterfaces") or [{}])[0]
    place = imp.container_for(nic.get("subnetwork") or nic.get("network"), a.project, _region(a.location),
                              "subnetwork" if nic.get("subnetwork") else "network")
    machine = str(a.data.get("machineType") or "").rsplit("/", 1)[-1]
    public = any(nic.get("accessConfigs") or [] for nic in a.data.get("networkInterfaces") or [])
    imp.add_node(a, "gcp.compute.ComputeEngine", " ".join(x for x in (machine, "public IP" if public else "") if x), place)


def _mig(imp: _Importer, a: _Asset) -> None:
    if a.name.startswith("gke-"):
        return
    template = imp.resolve(a.data.get("instanceTemplate") or _get(a.data, "versions", default=[{}])[0].get(
        "instanceTemplate"), a.project, _region(a.location), "")
    nic = ((_get(template.data, "properties", "networkInterfaces", default=[]) if template else []) or [{}])[0]
    place = imp.container_for(nic.get("subnetwork") or nic.get("network"), a.project, _region(a.location),
                              "subnetwork" if nic.get("subnetwork") else "network")
    size = int(a.data.get("targetSize") or 0)
    machine = str(_get(template.data, "properties", "machineType", default="") if template else "").rsplit("/", 1)[-1]
    imp.add_node(a, "gcp.compute.ComputeEngine", " ".join(x for x in ("MIG", machine) if x), place, count=max(size, 1))


def _gke(imp: _Importer, a: _Asset) -> None:
    region = _region(a.location) or a.location
    sub = _get(a.data, "networkConfig", "subnetwork") or a.data.get("subnetwork")
    net = _get(a.data, "networkConfig", "network") or a.data.get("network")
    place = imp.container_for(sub, a.project, region, "subnetwork") if sub else imp.container_for(
        net, a.project, region, "network")
    mode = "Autopilot" if _get(a.data, "autopilot", "enabled") else f"{a.data.get('currentNodeCount', '?')} nodes"
    box = a.path.replace("/zones/", "/locations/")
    imp.containers[box] = _Container(box, 3, f"GKE: {a.name} ({mode})", place)
    imp.add_node(a, "gcp.compute.KubernetesEngine", "control plane", box)
    imp.alias[box] = a.path
    key_name = _get(a.data, "databaseEncryption", "keyName")
    if key_name and _get(a.data, "databaseEncryption", "state") == "ENCRYPTED":
        imp.link(a.path, key_name, "kms", "CMEK", "dotted", a)


_K8S_ICONS = {
    "apps.k8s.io/Deployment": "k8s.compute.Deploy",
    "apps.k8s.io/StatefulSet": "k8s.compute.STS",
    "batch.k8s.io/CronJob": "k8s.compute.Cronjob",
}


def _k8s_workload(imp: _Importer, a: _Asset) -> None:
    m = _K8S_PATH.search(a.path)
    if not imp.include_kubernetes or not m or _K8S_SYSTEM_NS.search(m.group(4)):
        return
    project, location, cluster, namespace = m.groups()
    cluster_box = f"projects/{project}/locations/{location}/clusters/{cluster}"
    ns_box = f"{cluster_box}/k8s/namespaces/{namespace}"
    if ns_box not in imp.containers:
        imp.containers[ns_box] = _Container(ns_box, 4, f"Namespace: {namespace}",
                                            cluster_box if cluster_box in imp.containers else imp.project_box(project))
    replicas = _get(a.data, "spec", "replicas")
    detail = {"apps.k8s.io/StatefulSet": "StatefulSet", "batch.k8s.io/CronJob": f"CronJob {_get(a.data, 'spec', 'schedule', default='')}".strip()}.get(a.type, "")
    a.labels = _str_dict(_get(a.data, "metadata", "labels"))
    imp.add_node(a, _K8S_ICONS[a.type], detail, ns_box, count=int(replicas) if isinstance(replicas, int) else 1)
    _workload_links(imp, a.path, a, _containers(a.data.get("spec")), _region(location))


def _run_service(imp: _Importer, a: _Asset) -> None:
    labels = _str_dict(_get(a.data, "metadata", "labels")) or a.labels
    if labels.get("goog-managed-by") == "cloudfunctions":
        return  # drawn as the function (cloudfunctions handler aliases it)
    region = _region(a.location) or labels.get("cloud.googleapis.com/location", "")
    ann = _annotations(a.data)
    ingress = ann.get("run.googleapis.com/ingress") or str(a.data.get("ingress", "")).replace("INGRESS_TRAFFIC_", "").lower()
    place = None
    nics = ann.get("run.googleapis.com/network-interfaces")
    v2_nics = _get(a.data, "template", "vpcAccess", "networkInterfaces")
    try:
        nic = (json.loads(nics) if nics else v2_nics or [{}])[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        nic = {}
    if nic.get("subnetwork") or nic.get("network"):
        place = imp.container_for(nic.get("subnetwork") or nic.get("network"), a.project, region,
                                  "subnetwork" if nic.get("subnetwork") else "network")
    is_job = a.type == "run.googleapis.com/Job"
    detail = "job" if is_job else {"internal": "internal", "internal-and-cloud-load-balancing": "internal + LB",
                                    "all": "public", "": ""}.get(ingress.replace("_", "-"), ingress)
    imp.add_node(a, "gcp.compute.Run", detail, place)
    a.labels = labels
    imp.nodes[a.path].labels = labels
    if ingress in ("all", "") and not is_job:
        imp.pending.append(("users", a.path, "", "HTTPS", "", a.project, region))
    connector = ann.get("run.googleapis.com/vpc-access-connector") or _get(a.data, "template", "vpcAccess", "connector")
    imp.link(a.path, connector, "connector", "VPC egress", "dashed", a, region)
    for inst in (ann.get("run.googleapis.com/cloudsql-instances") or "").split(","):
        imp.link(a.path, inst.strip(), "sql", "SQL", "", a, region)
    for inst in _get(a.data, "template", "volumes", default=[]) or []:
        for c in _get(inst, "cloudSqlInstance", "instances", default=[]) or []:
            imp.link(a.path, c, "sql", "SQL", "", a, region)
    for vol in _get(a.data, "spec", "template", "spec", "volumes", default=[]) or []:
        imp.link(a.path, _get(vol, "secret", "secretName"), "secret", "secret", "dotted", a, region)
    _workload_links(imp, a.path, a, _containers(a.data.get("spec") or a.data.get("template")), region)


def _function(imp: _Importer, a: _Asset) -> None:
    region = _region(a.location)
    svc = imp.resolve(_get(a.data, "serviceConfig", "service"), a.project, region, "run")
    gen = "gen2" if a.data.get("serviceConfig") else "gen1"
    runtime = a.data.get("runtime") or _get(a.data, "buildConfig", "runtime", default="")
    imp.add_node(a, "gcp.compute.Functions", " ".join(x for x in (runtime, gen) if x))
    if svc is not None:
        imp.alias[svc.path] = a.path
    cfg = a.data.get("serviceConfig") or a.data
    imp.link(a.path, cfg.get("vpcConnector"), "connector", "VPC egress", "dashed", a, region)
    for value in (cfg.get("environmentVariables") or {}).values():
        imp.link(a.path, value, "", "", "", a, region)
    for s in cfg.get("secretEnvironmentVariables") or []:
        imp.link(a.path, s.get("secret"), "secret", "secret", "dotted", a, region)
    trigger = a.data.get("eventTrigger") or {}
    topic = trigger.get("pubsubTopic") or (trigger.get("resource") if "topics/" in str(trigger.get("resource")) else None)
    if topic:
        imp.link(a.path, topic, "topic", "<-trigger", "dashed", a, region)
    for f in trigger.get("eventFilters") or []:
        if f.get("attribute") == "bucket":
            imp.link(a.path, f.get("value"), "bucket", "<-" + str(trigger.get("eventType", "event")).rsplit(".", 1)[-1],
                     "dashed", a, region)
    if (cfg.get("ingressSettings") or "").endswith("ALLOW_ALL") and (a.data.get("httpsTrigger") or not trigger):
        imp.pending.append(("users", a.path, "", "HTTPS", "", a.project, region))


def _simple(icon: str, detail: Callable[[_Asset], str] | str = "", network: Callable[[_Asset], Any] | None = None,
            kms: Callable[[_Asset], Any] | None = None, skip: Callable[[_Asset], bool] | None = None,
            label: Callable[[_Asset], str] | None = None) -> Callable[[_Importer, _Asset], None]:
    def handler(imp: _Importer, a: _Asset) -> None:
        if skip and skip(a):
            return
        place = None
        if network:
            ref = network(a)
            if ref:
                place = imp.container_for(ref, a.project, _region(a.location), "network")
        text = detail(a) if callable(detail) else detail
        imp.add_node(a, icon, text, place, label=label(a) if label else None)
        if kms:
            imp.link(a.path, kms(a), "kms", "CMEK", "dotted", a)
    return handler


def _sql(imp: _Importer, a: _Asset) -> None:
    net = _get(a.data, "settings", "ipConfiguration", "privateNetwork")
    place = imp.container_for(net, a.project, _region(a.location), "network") if net else None
    ha = "HA" if _get(a.data, "settings", "availabilityType") == "REGIONAL" else ""
    replica = "replica" if a.data.get("masterInstanceName") else ""
    imp.add_node(a, "gcp.database.SQL", " ".join(x for x in (a.data.get("databaseVersion", ""), ha, replica) if x), place)
    if a.data.get("masterInstanceName"):
        imp.link(a.path, a.data["masterInstanceName"], "sql", "<-replication", "dashed", a)
    imp.link(a.path, _get(a.data, "diskEncryptionConfiguration", "kmsKeyName"), "kms", "CMEK", "dotted", a)


def _redis(imp: _Importer, a: _Asset) -> None:
    net = a.data.get("authorizedNetwork") or next(
        (p.get("network") for p in a.data.get("pscConfigs") or [] if p.get("network")), None)
    place = imp.container_for(net, a.project, _region(a.location), "network") if net else None
    version = str(a.data.get("redisVersion") or "").replace("REDIS_", "Redis ").replace("_", ".")
    size = f"{a.data['memorySizeGb']} GB" if a.data.get("memorySizeGb") else ""
    tier = "HA" if a.data.get("tier") == "STANDARD_HA" else ""
    imp.add_node(a, "gcp.database.Memorystore", " ".join(x for x in (version, size, tier) if x), place)
    imp.link(a.path, a.data.get("customerManagedKey"), "kms", "CMEK", "dotted", a)


def _connector(imp: _Importer, a: _Asset) -> None:
    place = imp.container_for(a.data.get("network") or _get(a.data, "subnet", "name"), a.project,
                              _region(a.location), "network")
    imp.add_node(a, "gcp.network.VirtualPrivateCloud", f"VPC connector {a.data.get('ipCidrRange', '')}".strip(), place)


def _router(imp: _Importer, a: _Asset) -> None:
    nats = a.data.get("nats") or []
    if not nats:
        return
    place = imp.container_for(a.data.get("network"), a.project, _region(a.location), "network")
    imp.add_node(a, "gcp.network.NAT", f"Cloud NAT ({a.name})", place, label=nats[0].get("name") or a.name)


def _vpn(imp: _Importer, a: _Asset) -> None:
    place = imp.container_for(a.data.get("network"), a.project, _region(a.location), "network")
    imp.add_node(a, "gcp.network.VPN", "HA VPN", place)


def _subscription_links(imp: _Importer, a: _Asset) -> None:
    topic = a.data.get("topic")
    topic_node = imp.resolve(topic, a.project, "", "topic")
    src = topic_node.path if topic_node is not None else None
    if src is None:
        return
    push = _get(a.data, "pushConfig", "pushEndpoint")
    if push:
        imp.pending.append((src, push, "", f"push: {a.name}", "dashed", a.project, ""))
    bq = _get(a.data, "bigqueryConfig", "table")
    if bq:
        imp.pending.append((src, bq, "bigquery", f"BigQuery sub: {a.name}", "dashed", a.project, ""))
    gcs = _get(a.data, "cloudStorageConfig", "bucket")
    if gcs:
        imp.pending.append((src, gcs, "bucket", f"Cloud Storage sub: {a.name}", "dashed", a.project, ""))
    dlq = _get(a.data, "deadLetterPolicy", "deadLetterTopic")
    if dlq:
        imp.pending.append((src, dlq, "topic", "dead-letter", "dotted", a.project, ""))


def _eventarc(imp: _Importer, a: _Asset) -> None:
    region = _region(a.location)
    dest = _get(a.data, "destination", "cloudRun")
    target = imp.resolve(dest.get("service"), a.project, dest.get("region") or region, "run") if dest else imp.resolve(
        _get(a.data, "destination", "cloudFunction"), a.project, region, "function")
    if target is None:
        return
    filters = {f.get("attribute"): f.get("value") for f in a.data.get("eventFilters") or []}
    event = str(filters.get("type", "event")).rsplit(".", 2)[-2:]
    label = ".".join(event)[:40]
    source = None
    if filters.get("bucket"):
        source = imp.resolve(filters["bucket"], a.project, region, "bucket")
    if source is None:
        source = imp.resolve(_get(a.data, "transport", "pubsub", "topic"), a.project, region, "topic")
    if source is not None:
        imp.pending.append((source.path, target.path, "", label, "dashed", a.project, region))


def _scheduler(imp: _Importer, a: _Asset) -> None:
    schedule = str(a.data.get("schedule") or "")
    imp.add_node(a, "gcp.devtools.Scheduler", schedule)
    imp.link(a.path, _get(a.data, "httpTarget", "uri"), "", "scheduled", "", a)
    imp.link(a.path, _get(a.data, "pubsubTarget", "topicName"), "topic", "scheduled", "dashed", a)
    target = _get(a.data, "appEngineHttpTarget", "appEngineRouting", "service")
    if target:
        imp.link(a.path, target, "", "scheduled", "", a)


def _bucket_skip(a: _Asset) -> bool:
    return bool(_SYSTEM_BUCKET.search(a.name))


def _dns_skip(a: _Asset) -> bool:
    return a.data.get("visibility") == "PRIVATE" or str(a.data.get("name", "")).startswith("gke-")


def _secret_label(a: _Asset) -> str:
    return a.name


HANDLERS: dict[str, Callable[[_Importer, _Asset], None]] = {
    # order matters: containers and aliased resources first
    "container.googleapis.com/Cluster": _gke,
    "cloudfunctions.googleapis.com/Function": _function,
    "cloudfunctions.googleapis.com/CloudFunction": _function,
    "compute.googleapis.com/InstanceGroupManager": _mig,
    "compute.googleapis.com/Instance": _instance,
    "run.googleapis.com/Service": _run_service,
    "run.googleapis.com/Job": _run_service,
    "apps.k8s.io/Deployment": _k8s_workload,
    "apps.k8s.io/StatefulSet": _k8s_workload,
    "batch.k8s.io/CronJob": _k8s_workload,
    "sqladmin.googleapis.com/Instance": _sql,
    "alloydb.googleapis.com/Cluster": _simple(
        "gcp.database.SQL", "AlloyDB", network=lambda a: _get(a.data, "networkConfig", "network") or a.data.get("network")),
    "redis.googleapis.com/Instance": _redis,
    "redis.googleapis.com/Cluster": _redis,
    "spanner.googleapis.com/Instance": _simple(
        "gcp.database.Spanner", lambda a: str(a.data.get("config", "")).rsplit("/", 1)[-1]),
    "bigtableadmin.googleapis.com/Instance": _simple("gcp.database.Bigtable"),
    "firestore.googleapis.com/Database": _simple(
        "gcp.database.Firestore", lambda a: str(a.data.get("type", "")).replace("_", " ").lower()),
    "file.googleapis.com/Instance": _simple(
        "gcp.storage.Filestore", lambda a: str(a.data.get("tier", "")).lower(),
        network=lambda a: (a.data.get("networks") or [{}])[0].get("network")),
    "storage.googleapis.com/Bucket": _simple(
        "gcp.storage.Storage", lambda a: str(a.data.get("location", "")).lower(), skip=_bucket_skip,
        kms=lambda a: _get(a.data, "encryption", "defaultKmsKeyName")),
    "bigquery.googleapis.com/Dataset": _simple(
        "gcp.analytics.Bigquery", lambda a: str(a.data.get("location", "")).lower(),
        kms=lambda a: _get(a.data, "defaultEncryptionConfiguration", "kmsKeyName"),
        label=lambda a: _get(a.data, "datasetReference", "datasetId", default=a.name)),
    "pubsub.googleapis.com/Topic": _simple("gcp.analytics.Pubsub", "topic", kms=lambda a: a.data.get("kmsKeyName")),
    "pubsub.googleapis.com/Subscription": _subscription_links,
    "eventarc.googleapis.com/Trigger": _eventarc,
    "cloudscheduler.googleapis.com/Job": _scheduler,
    "cloudtasks.googleapis.com/Queue": _simple("gcp.devtools.Tasks", "queue"),
    "dataflow.googleapis.com/Job": _simple("gcp.analytics.Dataflow", lambda a: str(a.data.get("type", "")).replace(
        "JOB_TYPE_", "").lower(), skip=lambda a: str(a.data.get("currentState", "")) in (
            "JOB_STATE_DONE", "JOB_STATE_CANCELLED", "JOB_STATE_FAILED", "JOB_STATE_DRAINED")),
    "dataproc.googleapis.com/Cluster": _simple(
        "gcp.analytics.Dataproc", label=lambda a: a.data.get("clusterName") or a.name,
        network=lambda a: _get(a.data, "config", "gceClusterConfig", "subnetworkUri") or _get(
            a.data, "config", "gceClusterConfig", "networkUri")),
    "composer.googleapis.com/Environment": _simple(
        "gcp.analytics.Composer", network=lambda a: _get(a.data, "config", "nodeConfig", "network")),
    "aiplatform.googleapis.com/Endpoint": _simple("gcp.ml.VertexAI", "endpoint",
                                                  label=lambda a: a.data.get("displayName") or a.name),
    "apigateway.googleapis.com/Gateway": _simple("gcp.api.APIGateway",
                                                 label=lambda a: a.data.get("displayName") or a.name),
    "appengine.googleapis.com/Application": _simple("gcp.compute.AppEngine",
                                                    lambda a: str(a.data.get("locationId", ""))),
    "secretmanager.googleapis.com/Secret": _simple("gcp.security.SecretManager", label=_secret_label),
    "cloudkms.googleapis.com/CryptoKey": _simple("gcp.security.KeyManagementService", "KMS key"),
    "artifactregistry.googleapis.com/Repository": _simple(
        "gcp.devtools.ContainerRegistry", lambda a: f"Artifact Registry {str(a.data.get('format', '')).lower()}".strip()),
    "dns.googleapis.com/ManagedZone": _simple(
        "gcp.network.DNS", lambda a: str(a.data.get("dnsName", "")).rstrip("."), skip=_dns_skip),
    "vpcaccess.googleapis.com/Connector": _connector,
    "compute.googleapis.com/Router": _router,
    "compute.googleapis.com/VpnGateway": _vpn,
}


# --------------------------------------------------------------------------- spec building


def _safe_id(key: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.:-]", "_", key)
    if len(base) <= 64:
        return base
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"{base[-55:]}_{digest}".lstrip("_.:-") or digest


def _matches(node: _Node, projects: set[str], regions: list[str], labels: dict[str, str]) -> bool:
    if projects and node.project not in projects:
        return False
    if regions:
        loc = node.location.lower()
        regional = bool(re.match(r"^[a-z]+-[a-z]+\d+", loc))
        if regional and not any(loc.startswith(r.lower()) for r in regions):
            return False
    return all(node.labels.get(k) == v for k, v in labels.items())


def to_spec(
    doc: Any,
    title: str,
    direction: str = "LR",
    projects: Iterable[str] | None = None,
    regions: Iterable[str] | None = None,
    labels: dict[str, str] | None = None,
    include_neighbors: bool = True,
    include_kubernetes: bool = True,
) -> InventoryImport:
    imp = _Importer(_items(doc), include_kubernetes)
    imp.build()

    project_filter = {imp.num2id.get(p, p) for p in projects or []}
    region_filter = list(regions or [])
    label_filter = dict(labels or {})
    # Projects and regions are hard limits; labels select a seed that pulls in the resources it connects to.
    in_bounds = {k for k, n in imp.nodes.items() if k != "users" and _matches(n, project_filter, region_filter, {})}
    selected = {k for k in in_bounds if _matches(imp.nodes[k], set(), [], label_filter)}
    if label_filter and include_neighbors:
        selected |= {d for s, d in imp.edges if s in selected and d in in_bounds}
        selected |= {s for s, d in imp.edges if d in selected and s in in_bounds}
    if any(s == "users" and d in selected for s, d in imp.edges):
        selected.add("users")
    filtered_out = len([k for k in imp.nodes if k != "users"]) - len(selected - {"users"})

    for key in selected:  # every drawn node's project needs a box before ids are assigned
        imp.project_box(imp.nodes[key].project)
    ids = {k: _safe_id(k) for k in imp.nodes}
    cids = {k: _safe_id("box:" + k) for k in imp.containers}
    root = "gcp_root"

    def parent_of(c: _Container) -> str:
        return cids[c.parent] if c.parent in imp.containers else root

    spec_nodes = []
    for key in sorted(selected, key=lambda k: (imp.nodes[k].place or "", imp.nodes[k].label)):
        n = imp.nodes[key]
        cluster = cids.get(n.place) if n.place else cids.get(imp.project_box(n.project) or "", None)
        if key == "users":
            cluster = None
        elif cluster is None:
            cluster = root
        spec_nodes.append(NodeSpec(id=ids[key], type=n.icon, label=n.label, cluster=cluster))

    used = {n.cluster for n in spec_nodes if n.cluster}
    parents = {cids[k]: parent_of(c) for k, c in imp.containers.items()}
    parents[root] = None  # type: ignore[assignment]
    keep: set[str] = set()
    for cid in used:
        while cid is not None and cid not in keep:
            keep.add(cid)
            cid = parents.get(cid)
    clusters = [ClusterSpec(id=root, label="Google Cloud", style="rounded")] if root in keep else []
    for key, c in sorted(imp.containers.items(), key=lambda kv: (kv[1].rank, kv[1].label)):
        if cids[key] in keep:
            clusters.append(ClusterSpec(id=cids[key], label=c.label[:200], parent=parent_of(c),
                                        style="dashed" if c.rank == 0 else "rounded"))

    spec_edges = [
        EdgeSpec(source=ids[s], target=ids[d], label=label, style=style)
        for (s, d), (label, style) in sorted(imp.edges.items())
        if s in selected and d in selected
    ]
    spec = DiagramSpec(
        title=title,
        nodes=spec_nodes or [NodeSpec(id="empty", type="generic.blank.Blank", label="No mapped resources")],
        edges=spec_edges,
        clusters=clusters,
        direction=direction,  # type: ignore[arg-type]
        filename=re.sub(r"\W+", "_", title)[:60],
    )
    return InventoryImport(
        spec=spec,
        mapped=len(spec_nodes),
        boundaries=len(clusters),
        edges=len(spec_edges),
        skipped_types=dict(sorted(imp.skipped.items(), key=lambda kv: -kv[1])[:25]),
        filtered_out=filtered_out,
        external_hosts=sorted(imp.external_hosts)[:25],
    )
