"""The local half of transcript checkpointing: read only the unprocessed
complete records of a Claude Code transcript, sanitize them before anything
crosses the network, identify the slice, and submit it -- advancing the
on-disk cursor only after the server has durably accepted that exact
identity (SPEC Phase 3 §4.2-§6).

This module is capture PLUMBING, not a semantic agent (Phase 3's
non-negotiable contract): it never decides memory kind, durability, bank or
Working State content. It only slices, redacts structurally, and submits.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from memory.capture.contracts import CheckpointAccepted, CheckpointSubmission

# --------------------------------------------------------------------------
# Record whitelist and slicing
# --------------------------------------------------------------------------

# Everything else -- summaries, meta records, queued-command output, any
# record type this client does not explicitly recognize -- is dropped
# whole. A canary planted in an unrecognized record type must never survive
# by accident just because some future record type resembles these two.
_ALLOWED_RECORD_TYPES = frozenset({"user", "assistant"})

# Tools whose result is a file's bytes reproduced verbatim: the file is
# already in the repository (or trivially re-readable), so its body is
# never worth the network trip or the leak surface. Only the fact that the
# tool ran, and whether it succeeded, survives.
_FILE_READING_TOOLS = frozenset({"Read", "NotebookRead"})

_TEXT_BLOCK_CAP = 4000
_TOOL_OUTPUT_CAP = 2000
_SLICE_CAP = 40_000


def _iter_complete_records(data: bytes) -> Iterator[tuple[dict, int]]:
    """Yield `(record, offset_after_this_line)` for each newline-terminated,
    well-formed JSON *object* line in `data`, in order.

    Stops at the first line that is not newline-terminated (still being
    written) or not a well-formed JSON object (a torn write, or content this
    client should never guess at) -- fail closed, offset never advances past
    a boundary this client cannot prove is a complete record.
    """
    offset = 0
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        stripped = line.strip()
        if not stripped:
            offset += len(line)
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            break
        if not isinstance(record, dict):
            break
        offset += len(line)
        yield record, offset


@dataclass(frozen=True)
class RawSlice:
    """A byte-identical slice of complete records plus where it ends.

    `raw` is exactly the bytes `content_hash` is computed over (SPEC: a
    slice identity is `(session_id, start_offset, end_offset, content_hash)`
    where `content_hash` is SHA-256 of the original complete-record byte
    slice) -- it is never sent over the network; only `sanitize()`'s output
    of the parsed records is.
    """

    raw: bytes
    records: list[dict] = field(default_factory=list)
    end_offset: int = 0


def read_new_slice(transcript_path: Path, start_offset: int) -> RawSlice:
    """The complete new records after `start_offset`, or an empty slice at
    `start_offset` (rewound to 0 first, if the file is now shorter than
    `start_offset` -- a rotated/rewritten transcript) if there are none yet.
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return RawSlice(raw=b"", records=[], end_offset=start_offset)

    if size < start_offset:
        start_offset = 0
    if size <= start_offset:
        return RawSlice(raw=b"", records=[], end_offset=start_offset)

    with transcript_path.open("rb") as handle:
        handle.seek(start_offset)
        chunk = handle.read()

    records: list[dict] = []
    consumed = 0
    for record, end in _iter_complete_records(chunk):
        records.append(record)
        consumed = end

    if consumed == 0:
        return RawSlice(raw=b"", records=[], end_offset=start_offset)
    return RawSlice(
        raw=chunk[:consumed], records=records, end_offset=start_offset + consumed
    )


# --------------------------------------------------------------------------
# Sanitization
# --------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{10,}"),
    # Common vendor token shapes: sk-..., ghp_..., xoxb-..., mem_..., etc.
    re.compile(r"\b(?:sk|gh[oprsu]|mem|xox[baprs])[-_][A-Za-z0-9]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    # scheme://user:pass@host credential URLs.
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@\S+"),
    # Assignment-style secrets: FOO_TOKEN=..., "password": "...", etc.
    re.compile(r"(?i)\b\w*(?:secret|password|passwd|token|api[_-]?key)\w*\s*[=:]\s*\S+"),
]


def redact(text: str) -> str:
    """Structural, not semantic: fixed patterns for shapes secrets commonly
    take. This is a floor, not a promise -- it cannot catch a secret with no
    recognizable shape, which is exactly why file bodies and tool inputs are
    dropped outright instead of redacted in place (see `_sanitize_block`)."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _text_from_content(content: object) -> str:
    """`tool_result.content` is a string, a list of content blocks, or
    absent -- normalize to plain text, keeping only what is itself text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def _message_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def sanitize(records: list[dict]) -> str:
    """The sanitized, submittable text for a list of parsed transcript
    records. Whitelisted record/content types only; everything else -- and
    every raw file body and tool input -- is dropped, not merely capped.
    """
    tool_names: dict[str, str] = {}
    lines: list[str] = []

    for record in records:
        if record.get("type") not in _ALLOWED_RECORD_TYPES:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue

        for block in _message_blocks(message):
            block_type = block.get("type")

            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    lines.append(f"{role}: {redact(text)[:_TEXT_BLOCK_CAP]}")

            elif block_type == "tool_use":
                name = block.get("name")
                if not isinstance(name, str):
                    continue
                tool_id = block.get("id")
                if isinstance(tool_id, str):
                    tool_names[tool_id] = name
                # Input is dropped whole, not redacted: a file path, a shell
                # command, a URL query string are all plausible secret or
                # cheaply-reproducible-content carriers with no fixed shape
                # `redact()` could reliably catch.
                lines.append(f"tool_use: {name}")

            elif block_type == "tool_result":
                tool_use_id = block.get("tool_use_id")
                name = tool_names.get(tool_use_id) if isinstance(tool_use_id, str) else None
                status = "error" if block.get("is_error") else "ok"
                if name in _FILE_READING_TOOLS:
                    lines.append(f"tool_result: {name} {status} [file content omitted]")
                else:
                    text = redact(_text_from_content(block.get("content")))[:_TOOL_OUTPUT_CAP]
                    label = name or "unknown"
                    lines.append(f"tool_result: {label} {status} {text}".rstrip())

    return "\n".join(lines)[:_SLICE_CAP]


