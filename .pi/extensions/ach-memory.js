// pi extension: one install wires up everything ach-memory needs.
//
//   pi install git:github.com/ackstorm/ach-memory
//
// `pi.extensions` and `pi.skills` in package.json give pi this file and the
// skill. MCP is not part of a pi package, so the extension writes the server
// entry into pi's own mcp.json once, pointing at this checkout -- the same
// no-pin arrangement the other three hosts get from their plugin root.
//
// pi has no compaction event (before_agent_start, before_provider_request,
// before_provider_headers, after_provider_response), so there is no PreCompact
// equivalent here; see docs/hosts.md.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function sessionStart() {
  try {
    return execFileSync(path.join(root, "hooks", "scripts", "session-start.sh"), {
      encoding: "utf8",
      timeout: 10000,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

/** Idempotent: rewrites the entry only when it does not already match. */
function ensureMcpServer() {
  const agentDir = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
  const file = path.join(agentDir, "mcp.json");
  const entry = {
    command: "uvx",
    args: ["--from", root, "ach-memory", "mcp", "--url",
           process.env.ACH_MEMORY_URL || "http://localhost:8000/mcp/"],
  };
  try {
    const cfg = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
    cfg.mcpServers = cfg.mcpServers || {};
    if (JSON.stringify(cfg.mcpServers["ach-memory"]) === JSON.stringify(entry)) return;
    cfg.mcpServers["ach-memory"] = entry;
    fs.mkdirSync(agentDir, { recursive: true });
    fs.writeFileSync(file, JSON.stringify(cfg, null, 2) + "\n");
  } catch {
    // A pi session must start whether or not memory can be wired up.
  }
}

export default function (pi) {
  ensureMcpServer();
  const context = sessionStart();
  if (!context || typeof pi?.on !== "function") return;
  pi.on("before_agent_start", async (event) => {
    if (typeof event?.systemPrompt !== "string" || event.systemPrompt.includes(context)) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${context}` };
  });
}
