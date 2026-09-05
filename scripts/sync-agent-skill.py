import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins/shared/ach-memory/SKILL.md"
SOURCE_ROOT = SOURCE.parent
HOST_SKILL_PATHS = [
    ROOT / f"plugins/{host}/skills/ach-memory/SKILL.md"
    for host in ("claude-code", "codex", "opencode", "pi")
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source_files = {
        path.relative_to(SOURCE_ROOT): path.read_bytes()
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file()
    }
    drift = []
    for skill_path in HOST_SKILL_PATHS:
        destination_root = skill_path.parent
        for relative, source in source_files.items():
            path = destination_root / relative
            if args.write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source)
            elif not path.exists() or path.read_bytes() != source:
                drift.append(str(path.relative_to(ROOT)))
    if drift:
        print("drifted skill paths:")
        print("\n".join(drift))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
