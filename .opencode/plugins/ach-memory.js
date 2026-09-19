// opencode plugin: one install wires up everything ach-memory needs.
//
//   opencode plugin ach-memory@git+https://github.com/ackstorm/ach-memory.git -g
//
// The package root is a checkout of this repository, so the `config` hook can
// point opencode at the skill and at the stdio proxy built from that same
// checkout -- no version pin to go stale. The two experimental hooks are
// opencode's equivalents of the Claude Code SessionStart and PreCompact hooks,
// and run the same shell scripts.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function hook(script) {
  try {
    return execFileSync(path.join(root, "hooks", "scripts", script), {
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
      config.mcp["ach-memory"] = {
        type: "local",
        command: ["uvx", "--from", root, "ach-memory", "mcp", "--url",
                  process.env.ACH_MEMORY_URL || "http://localhost:8000/mcp/"],
        environment: {
          ACH_MEMORY_API_KEY: "{env:ACH_MEMORY_API_KEY}",
          ACH_MEMORY_HEADER: "{env:ACH_MEMORY_HEADER}",
        },
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
  };
};
