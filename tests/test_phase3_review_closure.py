"""Every Phase 3 review finding, pinned as a regression.

The first test here is the gate the Phase 4 plan names: the exact body the
shipped Claude Code hook builds must traverse the real FastAPI route with no
test-only repair. The rest close the privacy, durability, lease-fencing and
default-activation findings the Phase 3 delivery gate did not cover.
"""

import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx
import pytest
import respx

from memory import activity, metrics, provenance
from memory.auth.principal import Principal
from memory.capture import configuration, filer, local, repository, worker
from memory.capture.classifier import CandidateRejected, classify
from memory.capture.contracts import CheckpointSubmission
from memory.capture.extractor import (
    EXTRACTION_PROMPT,
    ExtractionFailed,
    ExtractionResult,
    extract,
)
from memory.config import get_settings
from memory.errors import InvalidMetadata
from memory.hindsight.client import HindsightClient
from memory.ids import new_project_internal_id, new_user_id
from memory.models import Project, User
from memory.slugs import slug_from_locator

HOOK_URL = "http://ach-memory.test"
ORIGIN = "https://example.com/acme/hooked.git"


# ---------------------------------------------------------------------------
# Shared rigging: a real git worktree, a real hook environment, a real user.
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path, origin: str = ORIGIN) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
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


HINDSIGHT = "http://hindsight.test"
EXTRACT_BANK = "bank_1"


def _stub_extractor(*envelopes: dict) -> HindsightClient:
    """A Hindsight client whose dry-run extraction returns these envelopes."""
    respx.post(
        f"{HINDSIGHT}/v1/default/banks/{EXTRACT_BANK}/memories/dry-run-extract"
    ).mock(
        return_value=httpx.Response(
            200, json={"facts": [{"text": json.dumps(envelope)} for envelope in envelopes]}
        )
    )
    return HindsightClient(base_url=HINDSIGHT, api_key="secret", tenant_id="default")


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


def test_the_submission_body_and_the_route_body_are_one_model():
    """Not two models that happen to agree today.

    The seam above passes whenever the two definitions match. This is what
    keeps them from drifting apart again between one release and the next.
    """
    from memory.api import capture as capture_route

    assert capture_route.CheckpointRequest is CheckpointSubmission


# ---------------------------------------------------------------------------
# Finding 2: one project resolution, and no credential on the wire.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://x-access-token:ghp_CANARYTOKEN1234567890@github.com/acme/api.git",
        "https://ghp_CANARYTOKEN1234567890@github.com/acme/api.git",
        "https://user:CANARYPASSWORD99@github.com/acme/api.git",
        "ssh://git@github.com:22/acme/api.git",
    ],
)
def test_a_credential_in_the_git_origin_never_reaches_the_locator(tmp_path, origin):
    repo = _git_repo(tmp_path, origin)

    context = local.resolve_project_context(str(repo), env={})

    assert context is not None
    for canary in ("CANARYTOKEN1234567890", "CANARYPASSWORD99", "x-access-token", "@"):
        assert canary not in context.git_locator
        assert canary not in context.project_slug
    assert context.git_locator == "github.com/acme/api"


def test_every_spelling_of_one_remote_resolves_to_one_project(tmp_path):
    """A credentialed HTTPS clone and a plain SSH clone are one project.

    Two spellings producing two slugs would produce two banks for one
    repository, which SPEC inv. 12 calls a defect outright.
    """
    slugs = set()
    for index, origin in enumerate(
        [
            "https://ghp_TOKEN1234567890@github.com/acme/api.git",
            "git@github.com:acme/api.git",
            "ssh://git@github.com:22/acme/api",
            "https://github.com/acme/api",
        ]
    ):
        repo = _git_repo(tmp_path / f"clone{index}", origin)
        context = local.resolve_project_context(str(repo), env={})
        assert context is not None
        slugs.add(context.project_slug)

    assert len(slugs) == 1


def test_the_mcp_proxy_and_the_hook_resolve_the_same_project(tmp_path, monkeypatch):
    """One resolution, not two implementations that agree by coincidence."""
    from memory.mcp.proxy import resolve_project_context as proxy_resolve

    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    repo = _git_repo(tmp_path, "https://ghp_CANARYTOKEN1234567890@github.com/acme/api.git")

    context = local.resolve_project_context(str(repo), env={})

    assert proxy_resolve(str(repo)) == (context.project_slug, context.git_locator)
    assert "CANARYTOKEN1234567890" not in str(proxy_resolve(str(repo)))


def test_an_explicit_project_env_var_still_wins_over_git(tmp_path):
    repo = _git_repo(tmp_path)

    context = local.resolve_project_context(str(repo), env={"MEMORY_PROJECT": "payments-api"})

    assert context == local.ProjectContext(project_slug="payments-api", git_locator=None)


def _git_repo_no_origin(tmp_path: Path) -> Path:
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_a_repository_with_no_origin_resolves_to_no_context(tmp_path):
    assert local.resolve_project_context(str(_git_repo_no_origin(tmp_path)), env={}) is None


# ---------------------------------------------------------------------------
# Finding 3: rotation and capping must not lose or double-submit records.
# ---------------------------------------------------------------------------


