from arch_diagram_mcp import terraform


def _res(address, type_, name, values=None, **extra):
    return {"address": address, "mode": "managed", "type": type_, "name": name, "values": values or {}, **extra}


def _cfg(address, type_, name, refs=()):
    return {
        "address": address,
        "mode": "managed",
        "type": type_,
        "name": name,
        "expressions": {"x": {"references": list(refs)}},
    }


AWS_PLAN = {
    "planned_values": {
        "root_module": {
            "resources": [
                _res("aws_vpc.main", "aws_vpc", "main", {"cidr_block": "10.0.0.0/16", "tags": {"Name": "prod"}}),
                _res("aws_subnet.public[0]", "aws_subnet", "public", {"cidr_block": "10.0.1.0/24"}),
                _res("aws_subnet.public[1]", "aws_subnet", "public", {"cidr_block": "10.0.2.0/24"}),
                _res("aws_subnet.private", "aws_subnet", "private", {"cidr_block": "10.0.10.0/24"}),
                _res("aws_lb.web", "aws_lb", "web", {"load_balancer_type": "application"}),
                _res("aws_lb_listener.https", "aws_lb_listener", "https"),
                _res("aws_lb_target_group.app", "aws_lb_target_group", "app"),
                _res("aws_lb_target_group_attachment.app[0]", "aws_lb_target_group_attachment", "app"),
                _res("aws_instance.app[0]", "aws_instance", "app"),
                _res("aws_instance.app[1]", "aws_instance", "app"),
                _res("aws_db_instance.db", "aws_db_instance", "db"),
                _res("aws_security_group.sg", "aws_security_group", "sg"),
            ],
            "child_modules": [
                {"resources": [_res("module.storage.aws_s3_bucket.assets", "aws_s3_bucket", "assets", {"bucket": "assets"})]}
            ],
        }
    },
    "configuration": {
        "root_module": {
            "resources": [
                _cfg("aws_vpc.main", "aws_vpc", "main"),
                _cfg("aws_subnet.public", "aws_subnet", "public", ["aws_vpc.main.id", "aws_vpc.main"]),
                _cfg("aws_subnet.private", "aws_subnet", "private", ["aws_vpc.main.id"]),
                _cfg("aws_lb.web", "aws_lb", "web", ["aws_subnet.public", "aws_security_group.sg.id"]),
                _cfg("aws_lb_listener.https", "aws_lb_listener", "https", ["aws_lb.web.arn", "aws_lb_target_group.app.arn"]),
                _cfg("aws_lb_target_group.app", "aws_lb_target_group", "app", ["aws_vpc.main.id"]),
                _cfg(
                    "aws_lb_target_group_attachment.app",
                    "aws_lb_target_group_attachment",
                    "app",
                    ["aws_lb_target_group.app.arn", "aws_instance.app[0].id", "aws_instance.app"],
                ),
                _cfg(
                    "aws_instance.app",
                    "aws_instance",
                    "app",
                    ["aws_subnet.private.id", "aws_security_group.sg.id", "module.storage.bucket_arn", "aws_db_instance.db.address"],
                ),
                _cfg("aws_db_instance.db", "aws_db_instance", "db", ["aws_subnet.private.id", "var.password"]),
                _cfg("aws_security_group.sg", "aws_security_group", "sg", ["aws_vpc.main.id"]),
            ],
            "module_calls": {
                "storage": {
                    "module": {
                        "resources": [_cfg("aws_s3_bucket.assets", "aws_s3_bucket", "assets")],
                        "outputs": {"bucket_arn": {"expression": {"references": ["aws_s3_bucket.assets.arn", "aws_s3_bucket.assets"]}}},
                    }
                }
            },
        }
    },
}


def test_aws_plan_builds_nested_boundaries_and_edges():
    imp = terraform.to_spec(AWS_PLAN, "AWS plan")
    spec = imp.spec
    nodes = {n.id: n for n in spec.nodes}
    clusters = {c.id: c for c in spec.clusters}

    assert nodes["aws_lb.web"].type == "aws.network.ALB"
    assert nodes["aws_instance.app"].label == "app (x2)"
    assert nodes["aws_instance.app"].cluster == "aws_subnet.private"
    assert clusters["aws_subnet.private"].parent == "aws_vpc.main"
    assert clusters["aws_vpc.main"].parent == "provider_aws"
    assert clusters["aws_vpc.main"].label.startswith("VPC: prod 10.0.0.0/16")
    # Both public subnets come from one `count` resource, so they form a single boundary.
    assert nodes["aws_lb.web"].cluster == "aws_subnet.public"

    edges = {(e.source, e.target): e for e in spec.edges}
    assert ("aws_instance.app", "aws_db_instance.db") in edges
    # Module output resolved to the bucket inside the module.
    assert ("aws_instance.app", "module.storage.aws_s3_bucket.assets") in edges
    # LB -> instance inferred through listener / target group / attachment.
    assert edges[("aws_lb.web", "aws_instance.app")].style == "dashed"
    assert imp.inferred_edges >= 1
    assert imp.skipped_types == {"aws_security_group": 1}


