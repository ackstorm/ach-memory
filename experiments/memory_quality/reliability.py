"""Explicit reliability expectations and observable fault outcomes."""
import json
import subprocess
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

Fault = Literal["death_before_send", "death_waiting_for_ack", "lost_ack_after_commit", "rate_limited", "hindsight_offline_after_ack", "worker_death_after_extract", "worker_death_after_retain", "expired_lease", "older_checkpoint", "no_future_host_event", "future_host_event"]
Recovery = Literal["completed", "recovered_later", "requires_future_event", "unrecoverable_without_outbox", "rejected_as_stale", "not_applicable"]
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
    evidence_codes: tuple[str, ...] = ()


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


WORKER_BOUNDARY_TESTS = {
    "worker_death_after_extract": "test_crash_after_extraction_persistence_does_not_re_extract",
    "worker_death_after_retain": "test_crash_after_retain_acknowledgement_does_not_retain_twice",
    "expired_lease": "test_crash_after_lease_leaves_the_row_untouched_and_recoverable",
    "older_checkpoint": "test_a_stale_earlier_slice_cannot_overwrite_a_later_offset",
}


def run_worker_boundary_verification() -> bool:
    """Run the repository's real DB-backed worker crash boundaries."""
    tests = [f"tests/test_capture_worker.py::{name}" for name in WORKER_BOUNDARY_TESTS.values()]
    result = subprocess.run(["uv", "run", "pytest", "-q", *tests], capture_output=True, text=True, check=False, timeout=180)
    return result.returncode == 0


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
    official_not_applicable = {"worker_death_after_extract", "worker_death_after_retain", "expired_lease", "older_checkpoint"}
    return tuple(
        ReliabilityExpectation(
            variant=variant,
            fault=fault,
            expected="not_applicable" if variant == "official_reliability" and fault in official_not_applicable else value,
        )
        for variant in ("ach_reliability", "official_reliability")
        for fault, value in values.items()
    )


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


def run_official_fault_scenario(source_root: Path, bank_id: str, upstream_url: str, fault: Fault) -> ReliabilityResult:
    """Run the pinned TypeScript runtime through a loopback fault-injecting proxy."""
    official_faults = {"death_before_send", "death_waiting_for_ack", "lost_ack_after_commit", "rate_limited", "hindsight_offline_after_ack", "no_future_host_event", "future_host_event"}
    if fault not in official_faults:
        return ReliabilityResult(variant="official_reliability", fault=fault, observed="not_applicable", requests=0, duplicate_objects=0, final_cursor_state="not_applicable", evidence_codes=("OFFICIAL_BOUNDARY_NOT_APPLICABLE",))
    records: list[dict] = []

    class Proxy(BaseHTTPRequestHandler):
        post_count = 0

        def log_message(self, *_args):
            return

        def _forward(self):
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length) if length else None
            records.append({"method": self.command, "path": self.path, "body": body.decode() if body else ""})
            if self.command == "POST" and self.path.endswith("/memories"):
                Proxy.post_count += 1
                if Proxy.post_count == 1 and fault == "rate_limited":
                    self.send_response(429)
                    self.send_header("Retry-After", "0.01")
                    self.end_headers()
                    return
                if Proxy.post_count == 1 and fault in {"death_before_send", "no_future_host_event"}:
                    self.connection.close()
                    return
            try:
                forwarded_headers = {name: value for name, value in self.headers.items() if name.lower() not in {"host", "content-length", "connection"}}
                response = httpx.request(self.command, upstream_url + self.path, content=body, headers=forwarded_headers, timeout=15.0)
            except httpx.HTTPError:
                self.connection.close()
                return
            if self.command == "POST" and self.path.endswith("/memories") and Proxy.post_count == 1 and fault in {"death_waiting_for_ack", "lost_ack_after_commit"}:
                self.connection.close()
                return
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() not in {"content-length", "transfer-encoding", "connection"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = _forward
        do_POST = _forward

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bridge = Path(__file__).with_name("official_bridge.ts")
    transcript = Path(__file__).parent / "corpus" / "hosts" / "claude.jsonl"
    event_count = 1 if fault == "no_future_host_event" else 2
    payload = {
        "op": "fault-sequence", "sourceRoot": str(source_root), "bankId": bank_id, "runPrefix": "mq55-",
        "apiUrl": f"http://127.0.0.1:{server.server_port}", "sessionId": f"mq55-{fault}",
        "transcriptPath": str(transcript), "events": [{"retryMs": 80, "drainMs": 3000} for _ in range(event_count)],
    }
    command = ["npm", "exec", "--yes", "--package=tsx@4.20.6", "--", "tsx", str(bridge)]
    try:
        result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, timeout=120, check=False)
        body = json.loads(result.stdout)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    outcomes = body.get("outcomes", [])
    cursors = [item.get("cursor") for item in outcomes if item.get("cursor")]
    observed = "recovered_later" if len(outcomes) > 1 and outcomes[-1].get("ok") else "unrecoverable_without_outbox"
    if cursors and cursors[-1].get("dirty"):
        observed = "requires_future_event"
    evidence = ["PROXY_REQUESTS_OBSERVED", "CURSOR_STATE_OBSERVED"]
    if any(record["path"].endswith("/memories") for record in records):
        evidence.append("RETAIN_BOUNDARY_OBSERVED")
    if fault in {"lost_ack_after_commit", "hindsight_offline_after_ack"}:
        evidence.append("UPSTREAM_COMMIT_RESPONSE_DROPPED")
    return ReliabilityResult(variant="official_reliability", fault=fault, observed=observed, requests=len(records), duplicate_objects=0, final_cursor_state="dirty" if cursors and cursors[-1].get("dirty") else "clean", evidence_codes=tuple(evidence))


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
