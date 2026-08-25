"""Offline contract checks for the human-initiated release hand-off."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Files stating the version in a JSON "version" field. The two plugin manifests
# and the marketplace entry were absent from release-bump and drifted to 0.1.0
# while the package shipped 0.1.2 -- `claude plugin list` reported a version
# that had not existed for two releases, and nothing anywhere failed.
VERSIONED_MANIFESTS = (
    ".claude-plugin/marketplace.json",
    "plugins/claude-code/.claude-plugin/plugin.json",
    "plugins/codex/.codex-plugin/plugin.json",
)


def _release_fixture(tmp_path: Path) -> Path:
    """Create the smallest safe tree on which release-bump may operate."""
    for relative_path in (
        "Makefile",
        "pyproject.toml",
        "deploy/helm/ach-memory/Chart.yaml",
        "deploy/helm/ach-memory/values.yaml",
        *VERSIONED_MANIFESTS,
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
    for relative_path in VERSIONED_MANIFESTS:
        assert (
            _read_version(root / relative_path, r'"version": "([^"]+)"') == "1.2.3"
        ), relative_path


def test_release_bump_requires_a_valid_version_without_changing_files(tmp_path):
    """Missing or malformed versions must not partially rewrite release metadata."""
    root = _release_fixture(tmp_path)
    tracked = [
        root / "pyproject.toml",
        root / "deploy/helm/ach-memory/Chart.yaml",
        root / "deploy/helm/ach-memory/values.yaml",
    ]
    before = [path.read_bytes() for path in tracked]

    # Scrubbed, not inherited. `make release-cut VERSION=X` runs `$(MAKE)
    # verify`, and a variable set on make's command line is handed to every
    # sub-make through MAKEFLAGS and to each recipe's environment -- so this
    # subprocess saw VERSION=X, `test -n` passed, and the no-argument case
    # returned 0. The assertion below then failed for a reason that had
    # nothing to do with the guard it is testing, which made `make
    # release-cut` unable to pass its own gate. Standalone `make test` never
    # showed it, because nothing set VERSION there.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("VERSION", "MAKEFLAGS", "MFLAGS")
    }

    for arguments in ([], ["VERSION=1.2"]):
        result = subprocess.run(
            ["make", "release-bump", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        assert result.returncode != 0
        assert "VERSION" in result.stderr
    assert [path.read_bytes() for path in tracked] == before


def test_release_cut_gates_then_verifies_then_marks_and_pushes():
    """Release-cut must refuse unsafe state before its only three release actions.

    Order is load-bearing, not incidental: verify runs BEFORE the marker
    commit so a failed gate leaves nothing behind to clean up. The marker is
    empty, so both orders verify the same tree -- see the Makefile comment.
    """
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
        "$(MAKE) verify",
        'git commit --allow-empty -m "chore(release): v$(VERSION)"',
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
    # Gated on CI finishing green, not merely on the push. These raced before:
    # Release did not depend on CI, so v0.2.0 published while the image smoke
    # test was failing on the very commit it shipped.
    assert "workflows: [CI]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert (
        "startsWith(github.event.workflow_run.head_commit.message, 'chore(release): v')" in workflow
    )
    # workflow_run checks out the default branch's tip unless told otherwise,
    # which would publish whatever landed after the commit CI verified.
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "github.event.head_commit" not in workflow
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


def test_every_manifest_states_the_version_in_pyproject():
    """One version, stated in six places, with nothing to keep them equal.

    release-bump rewrites all of them now, but a bump is a step someone runs;
    this is the step nobody can forget. A new manifest added without being
    registered in VERSIONED_MANIFESTS and the Makefile will fail here on its
    first release rather than shipping a stale number to users.
    """
    expected = _read_version(REPO_ROOT / "pyproject.toml", r'^version = "([^"]+)"')
    chart = REPO_ROOT / "deploy/helm/ach-memory/Chart.yaml"

    assert _read_version(chart, r"^version: (.+)$") == expected
    assert _read_version(chart, r'^appVersion: "([^"]+)"') == expected
    for relative_path in VERSIONED_MANIFESTS:
        assert (
            _read_version(REPO_ROOT / relative_path, r'"version": "([^"]+)"') == expected
        ), relative_path


def test_release_bump_updates_every_versioned_manifest_the_repo_has():
    """Breaks when a manifest gains a version field but not a bump rule.

    Scanning the tree, rather than trusting the tuple above to be complete: the
    drift this guards against started exactly by someone adding a file with a
    version in it and no way to move it.
    """
    import json

    tracked = set(VERSIONED_MANIFESTS)
    found = set()
    for path in [*REPO_ROOT.glob(".claude-plugin/*.json"), *REPO_ROOT.glob(".agents/plugins/*.json"),
                 *REPO_ROOT.glob("plugins/*/.claude-plugin/*.json"),
                 *REPO_ROOT.glob("plugins/*/.codex-plugin/*.json")]:
        payload = json.loads(path.read_text())
        entries = [payload, *payload.get("plugins", [])]
        if any(isinstance(entry, dict) and "version" in entry for entry in entries):
            found.add(str(path.relative_to(REPO_ROOT)))

    assert found == tracked, f"unregistered: {sorted(found - tracked)}; stale: {sorted(tracked - found)}"

    recipe = "\n".join(_make_recipe("release-bump"))
    assert "$(PLUGIN_MANIFESTS)" in recipe
