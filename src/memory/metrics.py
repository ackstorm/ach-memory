"""Prometheus collectors. Aggregate only -- never per-identity.

Every label value here comes from a closed set: an action name chosen in our
own source, a two-value scope, a two-value surface, an outcome, a SPEC §18
error code, an HTTP method, a route TEMPLATE. `user_id` and `project_slug`
are deliberately absent: label values multiply into separate time series, so
a caller-supplied one is an unbounded-cardinality hole that kills the
scraping Prometheus rather than this service. Per-identity detail is the
activity table's job (memory/activity.py) -- that is why both exist.

Single-process by design: the Dockerfile runs one uvicorn worker with no
`--workers`, so prometheus_client's default in-process registry is correct
and no multiprocess directory is needed. Replicas are separate scrape
targets and sum in PromQL.
"""

from importlib.metadata import PackageNotFoundError, version

from prometheus_client import Counter, Gauge, Histogram

CALLS = Counter(
    "memory_calls_total",
    "Data-plane calls that resolved a bank.",
    ["action", "scope", "surface", "outcome"],
)

CALL_DURATION = Histogram(
    "memory_call_duration_seconds",
    "Wall time of a data-plane call, credential check included.",
    ["action", "surface"],
)

CONTENT_BYTES = Counter(
    "memory_content_bytes_total",
    "Bytes of content accepted for retention.",
    ["scope"],
)

ERRORS = Counter(
    "memory_errors_total",
    "Errors reported to a caller, by SPEC §18 code.",
    ["code"],
)

HINDSIGHT = Histogram(
    "memory_hindsight_request_seconds",
    "Upstream Hindsight calls.",
    # `method` and `status`, never the path: a Hindsight path carries the
    # bank id, so labelling by it would both leak the id into every scrape
    # and make the label unbounded. Which upstream operation is slow can be
    # added later as an explicit closed-set label; "is Hindsight slow or
    # erroring" is answered without it.
    ["method", "status"],
)

HTTP = Counter(
    "memory_http_requests_total",
    "HTTP requests, including those that never reached a bank.",
    # `route` is the FastAPI route TEMPLATE ("/v1/memory/{scope}"), never the
    # raw path -- otherwise a caller mints a new time series per URL they
    # invent. Overlaps memory_calls_total on purpose: this one sees the 401s
    # and the provisioning routes, while over MCP every tool is the same
    # POST /mcp and only memory_calls_total can tell the fifteen apart.
    ["route", "method", "status"],
)

BUILD = Gauge("memory_build_info", "Deployed version.", ["version"])

# The capture worker (memory/capture/worker.py) has no per-request edge to
# report through activity.py's ContextVar-based CALLS/CALL_DURATION (see
# that module's docstring) -- these are its equivalent, Prometheus-only.
# `stage` is the closed set {"extract", "retain", "apply"} and `outcome` is
# the closed set {"advanced", "waiting", "failed", "lease_lost"} ("waiting"
# is not a failure: it means an operation Hindsight is still working on,
# checked again next poll; "lease_lost" means this worker's lease expired
# and another worker owns the row, so its transition was refused). Never a
# session id, a project slug, a content hash or a bank id -- exactly the
# same discipline as every other label in this file.
CAPTURE_STAGE = Counter(
    "memory_capture_stage_total",
    "Capture queue worker stage transitions.",
    ["stage", "outcome"],
)

CAPTURE_STAGE_DURATION = Histogram(
    "memory_capture_stage_duration_seconds",
    "Wall time of one capture worker stage (one externally visible action: "
    "an extraction call, a retain call, an operation poll, or a Working "
    "State write).",
    ["stage"],
)

# Every retry the queue schedules, by the stage that failed and the error
# code that failed it. Both label sets are closed and defined in this
# repository (memory.capture.worker), so this cannot grow a series per
# caller. It is the counter that makes a row silently burning its eight
# attempts visible before it goes terminal.
CAPTURE_RETRY = Counter(
    "memory_capture_retries_total",
    "Capture queue retries scheduled after a failed stage.",
    ["stage", "error_code"],
)

# Checkpoint acceptance (POST /v1/capture/checkpoints). Deliberately not an
# activity_events row: that table records a project slug and a bank
# fingerprint, and this path fires once per Stop hook for every session
# (SPEC Phase 3: metrics carry counts, timings, modes and error codes only).
CAPTURE_CHECKPOINT = Counter(
    "memory_capture_checkpoint_total",
    "Checkpoint submissions accepted.",
    ["host", "status", "duplicate", "bytes_bucket"],
)

# `host` is caller-supplied, so it is bucketed to the hosts this repository
# actually ships a plugin for. Anything else is "other" -- a caller must not
# be able to mint a time series by inventing a host name.
KNOWN_HOSTS = frozenset({"claude-code", "codex", "opencode", "pi"})

_BYTE_BUCKETS = ((1_024, "1k"), (4_096, "4k"), (16_384, "16k"), (65_536, "64k"))


def host_label(host: str) -> str:
    """The bounded label for a client-declared host."""
    return host if host in KNOWN_HOSTS else "other"


def bytes_bucket(size: int) -> str:
    """The bounded label for a payload size. Order of magnitude, never the
    exact byte count: a size is a weak identifier of the content behind it."""
    for limit, name in _BYTE_BUCKETS:
        if size < limit:
            return name
    return "64k+"


def _version() -> str:
    try:
        return version("ach-memory")
    except PackageNotFoundError:  # running from a source tree, not installed
        return "unknown"


BUILD.labels(version=_version()).set(1)
