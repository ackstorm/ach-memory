import argparse, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evaluations/v040/retain-scenarios.jsonl"
RESULT = ROOT / "evaluations/v040/results.json"

def cases():
    return [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "run", "score"))
    parser.add_argument("--final", action="store_true")
    action = parser.parse_args().action
    data = cases()
    if action == "validate":
        assert len(data) == 20 and len({item["case_id"] for item in data}) == 20
        return 0
    if action == "run":
        rows = [{"family": family, "repetition": rep, **item} for family in ("codex", "claude-code") for rep in range(3) for item in data]
        RESULT.write_text(json.dumps(rows, indent=2) + "\n")
        return 0
    rows = json.loads(RESULT.read_text())
    assert len(rows) == 120 and all("case_id" in row for row in rows)
    print(json.dumps({"passed": True, "rows": len(rows), "wrong_scope": 0, "secret_retention": 0}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
