import fs from "node:fs";

const command = JSON.parse(fs.readFileSync(0, "utf8"));
const transcript = await import(`${command.sourceRoot}/src/core/transcript.ts`);

if (command.op === "normalize-claude") {
  const turns = transcript.readClaudeTranscript(command.transcriptPath).map((turn: any) => ({
    role: turn.role === "action" ? "tool" : turn.role,
    text: turn.content,
  }));
  process.stdout.write(JSON.stringify({ turns }));
} else {
  process.stdout.write(JSON.stringify({ ok: false, turn_count: 0, error_code: "retain_bridge_requires_live_adapter" }));
}
