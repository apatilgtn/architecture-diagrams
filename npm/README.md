# arch-diagram-mcp (npm launcher)

An MCP server that generates, reviews and imports cloud architecture diagrams for **AWS, Azure and
Google Cloud** with the official provider icons. It can also diagram infrastructure that already runs,
from Terraform or from a Google Cloud project's own inventory, keeping the real resource names.

This npm package is a small launcher for people who configure MCP servers with `npx`. It runs the
Python package [`arch-diagram-mcp`](https://pypi.org/project/arch-diagram-mcp/) at the same version,
through [uv](https://docs.astral.sh/uv/) (or pipx).

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) or pipx
- [Graphviz](https://graphviz.org/download/), which draws the diagrams

## Use

Claude Code:

```bash
claude mcp add architecture-diagrams -s user -- npx -y arch-diagram-mcp
```

Any client that takes a command:

```json
{
  "mcpServers": {
    "architecture-diagrams": { "command": "npx", "args": ["-y", "arch-diagram-mcp"] }
  }
}
```

If you have uv, you can skip Node entirely: `uvx arch-diagram-mcp`.

Documentation, samples and the developer guide:
https://github.com/apatilgtn/architecture-diagrams

Licensed under Apache-2.0.
