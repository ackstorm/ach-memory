import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx

from memory.capture import local

FIXTURES = Path(__file__).parent / "fixtures" / "claude-transcripts"
URL = "http://ach-memory.test"


def _line(**fields) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def _write(path: Path, *lines: bytes) -> None:
    with open(path, "wb") as handle:
        handle.writelines(lines)


def _user_line(text: str, session_id: str = "s1") -> bytes:
    return _line(
        type="user",
        message={"role": "user", "content": text},
        sessionId=session_id,
        cwd="/repo",
    )


# ---------------------------------------------------------------------------
# Slicing: offsets, unterminated records, empty slices, truncation
# ---------------------------------------------------------------------------


def test_read_new_slice_keeps_only_complete_newline_terminated_records(tmp_path):
    complete = _user_line("first") + _user_line("second")
    partial = b'{"type":"user","message":{"role":"user","content":"cut off'
    path = tmp_path / "t.jsonl"
    _write(path, complete, partial)

    result = local.read_new_slice(path, 0)

    assert len(result.records) == 2
    assert result.end_offset == len(complete)
    assert result.raw == complete


def test_read_new_slice_advances_incrementally_from_a_prior_offset(tmp_path):
    first = _user_line("first")
    second = _user_line("second")
    path = tmp_path / "t.jsonl"
    _write(path, first)
    first_result = local.read_new_slice(path, 0)
    assert first_result.end_offset == len(first)

    _write(path, first, second)
    second_result = local.read_new_slice(path, first_result.end_offset)

    assert len(second_result.records) == 1
    assert second_result.raw == second
    assert second_result.end_offset == len(first) + len(second)


def test_read_new_slice_is_empty_when_there_is_nothing_new(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _user_line("only"))
    end = local.read_new_slice(path, 0).end_offset

    result = local.read_new_slice(path, end)

    assert result.records == []
    assert result.raw == b""
    assert result.end_offset == end


def test_read_new_slice_is_empty_for_an_unterminated_file(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, b'{"type":"user","message":{"role":"user","content":"no newline yet"}')

    result = local.read_new_slice(path, 0)

    assert result.records == []
    assert result.end_offset == 0


def test_read_new_slice_restarts_from_zero_when_the_file_shrinks(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _user_line("a"), _user_line("b"), _user_line("c"))
    far_offset = local.read_new_slice(path, 0).end_offset

    # Transcript rewritten shorter than the stored cursor (rotation/rewrite).
    _write(path, _user_line("only-one-now"))

    result = local.read_new_slice(path, far_offset)

    assert len(result.records) == 1
    assert result.end_offset == len(_user_line("only-one-now"))


def test_missing_transcript_file_is_an_empty_slice_not_an_error(tmp_path):
    result = local.read_new_slice(tmp_path / "does-not-exist.jsonl", 0)

    assert result.records == []
    assert result.end_offset == 0


# ---------------------------------------------------------------------------
# Identity: raw bytes vs. sanitized text
# ---------------------------------------------------------------------------


def test_content_hash_covers_raw_bytes_while_only_sanitized_text_is_submittable(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _user_line("Bearer sk-CANARYIDENTITY1234567890"))

    result = local.read_new_slice(path, 0)
    sanitized = local.sanitize(result.records)

    assert hashlib.sha256(result.raw).hexdigest() != hashlib.sha256(
        sanitized.encode()
    ).hexdigest()
    assert b"CANARY" in result.raw
    assert "CANARY" not in sanitized


# ---------------------------------------------------------------------------
# Sanitization content rules, exercised through the checked-in fixtures
# ---------------------------------------------------------------------------


def _sanitize_fixture(name: str) -> str:
    path = FIXTURES / name
    result = local.read_new_slice(path, 0)
    assert result.records, f"fixture {name} produced no complete records"
    return local.sanitize(result.records)


def test_basic_transcript_keeps_dialogue_and_minimal_tool_evidence():
    text = _sanitize_fixture("basic.jsonl")

    assert "user: Can you check whether the auth middleware" in text
    assert "assistant: I'll check the auth middleware" in text
    assert "tool_use: Bash" in text
    assert 'tool_result: Bash ok 42: if payload' in text
    assert "assistant: Yes, line 42 raises Expired" in text


def test_basic_transcript_drops_the_non_whitelisted_summary_record():
    text = _sanitize_fixture("basic.jsonl")

    # The "summary" record type carries a greeting-shaped line; it is not in
    # the user/assistant whitelist and must not survive at all.
    assert "Hey there" not in text
    assert "summary" not in text.lower()


