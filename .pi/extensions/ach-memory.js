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
// pi's Stop. There is no PreCompact equivalent: `session_before_compact` can
// cancel or replace a compaction, not add to its prompt; see docs/hosts.md.
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
  // `before_agent_start` fires per user prompt: the standing context goes into
  // the system prompt once, and the idle nudge -- printed only when nothing was
  // retained for this checkout in the last 30 minutes -- rides in as a message.
  const context = hook("session-start.sh");
  pi.on("before_agent_start", async (event, ctx) => {
    const result = {};
    if (context && typeof event?.systemPrompt === "string" && !event.systemPrompt.includes(context)) {
      result.systemPrompt = `${event.systemPrompt}\n\n${context}`;
    }
    const nudge = hook("idle-nudge.sh", ctx?.sessionManager?.getSessionId?.() || "unknown");
    if (nudge) result.message = { customType: "ach-memory-nudge", content: nudge, display: false };
    return Object.keys(result).length ? result : undefined;
  });
}
