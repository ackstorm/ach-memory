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


def _version() -> str:
    try:
        return version("ach-memory")
    except PackageNotFoundError:  # running from a source tree, not installed
        return "unknown"


BUILD.labels(version=_version()).set(1)
