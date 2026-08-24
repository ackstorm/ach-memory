const fs = require("fs");
const path = require("path");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  try {
    const event = JSON.parse(input).hook_event_name;
    if (event !== "SessionStart" && event !== "SubagentStart") return;

    const context = fs.readFileSync(path.join(__dirname, "..", "activation.txt"), "utf8").trim();
    if (process.env.PLUGIN_DATA || event === "SubagentStart") {
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
