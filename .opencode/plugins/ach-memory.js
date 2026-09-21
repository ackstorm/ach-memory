// opencode plugin: one install wires up everything ach-memory needs.
//
//   opencode plugin ach-memory@git+https://github.com/ackstorm/ach-memory.git -g
//
// The package root is a checkout of this repository, so the `config` hook can
// point opencode at the skill and at the stdio proxy built from that same
// checkout -- no version pin to go stale. The two experimental hooks are
// opencode's equivalents of the Claude Code SessionStart and PreCompact hooks,
// and `chat.message` is its UserPromptSubmit; all run the same shell scripts.
import { execFileSync } from "node:child_process";
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

export const AchMemoryPlugin = async () => {
  const context = hook("session-start.sh");
  return {
    config: async (config) => {
      config.skills = config.skills || {};
      config.skills.paths = config.skills.paths || [];
      const skills = path.join(root, "skills");
      if (!config.skills.paths.includes(skills)) config.skills.paths.push(skills);

      config.mcp = config.mcp || {};
      // No `environment`: opencode interpolates `{env:VAR}` only in the config
      // file, so a placeholder set from here reaches the proxy as a literal --
      // `Illegal header name b'{env:ACH_MEMORY_HEADER}'`, and the server dies
      // on startup as `MCP error -32000: Connection closed`. The spawned proxy
      // inherits opencode's own environment, which is where the credential is.
      config.mcp["ach-memory"] = {
        type: "local",
        command: ["uvx", "--from", root, "ach-memory", "mcp", "--url",
                  process.env.ACH_MEMORY_URL || "http://localhost:8000/mcp/"],
        enabled: true,
      };
    },

    "experimental.chat.system.transform": async (_input, output) => {
      if (context && Array.isArray(output?.system) && !output.system.includes(context)) {
        output.system.push(context);
      }
    },

    // opencode's PreCompact: `output.context` is carried into the compaction
    // prompt, so the retain nudge reaches the agent while it still has the
    // session in front of it.
    "experimental.session.compacting": async (_input, output) => {
      const nudge = hook("pre-compact.sh");
      if (nudge && Array.isArray(output?.context)) output.context.push(nudge);
    },

    // opencode's UserPromptSubmit: the idle nudge rides into the user's own
    // message as one more text part. idle-nudge.sh prints nothing unless
    // nothing was retained for this checkout in the last 30 minutes.
    //
    // A part missing `id`/`sessionID`/`messageID` fails opencode's schema check
    // inside `SessionPrompt.createUserMessage`, which throws before the request
    // leaves the client: the user sees "unexpected server error", the gateway
    // logs nothing, and the turn never runs. It hit the first prompt of a
    // session -- the only one the idle clock lets the nudge through.
    "chat.message": async (input, output) => {
      const nudge = input?.sessionID && hook("idle-nudge.sh", input.sessionID);
      if (!nudge || !Array.isArray(output?.parts) || !output?.message) return;
      output.parts.push({
        id: `prt_${crypto.randomUUID().replaceAll("-", "")}`,
        sessionID: output.message.sessionID,
        messageID: output.message.id,
        type: "text",
        text: nudge,
        synthetic: true,
      });
    },
  };
};
