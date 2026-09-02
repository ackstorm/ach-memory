"""Delivery payload comparisons and the provider-neutral consumer boundary."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from memory.brief import Orientation, Section, compose_full, compose_index, token_upper_bound

from .contracts import DeliveryCase

DeliveryVariant = Literal["ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"]


class DeliveredArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    variant: DeliveryVariant
    context: str
    context_token_upper_bound: int
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    source_ids: tuple[str, ...]
    stale: bool


class LegacyCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_version: str
    scope: Literal["user", "project"]
    status_code: int
    latency_ms: int
    byte_count: int
    context_token_upper_bound: int
    recognized_sections: tuple[Literal["user_core", "project_metadata", "project_profile", "working_state", "memory_protocol"], ...]


class ConsumerAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    used_source_ids: tuple[str, ...]
    abstained: bool


def _section(lines) -> Section | None:
    return Section(text="\n".join(str(line) for line in lines), refreshed_at="2026-09-02T00:00:00Z") if lines else None


def build_delivery(case: DeliveryCase, variant: DeliveryVariant, banks=None) -> DeliveredArtifact:
    start = time.monotonic()
    user = _section(case.user_profile.get("lines", []))
    project = _section(case.project_profile.get("lines", []))
    state = _section([case.working_state["objective"]]) if case.working_state else None
    orientation = Orientation(name=None, canonical_spec=None, purpose=None)
    if variant == "ach_index":
        context = compose_index(1, None, user, orientation, project, state, 1800)
    elif variant == "ach_full":
        context = compose_full(1, None, user, orientation, project, state)
    else:
        if banks is None:
            raise ValueError("live delivery variants require a disposable bank client")
        bank = banks.create_bank(f"delivery-{case.id}-{variant}")
        evidence = "\n".join(case.history_evidence or case.project_metadata)
        if evidence:
            banks.retain_and_wait(bank, evidence, document_id=f"mq55:{case.id}:delivery")
        if variant == "official_reflect":
            response = banks.reflect(bank, case.task, max_tokens=256)
            context = str(response.get("text", response.get("answer", "")))
        else:
            banks.create_page(bank, f"memory-quality-{case.id}", case.task)
            pages = banks.search_pages(bank, case.task)
            page = pages[0] if pages else None
            page_text = ""
            if page:
                page_id = page.get("id", page.get("page_id"))
                if page_id:
                    page_text = str(banks.read_page(bank, str(page_id)).get("content", ""))
            if variant == "official_pages":
                context = page_text
            else:
                core = compose_full(1, None, user, orientation, None, state)
                context = f"{core}\n\n{page_text}" if page_text else core
    forbidden = set(case.secret_canaries)
    if any(secret in context for secret in forbidden):
        raise ValueError("secret canary reached delivered context")
    return DeliveredArtifact(case_id=case.id, variant=variant, context=context, context_token_upper_bound=token_upper_bound(context), latency_ms=int((time.monotonic() - start) * 1000), input_tokens=None, output_tokens=None, source_ids=tuple(case.history_evidence), stale=case.id == "D10-offline-last-good")


def run_consumer(artifact: DeliveredArtifact, command: Sequence[str]) -> ConsumerAnswer:
    if not command:
        raise ValueError("consumer command is empty")
    payload = json.dumps({"case_id": artifact.case_id, "task": artifact.case_id, "context": artifact.context})
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    with tempfile.TemporaryDirectory(prefix="mq55-consumer-") as home:
        env["HOME"] = home
        result = subprocess.run(list(command), input=payload, text=True, capture_output=True, timeout=120, env=env, check=False)
    if result.returncode:
        raise RuntimeError("consumer_nonzero_exit")
    try:
        return ConsumerAnswer.model_validate(json.loads(result.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("consumer_malformed_json") from exc


def measure_legacy_brief(scope: Literal["user", "project"], project_slug: str | None) -> LegacyCalibration:
    import httpx
    base = os.environ.get("ACH_MEMORY_URL")
    token = os.environ.get("ACH_MEMORY_API_KEY")
    if not base or not token or (scope == "project" and not project_slug):
        raise ValueError("legacy calibration requires explicit endpoint, key and scope")
    headers = {"Authorization": f"Bearer {token}"}
    start = time.monotonic()
    with httpx.Client(timeout=30) as client:
        version = client.get(f"{base.rstrip('/')}/openapi.json", headers=headers)
        version.raise_for_status()
        params = {"scope": scope}
        if project_slug:
            params["project_slug"] = project_slug
        brief = client.get(f"{base.rstrip('/')}/v1/session-brief", headers=headers, params=params)
        body = brief.content
        brief.raise_for_status()
    return LegacyCalibration(api_version=str(version.json().get("info", {}).get("version", "unknown")), scope=scope, status_code=brief.status_code, latency_ms=int((time.monotonic() - start) * 1000), byte_count=len(body), context_token_upper_bound=len(body) // 4, recognized_sections=())