# --------------------------------------------------------------------------
# Cursor: locked, atomic, filename-safe
# --------------------------------------------------------------------------


def cursor_digest(
    *, owner_fingerprint: str, service_url: str, project_key: str, workspace_id: str,
    session_id: str,
) -> str:
    material = f"{owner_fingerprint}|{service_url}|{project_key}|{workspace_id}|{session_id}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def cursor_path(cache_dir: Path, **digest_kwargs: str) -> Path:
    return cache_dir / "capture" / f"{cursor_digest(**digest_kwargs)}.cursor"


@contextmanager
def locked_cursor(path: Path) -> Iterator[None]:
    """Advisory exclusive lock held across read-build-submit-update, so two
    hook events firing at once (a real occurrence: Stop and a fast
    PreCompact can overlap) serialize onto one cursor instead of both
    computing the same start_offset and racing to submit divergent slices.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = Path(str(path) + ".lock")
    with open(lock_path, "a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_cursor(path: Path) -> int:
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, value)


def write_cursor(path: Path, offset: int) -> None:
    """Temp file + fsync + atomic replace, mode 0600: a crash mid-write must
    never leave a truncated or partially-written cursor that resubmits a
    wrong slice."""
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with open(tmp_path, "w") as handle:
        handle.write(str(offset))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


# --------------------------------------------------------------------------
# git-derived identity (never the raw path)
# --------------------------------------------------------------------------


def _run_git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_locator(cwd: str) -> str | None:
    return _run_git(cwd, "remote", "get-url", "origin")


def workspace_id_for(cwd: str) -> str | None:
    root = _run_git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    canonical = str(Path(root).resolve())
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"ws_{digest}"


# --------------------------------------------------------------------------
# Submission and the top-level entry point
# --------------------------------------------------------------------------

_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def _post_checkpoint(
    url: str, api_key: str, body: CheckpointSubmission
) -> httpx.Response | None:
    try:
        return httpx.post(
            f"{url.rstrip('/')}/v1/capture/checkpoints",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body.model_dump(exclude_none=True),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        return None


def _default_cache_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path(env.get("HOME", "/tmp")) / ".cache"
    return base / "ach-memory"


def checkpoint(hook_event: dict, *, env: Mapping[str, str] | None = None) -> None:
    """Entry point for `ach-memory capture-checkpoint`. Never raises: every
    missing prerequisite or transport failure is a silent no-op, matching
    the hook's own contract of zero stdout/stderr and exit 0 regardless.
    """
    try:
        _checkpoint(hook_event, env if env is not None else os.environ)
    except Exception:  # noqa: BLE001 -- must never propagate, see docstring
        return


def _checkpoint(hook_event: dict, env: Mapping[str, str]) -> None:
    if env.get("MEMORY_CAPTURE_ENABLED", "false").strip().lower() not in ("1", "true", "yes"):
        return

    api_key = env.get("ACH_MEMORY_API_KEY", "").strip()
    if not api_key:
        return

    transcript_path_raw = hook_event.get("transcript_path")
    session_id = hook_event.get("session_id")
    cwd = hook_event.get("cwd")
    if not transcript_path_raw or not session_id or not cwd:
        return

    locator = git_locator(cwd)
    workspace = workspace_id_for(cwd)
    if not locator or not workspace:
        return

    url = env.get("ACH_MEMORY_URL", "http://localhost:8000")
    cache_dir = Path(env["ACH_MEMORY_CACHE_DIR"]) if env.get("ACH_MEMORY_CACHE_DIR") else _default_cache_dir(env)
    owner_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    path = cursor_path(
        cache_dir,
        owner_fingerprint=owner_fingerprint,
        service_url=url,
        project_key=locator,
        workspace_id=workspace,
        session_id=str(session_id),
    )

    transcript_path = Path(transcript_path_raw)
    with locked_cursor(path):
        start = read_cursor(path)
        raw_slice = read_new_slice(transcript_path, start)
        if not raw_slice.records:
            return

        sanitized = sanitize(raw_slice.records)
        body = CheckpointSubmission(
            host="claude-code",
            session_id=str(session_id),
            git_locator=locator,
            workspace_id=workspace,
            start_offset=start,
            end_offset=raw_slice.end_offset,
            content_hash=hashlib.sha256(raw_slice.raw).hexdigest(),
            sanitized_hash=hashlib.sha256(sanitized.encode()).hexdigest(),
            content=sanitized,
        )

        response = _post_checkpoint(url, api_key, body)
        if response is None or response.status_code != 202:
            return
        try:
            accepted = CheckpointAccepted.model_validate(response.json())
        except (ValueError, TypeError):
            return
        if accepted.checkpoint_seq != raw_slice.end_offset:
            return

        write_cursor(path, raw_slice.end_offset)
