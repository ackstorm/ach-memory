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
import shlex
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from memory.capture.contracts import CheckpointAccepted, CheckpointSubmission
from memory.errors import ProjectInvalidSlug
from memory.slugs import canonical_locator, slug_from_locator

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
_FILE_READING_TOOLS = frozenset({"Read", "NotebookRead", "Grep"})

# Tools that run an arbitrary shell command. Their result is only as safe as
# the command behind it, so the command is classified (see
# `_shell_result_is_safe`) before its output is retained at all.
_SHELL_TOOLS = frozenset({"Bash", "BashOutput"})

# Programs that exist to print file bodies. `Bash` running any of these is a
# file read wearing a different tool name -- the exact hole the Phase 3
# review found, where `grep -n exp src/auth/middleware.py` put source lines
# on the wire that `Read` would have withheld.
_FILE_BODY_READERS = frozenset(
    {
        "awk",
        "base64",
        "bat",
        "cat",
        "cut",
        "diff",
        "dd",
        "egrep",
        "fgrep",
        "grep",
        "head",
        "hexdump",
        "jq",
        "less",
        "more",
        "nl",
        "od",
        "rg",
        "sed",
        "strings",
        "tac",
        "tail",
        "xxd",
        "yq",
    }
)

# Shell syntax this client will not reason about: a substitution, a
# redirection or an interpreter invocation can read a file under any program
# name, so a command carrying one is unclassifiable and its output is
# dropped (fail closed).
_UNCLASSIFIABLE_SHELL = ("$(", "`", "<", ">", "${")
_INDIRECT_SHELL = frozenset({"eval", "exec", "source", ".", "xargs", "sh", "bash", "zsh", "env"})
_SHELL_SEPARATORS = re.compile(r"\|\||&&|[|;&\n]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

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
    """A byte-identical slice of complete records plus where it starts and
    ends.

    `raw` is exactly the bytes `content_hash` is computed over (SPEC: a
    slice identity is `(session_id, start_offset, end_offset, content_hash)`
    where `content_hash` is SHA-256 of the original complete-record byte
    slice) -- it is never sent over the network; only `sanitize()`'s output
    of the parsed records is.

    `start_offset` is carried here rather than remembered by the caller
    because it is not always the offset that was asked for: a rotated
    transcript rewinds to 0, and a caller that kept its own copy would
    submit a stale start with a fresh end (SPEC Phase 3 review finding 3).

    `record_ends` is the absolute end offset of each record in `records`, so
    a bounded batch can stop between two records and still name the exact
    byte boundary it stopped at.
    """

    raw: bytes
    records: list[dict] = field(default_factory=list)
    record_ends: list[int] = field(default_factory=list)
    start_offset: int = 0
    end_offset: int = 0


def read_new_slice(transcript_path: Path, start_offset: int) -> RawSlice:
    """The complete new records after `start_offset`, or an empty slice at
    `start_offset` (rewound to 0 first, if the file is now shorter than
    `start_offset` -- a rotated/rewritten transcript) if there are none yet.
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return RawSlice(raw=b"", start_offset=start_offset, end_offset=start_offset)

    if size < start_offset:
        start_offset = 0
    if size <= start_offset:
        return RawSlice(raw=b"", start_offset=start_offset, end_offset=start_offset)

    with transcript_path.open("rb") as handle:
        handle.seek(start_offset)
        chunk = handle.read()

    records: list[dict] = []
    record_ends: list[int] = []
    consumed = 0
    for record, end in _iter_complete_records(chunk):
        records.append(record)
        record_ends.append(start_offset + end)
        consumed = end

    if consumed == 0:
        return RawSlice(raw=b"", start_offset=start_offset, end_offset=start_offset)
    return RawSlice(
        raw=chunk[:consumed],
        records=records,
        record_ends=record_ends,
        start_offset=start_offset,
        end_offset=start_offset + consumed,
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
    # scheme://token@host -- userinfo with no colon is still a credential,
    # and it is the exact spelling `git remote set-url` writes for a PAT.
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+@\S+"),
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


def shell_output_is_retainable(command: object) -> bool:
    """Whether a shell command's OUTPUT may be retained (SPEC §7.2, Phase 3
    review finding 4).

    Fails closed on every doubt. `Read` withholds a file body, so `Bash`
    must not hand the same bytes over under a different tool name: any
    command whose program is a file-body printer is treated as a file read,
    and any command this function cannot decompose into plain programs --
    a substitution, a redirection, an interpreter, an unbalanced quote --
    is unclassifiable and therefore also withheld.

    The command itself is never returned or logged; only this yes/no.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    if any(token in command for token in _UNCLASSIFIABLE_SHELL):
        return False

    for segment in _SHELL_SEPARATORS.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            # An unbalanced quote: the real word split is unknowable here.
            return False
        words = [word for word in words if not _ENV_ASSIGNMENT.match(word)]
        if not words:
            continue
        program = os.path.basename(words[0])
        if program in _FILE_BODY_READERS or program in _INDIRECT_SHELL:
            return False
    return True


