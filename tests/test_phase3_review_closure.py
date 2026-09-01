"""Every Phase 3 review finding, pinned as a regression.

The first test here is the gate the Phase 4 plan names: the exact body the
shipped Claude Code hook builds must traverse the real FastAPI route with no
test-only repair. The rest close the privacy, durability, lease-fencing and
default-activation findings the Phase 3 delivery gate did not cover.
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from memory.capture import local
from memory.slugs import slug_from_locator

HOOK_URL = "http://ach-memory.test"
ORIGIN = "https://example.com/acme/hooked.git"


# ---------------------------------------------------------------------------
# Shared rigging: a real git worktree, a real hook environment, a real user.
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path, origin: str = ORIGIN) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
    return repo


def _hook_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "MEMORY_CAPTURE_ENABLED": "true",
        "ACH_MEMORY_API_KEY": "mem_test_key",
        "ACH_MEMORY_URL": HOOK_URL,
        "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
        "HOME": str(tmp_path),
    }
    env.update(overrides)
    return env


def _line(**fields) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def _user_line(text: str) -> bytes:
    return _line(type="user", message={"role": "user", "content": text})


@pytest.fixture
def hook_user(client, master_headers) -> dict:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def _accepted_response(checkpoint_seq: int, slug: str) -> httpx.Response:
    return httpx.Response(
        202,
        json={
            "capture_id": "cap_1",
            "status": "pending",
            "duplicate": False,
            "session_epoch": 1,
            "checkpoint_seq": checkpoint_seq,
            "project_slug": slug,
            "resolved_from": None,
        },
    )


# ---------------------------------------------------------------------------
# Finding 1: the shipped hook body must traverse FastAPI unrepaired.
# ---------------------------------------------------------------------------


@respx.mock
def test_the_shipped_hook_body_is_accepted_by_fastapi_without_repair(
    client, hook_user, tmp_path
):
    repo = _git_repo(tmp_path)
    slug = slug_from_locator(ORIGIN)
    created = client.post(
        "/v1/projects", json={"project_slug": slug}, headers=hook_user["headers"]
    )
    assert created.status_code == 201, created.text

    transcript = repo / "transcript.jsonl"
    transcript.write_bytes(
        _user_line("Always run the linter before pushing.")
        + _user_line("Do not squash commits on this project.")
    )

    route = respx.post(f"{HOOK_URL}/v1/capture/checkpoints").mock(
        return_value=_accepted_response(transcript.stat().st_size, slug)
    )

    local.checkpoint(
        {
            "transcript_path": str(transcript),
            "session_id": "sess-hook-seam",
            "cwd": str(repo),
        },
        env=_hook_env(tmp_path),
    )

    assert route.called, "the shipped hook produced no submission at all"
    submitted = json.loads(route.calls.last.request.read())

    # The exact bytes the hook sent, replayed against the real route. No
    # field is added, renamed or repaired on the way in.
    accepted = client.post(
        "/v1/capture/checkpoints", json=submitted, headers=hook_user["headers"]
    )

    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["project_slug"] == slug