@respx.mock
def test_a_rotated_transcript_submits_the_offset_it_actually_read_from(tmp_path):
    """After rotation the cursor is past the end of the new file.

    The read rewinds to 0; a submission that still carried the old cursor as
    `start_offset` would name a range that never existed -- and, being
    greater than `end_offset`, would not even be a valid body.
    """
    repo = _git_repo(tmp_path)
    slug = slug_from_locator(ORIGIN)
    transcript = repo / "transcript.jsonl"
    env = _hook_env(tmp_path)

    transcript.write_bytes(_user_line("first session, a long line to push the offset out") * 4)
    route = respx.post(f"{HOOK_URL}/v1/capture/checkpoints").mock(
        return_value=_accepted_response(transcript.stat().st_size, slug)
    )
    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(repo)}, env=env
    )
    first_end = json.loads(route.calls.last.request.read())["end_offset"]
    assert first_end == transcript.stat().st_size

    # Rotation: the same path, now a shorter file.
    transcript.write_bytes(_user_line("second session after rotation"))
    rotated_size = transcript.stat().st_size
    assert rotated_size < first_end, "fixture must actually shrink the file"
    route.mock(return_value=_accepted_response(rotated_size, slug))

    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(repo)}, env=env
    )

    body = json.loads(route.calls.last.request.read())
    assert body["start_offset"] == 0
    assert body["end_offset"] == rotated_size
    assert "second session after rotation" in body["content"]
    # And the submission is a valid one: the model accepts it.
    assert CheckpointSubmission.model_validate(body).start_offset == 0


def test_a_capped_batch_leaves_the_tail_reachable_instead_of_dropping_it(tmp_path):
    """A decision in the tail of an oversized slice must survive.

    Truncating the joined sanitized text is what loses it: the cursor
    advances over every record read, including the ones cut off the end.
    """
    transcript = tmp_path / "transcript.jsonl"
    filler = _user_line("padding " + "z" * 400)
    tail = _user_line("We decided to drop the legacy importer entirely.")
    transcript.write_bytes(filler * 300 + tail)

    first = local.build_batch(local.read_new_slice(transcript, 0))

    assert len(first.content) <= local._SLICE_CAP
    assert "legacy importer" not in first.content, "fixture must overflow the cap"
    assert first.end_offset < transcript.stat().st_size

    # The next checkpoint resumes exactly where the last one stopped, and
    # the decision is still there to be found.
    seen = first.content
    offset = first.end_offset
    for _ in range(20):
        raw_slice = local.read_new_slice(transcript, offset)
        if not raw_slice.records:
            break
        batch = local.build_batch(raw_slice)
        seen += batch.content
        offset = batch.end_offset
        if "legacy importer" in batch.content:
            break

    assert "legacy importer" in seen, "the tail was never reachable"
    assert offset == transcript.stat().st_size


