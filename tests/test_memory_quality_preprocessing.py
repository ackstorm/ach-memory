from pathlib import Path

from memory.capture.local import sanitize


def test_preprocessing_fixture_canaries_are_removed_by_ach_policy():
    path = Path("experiments/memory_quality/corpus/hosts/claude.jsonl")
    raw = [__import__("json").loads(line) for line in path.read_text().splitlines()]
    assert "CANARYTOKEN123456" in path.read_text()
    assert "CANARYTOKEN123456" not in sanitize(raw)
