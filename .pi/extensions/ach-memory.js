// pi extension: one install wires up everything ach-memory needs.
//
//   pi install git:github.com/ackstorm/ach-memory
//
// `pi.extensions` and `pi.skills` in package.json give pi this file and the
// skill. MCP is not part of a pi package, so the extension registers the server
// for the session, pointing at this checkout -- the same no-pin arrangement the
// other hosts get from their plugin root. pi before `registerMcpServer` gets
// the entry written into its user-wide mcp.json instead.
//
// pi loads every install it is given, a project's before the user's, so one
// process can hold two copies of this file. The first one loaded runs; the
// others do nothing, or the context would load twice and the server clash.
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

const server = { command: "uvx", args: ["--from", root, "python", "-m", "memory.cli", "mcp"] };

/** Sets this extension's entry in pi's user-wide mcp.json; `null` removes one it wrote. */
function writeMcpEntry(entry) {
  const agentDir = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
  const file = path.join(agentDir, "mcp.json");
  try {
    const cfg = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
    cfg.mcpServers = cfg.mcpServers || {};
    const current = cfg.mcpServers["ach-memory"];
    if (JSON.stringify(current) === JSON.stringify(entry ?? undefined)) return;
    if (entry) {
      cfg.mcpServers["ach-memory"] = entry;
    } else {
      // Only an entry of this extension's shape: a hand-written one is the user's.
      const ours = current?.command === "uvx" && current.args?.[0] === "--from"
        && ["ach-memory mcp", "python -m memory.cli mcp"].some((c) => `${current.args.slice(2).join(" ")} `.startsWith(`${c} `));
      if (!ours) return;
      delete cfg.mcpServers["ach-memory"];
    }
    fs.mkdirSync(agentDir, { recursive: true });
    fs.writeFileSync(file, JSON.stringify(cfg, null, 2) + "\n");
  } catch {
    // A pi session must start whether or not memory can be wired up.
  }
}

const loaded = Symbol.for("ach-memory.pi");

export default function (pi) {
  if (globalThis[loaded]) return;
  globalThis[loaded] = root;
  if (typeof pi?.registerMcpServer === "function") {
    // An mcp.json entry of the same name wins over a registration.
    writeMcpEntry(null);
    pi.registerMcpServer("ach-memory", server);
  } else {
    writeMcpEntry(server);
  }
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