def test_two_checkpoints_cover_every_byte_exactly_once(tmp_path):
    """No gap (a lost record) and no overlap (a double submission)."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"".join(_user_line(f"message {i}") for i in range(6)))

    first = local.build_batch(local.read_new_slice(transcript, 0))
    second = local.build_batch(local.read_new_slice(transcript, first.end_offset))

    assert first.start_offset == 0
    assert first.raw == transcript.read_bytes()
    assert second.raw == b"", "nothing new is left to send"
    assert second.end_offset == first.end_offset


def test_an_all_empty_batch_advances_only_across_records_with_no_retained_text(tmp_path):
    """Summaries carry nothing retainable, so the cursor may cross them --
    but only them. The meaningful record after them must still arrive."""
    transcript = tmp_path / "transcript.jsonl"
    summaries = b"".join(
        _line(type="summary", summary=f"Hi there! Thanks. CANARYSUMMARY{i}", leafUuid=f"u{i}")
        for i in range(3)
    )
    transcript.write_bytes(summaries)

    empty = local.build_batch(local.read_new_slice(transcript, 0))

    assert empty.content == ""
    assert empty.end_offset == len(summaries), "the cursor may cross proven-empty records"

    # Now a real record lands after them.
    transcript.write_bytes(summaries + _user_line("Always run make check before pushing."))
    later = local.build_batch(local.read_new_slice(transcript, empty.end_offset))

    assert "Always run make check before pushing." in later.content
    assert "CANARYSUMMARY" not in later.content


@respx.mock
def test_an_empty_batch_advances_the_cursor_without_submitting_anything(tmp_path):
    repo = _git_repo(tmp_path)
    slug = slug_from_locator(ORIGIN)
    transcript = repo / "transcript.jsonl"
    summary = _line(type="summary", summary="Hi!", leafUuid="u1")
    transcript.write_bytes(summary)
    env = _hook_env(tmp_path)

    route = respx.post(f"{HOOK_URL}/v1/capture/checkpoints").mock(
        return_value=_accepted_response(1, slug)
    )
    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(repo)}, env=env
    )

    assert not route.called, "an empty batch is not worth a network call"

    # But the cursor moved, so the next meaningful record does not sit
    # behind those summaries forever.
    transcript.write_bytes(summary + _user_line("Never force-push to main."))
    route.mock(return_value=_accepted_response(transcript.stat().st_size, slug))
    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(repo)}, env=env
    )

    assert route.called
    body = json.loads(route.calls.last.request.read())
    assert "Never force-push to main." in body["content"]
    assert body["start_offset"] == len(summary), "the summary was already acknowledged"


# ---------------------------------------------------------------------------
# Finding 4: nothing a canary can ride out on.
# ---------------------------------------------------------------------------

# Every one of these appears somewhere in the synthetic transcript below and
# must appear nowhere in what leaves this machine.
CANARIES = [
    "CANARYFILEBODY",  # a file body returned by a shell reader
    "CANARYSHELLCMD",  # the command string itself
    "CANARYPEMBODY",  # a private key in a tool result
    "CANARYUSERINFO",  # userinfo in a URL
    "CANARYBEARER",  # a bearer token
    "CANARYASSIGN",  # an assignment-style secret
    "CANARYSUMMARYTEXT",  # an unrecognized record type
    "CANARYPATH",  # a file path in tool input
    "CANARYBASH2",  # a second, differently-shaped shell read
]


def _canary_transcript() -> list[dict]:
    return [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    "Deploy uses https://admin:CANARYUSERINFO@deploy.internal/hook "
                    "and Bearer sk-CANARYBEARER1234567890, with "
                    "API_KEY=CANARYASSIGN98765 in the env."
                ),
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking the config."},
                    {
                        "type": "tool_use",
                        "id": "t_shell",
                        "name": "Bash",
                        "input": {"command": "cat /etc/CANARYSHELLCMD/secrets.env"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_shell",
                        # Plain source, deliberately not shaped like a
                        # secret: `redact()` must not be what saves this,
                        # or the test proves nothing about classification.
                        "content": "42:    return CANARYFILEBODY",
                        "is_error": False,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t_pipe",
                        "name": "Bash",
                        # A pipeline whose reader is not the first program.
                        "input": {"command": "ls -la | grep CANARYBASH2"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_pipe",
                        # A multi-block result, not a plain string.
                        "content": [
                            {"type": "text", "text": "first block CANARYBASH2"},
                            {"type": "text", "text": "second block CANARYBASH2"},
                        ],
                        "is_error": False,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t_read",
                        "name": "Read",
                        "input": {"file_path": "/home/user/.ssh/id_rsa_CANARYPATH"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t_read",
                        "content": (
                            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                            "CANARYPEMBODY\n"
                            "-----END OPENSSH PRIVATE KEY-----"
                        ),
                        "is_error": False,
                    }
                ],
            },
        },
        {"type": "summary", "summary": "Thanks! CANARYSUMMARYTEXT", "leafUuid": "u9"},
    ]


def test_no_canary_survives_sanitization():
    text = local.sanitize(_canary_transcript())

    for canary in CANARIES:
        assert canary not in text, f"{canary} leaked into the submitted body"


def test_the_evidence_that_a_tool_ran_still_survives():
    """Fail-closed must not mean fail-silent: the extractor still needs to
    know a command ran and whether it worked."""
    text = local.sanitize(_canary_transcript())

    assert "tool_use: Bash" in text
    assert "tool_result: Bash ok [output omitted]" in text
    assert "tool_result: Read ok [output omitted]" in text
    assert "user: Deploy uses" in text


@pytest.mark.parametrize(
    "command",
    [
        "cat secrets.env",
        "sed -n 1,50p src/app.py",
        "awk '{print $1}' data.txt",
        "grep -n token src/auth.py",
        "rg --files-with-matches password",
        "head -c 200 /etc/passwd",
        "tail -f app.log",
        "/bin/cat secrets.env",  # an absolute path is the same program
        "ls && cat secrets.env",  # a reader anywhere in the chain
        "ls | grep secret",  # a reader downstream of a pipe
        "cat < secrets.env",  # a redirection
        "echo $(cat secrets.env)",  # a substitution
        "eval \"$CMD\"",  # indirection
        "sh -c 'cat secrets.env'",  # an interpreter
        "echo 'unbalanced",  # unparseable
        "",  # nothing to classify
    ],
)
def test_a_shell_command_that_could_read_a_file_has_its_output_withheld(command):
    assert local.shell_output_is_retainable(command) is False


@pytest.mark.parametrize(
    "command",
    ["git status", "npm test", "make check", "pytest -q", "ls -la", "git log --oneline -5"],
)
def test_a_shell_command_that_clearly_reads_no_file_keeps_its_output(command):
    assert local.shell_output_is_retainable(command) is True


def test_an_unclassifiable_tool_use_is_withheld_rather_than_guessed():
    """A result whose tool_use was never seen, and a shell call with no
    command in hand, are both unknown -- and unknown fails closed."""
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash"}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "CANARYNOINPUT"},
                    {"type": "tool_result", "tool_use_id": "t_never_seen", "content": "CANARYORPHAN"},
                ],
            },
        },
    ]

    text = local.sanitize(records)

    assert "CANARYNOINPUT" not in text
    assert "CANARYORPHAN" not in text


@respx.mock
def test_no_canary_reaches_the_wire_the_cursor_key_or_the_logs(tmp_path, caplog):
    """The three places a leak could still hide after sanitization."""
    repo = _git_repo(tmp_path, "https://admin:CANARYUSERINFO@example.com/acme/api.git")
    slug = slug_from_locator("example.com/acme/api")
    transcript = repo / "transcript.jsonl"
    transcript.write_bytes(
        b"".join(
            (json.dumps(record) + "\n").encode() for record in _canary_transcript()
        )
    )
    cache_dir = tmp_path / "cache"

    route = respx.post(f"{HOOK_URL}/v1/capture/checkpoints").mock(
        return_value=_accepted_response(transcript.stat().st_size, slug)
    )

    with caplog.at_level(0):
        local.checkpoint(
            {
                "transcript_path": str(transcript),
                "session_id": "sess-canary",
                "cwd": str(repo),
            },
            env=_hook_env(tmp_path),
        )

    assert route.called
    wire = route.calls.last.request.read().decode()
    cursor_files = " ".join(str(path) for path in cache_dir.rglob("*"))
    logs = caplog.text

    for canary in [*CANARIES, "CANARYUSERINFO"]:
        assert canary not in wire, f"{canary} reached the wire"
        assert canary not in cursor_files, f"{canary} reached a cursor filename"
        assert canary not in logs, f"{canary} reached the logs"


# ---------------------------------------------------------------------------
# Findings 5, 7 and 8: durability, server-owned provenance, extractor rules.
# ---------------------------------------------------------------------------


def _envelope(**overrides) -> dict:
    envelope = {
        "record": "candidate",
        "text": "The team uses trunk-based development.",
        "kind": "convention",
        "origin": "stated",
        "subject": "project",
        "negative": False,
        "correction": False,
        "provenance": None,
        "failure": None,
        "cause": None,
        "reproduction": None,
    }
    envelope.update(overrides)
    return envelope


@pytest.mark.parametrize("kind", ["preference", "decision"])
def test_an_observed_preference_or_decision_is_evidence_not_profile_truth(kind):
    """SPEC §6.4 and §6.6: observed preferences remain evidence.

    Watching someone do a thing is not the same as their saying they want it
    done that way, and this is the cell the old rule of thumb got wrong.
    """
    result = classify(
        _envelope(
            text="Runs the linter before every push.",
            kind=kind,
            origin="observed",
            subject="user",
            provenance={"type": "transcript", "start": 0, "end": 20},
        )
    )

    assert result.eligible == "evidence_only"
    assert "profile_eligible" not in result.tags


@pytest.mark.parametrize("origin", ["stated", "confirmed", "observed", "inferred"])
def test_a_technical_claim_is_never_profile_eligible(origin):
    result = classify(
        _envelope(
            text="The build runs on Python 3.12.",
            kind="technical_claim",
            origin=origin,
            provenance=(
                {"type": "transcript", "start": 0, "end": 20} if origin == "observed" else None
            ),
        )
    )

    assert result.eligible == "evidence_only"


@pytest.mark.parametrize("kind", ["preference", "decision", "convention", "gotcha"])
def test_an_inferred_claim_never_reaches_a_durable_profile(kind):
    """Core invariant 17, stated as its own regression."""
    extra = {"failure": "it crashed", "cause": "a null pointer"} if kind == "gotcha" else {}
    result = classify(
        _envelope(text="Something was inferred.", kind=kind, origin="inferred", subject="user", **extra)
    )

    assert result.eligible == "evidence_only"


def test_negative_true_must_match_an_explicit_negative_claim():
    with pytest.raises(CandidateRejected, match="negative"):
        classify(_envelope(text="The team uses trunk-based development.", negative=True))


def test_a_real_prohibition_keeps_its_negative_flag():
    result = classify(_envelope(text="Do not force-push to main.", negative=True))

    assert result.negative is True


def test_repository_local_wording_cannot_widen_into_user_scope():
    """SPEC §6.5: 'here' and 'in this repo' are project truth however phrased."""
    with pytest.raises(CandidateRejected, match="widen"):
        classify(
            _envelope(
                text="Always run make check here before pushing.",
                kind="preference",
                subject="user",
            )
        )


@respx.mock
def test_a_provenance_span_outside_the_slice_fails_the_extraction():
    """An `observed` claim survives on the strength of its artifact, so a
    span that runs off the end of the slice is an invented anchor."""
    client = _stub_extractor(
        _envelope(
            text="CI runs on every push.",
            origin="observed",
            provenance={"type": "transcript", "start": 0, "end": 5_000},
        )
    )

    with pytest.raises(ExtractionFailed, match="provenance span"):
        extract(client, EXTRACT_BANK, "user: a short slice")


@respx.mock
def test_a_provenance_span_inside_the_slice_is_accepted():
    client = _stub_extractor(
        _envelope(
            text="CI runs on every push.",
            origin="observed",
            provenance={"type": "transcript", "start": 0, "end": 10},
        )
    )

    result = extract(client, EXTRACT_BANK, "user: a slice long enough to hold that span")

    assert len(result.candidates) == 1
    assert result.candidates[0].origin == "observed"


@pytest.mark.parametrize(
    "rule",
    [
        "authorization",
        "Working State",
        "correction",
        "negative",
        "impersonal",
    ],
)
def test_the_extraction_mission_pins_the_rules_the_review_found_missing(rule):
    assert rule.lower() in EXTRACTION_PROMPT.lower()


@pytest.mark.parametrize(
    "key",
    [
        "origin",
        "kind",
        "negative",
        "correction",
        "provenance",
        "explicit_request",
        "profile_eligible",
        "eligible",
        "session_id",
        "session_epoch",
        "checkpoint_seq",
        "slice_hash",
    ],
)
def test_classification_metadata_is_reserved_from_callers(key):
    """Invariant 13: eligibility is computed by the harness, never declared
    by an agent -- and a metadata key is a prompt with extra steps."""
    assert key in provenance.RESERVED_KEYS
    with pytest.raises(InvalidMetadata):
        provenance.check_reserved({key: "anything"})


def test_explicit_retain_stamps_its_own_classification(client, hook_user):
    """SPEC §7.6: retain is evidence capture, not durable-memory creation."""
    captured = {}

    class _StubClient:
        def retain_items(self, bank_id, items, **kwargs):
            captured["item"] = items[0]
            return {"operation_id": "op_1", "status": "pending"}

    with mock.patch("memory.api.memory.get_client", return_value=_StubClient()):
        response = client.post(
            "/v1/memory/retain",
            json={"scope": "user", "content": "Remember that I prefer tabs."},
            headers=hook_user["headers"],
        )

    assert response.status_code == 200, response.text
    metadata = captured["item"].metadata
    assert metadata["origin"] == "stated"
    assert metadata["kind"] == "technical_claim"
    assert metadata["explicit_request"] is True
    assert metadata["provenance"] == {"type": "explicit_request"}
    assert captured["item"].tags == ["kind:technical_claim", "evidence_only"]


def test_explicit_retain_refuses_a_caller_supplied_classification(client, hook_user):
    """A caller able to send origin=confirmed + kind=decision could promote
    its own proposal to durable truth with no human ever accepting it."""
    response = client.post(
        "/v1/memory/retain",
        json={
            "scope": "user",
            "content": "We decided to rewrite the importer.",
            "metadata": {"origin": "confirmed", "kind": "decision"},
        },
        headers=hook_user["headers"],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_METADATA"


# ---------------------------------------------------------------------------
# Finding 6: leases are owner/generation fenced, and one row cannot kill the
# loop.
# ---------------------------------------------------------------------------


def _capture_row(session, tenant):
    """One accepted checkpoint, ready to be leased."""
    user = User(id=new_user_id(), tenant_id=tenant, bank_id=str(uuid.uuid4()))
    project = Project(
        internal_id=new_project_internal_id(),
        tenant_id=tenant,
        project_slug="fenced-project",
        bank_id=str(uuid.uuid4()),
        owner_type="user",
        owner_id=user.id,
    )
    session.add_all([user, project])
    session.flush()
    principal = Principal(
        tenant_id=tenant,
        user_id=user.id,
        is_master=False,
        key_id="k",
        groups=frozenset(),
        credential_id="k",
    )
    content = "user: we decided to keep the importer"
    result = repository.accept_checkpoint(
        session,
        principal,
        host="claude-code",
        session_id="sess-fence",
        project_slug="fenced-project",
        git_locator=None,
        workspace_id="ws_" + "a" * 32,
        start_offset=0,
        end_offset=len(content),
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        sanitized_hash=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    )
    session.commit()
    return result.row


def test_a_lease_token_is_never_reused(session, tenant):
    """The generation half of the fence.

    A token derived from the process or its session repeats across cycles,
    so a worker whose lease expired mid-call would still match its own row
    after another worker had taken it and handed it back.
    """
    assert repository.new_lease_owner() != repository.new_lease_owner()


@pytest.mark.parametrize(
    "transition",
    [
        lambda db, row, owner: repository.advance_stage(db, row, owner=owner, status="applying"),
        lambda db, row, owner: repository.release_lease(db, row, owner=owner),
        lambda db, row, owner: repository.record_failure(db, row, owner=owner, error_code="E"),
        lambda db, row, owner: repository.complete(db, row, owner=owner),
    ],
)
def test_a_stale_owner_cannot_transition_a_re_leased_row(session, tenant, transition):
    """The lease expired during an external call and another worker took the
    row. The slow worker's write must not land on top of it."""
    row = _capture_row(session, tenant)
    stale_owner = repository.new_lease_owner("slow")
    repository.acquire_lease(session, owner=stale_owner, lease_seconds=60)
    session.commit()

    # The lease expires and a second worker claims the row.
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    fresh_owner = repository.new_lease_owner("fast")
    assert repository.acquire_lease(session, owner=fresh_owner, lease_seconds=60)
    session.commit()

    with pytest.raises(repository.LeaseLost):
        transition(session, row, stale_owner)


