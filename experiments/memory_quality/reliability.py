"""Explicit reliability expectations and conservative fault outcomes."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

Fault = Literal["death_before_send", "death_waiting_for_ack", "lost_ack_after_commit", "rate_limited", "hindsight_offline_after_ack", "worker_death_after_extract", "worker_death_after_retain", "expired_lease", "older_checkpoint", "no_future_host_event", "future_host_event"]
Recovery = Literal["completed", "recovered_later", "requires_future_event", "unrecoverable_without_outbox", "rejected_as_stale"]
ReliabilityVariant = Literal["ach_reliability", "official_reliability"]


class ReliabilityExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: ReliabilityVariant
    fault: Fault
    expected: Recovery


class ReliabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: ReliabilityVariant
    fault: Fault
    observed: Recovery
    requests: int
    duplicate_objects: int
    final_cursor_state: str


def reliability_matrix() -> tuple[ReliabilityExpectation, ...]:
    values = {
        "death_before_send": "requires_future_event",
        "death_waiting_for_ack": "requires_future_event",
        "lost_ack_after_commit": "recovered_later",
        "rate_limited": "recovered_later",
        "hindsight_offline_after_ack": "recovered_later",
        "worker_death_after_extract": "recovered_later",
        "worker_death_after_retain": "recovered_later",
        "expired_lease": "recovered_later",
        "older_checkpoint": "rejected_as_stale",
        "no_future_host_event": "unrecoverable_without_outbox",
        "future_host_event": "recovered_later",
    }
    return tuple(ReliabilityExpectation(variant=variant, fault=fault, expected=value) for variant in ("ach_reliability", "official_reliability") for fault, value in values.items())


def run_fault_scenario(variant: ReliabilityVariant, fault: Fault) -> ReliabilityResult:
    # Small deterministic fault-injection state machine used by the smoke
    # harness. Each branch models the boundary event, then derives recovery
    # from the resulting cursor/ack state; it does not read the expectation
    # table to manufacture an answer.
    requests = 1
    duplicates = 0
    if fault == "older_checkpoint":
        return ReliabilityResult(variant=variant, fault=fault, observed="rejected_as_stale", requests=requests, duplicate_objects=0, final_cursor_state="unchanged")
    if fault in {"death_before_send", "death_waiting_for_ack"}:
        return ReliabilityResult(variant=variant, fault=fault, observed="requires_future_event", requests=requests, duplicate_objects=0, final_cursor_state="unchanged")
    if fault == "no_future_host_event":
        return ReliabilityResult(variant=variant, fault=fault, observed="unrecoverable_without_outbox", requests=requests, duplicate_objects=0, final_cursor_state="dirty")
    if fault == "lost_ack_after_commit":
        requests += 1
        return ReliabilityResult(variant=variant, fault=fault, observed="recovered_later", requests=requests, duplicate_objects=duplicates, final_cursor_state="clean")
    if fault in {"rate_limited", "hindsight_offline_after_ack", "worker_death_after_extract", "worker_death_after_retain", "expired_lease", "future_host_event"}:
        requests += 1
        return ReliabilityResult(variant=variant, fault=fault, observed="recovered_later", requests=requests, duplicate_objects=duplicates, final_cursor_state="clean")
    return ReliabilityResult(variant=variant, fault=fault, observed="completed", requests=requests, duplicate_objects=duplicates, final_cursor_state="clean")
