"""Component-level transcript preprocessing comparisons."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from memory.capture import local

from .contracts import RunObservation

HOST_CANARIES = (
    "CANARY-INJECTED",
    "CANARY-HOST",
    "CANARY-HOST-ASSISTANT",
    "CANARY-FILE",
    "private-key header",
    "assignment-secret",
    "credential URL",
    "CANARYTOKEN123456",
)


def scan_canaries(paths: tuple[Path, ...], canaries: tuple[str, ...]) -> tuple[str, ...]:
    """Return only canaries found in serialized experiment artifacts."""
    found = set()
    files = (
        candidate
        for path in paths
        for candidate in ((path,) if path.is_file() else path.rglob("*") if path.is_dir() else ())
        if candidate.is_file()
    )
    for path in files:
        content = path.read_text(errors="replace")
        found.update(canary for canary in canaries if canary in content)
    return tuple(f"CANARY_{index:02d}_PRESENT" for index, canary in enumerate(canaries) if canary in found)


class Preprocessed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: str
    content: str = Field(exclude=True)
    byte_count: int
    turn_count: int
    retained_role_count: int
    tool_action_count: int
    source_span_count: int
    canary_present: dict[str, bool]
    duration_ms: int


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _measure(variant: str, content: str, raw: list[dict], canaries: tuple[str, ...]) -> Preprocessed:
    return Preprocessed(
        variant=variant, content=content, byte_count=len(content.encode()),
        turn_count=len(raw), retained_role_count=sum(content.count(f"{role}:") for role in ("user", "assistant")),
        tool_action_count=content.count("tool_use:"), source_span_count=content.count("[raw "),
        canary_present={canary: canary in content for canary in canaries}, duration_ms=0,
    )


def run_preprocessing(case_path: Path, runtime) -> tuple[RunObservation, ...]:
    raw = _records(case_path)
    canaries = HOST_CANARIES
    results: list[RunObservation] = []
    ach = local.sanitize(raw)
    official_turns = runtime.normalize_claude(case_path)
    official = "\n".join(f"{turn.role}: {turn.text}" for turn in official_turns)
    hybrid = local.sanitize([{"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}} for role, text in ((turn.role, turn.text) for turn in official_turns) if role in {"user", "assistant"}])
    for variant, content in (("ach_preprocess", ach), ("official_preprocess", official), ("hybrid_preprocess", hybrid)):
        measured = _measure(variant, content, raw, canaries)
        results.append(RunObservation(case_id=case_path.stem, variant=variant, repetition=1, artifact_relpath=f"preprocessing/{variant}.json", hard_gate_flags={"no_canary": not any(measured.canary_present.values())}, metric_values={"byte_count": measured.byte_count, "turn_count": measured.turn_count, "retained_role_count": measured.retained_role_count, "tool_action_count": measured.tool_action_count, "source_span_count": measured.source_span_count, "duration_ms": measured.duration_ms}))
    return tuple(results)
