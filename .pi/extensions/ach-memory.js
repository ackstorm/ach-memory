// pi extension: one install wires up everything ach-memory needs.
//
//   pi install git:github.com/ackstorm/ach-memory
//
// `pi.extensions` and `pi.skills` in package.json give pi this file and the
// skill. MCP is not part of a pi package, so the extension writes the server
// entry into pi's own mcp.json once, pointing at this checkout -- the same
// no-pin arrangement the other three hosts get from their plugin root.
//
// `before_agent_start` carries the standing context and `agent_settled` is
// pi's Stop. No PreCompact equivalent is wired here; see docs/hosts.md.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function hook(script, ...args) {
  try {
    return execFileSync(path.join(root, "hooks", "scripts", script), args, {
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
  if (typeof pi?.on !== "function") return;
  const context = hook("session-start.sh");
  if (context) {
    pi.on("before_agent_start", async (event) => {
      if (typeof event?.systemPrompt !== "string" || event.systemPrompt.includes(context)) return;
      return { systemPrompt: `${event.systemPrompt}\n\n${context}` };
    });
  }
  // pi's Stop: `agent_settled` fires once the run is fully over (no retry,
  // compaction or queued continuation pending). The throttled nudge goes in as
  // a message that starts one more turn. That turn settles too: `nudging` is
  // the `stop_hook_active` of this host, so it ends there instead of looping.
  let nudging = false;
  pi.on("agent_settled", async (_event, ctx) => {
    if (nudging) {
      nudging = false;
      return;
    }
    const nudge = hook("retain-nudge.sh", ctx?.sessionManager?.getSessionId?.() || "unknown");
    if (!nudge) return;
    nudging = true;
    await pi.sendMessage(
      { customType: "ach-memory-nudge", content: nudge, display: false },
      { triggerTurn: true },
    );
  });
}
