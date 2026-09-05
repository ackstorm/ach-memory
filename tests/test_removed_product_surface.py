from pathlib import Path


def test_retired_runtime_modules_are_absent():
    for name in ("capture", "profiles.py", "brief.py", "revisions.py"):
        assert not (Path("src/memory") / name).exists()


def test_retired_import_tokens_are_absent_from_active_runtime():
    source = "\n".join(path.read_text() for path in Path("src/memory").rglob("*.py"))
    for token in ("memory.capture", "memory.profiles", "memory.brief", "memory.revisions"):
        assert token not in source
