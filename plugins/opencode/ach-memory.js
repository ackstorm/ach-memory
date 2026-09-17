// opencode plugin: injects the ach-memory session-start output (activation
// policy + standing context) into the system prompt. Same script the Claude
// and Codex hooks run; `init` copies it next to this file under ./ach-memory/.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

function sessionStart() {
  try {
    return execFileSync(path.join(here, "ach-memory", "scripts", "session-start.sh"), {
      encoding: "utf8",
      timeout: 6000,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

export const AchMemoryPlugin = async () => {
  const context = sessionStart();
  if (!context) return {};
  return {
    "experimental.chat.system.transform": async (_input, output) => {
      if (Array.isArray(output?.system) && !output.system.includes(context)) output.system.push(context);
    },
  };
};
