# Developer guide: using the architecture-diagrams MCP server

This guide is for developers who want to create cloud architecture diagrams from their AI assistant
(Claude Code, VS Code, Cursor, Claude Desktop). You describe the system, or hand over Terraform, and the
assistant produces a diagram with the official AWS, Azure and Google Cloud icons, plus an architecture
review.

You don't call the tools yourself. You ask your assistant in plain language, and it picks the tools.

- [1. Choose how to connect](#1-choose-how-to-connect)
- [2. Option A: run it on your machine](#2-option-a-run-it-on-your-machine)
- [3. Option B: connect to your team's shared server](#3-option-b-connect-to-your-teams-shared-server)
- [4. Check that it works](#4-check-that-it-works)
- [5. Everyday use, with example prompts](#5-everyday-use-with-example-prompts)
- [6. Getting good diagrams](#6-getting-good-diagrams)
- [7. Working with the output](#7-working-with-the-output)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Do and don't](#9-do-and-dont)

## 1. Choose how to connect

| | **A. On your machine** (stdio) | **B. Shared team server** (HTTP) |
|---|---|---|
| Best for | Trying it out; working alone; offline | Teams; nothing to install per developer |
| You install | Python 3.11+, Graphviz, this package | Nothing. You get a URL and a token from your platform team |
| Diagrams arrive as | Files on your disk, plus a preview in chat | Download links that expire (1 hour by default), plus a preview in chat |
| Terraform from a file path | Yes, inside the allowed folders | No. Paste the JSON instead |

If your team already runs a shared server, use **B**.

## 2. Option A: run it on your machine

### Install

1. Install Graphviz, which draws the diagrams:

   ```powershell
   winget install Graphviz.Graphviz      # Windows
   brew install graphviz                 # macOS
   sudo apt install graphviz             # Debian / Ubuntu
   ```

2. Install the server from GitHub. `pipx` keeps it in its own environment and puts the
   `arch-diagram-mcp` command on your PATH:

   ```bash
   pipx install git+https://github.com/apatilgtn/architecture-diagrams.git
   ```

   Plain `pip install git+https://github.com/apatilgtn/architecture-diagrams.git` into a virtual
   environment works too. To upgrade later, run `pipx upgrade arch-diagram-mcp`.

3. Pick a folder for the diagrams, for example `~/architecture-diagrams`. That's the default if you
   don't set `ARCH_DIAGRAM_OUTPUT_DIR`.

### Add it to your assistant

Use the client you have. In each case the server is the `arch-diagram-mcp` command. If your client can't
find it, use the full path that `pipx` printed during the install.

**Claude Code** (CLI, and the VS Code / JetBrains extensions, which share its configuration):

```bash
claude mcp add architecture-diagrams -s user \
  -e ARCH_DIAGRAM_OUTPUT_DIR="$HOME/architecture-diagrams" \
  -e ARCH_DIAGRAM_INPUT_ROOTS="$HOME/code" \
  -- arch-diagram-mcp
```

`-s user` makes it available in every project. `ARCH_DIAGRAM_INPUT_ROOTS` lists the folders the server
may read Terraform files from. Separate several folders with `:` on macOS/Linux or `;` on Windows.

**VS Code** (GitHub Copilot agent mode). Create `.vscode/mcp.json` in your workspace, or run
*MCP: Open User Configuration* to add it for all workspaces:

```json
{
  "servers": {
    "architecture-diagrams": {
      "type": "stdio",
      "command": "arch-diagram-mcp",
      "env": {
        "ARCH_DIAGRAM_OUTPUT_DIR": "${userHome}/architecture-diagrams",
        "ARCH_DIAGRAM_INPUT_ROOTS": "${workspaceFolder}"
      }
    }
  }
}
```

**Cursor**: `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "architecture-diagrams": {
      "command": "arch-diagram-mcp",
      "env": { "ARCH_DIAGRAM_OUTPUT_DIR": "/Users/<you>/architecture-diagrams" }
    }
  }
}
```

**Claude Desktop**: *Settings → Developer → Edit Config* opens `claude_desktop_config.json`. Add the
same `mcpServers` block as for Cursor, then restart Claude Desktop.

## 3. Option B: connect to your team's shared server

Your platform team gives you two things:

- the server URL, ending in `/mcp`, for example `https://diagrams.example.com/mcp`;
- either a **personal token**, or the instruction to **sign in with your company account** (OIDC).

Treat the token like a password. Don't commit it, and don't paste it into chat. The examples below read
it from an environment variable or ask for it at startup.

**Claude Code:**

```bash
export ARCH_DIAGRAMS_TOKEN=<your token>      # PowerShell: $env:ARCH_DIAGRAMS_TOKEN = "<your token>"
claude mcp add --transport http architecture-diagrams https://diagrams.example.com/mcp \
  --header "Authorization: Bearer $ARCH_DIAGRAMS_TOKEN" -s user
```

**VS Code.** The `inputs` block makes VS Code ask for the token once and store it securely, instead of
keeping it in the file:

```json
{
  "inputs": [
    { "type": "promptString", "id": "arch-diagrams-token", "description": "Architecture diagrams token", "password": true }
  ],
  "servers": {
    "architecture-diagrams": {
      "type": "http",
      "url": "https://diagrams.example.com/mcp",
      "headers": { "Authorization": "Bearer ${input:arch-diagrams-token}" }
    }
  }
}
```

**Cursor:**

```json
{
  "mcpServers": {
    "architecture-diagrams": {
      "url": "https://diagrams.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:ARCH_DIAGRAMS_TOKEN}" }
    }
  }
}
```

**Claude Desktop.** Its config file only starts local commands, so bridge to the remote server with
`mcp-remote` (needs Node.js):

```json
{
  "mcpServers": {
    "architecture-diagrams": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://diagrams.example.com/mcp", "--header", "Authorization:${AUTH}"],
      "env": { "AUTH": "Bearer <your token>" }
    }
  }
}
```

**If your team uses company sign-in (Entra ID).** Your token is a normal access token for the
diagrams API. Get one with the Azure CLI and use it in place of the personal token above:

```bash
az account get-access-token --resource api://arch-diagrams --query accessToken -o tsv
```

These tokens expire after about an hour. With sign-in, what you can do depends on your role:

| Role | Can |
|---|---|
| Reader | Search icons, browse templates, validate and review specs |
| Author | All of the above, plus render diagrams and import Terraform or cloud inventories |

If the server sits behind Cloud Run IAM or another gateway, your platform team will tell you about the
extra header it needs. [CLOUD_TESTING.md](CLOUD_TESTING.md) shows the Cloud Run setup.

## 4. Check that it works

Restart your assistant or reload its MCP servers, then ask:

> Call server_info for the architecture-diagrams server.

A healthy answer shows the version, a Graphviz version, about 1,800 icons and 6 templates. On a shared
server, the `caller` field shows the name the server knows you by. That name is recorded in the audit log.

In Claude Code, `/mcp` also lists the server and whether it is connected.

## 5. Everyday use, with example prompts

### Start from a reference architecture

Six templates are included. The quickest good result is to start from one and change it:

> Show me the architecture-diagrams templates for Azure.

> Take the azure_aks_microservices template, rename the services to orders, payments and catalog,
> add Azure Cache for Redis, and render it as PNG and SVG.

See all six rendered in the [README gallery](../README.md#samples).

### Describe a system in plain language

> Draw our AWS architecture: users hit CloudFront and a WAF, then an ALB in front of ECS Fargate
> services (orders-api, payments-api) in private subnets across two AZs. Orders writes to Aurora
> PostgreSQL, payments publishes to SQS, a Lambda consumes the queue and writes to DynamoDB.
> Secrets in Secrets Manager, logs in CloudWatch. Group by VPC, public/private subnet and AZ.

The more you say about **boundaries** (account, subscription, project, VPC/VNet, subnet, AZ, cluster)
and **connections** (protocol, sync or async), the closer the first render will be.

### Import from Terraform

Run this in your Terraform working directory:

```bash
terraform plan -out plan.out
terraform show -json plan.out > plan.json
```

Then ask:

> Create a diagram from plan.json with diagram_from_terraform, titled "Payments platform (prod)".

- **Local server:** give the path. The file must be inside `ARCH_DIAGRAM_INPUT_ROOTS`.
- **Shared server:** reading paths is disabled, so the assistant sends the JSON content instead.

A **plan** gives a better diagram than **state**, because it includes the references between
resources. The result also lists the resource types it skipped, so you can see what's missing.

### Diagram what's already running in Google Cloud

Use this when services are already deployed, whether they were created in the console, with `gcloud`,
by scripts or by any IaC tool. Google's Cloud Asset Inventory knows everything in the project, and the
server turns its export into a diagram with the **real resource names**.

1. Export the project's inventory. You need `roles/cloudasset.viewer` on the project, and the Cloud
   Asset API enabled (`gcloud services enable cloudasset.googleapis.com`).

   ```bash
   gcloud asset list --project <project-id> --content-type resource \
     --asset-types "$(arch-diagram-mcp --gcp-asset-types)" --format json > assets.json
   ```

   PowerShell:

   ```powershell
   gcloud asset list --project <project-id> --content-type resource `
     --asset-types (arch-diagram-mcp --gcp-asset-types) --format json | Out-File -Encoding utf8 assets.json
   ```

   `--gcp-asset-types` prints the resource types the server understands, which keeps the file small.
   If you connect to a shared server and don't have the command installed, ask your platform team for
   the list, or leave `--asset-types` out (the export is then several times larger).

2. Ask your assistant:

   > Create a diagram of assets.json with diagram_from_inventory, titled "Payments platform (prod)",
   > only resources labelled app=payments.

What you get:
- Project › VPC › Subnet boxes, with GKE clusters and their namespaces inside, and each resource
  under its real name. A second line adds detail such as the database version or replica count.
- Connections the configuration proves: load balancer › backends (with path rules, CDN and Cloud
  Armor), Cloud Run › Cloud SQL, secrets, Pub/Sub, Redis, push subscriptions, Scheduler targets,
  Eventarc triggers, VPC connectors, and URLs in environment variables matched to the services that
  own them.

Narrowing it down:

| Ask for | Effect |
|---|---|
| `labels: {"app": "payments"}` | Only resources with that label, plus what they connect to (turn off with `include_neighbors: false`) |
| `regions: ["europe-west1"]` | Only that region; global resources such as topics and load balancers stay |
| `projects: [...]` | Only those projects, when the export covers several (for example `--folder` or `--organization`) |
| `include_kubernetes: false` | GKE clusters without their workloads |

The diagram can only show what the configuration contains. If `orders` calls `payments` through a
hostname that is hard-coded in the application, ask the assistant to add that edge to the returned
spec and render it again. Hostnames outside Google that appear in environment variables are listed as
`external_hosts`, so you can decide whether to show them.

### Review an architecture

> Review this architecture and list the findings by severity. Fix the ones that are just missing
> components in the diagram, and explain the rest.

The review scores 0–100 against Well-Architected style rules covering security, reliability,
operations and performance. Examples are a database reachable from the internet, a single region, or
no WAF, CDN, monitoring or backups. It only sees what the diagram shows, so treat findings as questions
for the design, not as proof of a misconfiguration.

### Change the look

> Same diagram, but top-to-bottom, dark theme, with orthogonal edges.

> Make the Service Bus edges dashed and label them "async".

Options:
- `direction`: `LR`, `TB`, `RL` or `BT`;
- `theme`: `light` or `dark`;
- `curve_style`: `spline`, `ortho`, `polyline` or `curved`;
- formats: `png`, `svg`, `pdf`, `jpg`.

### Guided design

The server includes a prompt, `design_architecture`. It walks the assistant through choosing a
template, finding icons, validating, rendering and reviewing. In Claude Code it appears as
`/mcp__architecture-diagrams__design_architecture`. Other clients list MCP prompts in their prompt or
attachment menu.

## 6. Getting good diagrams

- **Use the provider's own grouping.** Nest boundaries the way the provider draws them:
  - AWS: Cloud/Account › Region › VPC › Subnet/AZ;
  - Azure: Subscription › Resource Group or VNet › Subnet;
  - GCP: Org › Project › VPC › Subnet.
- **Keep it to one level of detail.** About 15–40 nodes reads well. Make separate diagrams for the
  overview, the network, and a single service, rather than one huge one.
- **Label edges with what flows**, such as `HTTPS`, `gRPC`, `OrderPlaced` or `CDC`. Use dashed edges
  for asynchronous or control-plane traffic.
- **Exact icons.** If the assistant picks a generic icon, ask it to `search_icons` for the service.
  Aliases work: `aks`, `ecs`, `s3`, `cloud run`, `cosmos`, `redis`.
- **Validate first.** The assistant should call `validate_diagram` before rendering. Errors come with
  suggestions, for example an unknown icon id plus the closest matches.
- **Keep the spec.** The diagram is generated from a JSON spec. Ask the assistant to save it next to
  your code (for example `docs/architecture/payments.json`), so the next person can change and
  re-render it instead of redrawing it.

## 7. Working with the output

**On your machine.** Files go to your output folder, named `<title>_<date>_<time>_<id>.<ext>`. The
`.dot` Graphviz source is kept too, for hand-editing. The chat shows a PNG preview.

**On a shared server.** You get download **links**, not files. They expire after the time your team
configured (1 hour by default). Download what you want to keep; if a link has expired, ask for the
diagram again. The links are signed, so changing or reusing them after expiry fails.

**Which format to use:**

| Use | Format |
|---|---|
| README, wiki, Confluence, Markdown docs | **SVG**. It's sharp at any size, icons are embedded, and it's one self-contained file |
| Slides, chat, tickets | PNG |
| Printing, design reviews, sign-off | PDF |

**Diagrams as code.** Commit the spec JSON and the SVG together. When the architecture changes, update
the spec and re-render it, so the diagram and the change are reviewed in the same pull request.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The assistant doesn't list the tools | The server didn't start. In Claude Code run `/mcp`; in VS Code open *MCP: List Servers* and view the output. Check that `arch-diagram-mcp` runs in a terminal. |
| `Graphviz 'dot' was not found` | Install Graphviz (section 2), then restart your assistant so it picks up the new PATH. |
| `Unknown icon type '...'` | Ask the assistant to use the suggested ids, or to `search_icons` for the service. |
| `401 Unauthorized` | The token is missing, wrong or expired. Company sign-in tokens last about an hour; get a new one. |
| `Forbidden: '<you>' needs the '<role>' role ...` | Your sign-in lacks that role. Reader can't render or import Terraform or inventories; ask your platform team for the Author role. |
| `421 Misdirected Request` | The URL's hostname isn't on the server's allow-list. Use exactly the URL your platform team gave you. |
| Download link returns 400/403 | The link expired or was changed. Ask for the diagram again. |
| `` `path` is disabled over HTTP `` | Shared servers don't read files from disk. Have the assistant send the Terraform JSON content instead. |
| `Path is outside the allowed input roots` | Local server: add the folder to `ARCH_DIAGRAM_INPUT_ROOTS` and restart. |
| Diagram is too big or rejected by limits | Split it (section 6). The default limits are 300 nodes, 800 edges and 100 clusters. |
| `The inventory maps to N resources, above the ...-node limit` | Narrow it with `labels`, `regions` or `projects`, or export one project at a time. |
| `gcloud asset list` fails with `PERMISSION_DENIED` or "API not enabled" | You need `roles/cloudasset.viewer`, and the project needs `cloudasset.googleapis.com` enabled. |
| `INVALID_ARGUMENT: No RESOURCE found that matches asset type ...` | Your `--asset-types` list contains a name the API doesn't know. Use `arch-diagram-mcp --gcp-asset-types` from the latest version. |
| A resource you expected is missing | Check `unmapped_asset_types` in the result, and whether a filter removed it (`filtered_out`). Resources created in the last few minutes may not be in the inventory yet. |
| Text looks blurry on Windows | Upgrade to the latest version; it renders anti-aliased text with Segoe UI. |

## 9. Do and don't

**Do**
- Start from a template when one is close.
- Ask for a review before sharing a design, and act on the critical and high findings.
- Keep the spec in your repository next to the code it describes.

**Don't**
- Put secrets, keys, connection strings or customer data in labels. Diagrams get shared widely.
- Send a full Terraform **state** file to a shared server unless you need to. State files can contain
  secrets. A plan (`terraform show -json plan.out`) is better for diagrams and usually safer, though
  plans can contain sensitive values too.
- Send an inventory export to a shared server without thinking about what's in it. It contains your
  resources' configuration, including the **values of environment variables** of Cloud Run services
  and Kubernetes workloads. Secrets should be in Secret Manager (the export then only names them), but
  if any are set as plain env vars, they travel with the file. The server never puts env values into
  diagrams.
- Share download links outside your team. Anyone with an unexpired link can open the file.

For server setup, authentication modes, storage backends and every configuration option, see the
[README](../README.md).