def test_redaction_fixture_strips_every_secret_shape():
    text = _sanitize_fixture("redaction.jsonl")

    # One blunt check first: no canary of any kind survives, in any form.
    assert "CANARY" not in text
    assert "[redacted]" in text


def test_redaction_fixture_replaces_file_reading_tool_output_with_a_bounded_marker():
    text = _sanitize_fixture("redaction.jsonl")

    assert "tool_use: Read" in text
    assert "[file content omitted]" in text
    # Neither the file path (tool_use input) nor the file body (tool_result
    # content) is redacted-in-place -- both are dropped outright.
    assert "id_rsa" not in text
    assert "OPENSSH" not in text


def test_redaction_fixture_drops_the_summary_canary_too():
    text = _sanitize_fixture("redaction.jsonl")

    assert "Hi there" not in text


def test_tool_output_is_capped_per_call():
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
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "x" * (local._TOOL_OUTPUT_CAP * 5),
                        "is_error": False,
                    }
                ],
            },
        },
    ]

    text = local.sanitize(records)

    result_line = next(line for line in text.splitlines() if line.startswith("tool_result"))
    assert len(result_line) <= local._TOOL_OUTPUT_CAP + len("tool_result: Bash ok ")


def test_whole_slice_is_capped_even_across_many_small_messages():
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"line {i}: " + "y" * 200},
        }
        for i in range(500)
    ]

    text = local.sanitize(records)

    assert len(text) <= local._SLICE_CAP


# ---------------------------------------------------------------------------
# Cursor: atomic persistence, filename safety, locking
# ---------------------------------------------------------------------------


def test_cursor_round_trips_and_is_written_atomically_with_restricted_mode(tmp_path):
    path = tmp_path / "capture" / "abc.cursor"
    path.parent.mkdir(parents=True)

    local.write_cursor(path, 4812)

    assert local.read_cursor(path) == 4812
    assert oct(path.stat().st_mode)[-3:] == "600"
    # No leftover temp files from the atomic replace.
    assert list(path.parent.iterdir()) == [path]


def test_cursor_defaults_to_zero_when_absent(tmp_path):
    assert local.read_cursor(tmp_path / "missing.cursor") == 0


def test_cursor_digest_never_embeds_raw_identifying_material():
    path = local.cursor_path(
        Path("/cache"),
        owner_fingerprint="owner-secret-abc",
        service_url="https://ach-memory.example.com",
        project_key="git@github.com:acme/super-secret-repo.git",
        workspace_id="ws_" + "a" * 32,
        session_id="session-with-a-readable-name",
    )

    rendered = str(path)
    for sensitive in (
        "owner-secret-abc",
        "ach-memory.example.com",
        "super-secret-repo",
        "session-with-a-readable-name",
    ):
        assert sensitive not in rendered


def test_locked_cursor_serializes_concurrent_hook_events(tmp_path):
    path = tmp_path / "capture" / "concurrent.cursor"
    events: list[str] = []

    def worker(name: str) -> None:
        with local.locked_cursor(path):
            events.append(f"{name}-start")
            time.sleep(0.05)
            events.append(f"{name}-end")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("stop", "precompact")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Whichever ran first must fully finish before the other starts -- no
    # "stop-start, precompact-start, stop-end, precompact-end" interleaving.
    assert events[0].endswith("-start")
    assert events[1].endswith("-end")
    assert events[0].split("-")[0] == events[1].split("-")[0]


# ---------------------------------------------------------------------------
# checkpoint(): the full pipeline, prerequisites, retries, silence
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/acme/super-secret.git"],
        cwd=repo,
        check=True,
    )
    return repo


@pytest.fixture
def base_env(tmp_path) -> dict:
    return {
        "MEMORY_CAPTURE_ENABLED": "true",
        "ACH_MEMORY_API_KEY": "mem_test_key",
        "ACH_MEMORY_URL": URL,
        "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
        "HOME": str(tmp_path),
    }


def _copy_fixture(git_repo: Path, name: str) -> Path:
    transcript = git_repo / "transcript.jsonl"
    transcript.write_bytes((FIXTURES / name).read_bytes())
    return transcript


