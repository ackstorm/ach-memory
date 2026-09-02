import fs from "node:fs";

async function main() {
  const command = JSON.parse(fs.readFileSync(0, "utf8"));
  const transcript = await import(`${command.sourceRoot}/src/core/transcript.ts`);
  if (command.op === "normalize-claude") {
    const turns = transcript.readClaudeTranscript(command.transcriptPath).map((turn: any) => ({
      role: turn.role === "action" ? "tool" : turn.role,
      text: turn.content,
    }));
    process.stdout.write(JSON.stringify({ turns }));
    return;
  }
  if (typeof command.bankId !== "string" || !command.bankId.startsWith(command.runPrefix ?? "")) {
    process.stdout.write(JSON.stringify({ ok: false, turn_count: 0, error_code: "foreign_bank" }));
    return;
  }
  const { HindsightClient } = await import(`${command.sourceRoot}/src/core/hindsight.ts`);
  const { retainLiveSession } = await import(`${command.sourceRoot}/src/core/chat.ts`);
  const { memoryCursorStore } = await import(`${command.sourceRoot}/src/core/retain-cursor.ts`);
  const turns = transcript.readClaudeTranscript(command.transcriptPath);
  const client = new HindsightClient({ apiUrl: command.apiUrl, apiToken: command.apiToken, bank: command.bankId });
  const cursors = memoryCursorStore();
  try {
    await retainLiveSession(client, command.sessionId, turns, command.startTs ?? new Date().toISOString(), "memory-quality", { cursors });
    await client.drain(client.opIds, "memory-quality", 120_000);
    process.stdout.write(JSON.stringify({ ok: true, turn_count: turns.length }));
  } catch {
    process.stdout.write(JSON.stringify({ ok: false, turn_count: turns.length, error_code: "retain_failed" }));
  }
}

main().catch(() => process.stdout.write(JSON.stringify({ ok: false, turn_count: 0, error_code: "bridge_failed" })));
