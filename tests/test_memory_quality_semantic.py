from experiments.memory_quality.semantic import split_minimal


def test_minimal_projection_has_only_scope_and_provenance():
    value = split_minimal("user: concise replies\nproject: use JSONL")
    assert value["claims"][0] == {"text": "concise replies", "bank": "user", "provenance": "raw-span"}
    assert set(value) == {"claims", "working_state"}
