def test_upgrade_revision_is_single_forward_head():
    from pathlib import Path
    text = (Path("migrations/versions/a7b8c9d0e1f2_remove_superseded_memory_pipeline.py")).read_text()
    assert 'down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"' in text