@dataclass(frozen=True)
class _ToolUse:
    """What a `tool_result` needs to know about the `tool_use` it answers."""

    name: str
    output_retainable: bool


def _classify_tool_use(block: dict, name: str) -> _ToolUse:
    """Decide a tool use's disposition while its input is still in hand.

    A `tool_result` names only the id of the use it answers and carries no
    command of its own, so classification cannot be deferred to it.
    """
    if name in _FILE_READING_TOOLS:
        return _ToolUse(name=name, output_retainable=False)
    if name in _SHELL_TOOLS:
        raw_input = block.get("input")
        command = raw_input.get("command") if isinstance(raw_input, dict) else None
        return _ToolUse(name=name, output_retainable=shell_output_is_retainable(command))
    return _ToolUse(name=name, output_retainable=True)


def _sanitize_record(record: dict, tool_uses: dict[str, _ToolUse]) -> list[str]:
    """The sanitized lines for one transcript record.

    `tool_uses` is threaded across records on purpose: a `tool_result` names
    only the id of the `tool_use` it answers, and that `tool_use` is almost
    always in an earlier record.
    """
    if record.get("type") not in _ALLOWED_RECORD_TYPES:
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    if role not in ("user", "assistant"):
        return []

    lines: list[str] = []
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
                tool_uses[tool_id] = _classify_tool_use(block, name)
            # Input is dropped whole, not redacted: a file path, a shell
            # command, a URL query string are all plausible secret or
            # cheaply-reproducible-content carriers with no fixed shape
            # `redact()` could reliably catch.
            lines.append(f"tool_use: {name}")

        elif block_type == "tool_result":
            tool_use_id = block.get("tool_use_id")
            use = tool_uses.get(tool_use_id) if isinstance(tool_use_id, str) else None
            status = "error" if block.get("is_error") else "ok"
            if use is None:
                # A result whose tool_use was never seen -- it may be a file
                # read, a shell read or anything else. Unclassifiable, so
                # the same fail-closed rule applies.
                lines.append(f"tool_result: unknown {status} [output omitted]")
            elif not use.output_retainable:
                lines.append(f"tool_result: {use.name} {status} [output omitted]")
            else:
                text = redact(_text_from_content(block.get("content")))[:_TOOL_OUTPUT_CAP]
                lines.append(f"tool_result: {use.name} {status} {text}".rstrip())

    return lines


def sanitize(records: list[dict]) -> str:
    """The sanitized, submittable text for a list of parsed transcript
    records. Whitelisted record/content types only; everything else -- and
    every raw file body, shell file read and tool input -- is dropped, not
    merely capped.

    Note this applies no batch-level byte cap: truncating the joined text
    silently discards records the caller is about to acknowledge (SPEC
    Phase 3 review finding 3). `build_batch()` is what bounds a submission,
    by dropping whole records rather than cutting text off the end.
    """
    tool_uses: dict[str, _ToolUse] = {}
    lines: list[str] = []
    for record in records:
        lines.extend(_sanitize_record(record, tool_uses))
    return "\n".join(lines)


@dataclass(frozen=True)
class SanitizedBatch:
    """Exactly the bytes that are hashed, submitted and then acknowledged.

    `raw`/`start_offset`/`end_offset` describe one whole number of complete
    records: never a prefix of the sanitized text of a batch whose tail was
    dropped, which is how a cursor came to advance past records that were
    never sent.
    """

    raw: bytes
    content: str
    start_offset: int
    end_offset: int


def build_batch(slice_: RawSlice) -> SanitizedBatch:
    """Bound a raw slice to a submittable batch on whole-record boundaries.

    Records are sanitized one at a time and added while the running
    sanitized size fits `_SLICE_CAP`; the first record that would overflow
    it ends the batch, and it and everything after it stay unread for the
    next checkpoint. The first record is always included even if it alone
    overflows -- otherwise one huge record would wedge the cursor forever --
    and only then is its own text capped.
    """
    tool_uses: dict[str, _ToolUse] = {}
    lines: list[str] = []
    used = 0
    end_offset = slice_.start_offset
    consumed = 0

    for record, record_end in zip(slice_.records, slice_.record_ends, strict=True):
        record_lines = _sanitize_record(record, tool_uses)
        size = sum(len(line.encode("utf-8")) + 1 for line in record_lines)
        if lines and used + size > _SLICE_CAP:
            break
        lines.extend(record_lines)
        used += size
        end_offset = record_end
        consumed = record_end - slice_.start_offset

    content = "\n".join(lines)
    if len(content) > _SLICE_CAP:
        # Only reachable for a single oversized record, which is included
        # whole above so the queue cannot stall on it.
        content = content[:_SLICE_CAP]

    return SanitizedBatch(
        raw=slice_.raw[:consumed],
        content=content,
        start_offset=slice_.start_offset,
        end_offset=end_offset,
    )


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


