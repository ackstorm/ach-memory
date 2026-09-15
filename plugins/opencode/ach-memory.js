// opencode plugin: injects the ach-memory session-start output (activation
// policy + standing context) into the system prompt. Same script the Claude
// and Codex hooks run; `init` copies it next to this file under ./ach-memory/.
const { execFileSync } = require("child_process");
const path = require("path");

function sessionStart() {
  try {
    return execFileSync(path.join(__dirname, "ach-memory", "scripts", "session-start.sh"), {
      encoding: "utf8",
      timeout: 6000,
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

module.exports = async function () {
  const context = sessionStart();
  if (!context) return {};
  return {
    "experimental.chat.system.transform": async (_input, output) => {
      if (Array.isArray(output?.system) && !output.system.includes(context)) output.system.push(context);
    },
  };
};
