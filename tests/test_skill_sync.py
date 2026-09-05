from pathlib import Path

ROOT = Path(__file__).parents[1]
HOSTS = ("claude-code", "codex", "opencode", "pi")


def test_every_host_receives_the_canonical_skill_and_its_references():
    canonical = ROOT / "plugins/shared/ach-memory"
    expected = {
        path.relative_to(canonical): path.read_bytes()
        for path in canonical.rglob("*")
        if path.is_file()
    }

    for host in HOSTS:
        installed = ROOT / "plugins" / host / "skills/ach-memory"
        actual = {
            path.relative_to(installed): path.read_bytes()
            for path in installed.rglob("*")
            if path.is_file()
        }
        assert actual == expected