@dataclass(frozen=True)
class ProjectContext:
    """The project identity a client sends: a slug, and the locator it was
    derived from. Both are canonical, or there is no context at all."""

    project_slug: str
    git_locator: str | None


def git_locator(cwd: str) -> str | None:
    """The repository's origin in ONE canonical spelling, or None.

    Never the raw remote URL. `git remote get-url origin` returns whatever
    is in the config, and on a machine that clones over HTTPS that routinely
    includes a personal access token -- `https://x:ghp_...@github.com/acme/
    api.git`. Sending it would put a live credential on the wire, in the
    server's project row, and in this client's own cursor key material.
    `canonical_locator()` drops the userinfo along with the scheme, port and
    `.git` suffix (SPEC §8.2, Phase 3 review finding 2).
    """
    raw = _run_git(cwd, "remote", "get-url", "origin")
    if not raw:
        return None
    try:
        locator = canonical_locator(raw)
    except ProjectInvalidSlug:
        # A remote naming no host and path: a local clone, a bare path, a
        # bundle. It identifies no project, and guessing is worse than
        # sending nothing.
        return None
    if "@" in locator:
        # canonical_locator strips one leading userinfo run; an embedded `@`
        # surviving it means the credential was spelled in a way this client
        # cannot confidently strip. Fail closed rather than leak a fragment.
        return None
    return locator


def resolve_project_context(cwd: str, env: Mapping[str, str] | None = None) -> ProjectContext | None:
    """SPEC §8 order: `MEMORY_PROJECT`, else the repo's canonical origin,
    else no context.

    The one project-context resolution for local clients (Phase 3 review
    finding 2). Deriving the slug from the remote is the CLIENT's job
    (§8.2, §10) and the bare locator never resolves identity on its own --
    a locator is metadata, deliberately not unique (§17), so a submission
    carrying only one is refused by the server whatever it contains.
    """
    environ = os.environ if env is None else env
    slug = environ.get("MEMORY_PROJECT")
    if slug:
        return ProjectContext(project_slug=slug, git_locator=None)

    locator = git_locator(cwd)
    if not locator:
        return None
    try:
        return ProjectContext(project_slug=slug_from_locator(locator), git_locator=locator)
    except ProjectInvalidSlug:
        return None


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

    context = resolve_project_context(cwd, env)
    workspace = workspace_id_for(cwd)
    if context is None or not workspace:
        return

    url = env.get("ACH_MEMORY_URL", "http://localhost:8000")
    cache_dir = Path(env["ACH_MEMORY_CACHE_DIR"]) if env.get("ACH_MEMORY_CACHE_DIR") else _default_cache_dir(env)
    owner_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    path = cursor_path(
        cache_dir,
        owner_fingerprint=owner_fingerprint,
        service_url=url,
        project_key=context.project_slug,
        workspace_id=workspace,
        session_id=str(session_id),
    )

    transcript_path = Path(transcript_path_raw)
    with locked_cursor(path):
        start = read_cursor(path)
        raw_slice = read_new_slice(transcript_path, start)
        if not raw_slice.records:
            return

        batch = build_batch(raw_slice)
        if not batch.content:
            # Every record in this batch sanitized to nothing -- a run of
            # summaries or meta records with no retained text. There is
            # nothing to submit, but the cursor may still cross exactly
            # those records; the meaningful ones after them stay unread and
            # arrive at the next checkpoint (SPEC Phase 3 review finding 3).
            if batch.end_offset > batch.start_offset:
                write_cursor(path, batch.end_offset)
            return

        body = CheckpointSubmission(
            host="claude-code",
            session_id=str(session_id),
            project_slug=context.project_slug,
            git_locator=context.git_locator,
            workspace_id=workspace,
            # Not `start`: a rotated transcript rewinds the read, and the
            # batch is the authority on which bytes it actually covers.
            start_offset=batch.start_offset,
            end_offset=batch.end_offset,
            content_hash=hashlib.sha256(batch.raw).hexdigest(),
            sanitized_hash=hashlib.sha256(batch.content.encode()).hexdigest(),
            content=batch.content,
        )

        response = _post_checkpoint(url, api_key, body)
        if response is None or response.status_code != 202:
            return
        try:
            accepted = CheckpointAccepted.model_validate(response.json())
        except (ValueError, TypeError):
            return
        if accepted.checkpoint_seq != batch.end_offset:
            return

        write_cursor(path, batch.end_offset)