def test_the_current_owner_may_still_transition_the_row(session, tenant):
    """The fence refuses stale owners, not every owner."""
    row = _capture_row(session, tenant)
    owner = repository.new_lease_owner()
    repository.acquire_lease(session, owner=owner, lease_seconds=60)
    session.commit()

    repository.advance_stage(session, row, owner=owner, status="applying")
    session.commit()

    assert row.status == "applying"


def test_a_stale_worker_stage_is_reported_as_lease_lost_not_as_success(session, tenant):
    """process_row swallows LeaseLost and rolls back, rather than letting a
    stale worker's result reach the database."""
    row = _capture_row(session, tenant)
    stale_owner = repository.new_lease_owner("slow")
    repository.acquire_lease(session, owner=stale_owner, lease_seconds=60)
    session.commit()
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    repository.acquire_lease(session, owner=repository.new_lease_owner("fast"), lease_seconds=60)
    session.commit()

    with mock.patch.object(
        worker, "extract", return_value=ExtractionResult(candidates=[], working_state=None)
    ):
        worker.process_row(
            session,
            mock.Mock(),
            row,
            owner=stale_owner,
            lease_seconds=60,
            max_attempts=8,
            correction_refresh_enabled=False,
            profile_delivery_mode="legacy",
        )

    session.refresh(row)
    assert row.status == "pending", "the stale worker must not have advanced the row"


