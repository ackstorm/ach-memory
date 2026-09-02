from pathlib import Path

from experiments.memory_quality.preprocessing import Preprocessed, scan_canaries
from memory.capture.local import sanitize


def test_preprocessing_fixture_canaries_are_removed_by_ach_policy():
    path = Path("experiments/memory_quality/corpus/hosts/claude.jsonl")
    raw = [__import__("json").loads(line) for line in path.read_text().splitlines()]
    assert "CANARYTOKEN123456" in path.read_text()
    assert "CANARYTOKEN123456" not in sanitize(raw)


def test_canary_scan_returns_codes_and_preprocessed_content_is_not_serialized(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text("private synthetic CANARY-ONE")
    codes = scan_canaries((path,), ("CANARY-ONE",))
    assert codes == ("CANARY_00_PRESENT",)
    value = Preprocessed(variant="x", content="secret", byte_count=6, turn_count=1, retained_role_count=1, tool_action_count=0, source_span_count=0, canary_present={}, duration_ms=0)
    assert "content" not in value.model_dump()
