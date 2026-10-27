#!/usr/bin/env node
// Launches the Python MCP server published on PyPI as arch-diagram-mcp, pinned to this package's version.
// stdio is inherited, so the MCP protocol flows straight through. Prefers uvx, then pipx.
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const { version } = require("../package.json");

const args = process.argv.slice(2);
// ARCH_DIAGRAM_MCP_SPEC overrides the package spec, e.g. a local checkout path for testing.
const spec = process.env.ARCH_DIAGRAM_MCP_SPEC || `arch-diagram-mcp==${version}`;
const runners = [
  ["uvx", ["--from", spec, "arch-diagram-mcp", ...args]],
  ["pipx", ["run", "--spec", spec, "arch-diagram-mcp", ...args]],
];

function available(cmd) {
  const probe = spawnSync(cmd, ["--version"], { stdio: "ignore", shell: process.platform === "win32" });
  return probe.status === 0;
}

const runner = runners.find(([cmd]) => available(cmd));
if (!runner) {
  process.stderr.write(
    "arch-diagram-mcp needs uv (https://docs.astral.sh/uv/) or pipx to run the Python server, " +
      "plus Graphviz (https://graphviz.org/download/).\n" +
      "Install uv, then run this again. Or install directly: pipx install arch-diagram-mcp\n"
  );
  process.exit(1);
}

const [cmd, cmdArgs] = runner;
const child = spawn(cmd, cmdArgs, { stdio: "inherit", shell: process.platform === "win32" });
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 1);
});
child.on("error", (err) => {
  process.stderr.write(`arch-diagram-mcp: failed to start ${cmd}: ${err.message}\n`);
  process.exit(1);
});