@pytest.mark.parametrize("status", ["failed", "not_found", "cancelled", "something-new"])
def test_a_terminal_hindsight_operation_is_not_treated_as_pending(status):
    """A dead operation id read as 'still pending' parks the row on this
    stage forever, re-polling it every cycle."""
    assert filer.is_pending({"status": status}) is False
    assert filer.is_complete({"status": status}) is False


@pytest.mark.parametrize("status", ["pending", "running"])
def test_an_unfinished_hindsight_operation_is_still_waited_for(status):
    assert filer.is_pending({"status": status}) is True


def test_one_poison_row_does_not_stop_the_rows_behind_it(session, tenant, monkeypatch):
    """A row that fails in a way no stage handler anticipated is charged an
    attempt and skipped; the queue keeps draining."""
    poison = _capture_row(session, tenant)
    good = _capture_row_at(session, tenant, poison, start_offset=100)

    settings = get_settings().model_copy(
        update={"capture_worker_enabled": True, "capture_worker_batch_size": 5}
    )
    processed = []
    real_extract = worker.extract

    def _extract(client, bank_id, content):
        if content == poison.sanitized_content:
            raise RuntimeError("simulated unforeseen failure")
        processed.append(content)
        return ExtractionResult(candidates=[], working_state=None)

    monkeypatch.setattr(worker, "extract", _extract)
    assert real_extract is not _extract

    worker.run_once(session, mock.Mock(), settings=settings)

    session.refresh(poison)
    session.refresh(good)
    assert poison.attempt_count == 1
    assert poison.last_error_code == "UNEXPECTED_ERROR"
    assert good.status == "retaining", "the row behind the poison row still ran"


