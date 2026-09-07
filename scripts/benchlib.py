"""Shared plumbing for the two benchmark scripts.

Deliberately NOT imported by scripts/e2e.py. e2e.py is a released gate that
went green as-is; refactoring it to share this helper would risk a passing
gate to save fifteen lines. If a third consumer ever appears, fold e2e.py in
then.

The two consumers are:
  scripts/bench.py          -- the capability differential (no LLM needed)
  scripts/bench_quality.py  -- retrieval quality (needs a REAL LLM stack)
"""

from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Self

import httpx

API = os.environ.get("API", "http://localhost:8000")
HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

# Hindsight pins every bank route under the literal `default` segment and
# resolves tenancy from the Authorization header, never the URL
# (memory/hindsight/paths.py). The vanilla arm therefore addresses banks the
# same way ach-memory's own client does.
HINDSIGHT_TENANT = "default"


def bank_path(bank_id: str) -> str:
    return f"/v1/{HINDSIGHT_TENANT}/banks/{bank_id}"


# --------------------------------------------------------------------------
# Verdicts
#
# Three values, and the third one matters: Hindsight is a single-tenant
# engine. Reporting "NOT ENFORCED" where it was never meant to enforce
# anything reads as a rigged comparison and invites a reader to discard the
# whole table. OUT_OF_SCOPE says the capability is absent by design, which is
# a true statement about the layering, not a defect claim.
# --------------------------------------------------------------------------
Verdict = Literal["ENFORCED", "NOT_ENFORCED", "OUT_OF_SCOPE", "ERROR"]

MARK = {
    "ENFORCED": "enforced",
    "NOT_ENFORCED": "not enforced",
    "OUT_OF_SCOPE": "out of scope for the engine",
    "ERROR": "probe error",
}


@dataclass
class Outcome:
    verdict: Verdict
    detail: str


@dataclass
class ProbeResult:
    name: str
    question: str
    ach: Outcome
    vanilla: Outcome


class Http:
    """One client per arm. Every response is returned as (status, parsed)."""

    def __init__(self, base_url: str, *, key: str | None = None) -> None:
        self._key = key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, Any]:
        token = key if key is not None else self._key
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await self._client.request(
            method, path, json=json_body, params=params, headers=headers, timeout=timeout
        )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text

    async def timed(self, *args: Any, **kw: Any) -> tuple[int, Any, float]:
        started = time.monotonic()
        status, data = await self.call(*args, **kw)
        return status, data, (time.monotonic() - started) * 1000.0


@dataclass
class Samples:
    """A metric measured over repeated runs. Reported as mean +/- stdev,
    never as a single number: one run of an LLM-backed retrieval benchmark is
    an anecdote, and a mean with no spread hides that."""

    values: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.values.append(value)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    @property
    def stdev(self) -> float:
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0

    def render(self, *, pct: bool = False, digits: int = 1) -> str:
        if not self.values:
            return "n/a"
        scale = 100.0 if pct else 1.0
        suffix = "%" if pct else ""
        mean = self.mean * scale
        if len(self.values) == 1:
            return f"{mean:.{digits}f}{suffix}"
        return f"{mean:.{digits}f} +/- {self.stdev * scale:.{digits}f}{suffix}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    rule = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = [
        "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"
        for row in rows
    ]
    return "\n".join([line, rule, *body])
