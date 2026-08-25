const fs = require("fs");
const path = require("path");

// Session and subagent starts get the full policy. UserPromptSubmit fires on
// EVERY prompt, so it gets one sentence instead: the policy is already in
// context by then, and re-injecting it per prompt is how a memory plugin turns
// into the thing users uninstall.
const SOURCES = {
  SessionStart: "activation.txt",
  SubagentStart: "activation.txt",
  UserPromptSubmit: "prompt-hint.txt",
};

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  try {
    const event = JSON.parse(input).hook_event_name;
    const source = SOURCES[event];
    if (!source) return;

    const context = fs.readFileSync(path.join(__dirname, "..", source), "utf8").trim();
    // Claude reads SessionStart from plain stdout; every other event, and Codex
    // throughout, needs the explicit hookSpecificOutput envelope.
    if (process.env.PLUGIN_DATA || event !== "SessionStart") {
      process.stdout.write(JSON.stringify({
        hookSpecificOutput: { hookEventName: event, additionalContext: context },
      }));
    } else {
      process.stdout.write(context);
    }
  } catch {
    process.exitCode = 0;
  }
});
