// pi extension: appends the ach-memory session-start output (activation
// policy + standing context) to the system prompt. pi has no hooks; this is
// the SessionStart equivalent. `init` copies the script under ./ach-memory/.
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

module.exports = function (pi) {
  const context = sessionStart();
  if (!context || typeof pi?.on !== "function") return;
  pi.on("before_agent_start", async (event) => {
    if (typeof event?.systemPrompt !== "string" || event.systemPrompt.includes(context)) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${context}` };
  });
};