@respx.mock
def test_checkpoint_submits_the_sanitized_slice_and_advances_the_cursor(git_repo, base_env):
    transcript = _copy_fixture(git_repo, "redaction.jsonl")
    route = respx.post(f"{URL}/v1/capture/checkpoints").mock(
        return_value=httpx.Response(
            202,
            json={
                "capture_id": "cap_1",
                "status": "pending",
                "duplicate": False,
                "session_epoch": 1,
                "checkpoint_seq": transcript.stat().st_size,
                "project_slug": "super-secret",
                "resolved_from": None,
            },
        )
    )

    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(git_repo)},
        env=base_env,
    )

    assert route.called
    request_body = json.loads(route.calls.last.request.read())
    assert request_body["session_id"] == "sess-1"
    assert request_body["git_locator"] == "https://example.com/acme/super-secret.git"
    assert request_body["workspace_id"] == local.workspace_id_for(str(git_repo))
    for sensitive in ("CANARY", str(git_repo), "id_rsa", "super-secret.git"):
        assert sensitive not in request_body["content"]

    path = local.cursor_path(
        Path(base_env["ACH_MEMORY_CACHE_DIR"]),
        owner_fingerprint=hashlib.sha256(base_env["ACH_MEMORY_API_KEY"].encode()).hexdigest(),
        service_url=base_env["ACH_MEMORY_URL"],
        project_key="https://example.com/acme/super-secret.git",
        workspace_id=local.workspace_id_for(str(git_repo)),
        session_id="sess-1",
    )
    assert local.read_cursor(path) == transcript.stat().st_size


@respx.mock
def test_a_lost_acknowledgement_resubmits_the_identical_slice(git_repo, base_env):
    transcript = _copy_fixture(git_repo, "basic.jsonl")
    route = respx.post(f"{URL}/v1/capture/checkpoints").mock(
        side_effect=httpx.ConnectError("boom")
    )
    hook_event = {
        "transcript_path": str(transcript),
        "session_id": "sess-1",
        "cwd": str(git_repo),
    }

    local.checkpoint(hook_event, env=base_env)
    local.checkpoint(hook_event, env=base_env)

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.read())
    second = json.loads(route.calls[1].request.read())
    assert first == second


@respx.mock
def test_a_non_202_response_does_not_advance_the_cursor(git_repo, base_env):
    transcript = _copy_fixture(git_repo, "basic.jsonl")
    respx.post(f"{URL}/v1/capture/checkpoints").mock(return_value=httpx.Response(500))
    hook_event = {
        "transcript_path": str(transcript),
        "session_id": "sess-1",
        "cwd": str(git_repo),
    }

    local.checkpoint(hook_event, env=base_env)

    path = local.cursor_path(
        Path(base_env["ACH_MEMORY_CACHE_DIR"]),
        owner_fingerprint=hashlib.sha256(base_env["ACH_MEMORY_API_KEY"].encode()).hexdigest(),
        service_url=base_env["ACH_MEMORY_URL"],
        project_key="https://example.com/acme/super-secret.git",
        workspace_id=local.workspace_id_for(str(git_repo)),
        session_id="sess-1",
    )
    assert local.read_cursor(path) == 0


@respx.mock
def test_checkpoint_is_silent_without_an_api_key(git_repo, base_env, capsys):
    transcript = _copy_fixture(git_repo, "basic.jsonl")
    route = respx.post(f"{URL}/v1/capture/checkpoints").mock(return_value=httpx.Response(202))
    base_env["ACH_MEMORY_API_KEY"] = ""

    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(git_repo)},
        env=base_env,
    )

    assert not route.called
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@respx.mock
def test_checkpoint_is_silent_when_capture_is_not_enabled(git_repo, base_env):
    transcript = _copy_fixture(git_repo, "basic.jsonl")
    route = respx.post(f"{URL}/v1/capture/checkpoints").mock(return_value=httpx.Response(202))
    base_env["MEMORY_CAPTURE_ENABLED"] = "false"

    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(git_repo)},
        env=base_env,
    )

    assert not route.called


@respx.mock
def test_checkpoint_is_silent_outside_a_git_repository(tmp_path, base_env):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes((FIXTURES / "basic.jsonl").read_bytes())
    route = respx.post(f"{URL}/v1/capture/checkpoints").mock(return_value=httpx.Response(202))

    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(tmp_path)},
        env=base_env,
    )

    assert not route.called


def test_checkpoint_never_raises_on_a_malformed_hook_event(base_env):
    local.checkpoint({}, env=base_env)
    local.checkpoint({"session_id": "s1"}, env=base_env)
    local.checkpoint(None or {}, env=base_env)


@respx.mock
def test_checkpoint_produces_no_stdout_or_stderr_on_success(git_repo, base_env, capsys):
    transcript = _copy_fixture(git_repo, "basic.jsonl")
    respx.post(f"{URL}/v1/capture/checkpoints").mock(
        return_value=httpx.Response(
            202,
            json={
                "capture_id": "cap_1",
                "status": "pending",
                "duplicate": False,
                "checkpoint_seq": transcript.stat().st_size,
            },
        )
    )

    local.checkpoint(
        {"transcript_path": str(transcript), "session_id": "sess-1", "cwd": str(git_repo)},
        env=base_env,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