def _capture_row_at(session, tenant, sibling, *, start_offset: int):
    """A second accepted checkpoint in the same session, later in the file."""
    principal = Principal(
        tenant_id=tenant,
        user_id=sibling.user_id,
        is_master=False,
        key_id="k",
        groups=frozenset(),
        credential_id="k",
    )
    content = "user: and the linter runs in CI"
    result = repository.accept_checkpoint(
        session,
        principal,
        host="claude-code",
        session_id=sibling.session_id,
        project_slug="fenced-project",
        git_locator=None,
        workspace_id=sibling.workspace_id,
        start_offset=start_offset,
        end_offset=start_offset + len(content),
        content_hash=hashlib.sha256((content + "x").encode()).hexdigest(),
        sanitized_hash=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    )
    session.commit()
    return result.row


# ---------------------------------------------------------------------------
# Finding 9: cheap and off by default, and content-free telemetry.
# ---------------------------------------------------------------------------


def _hook_script() -> str:
    return Path("plugins/claude-code/scripts/capture-checkpoint.sh").read_text()


def test_the_hook_checks_the_flag_before_spawning_uvx():
    """This runs on every Stop and PreCompact of every session, so the
    disabled path must not pay for resolving a uvx environment first."""
    lines = [line for line in _hook_script().splitlines() if not line.lstrip().startswith("#")]
    gate = next(i for i, line in enumerate(lines) if "MEMORY_CAPTURE_ENABLED" in line)
    spawn = next(i for i, line in enumerate(lines) if "uvx" in line)

    assert gate < spawn, "the enablement gate must come before the spawn"


