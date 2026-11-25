// opencode plugin: one install wires up everything ach-memory needs.
//
//   opencode plugin add ach-memory@git+https://github.com/ackstorm/ach-memory.git      (OpenCode 2)
//   opencode plugin ach-memory@git+https://github.com/ackstorm/ach-memory.git -g       (OpenCode 1)
//
// The package root is a checkout of this repository, so the plugin can point
// opencode at the skill and at the stdio proxy built from that same checkout --
// no version pin to go stale. Standing context, the retain nudge before a
// compaction and the idle nudge are opencode's equivalents of the Claude Code
// SessionStart, PreCompact and UserPromptSubmit hooks; all run the same shell
// scripts.
//
// OpenCode 2 loads the default export and calls `setup`. OpenCode 1 (1.18.29
// and newer) calls `server` on the default export, which is `AchMemoryPlugin`.
// The two APIs share nothing but the helpers below.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
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

// No environment block: opencode interpolates `{env:VAR}` only in the config
// file, so a placeholder set from here reaches the proxy as a literal --
// `Illegal header name b'{env:ACH_MEMORY_HEADER}'`, and the server dies on
// startup as `MCP error -32000: Connection closed`. The spawned proxy inherits
// opencode's own environment, which is where the credential is.
function proxy() {
  return ["uvx", "--from", root, "python", "-m", "memory.cli", "mcp"];
}

// OpenCode 2 takes a skill as data, not as a directory to scan.
// ponytail: `name` and `description` are read as single-line frontmatter values;
// use a YAML parser if SKILL.md ever needs more.
function skill() {
  const file = path.join(root, "skills", "ach-memory", "SKILL.md");
  try {
    const [, head, content] = /^---\n([\s\S]*?)\n---\n?([\s\S]*)$/.exec(fs.readFileSync(file, "utf8"));
    const field = (name) => new RegExp(`^${name}:[ \\t]*(.*)$`, "m").exec(head)?.[1].trim();
    return { id: "ach-memory", name: field("name") || "ach-memory", description: field("description"),
             path: file, content };
  } catch {
    return undefined;
  }
}

const NUDGE = "ach-memory.nudge";

async function setup(ctx) {
  // OpenCode 1.19 calls `setup` too, with a context that has no `mcp` or
  // `session` domain; `server` covers that release.
  if (!ctx.mcp || !ctx.session) return;
  const context = hook("session-start.sh");
  const memory = skill();
  const text = (value) => ({ type: "text", text: value });

  await ctx.skill.transform((editor) => {
    if (memory && !editor.get(memory.id)) editor.add(memory);
  });
  await ctx.mcp.transform((editor) => {
    editor.set("ach-memory", { type: "local", command: proxy() });
  });

  // The idle nudge is stored on the user's message and added to that message in
  // every request that replays it: one more text part, invisible in the
  // transcript, stable for the provider's prompt cache. idle-nudge.sh prints
  // nothing unless nothing was retained for this checkout in the last 30 minutes.
  await ctx.session.hook("prompt", (event) => {
    const nudge = hook("idle-nudge.sh", event.sessionID);
    if (nudge) event.metadata = { ...event.metadata, [NUDGE]: nudge };
  });
  await ctx.session.hook("context", (event) => {
    if (context) event.system.push(text(context));
    event.messages.forEach((message, index) => {
      const nudge = message.role === "user" && message.metadata?.[NUDGE];
      if (nudge) event.messages[index] = { ...message, content: [...message.content, text(nudge)] };
    });
  });
  // The compaction request is built from the session in front of the agent, so
  // the retain nudge reaches it while it can still act on it.
  await ctx.session.hook("compaction", (event) => {
    const nudge = hook("pre-compact.sh");
    if (nudge) event.system.push(text(nudge));
  });
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
      config.mcp["ach-memory"] = { type: "local", command: proxy(), enabled: true };
    },

    "experimental.chat.system.transform": async (_input, output) => {
      if (context && Array.isArray(output?.system) && !output.system.includes(context)) {
        output.system.push(context);
      }
    },

    // `output.context` is carried into the compaction prompt, so the retain
    // nudge reaches the agent while it still has the session in front of it.
    "experimental.session.compacting": async (_input, output) => {
      const nudge = hook("pre-compact.sh");
      if (nudge && Array.isArray(output?.context)) output.context.push(nudge);
    },

    // The idle nudge rides into the user's own message as one more text part.
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

export default { id: "ach-memory", setup, server: AchMemoryPlugin };
