const fs = require("fs");
const path = require("path");

module.exports = function (pi) {
  let activation;
  try {
    activation = fs.readFileSync(path.join(__dirname, "ach-memory", "activation.txt"), "utf8").trim();
  } catch {
    return;
  }
  if (!activation || typeof pi?.on !== "function") return;

  pi.on("before_agent_start", async (event) => {
    if (typeof event?.systemPrompt !== "string" || event.systemPrompt.includes(activation)) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${activation}` };
  });
};
