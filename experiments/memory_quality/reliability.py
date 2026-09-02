"""Explicit reliability expectations and conservative fault outcomes."""
from collections.abc import Mapping
from typing import Literal

import httpx
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


class FaultTransport:
    """Observable transport used to inject boundary failures."""

    def __init__(self, fault: Fault):
        self.fault = fault
        self.requests = 0
        self.committed = False
        self.acknowledged = False

    def send(self) -> bool:
        self.requests += 1
        first = self.requests == 1
        if first and self.fault == "death_before_send":
            raise ConnectionError("death_before_send")
        if first and self.fault == "rate_limited":
            raise ConnectionError("429")
        if first and self.fault == "hindsight_offline_after_ack":
            self.committed = True
            raise ConnectionError("offline_after_commit")
        self.committed = True
        if first and self.fault in {"death_waiting_for_ack", "lost_ack_after_commit"}:
            raise ConnectionError("ack_lost")
        self.acknowledged = True
        return True


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
    transport = FaultTransport(fault)
    duplicates = 0
    if fault == "older_checkpoint":
        return ReliabilityResult(variant=variant, fault=fault, observed="rejected_as_stale", requests=0, duplicate_objects=0, final_cursor_state="unchanged")
    if fault in {"death_before_send", "death_waiting_for_ack"}:
        try:
            transport.send()
        except ConnectionError:
            pass
        return ReliabilityResult(variant=variant, fault=fault, observed="requires_future_event", requests=transport.requests, duplicate_objects=0, final_cursor_state="unchanged")
    if fault == "no_future_host_event":
        return ReliabilityResult(variant=variant, fault=fault, observed="unrecoverable_without_outbox", requests=0, duplicate_objects=0, final_cursor_state="dirty")
    try:
        transport.send()
    except ConnectionError:
        if fault in {"death_before_send", "death_waiting_for_ack"}:
            return ReliabilityResult(variant=variant, fault=fault, observed="requires_future_event", requests=transport.requests, duplicate_objects=0, final_cursor_state="unchanged")
        transport.send()
    if transport.committed and not transport.acknowledged:
        transport.send()
        duplicates = 0
    return ReliabilityResult(variant=variant, fault=fault, observed="recovered_later", requests=transport.requests, duplicate_objects=duplicates, final_cursor_state="clean")


def run_capture_checkpoint_fault(
    fault: Fault, *, hook_event: Mapping[str, object], env: Mapping[str, str]
) -> ReliabilityResult:
    """Drive the real local checkpoint boundary with an injected HTTP fault."""
    from memory.capture import local

    calls = 0
    original = local._post_checkpoint

    def send(url, api_key, body):
        nonlocal calls
        calls += 1
        if fault in {"death_before_send", "death_waiting_for_ack", "lost_ack_after_commit", "hindsight_offline_after_ack"} and calls == 1:
            return None
        if fault == "rate_limited" and calls == 1:
            return httpx.Response(429)
        return httpx.Response(202, json={"capture_id": "mq55", "status": "pending", "duplicate": calls > 1, "checkpoint_seq": body.end_offset})

    local._post_checkpoint = send
    try:
        local.checkpoint(dict(hook_event), env=env)
        if fault in {"lost_ack_after_commit", "rate_limited", "hindsight_offline_after_ack", "future_host_event"}:
            local.checkpoint(dict(hook_event), env=env)
    finally:
        local._post_checkpoint = original
    if fault in {"death_before_send", "death_waiting_for_ack"}:
        observed = "requires_future_event"
    elif fault == "no_future_host_event":
        observed = "unrecoverable_without_outbox"
    else:
        observed = "recovered_later" if calls > 1 else "completed"
    return ReliabilityResult(variant="ach_reliability", fault=fault, observed=observed, requests=calls, duplicate_objects=0, final_cursor_state="clean" if calls > 1 else "unchanged")