def test_the_hook_exits_silently_and_zero_when_capture_is_disabled(tmp_path):
    result = subprocess.run(
        ["bash", "plugins/claude-code/scripts/capture-checkpoint.sh"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "ACH_MEMORY_API_KEY": "mem_key",
            "MEMORY_CAPTURE_ENABLED": "false",
        },
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_the_hook_is_disabled_when_the_flag_is_absent_entirely():
    result = subprocess.run(
        ["bash", "plugins/claude-code/scripts/capture-checkpoint.sh"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "ACH_MEMORY_API_KEY": "mem_key"},
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_the_compose_worker_flag_defaults_to_false():
    """Selecting the capture profile starts the process; it does not also
    enable it. Two switches, both off."""
    compose = Path("docker-compose.yml").read_text()

    assert "MEMORY_CAPTURE_WORKER_ENABLED: ${MEMORY_CAPTURE_WORKER_ENABLED:-false}" in compose
    assert "${MEMORY_CAPTURE_WORKER_ENABLED:-true}" not in compose


def test_the_worker_flag_and_the_capture_flag_both_default_off():
    settings = get_settings()

    assert settings.capture_worker_enabled is False
    assert settings.capture_correction_refresh_enabled is False


def test_checkpoint_acceptance_records_no_slug_and_no_bank_fingerprint(client, hook_user):
    """Telemetry is counts and modes only: this path fires once per Stop
    hook for every session of every user."""
    slug = slug_from_locator(ORIGIN)
    client.post("/v1/projects", json={"project_slug": slug}, headers=hook_user["headers"])
    content = "user: we keep the importer"
    body = CheckpointSubmission(
        host="claude-code",
        session_id="sess-telemetry",
        project_slug=slug,
        workspace_id="ws_" + "b" * 32,
        start_offset=0,
        end_offset=len(content),
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        sanitized_hash=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    ).model_dump(exclude_none=True)

    before = _metric_text()
    response = client.post("/v1/capture/checkpoints", json=body, headers=hook_user["headers"])
    assert response.status_code == 202, response.text
    after = _metric_text()

    assert "memory_capture_checkpoint_total" in after
    for forbidden in (slug, "sess-telemetry", body["content_hash"]):
        assert forbidden not in after
    assert before != after


def _metric_text() -> str:
    from prometheus_client import generate_latest

    return generate_latest().decode()


def test_the_capture_metric_labels_are_all_closed_sets():
    """A caller must not be able to mint a time series by inventing a host."""
    assert metrics.host_label("claude-code") == "claude-code"
    assert metrics.host_label("attacker-supplied-host") == "other"
    assert metrics.bytes_bucket(10) == "1k"
    assert metrics.bytes_bucket(100_000) == "64k+"
    assert len({metrics.bytes_bucket(n) for n in range(0, 200_000, 977)}) <= 5


def test_a_retry_counter_exists_and_is_content_free():
    metrics.CAPTURE_RETRY.labels(stage="retain", error_code="RETAIN_FAILED").inc()

    assert "memory_capture_retries_total" in _metric_text()
    assert metrics.CAPTURE_RETRY._labelnames == ("stage", "error_code")


def test_bank_ids_are_redacted_even_when_embedded_in_a_longer_string():
    """An exact-match-only filter leaks every embedded occurrence -- in a
    URL a config echoes, in an error string, in a model name."""
    bank_id = "user_11111111-1111-1111-1111-111111111111"

    redacted = configuration.redact_for_display(
        {
            "exact": bank_id,
            "embedded": f"https://hindsight/v1/banks/{bank_id}/memories",
            "nested": [{"error": f"bank {bank_id} not found"}],
        },
        bank_id,
    )

    assert bank_id not in json.dumps(redacted)
    assert redacted["exact"] == activity.fingerprint(bank_id)


# ---------------------------------------------------------------------------
# Review round 2, finding 3: a semantic rule drops one candidate, not the
# whole slice.
# ---------------------------------------------------------------------------


@respx.mock
def test_a_semantically_dropped_candidate_leaves_its_siblings_intact():
    """The blast radius of the two new semantic rules.

    Failing the whole extraction would discard every good candidate beside
    the offending one and then retry the identical prompt to the attempt
    limit, losing the slice permanently.
    """
    client = _stub_extractor(
        _envelope(text="The team uses trunk-based development."),
        # Mislabelled: negative=true with no prohibition in the text.
        _envelope(text="The team uses feature flags.", negative=True),
        _envelope(text="Migrations run before deploys."),
    )

    result = extract(client, EXTRACT_BANK, "user: a slice about how the team works")

    assert [c.text for c in result.candidates] == [
        "The team uses trunk-based development.",
        "Migrations run before deploys.",
    ]
    assert result.dropped == 1


@respx.mock
def test_local_scope_widening_drops_only_the_offending_candidate():
    client = _stub_extractor(
        _envelope(text="Prefers tabs over spaces.", kind="preference", subject="user"),
        _envelope(
            text="Always run make check here before pushing.",
            kind="preference",
            subject="user",
        ),
    )

    result = extract(client, EXTRACT_BANK, "user: a slice about preferences")

    assert [c.text for c in result.candidates] == ["Prefers tabs over spaces."]
    assert result.dropped == 1


@respx.mock
def test_a_routing_error_still_fails_the_whole_slice():
    """Drop-one is for semantic disagreement, not for a misread slice: a
    preference aimed at the project bank means the model misunderstood which
    bank it was writing to, and the rest of its output is not more
    trustworthy for it."""
    client = _stub_extractor(
        _envelope(text="The team uses trunk-based development."),
        _envelope(text="Prefers tabs over spaces.", kind="preference", subject="project"),
    )

    with pytest.raises(ExtractionFailed):
        extract(client, EXTRACT_BANK, "user: a slice")


@respx.mock
def test_a_malformed_envelope_still_fails_the_whole_slice():
    """Parse and schema failures keep their atomicity."""
    respx.post(
        f"{HINDSIGHT}/v1/default/banks/{EXTRACT_BANK}/memories/dry-run-extract"
    ).mock(return_value=httpx.Response(200, json={"facts": [{"text": "{not json"}]}))
    client = HindsightClient(base_url=HINDSIGHT, api_key="secret", tenant_id="default")

    with pytest.raises(ExtractionFailed):
        extract(client, EXTRACT_BANK, "user: a slice")


@pytest.mark.parametrize(
    "text",
    [
        "Refrain from force-pushing to main.",
        "Skip the full suite on a docs-only change.",
        "Omit the debug flag in production builds.",
        "Never force-push to main.",
        "Do not commit generated files.",
        "Avoid global installs.",
        "Exclude vendored code from coverage.",
        "Leave out the timing logs.",
    ],
)
def test_ordinary_prohibitions_are_recognized_as_negative_claims(text):
    """A prohibition with no negation particle in it is still a prohibition."""
    result = classify(_envelope(text=text, negative=True))

    assert result.negative is True


# ---------------------------------------------------------------------------
# Review round 2, finding 1: the fence made an overrun safe; renewal makes it
# live.
# ---------------------------------------------------------------------------


def test_renew_lease_extends_the_window_for_the_current_owner(session, tenant):
    row = _capture_row(session, tenant)
    owner = repository.new_lease_owner()
    repository.acquire_lease(session, owner=owner, lease_seconds=60)
    session.commit()
    original = row.lease_until

    repository.renew_lease(session, row, owner=owner, lease_seconds=600)
    session.commit()

    assert row.lease_until > original
    assert row.lease_owner == owner


def test_renew_lease_refuses_an_owner_that_no_longer_holds_the_row(session, tenant):
    """There is nothing to renew, and the caller must not proceed."""
    row = _capture_row(session, tenant)
    stale = repository.new_lease_owner("slow")
    repository.acquire_lease(session, owner=stale, lease_seconds=60)
    session.commit()
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    repository.acquire_lease(session, owner=repository.new_lease_owner("fast"), lease_seconds=60)
    session.commit()

    with pytest.raises(repository.LeaseLost):
        repository.renew_lease(session, row, owner=stale, lease_seconds=60)


def test_a_row_queued_behind_a_slow_sibling_is_not_stolen_mid_flight(
    session, tenant, monkeypatch
):
    """The batch case the fence alone did not cover.

    Five rows are leased at once but processed one at a time. If the first
    row's extraction takes longer than the lease, every row behind it is
    stealable before its own work even starts -- so it gets stolen, and the
    LLM call this worker is about to make on it is thrown away. Renewing
    immediately before the call gives the row a full window of its own.
    """
    slow = _capture_row(session, tenant)
    queued = _capture_row_at(session, tenant, slow, start_offset=100)

    owner = repository.new_lease_owner("worker-a")
    leased = repository.acquire_lease(session, owner=owner, lease_seconds=60, batch_size=5)
    assert len({row.id for row in leased}) == 2
    session.commit()

    # The lease window elapses while the first row is still extracting.
    for row in leased:
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    thief = repository.new_lease_owner("worker-b")
    stolen: list = []

    def _extract_that_a_rival_worker_races(client, bank_id, content):
        stolen.extend(
            repository.acquire_lease(session, owner=thief, lease_seconds=60, batch_size=5)
        )
        session.commit()
        return ExtractionResult(candidates=[], working_state=None)

    monkeypatch.setattr(worker, "extract", _extract_that_a_rival_worker_races)

    worker.process_row(
        session,
        mock.Mock(),
        queued,
        owner=owner,
        lease_seconds=60,
        max_attempts=8,
        correction_refresh_enabled=False,
        profile_delivery_mode="legacy",
    )

    assert queued.id not in {row.id for row in stolen}, "the row was stolen mid-extraction"
    # The row this worker was NOT processing is still fair game -- renewal
    # protects the row in flight, it does not hold the whole batch hostage.
    assert slow.id in {row.id for row in stolen}
    session.refresh(queued)
    assert queued.status == "retaining", "the extraction result was kept"


def test_the_lease_is_renewed_on_both_sides_of_the_external_call(session, tenant, monkeypatch):
    """Before, so the call starts with a full window; after, so a call that
    outran even a fresh lease is caught before its result is written."""
    row = _capture_row(session, tenant)
    owner = repository.new_lease_owner()
    repository.acquire_lease(session, owner=owner, lease_seconds=60)
    session.commit()

    renewals: list[str] = []
    real_renew = repository.renew_lease

    def _counting_renew(db, target, *, owner, lease_seconds):
        renewals.append("renew")
        return real_renew(db, target, owner=owner, lease_seconds=lease_seconds)

    monkeypatch.setattr(worker.repository, "renew_lease", _counting_renew)
    monkeypatch.setattr(
        worker,
        "extract",
        lambda client, bank_id, content: (
            renewals.append("external-call"),
            ExtractionResult(candidates=[], working_state=None),
        )[1],
    )

    worker.process_row(
        session,
        mock.Mock(),
        row,
        owner=owner,
        lease_seconds=60,
        max_attempts=8,
        correction_refresh_enabled=False,
        profile_delivery_mode="legacy",
    )

    assert renewals == ["renew", "external-call", "renew"]


# ---------------------------------------------------------------------------
# Review round 2, finding 4: per-record [raw_start, raw_end) markers.
# ---------------------------------------------------------------------------


def test_every_included_sanitized_record_carries_its_own_raw_span(tmp_path):
    """Batch-level offsets say which bytes the batch covers; these say which
    bytes each individual sanitized record came from.

    The sanitized form is a lossy projection of the transcript, so without
    the per-record mapping a claim is only traceable back to the whole
    batch.
    """
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        _user_line("Always run the linter before pushing."),
        _user_line("Never force-push to main."),
        _user_line("We decided to keep the importer."),
    ]
    transcript.write_bytes(b"".join(lines))

    batch = local.build_batch(local.read_new_slice(transcript, 0))

    # One span per record, each exactly the bytes of that record's line.
    expected = []
    start = 0
    for line in lines:
        expected.append((start, start + len(line)))
        start += len(line)
    assert batch.record_spans == expected

    # And each span is written into the content ahead of the record it marks.
    body = batch.content.splitlines()
    assert body == [
        local.raw_marker(0, expected[0][1]),
        "user: Always run the linter before pushing.",
        local.raw_marker(expected[0][1], expected[1][1]),
        "user: Never force-push to main.",
        local.raw_marker(expected[1][1], expected[2][1]),
        "user: We decided to keep the importer.",
    ]


def test_a_raw_span_names_the_exact_bytes_of_its_record(tmp_path):
    """The span is checkable against the file, not just internally consistent."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(
        _user_line("first claim about the build") + _user_line("second claim about the tests")
    )

    batch = local.build_batch(local.read_new_slice(transcript, 0))
    raw = transcript.read_bytes()

    assert len(batch.record_spans) == 2
    for (raw_start, raw_end), needle in zip(
        batch.record_spans, ["first claim about the build", "second claim about the tests"]
    ):
        record = json.loads(raw[raw_start:raw_end])
        assert record["message"]["content"] == needle


def test_raw_spans_continue_across_checkpoints_in_absolute_file_offsets(tmp_path):
    """A second checkpoint's markers are offsets into the file, not into its
    own slice -- otherwise every batch would restart at zero and two records
    would claim the same bytes."""
    transcript = tmp_path / "transcript.jsonl"
    first_line = _user_line("the first checkpoint")
    transcript.write_bytes(first_line)
    first = local.build_batch(local.read_new_slice(transcript, 0))

    transcript.write_bytes(first_line + _user_line("the second checkpoint"))
    second = local.build_batch(local.read_new_slice(transcript, first.end_offset))

    assert first.record_spans == [(0, len(first_line))]
    assert second.record_spans == [(len(first_line), transcript.stat().st_size)]
    assert local.raw_marker(len(first_line), transcript.stat().st_size) in second.content


def test_a_record_with_no_retained_text_gets_no_marker(tmp_path):
    """There is no sanitized record for it to mark, and a marker announcing
    an absence would spend bytes saying nothing."""
    transcript = tmp_path / "transcript.jsonl"
    summary = _line(type="summary", summary="Hi there!", leafUuid="u1")
    kept = _user_line("Always run make check.")
    transcript.write_bytes(summary + kept)

    batch = local.build_batch(local.read_new_slice(transcript, 0))

    assert batch.content.count("[raw ") == 1
    # The marker names the marked record's OWN bytes, not the dropped
    # summary's as well: a span that swallowed the summary would point at
    # bytes the sanitized text beside it never came from.
    assert batch.record_spans == [(len(summary), len(summary) + len(kept))]
    # The cursor still crosses the summary -- it is acknowledged, just not
    # marked.
    assert batch.end_offset == transcript.stat().st_size


def test_the_markers_are_code_owned_and_the_prompt_says_so():
    """The model may not emit or alter one, and must not read the byte
    numbers as provenance offsets."""
    assert "[raw N:M)" in EXTRACTION_PROMPT
    assert "never emit one" in EXTRACTION_PROMPT


@respx.mock
def test_the_submitted_body_carries_the_markers(tmp_path):
    """They travel with the content, not just in a local dataclass."""
    repo = _git_repo(tmp_path)
    slug = slug_from_locator(ORIGIN)
    transcript = repo / "transcript.jsonl"
    transcript.write_bytes(_user_line("Always run the linter before pushing."))

    route = respx.post(f"{HOOK_URL}/v1/capture/checkpoints").mock(
        return_value=_accepted_response(transcript.stat().st_size, slug)
    )
    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(repo)},
        env=_hook_env(tmp_path),
    )

    body = json.loads(route.calls.last.request.read())
    assert local.raw_marker(0, transcript.stat().st_size) in body["content"]
    # And the hash still covers exactly what was sent.
    assert (
        hashlib.sha256(body["content"].encode()).hexdigest() == body["sanitized_hash"]
    )
