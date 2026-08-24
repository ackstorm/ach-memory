"""Offline contract checks for the human-initiated release hand-off."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _release_fixture(tmp_path: Path) -> Path:
    """Create the smallest safe tree on which release-bump may operate."""
    for relative_path in (
        "Makefile",
        "pyproject.toml",
        "deploy/helm/ach-memory/Chart.yaml",
        "deploy/helm/ach-memory/values.yaml",
    ):
        source = REPO_ROOT / relative_path
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return tmp_path


def _read_version(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(), re.MULTILINE)
    assert match, f"version missing from {path}"
    return match.group(1)


def _make_recipe(target: str) -> list[str]:
    """Return a target's tab-indented recipe without asking Make to run it."""
    lines = (REPO_ROOT / "Makefile").read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{target}:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_release_bump_synchronizes_bare_versions_without_pinning_values_tag(tmp_path):
    """A release bump must update release metadata but leave the tag default intact."""
    root = _release_fixture(tmp_path)

    result = subprocess.run(
        ["make", "release-bump", "VERSION=1.2.3"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _read_version(root / "pyproject.toml", r'^version = "([^"]+)"') == "1.2.3"
    assert _read_version(
        root / "deploy/helm/ach-memory/Chart.yaml", r"^version: ([^\n]+)"
    ) == "1.2.3"
    assert _read_version(
        root / "deploy/helm/ach-memory/Chart.yaml", r'^appVersion: "([^"]+)"'
    ) == "1.2.3"
    assert "  tag: \"\"" in (root / "deploy/helm/ach-memory/values.yaml").read_text()


def test_release_bump_requires_a_valid_version_without_changing_files(tmp_path):
    """Missing or malformed versions must not partially rewrite release metadata."""
    root = _release_fixture(tmp_path)
    tracked = [
        root / "pyproject.toml",
        root / "deploy/helm/ach-memory/Chart.yaml",
        root / "deploy/helm/ach-memory/values.yaml",
    ]
    before = [path.read_bytes() for path in tracked]

    for arguments in ([], ["VERSION=1.2"]):
        result = subprocess.run(
            ["make", "release-bump", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "VERSION" in result.stderr
    assert [path.read_bytes() for path in tracked] == before


def test_release_cut_gates_marker_commit_then_verifies_then_pushes():
    """Release-cut must refuse unsafe state before its only three release actions."""
    recipe = _make_recipe("release-cut")
    body = "\n".join(recipe)

    assert "$(require_release_version)" in recipe
    assert 'git rev-parse --abbrev-ref HEAD)' in body
    assert '= "main"' in body
    assert 'git status --porcelain)' in body
    assert 'version = "$(VERSION)"' in body
    assert 'version: $(VERSION)' in body
    assert 'appVersion: "$(VERSION)"' in body

    actions = [
        'git commit --allow-empty -m "chore(release): v$(VERSION)"',
        "$(MAKE) verify",
        "git push origin main",
    ]
    positions = [recipe.index(action) for action in actions]
    assert positions == sorted(positions)
    assert all(position > 0 for position in positions)


def test_release_workflow_is_main_marker_driven_and_creates_release_artifacts():
    """Only a canonical marker on main can publish, tag, and create a release."""
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()

    assert "branches: [main]" in workflow
    assert "tags:" not in workflow
    assert "startsWith(github.event.head_commit.message, 'chore(release): v')" in workflow
    assert "^chore\\(release\\): v" in workflow
    marker_pattern = next(
        pattern
        for pattern in re.findall(r"grep -Eq '([^']+)'", workflow)
        if pattern.startswith(r"^chore\(release\): v")
    )
    assert re.fullmatch(marker_pattern, "chore(release): v1.2.3-rc.1")
    for invalid_marker in (
        "chore(release): v1.2",
        "chore(release): v1.2.3+build.1",
        "prefix chore(release): v1.2.3",
        "chore(release): v1.2.3 extra",
    ):
        assert not re.fullmatch(marker_pattern, invalid_marker)
    assert 'git tag -a "v${VERSION}" -m "v${VERSION}"' in workflow
    assert 'git push origin "v${VERSION}"' in workflow
    assert 'gh release create "v${VERSION}" --title "v${VERSION}" --generate-notes' in workflow