def test_azure_vm_lands_in_subnet_via_nic_and_identity_edge():
    def r(addr, type_, values=None):
        return _res(addr, type_, addr.split(".")[-1], values)

    doc = {
        "planned_values": {
            "root_module": {
                "resources": [
                    r("azurerm_resource_group.rg", "azurerm_resource_group", {"name": "rg-prod", "location": "eastus2"}),
                    r("azurerm_virtual_network.vnet", "azurerm_virtual_network", {"name": "vnet", "address_space": ["10.1.0.0/16"]}),
                    r("azurerm_subnet.app", "azurerm_subnet", {"name": "snet-app"}),
                    r("azurerm_network_interface.nic", "azurerm_network_interface"),
                    r("azurerm_linux_virtual_machine.vm", "azurerm_linux_virtual_machine", {"name": "vm-app"}),
                    r("azurerm_user_assigned_identity.mi", "azurerm_user_assigned_identity", {"name": "mi-app"}),
                    r("azurerm_key_vault.kv", "azurerm_key_vault", {"name": "kv-prod"}),
                    r("azurerm_role_assignment.kv", "azurerm_role_assignment"),
                ]
            }
        },
        "configuration": {
            "root_module": {
                "resources": [
                    _cfg("azurerm_virtual_network.vnet", "azurerm_virtual_network", "vnet", ["azurerm_resource_group.rg.name"]),
                    _cfg("azurerm_subnet.app", "azurerm_subnet", "app", ["azurerm_virtual_network.vnet.name", "azurerm_resource_group.rg.name"]),
                    _cfg("azurerm_network_interface.nic", "azurerm_network_interface", "nic", ["azurerm_subnet.app.id"]),
                    _cfg(
                        "azurerm_linux_virtual_machine.vm",
                        "azurerm_linux_virtual_machine",
                        "vm",
                        ["azurerm_network_interface.nic.id", "azurerm_resource_group.rg.name", "azurerm_user_assigned_identity.mi.id"],
                    ),
                    _cfg("azurerm_key_vault.kv", "azurerm_key_vault", "kv", ["azurerm_resource_group.rg.name"]),
                    _cfg(
                        "azurerm_role_assignment.kv",
                        "azurerm_role_assignment",
                        "kv",
                        ["azurerm_key_vault.kv.id", "azurerm_user_assigned_identity.mi.principal_id"],
                    ),
                ]
            }
        },
    }
    spec = terraform.to_spec(doc, "Azure").spec
    nodes = {n.id: n for n in spec.nodes}
    clusters = {c.id: c for c in spec.clusters}
    assert nodes["azurerm_linux_virtual_machine.vm"].cluster == "azurerm_subnet.app"
    assert clusters["azurerm_subnet.app"].parent == "azurerm_virtual_network.vnet"
    assert clusters["azurerm_virtual_network.vnet"].parent == "azurerm_resource_group.rg"
    assert nodes["azurerm_key_vault.kv"].cluster == "azurerm_resource_group.rg"
    edges = {(e.source, e.target) for e in spec.edges}
    assert ("azurerm_user_assigned_identity.mi", "azurerm_key_vault.kv") in edges


def test_raw_tfstate_v4():
    doc = {
        "version": 4,
        "resources": [
            {"mode": "managed", "type": "google_compute_network", "name": "vpc", "instances": [{"attributes": {"name": "prod-vpc"}}]},
            {
                "mode": "managed",
                "type": "google_cloud_run_v2_service",
                "name": "api",
                "instances": [{"attributes": {"name": "api"}, "dependencies": ["google_sql_database_instance.db"]}],
            },
            {"mode": "managed", "type": "google_sql_database_instance", "name": "db", "instances": [{"attributes": {"name": "db"}}]},
            {"mode": "data", "type": "google_project", "name": "p", "instances": [{}]},
        ],
    }
    spec = terraform.to_spec(doc, "GCP state").spec
    assert {n.type for n in spec.nodes} == {"gcp.compute.Run", "gcp.database.SQL"}
    assert ("google_cloud_run_v2_service.api", "google_sql_database_instance.db") in {(e.source, e.target) for e in spec.edges}
    # The VPC has no members, so it is dropped rather than drawn empty.
    assert all(c.id != "google_compute_network.vpc" for c in spec.clusters)


def test_every_mapped_icon_exists():
    from arch_diagram_mcp import catalog

    for tf_type, icon in terraform.NODE_TYPES.items():
        if callable(icon):
            continue
        catalog.resolve(icon)  # raises if missing
    for fn, values in [
        (terraform._lb_icon, {"load_balancer_type": "network"}),
        (terraform._lb_icon, {}),
        (terraform._ecs_service_icon, {"launch_type": "FARGATE"}),
        (terraform._ecs_service_icon, {}),
        (terraform._cognitive_icon, {"kind": "OpenAI"}),
        (terraform._cognitive_icon, {}),
    ]:
        catalog.resolve(fn(values))


def test_unrecognised_document():
    import pytest

    with pytest.raises(ValueError, match="Unrecognised"):
        terraform.to_spec({"foo": 1}, "x")
