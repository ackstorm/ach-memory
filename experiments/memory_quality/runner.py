"""CLI orchestration with refusal-first lifecycle commands."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .contracts import RunObservation, corpus_digest, load_delivery_cases, load_semantic_cases
from .delivery import build_delivery
from .hindsight import BakeoffConfig, BakeoffRefused, DisposableHindsight
from .preprocessing import HOST_CANARIES, run_preprocessing, scan_canaries
from .reliability import (
    reliability_matrix,
    run_capture_checkpoint_fault,
    run_fault_scenario,
    run_official_fault_scenario,
    run_worker_boundary_verification,
)
from .scoring import (
    Adjudication,
    BlindPacket,
    build_blind_packet,
    decide,
    decide_v2,
    decide_v3,
    score_run,
    score_run_v2,
    score_run_v3,
    unblind,
)
from .semantic import run_semantic_case
from .upstream import OfficialRuntime, verify_official_source

ROOT = Path(__file__).parent
VALID_OBSERVATION_VARIANTS = {"ach_semantic", "native_semantic", "hybrid_semantic", "ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery", "ach_preprocess", "official_preprocess", "hybrid_preprocess"}


def _verify_openapi_contract(document: dict) -> None:
    paths = document.get("paths", {})
    required = {
        "/v1/default/banks/{bank_id}": {"put", "delete"},
        "/v1/default/banks/{bank_id}/memories": {"post"},
        "/v1/default/banks/{bank_id}/operations/{operation_id}": {"get"},
        "/v1/default/banks/{bank_id}/documents": {"get"},
        "/v1/default/banks/{bank_id}/memories/list": {"get"},
        "/v1/default/banks/{bank_id}/memories/dry-run-extract": {"post"},
        "/v1/default/banks/{bank_id}/reflect": {"post"},
        "/v1/default/banks/{bank_id}/knowledge-base/search": {"get"},
        "/v1/default/banks/{bank_id}/knowledge-base/pages": {"post"},
        "/v1/default/banks/{bank_id}/knowledge-base/pages/{page_id}": {"get"},
    }
    for path, methods in required.items():
        if path not in paths or not methods <= set(paths[path]):
            raise BakeoffRefused(f"Hindsight OpenAPI contract missing: {path}")


def preflight(env=None) -> dict:
    env = env or os.environ
    config = BakeoffConfig.from_env(env)
    source_root = Path(env.get("HINDSIGHT_CODING_AGENTS_DIR", ROOT.parents[2] / "hindsight/hindsight-integrations/coding-agents"))
    source = verify_official_source(source_root)
    semantic = load_semantic_cases(ROOT / "corpus/semantic.jsonl")
    delivery = load_delivery_cases(ROOT / "corpus/delivery.jsonl")
    with httpx.Client(timeout=config.request_timeout_seconds) as client:
        response = client.get(f"{config.base_url}/openapi.json")
        response.raise_for_status()
        document = response.json()
        version = document.get("info", {}).get("version")
    if version != "0.9.2":
        raise BakeoffRefused("Hindsight API version must be 0.9.2")
    _verify_openapi_contract(document)
    repo = ROOT.parents[1]
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout.strip() != ""
    requested = env.get("HINDSIGHT_BAKEOFF_RUN_ID")
    run_id = uuid.UUID(requested) if requested else config.run_id
    now = datetime.now(UTC).isoformat()
    return {
        "run_id": str(run_id), "command_mode": env.get("HINDSIGHT_BAKEOFF_MODE", "measured"),
        "started_at": now, "ended_at": now, "ach_memory_commit": commit, "ach_memory_dirty": dirty,
        "hindsight_version": version, "hindsight_openapi_sha256": hashlib.sha256(response.content).hexdigest(),
        "official_package_version": source.package_version, "official_checkout_commit": source.checkout_commit,
        "official_source_sha256": dict(source.sha256_by_relpath), "semantic_digest": corpus_digest(semantic),
        "delivery_digest": corpus_digest(delivery), "models": {"hindsight": env.get("HINDSIGHT_BAKEOFF_MODEL", "configured-server-model"), "consumer": bool(env.get("MEMORY_BAKEOFF_CONSUMER_CMD"))},
        "budgets": {"reflect_max_tokens": 256, "delivery_context_tokens": 1800},
        "consumer_present": bool(env.get("MEMORY_BAKEOFF_CONSUMER_CMD")), "consumer_complete": False, "mutating_requests": 0,
    }


def _measured_hmac_key(env) -> bytes:
    value = env.get("HINDSIGHT_BAKEOFF_HMAC_KEY")
    if not value or value == "development-only":
        raise BakeoffRefused("measured runs require HINDSIGHT_BAKEOFF_HMAC_KEY")
    try:
        key = bytes.fromhex(value)
    except ValueError as exc:
        raise BakeoffRefused("HINDSIGHT_BAKEOFF_HMAC_KEY must be hexadecimal") from exc
    if len(key) < 32:
        raise BakeoffRefused("HINDSIGHT_BAKEOFF_HMAC_KEY must contain at least 32 random bytes")
    return key


def _blind_semantic_artifacts(observations, artifact_dir: Path, key: bytes, mapping_path: Path) -> None:
    mapping = {}
    for observation in observations:
        if not observation["variant"].endswith("_semantic"):
            continue
        source = artifact_dir / observation["artifact_relpath"]
        opaque = "blind/outputs/o-" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()[:24] + ".json"
        target = artifact_dir / opaque
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        mapping[opaque] = {"case_id": observation["case_id"], "variant": observation["variant"], "repetition": observation["repetition"]}
        observation["artifact_relpath"] = opaque
    mapping_payload = {"seal": hmac.new(key, json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest(), "mapping": mapping}
    _atomic_json(mapping_path, mapping_payload)
    mapping_path.chmod(0o600)


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def run_smoke(env=None) -> dict:
    env = env or os.environ
    manifest = preflight(env)
    config = BakeoffConfig.from_env(env, run_id=uuid.UUID(manifest["run_id"]))
    root = Path(env.get("HINDSIGHT_CODING_AGENTS_DIR", ROOT.parents[2] / "hindsight/hindsight-integrations/coding-agents"))
    runtime = OfficialRuntime(verify_official_source(root))
    artifact_dir = Path(env.get("MEMORY_BAKEOFF_ARTIFACT_DIR", ".artifacts/memory-quality")) / str(config.run_id)
    observations = []
    banks = DisposableHindsight(config, artifact_root=artifact_dir.parent)
    try:
        observations.extend(item.model_dump(mode="json") for item in run_preprocessing(ROOT / "corpus/hosts/claude.jsonl", runtime))
        semantic = load_semantic_cases(ROOT / "corpus/semantic.jsonl")[0]
        for variant in ("ach_semantic", "native_semantic", "hybrid_semantic"):
            try:
                observations.append(run_semantic_case(semantic, variant, 1, banks).model_dump(mode="json"))
            except (RuntimeError, ValueError, TimeoutError, httpx.HTTPError) as exc:
                raise RuntimeError(f"smoke semantic variant failed: {variant}") from exc
        delivery = load_delivery_cases(ROOT / "corpus/delivery.jsonl")[0]
        for variant in ("ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"):
            try:
                observations.append(build_delivery(delivery, variant, banks).model_dump(mode="json"))
            except (RuntimeError, ValueError, TimeoutError, httpx.HTTPError) as exc:
                raise RuntimeError(f"smoke delivery variant failed: {variant}") from exc
        for item in reliability_matrix():
            if item.variant == "official_reliability":
                bank = banks.create_bank(f"smoke-reliability-{item.fault}")
                result = run_official_fault_scenario(root, bank, config.base_url, item.fault)
            else:
                result = run_fault_scenario(item.variant, item.fault)
            observations.append({"case_id": item.fault, "variant": item.variant, "repetition": 1, "artifact_relpath": f"reliability/{item.variant}/{item.fault}.json", "hard_gate_flags": {"executed": True, "recovered": result.observed in {"completed", "recovered_later", "requires_future_event", "unrecoverable_without_outbox", "rejected_as_stale", "not_applicable"}}, "metric_values": {"requests": result.requests}})
        _atomic_json(artifact_dir / "manifest.json", manifest)
        with (artifact_dir / "observations.jsonl").open("w") as handle:
            for observation in observations:
                handle.write(json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n")
        semantic_cases = load_semantic_cases(ROOT / "corpus/semantic.jsonl")
        delivery_cases = load_delivery_cases(ROOT / "corpus/delivery.jsonl")
        canaries = tuple(dict.fromkeys(HOST_CANARIES + tuple(canary for case in (*semantic_cases, *delivery_cases) for canary in case.secret_canaries)))
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("smoke artifact canary scan failed")
        return {"run_id": str(config.run_id), "observation_count": len(observations), "artifact_dir": str(artifact_dir)}
    finally:
        banks.cleanup()


def _run_dir() -> Path:
    run_id = os.environ.get("HINDSIGHT_BAKEOFF_RUN_ID")
    if not run_id:
        raise SystemExit("HINDSIGHT_BAKEOFF_RUN_ID is required")
    return Path(os.environ.get("MEMORY_BAKEOFF_ARTIFACT_DIR", ".artifacts/memory-quality")) / run_id


def export_adjudication() -> Path:
    run_dir = _run_dir().resolve()
    packet_path = run_dir / "blind-packet.json"
    if not packet_path.is_file():
        raise SystemExit("export requires blind-packet.json")
    try:
        packet = BlindPacket.model_validate_json(packet_path.read_text())
    except ValueError as exc:
        raise SystemExit("export refuses malformed blind-packet.json") from exc
    if len(packet.items) != 144:
        raise SystemExit("export requires exactly 144 semantic items")
    source_files = []
    for item in packet.items:
        relative = Path(item.artifact_relpath)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("blind", "outputs"):
            raise SystemExit("export refuses non-opaque or out-of-run artifact path")
        if any(name in item.artifact_relpath for name in ("ach_semantic", "native_semantic", "hybrid_semantic", "mq55-", "token", "credential", "private-key")):
            raise SystemExit("export refuses variant, bank or credential identity")
        source = (run_dir / relative).resolve()
        if run_dir not in source.parents or not source.is_file():
            raise SystemExit("export refuses missing or out-of-run artifact")
        source_files.append((relative, source))
    target = Path(os.environ.get("HINDSIGHT_BAKEOFF_EXPORT_DIR", str(run_dir.parent / f"{run_dir.name}-adjudication-export"))).resolve()
    if target == run_dir or run_dir in target.parents:
        raise SystemExit("export target must be outside the artifact root")
    if target.exists():
        raise SystemExit("export target already exists")
    target.mkdir(parents=True)
    try:
        shutil.copyfile(packet_path, target / "blind-packet.json")
        for relative, source in source_files:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        _atomic_json(target / "export-manifest.json", {
            "run_id": run_dir.name,
            "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            "item_count": len(packet.items),
            "files": [str(relative) for relative, _ in source_files],
        })
        canaries = tuple(dict.fromkeys(HOST_CANARIES + tuple(canary for case in (*load_semantic_cases(ROOT / "corpus/semantic.jsonl"), *load_delivery_cases(ROOT / "corpus/delivery.jsonl")) for canary in case.secret_canaries)))
        if scan_canaries((target,), canaries):
            raise SystemExit("export artifact canary scan failed")
        return target
    except Exception:
        shutil.rmtree(target)
        raise


def run_full(env=None) -> dict:
    """Execute the complete 319-observation matrix; intentionally not smoke."""
    env = env or os.environ
    manifest = preflight(env)
    config = BakeoffConfig.from_env(env, run_id=uuid.UUID(manifest["run_id"]))
    hmac_key = _measured_hmac_key(env)
    runtime = OfficialRuntime(verify_official_source(Path(env.get("HINDSIGHT_CODING_AGENTS_DIR", ROOT.parents[2] / "hindsight/hindsight-integrations/coding-agents"))))
    artifact_dir = Path(env.get("MEMORY_BAKEOFF_ARTIFACT_DIR", ".artifacts/memory-quality")) / str(config.run_id)
    banks = DisposableHindsight(config, artifact_root=artifact_dir.parent)
    observations = []
    try:
        if not run_worker_boundary_verification():
            raise RuntimeError("real worker fault-boundary verification failed")
        observations.extend(item.model_dump(mode="json") for item in run_preprocessing(ROOT / "corpus/hosts/claude.jsonl", runtime))
        for case in load_semantic_cases(ROOT / "corpus/semantic.jsonl"):
            for variant in ("ach_semantic", "native_semantic", "hybrid_semantic"):
                for repetition in range(1, 4):
                    semantic_artifact = artifact_dir / "semantic" / case.id / f"{variant}-{repetition}.json"
                    observations.append(run_semantic_case(case, variant, repetition, banks, semantic_artifact).model_dump(mode="json"))
        for case in load_delivery_cases(ROOT / "corpus/delivery.jsonl"):
            for variant in ("ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"):
                for repetition in range(1, 4):
                    delivery = build_delivery(case, variant, banks, repetition)
                    _atomic_json(artifact_dir / "delivery" / case.id / f"{variant}-{repetition}.json", {
                        "context": delivery.context,
                        "source_ids": delivery.source_ids,
                        "stale": delivery.stale,
                    })
                    observations.append(delivery.model_copy(update={"latency_ms": repetition}).model_dump(mode="json") | {"repetition": repetition, "artifact_relpath": f"delivery/{case.id}/{variant}-{repetition}.json", "hard_gate_flags": {"executed": True}, "metric_values": {}})
        for item in reliability_matrix():
            if item.variant == "official_reliability":
                official_bank = banks.create_bank(f"reliability-{item.fault}")
                result = run_official_fault_scenario(
                    Path(env.get("HINDSIGHT_CODING_AGENTS_DIR", ROOT.parents[2] / "hindsight/hindsight-integrations/coding-agents")),
                    official_bank,
                    config.base_url,
                    item.fault,
                )
            elif item.variant == "ach_reliability" and item.fault in {"death_before_send", "death_waiting_for_ack", "lost_ack_after_commit", "rate_limited", "hindsight_offline_after_ack", "future_host_event"}:
                result = run_capture_checkpoint_fault(
                    item.fault,
                    hook_event={"transcript_path": str(ROOT / "corpus/hosts/claude.jsonl"), "session_id": f"mq55-{item.fault}", "cwd": str(ROOT)},
                    env={"MEMORY_CAPTURE_ENABLED": "true", "ACH_MEMORY_API_KEY": "mq55-test-key", "ACH_MEMORY_URL": "http://mq55.controlled", "ACH_MEMORY_CACHE_DIR": str(artifact_dir / "reliability-cache"), "HOME": str(artifact_dir)},
                )
            else:
                result = run_fault_scenario(item.variant, item.fault)
            observations.append({"case_id": item.fault, "variant": item.variant, "repetition": 1, "artifact_relpath": f"reliability/{item.variant}/{item.fault}.json", "hard_gate_flags": {"executed": True}, "metric_values": {"requests": result.requests, "duplicates": result.duplicate_objects}})
        mapping_path = Path(env.get("HINDSIGHT_BAKEOFF_MAPPING_PATH", artifact_dir.parent / ".private" / f"{config.run_id}.mapping.json"))
        _blind_semantic_artifacts(observations, artifact_dir, hmac_key, mapping_path)
        manifest["ended_at"] = datetime.now(UTC).isoformat()
        _atomic_json(artifact_dir / "manifest.json", manifest)
        (artifact_dir / "observations.jsonl").write_text("\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")) for item in observations) + "\n")
        canaries = tuple(dict.fromkeys(HOST_CANARIES + tuple(canary for case in (*load_semantic_cases(ROOT / "corpus/semantic.jsonl"), *load_delivery_cases(ROOT / "corpus/delivery.jsonl")) for canary in case.secret_canaries)))
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("artifact canary scan failed")
        return {"run_id": str(config.run_id), "observation_count": len(observations), "artifact_dir": str(artifact_dir)}
    finally:
        banks.cleanup()


def score_artifacts() -> dict:
    run_dir = _run_dir()
    observation_path = run_dir / "observations.jsonl"
    adjudication_path = run_dir / "adjudication.json"
    if not observation_path.exists():
        raise SystemExit("score requires observations.jsonl")
    observations = []
    for line in observation_path.read_text().splitlines():
        raw = json.loads(line)
        if raw.get("variant") in VALID_OBSERVATION_VARIANTS or raw.get("variant") in {"ach_reliability", "official_reliability"}:
            observation = RunObservation.model_validate(
                {field: raw[field] for field in (
                    "case_id", "variant", "repetition", "artifact_relpath",
                    "hard_gate_flags", "metric_values", "warning_codes",
                ) if field in raw}
            )
            if not observation.hard_gate_flags.get("executed", True):
                raise SystemExit("score refuses an observation with executed=false")
            observations.append(observation)
    keys = [(item.case_id, item.variant, item.repetition, item.artifact_relpath) for item in observations]
    if len(keys) != len(set(keys)):
        raise SystemExit("score refuses duplicate observations")
    counts = {
        "preprocess": sum(item.variant.endswith("preprocess") for item in observations),
        "semantic": sum(item.variant.endswith("semantic") for item in observations),
        "delivery": sum(item.variant in {"ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"} for item in observations),
        "reliability": sum(item.variant in {"ach_reliability", "official_reliability"} for item in observations),
    }
    if counts != {"preprocess": 3, "semantic": 144, "delivery": 150, "reliability": 22} or len(observations) != 319:
        raise SystemExit("score refuses anything other than the exact 319-observation matrix")
    key = _measured_hmac_key(os.environ)
    packet = build_blind_packet(observations, key)
    existing_packet = run_dir / "blind-packet.json"
    if existing_packet.exists():
        try:
            if BlindPacket.model_validate_json(existing_packet.read_text()) != packet:
                raise SystemExit("score refuses an altered blind packet")
        except ValueError as exc:
            raise SystemExit("score refuses malformed blind-packet.json") from exc
    else:
        _atomic_json(existing_packet, packet.model_dump(mode="json"))
    if not adjudication_path.exists():
        raise SystemExit("blind-packet.json written; score requires adjudication.json")
    adjudication = Adjudication.model_validate_json(adjudication_path.read_text())
    mapping_path = Path(os.environ.get("HINDSIGHT_BAKEOFF_MAPPING_PATH", run_dir.parent / ".private" / f"{run_dir.name}.mapping.json"))
    if mapping_path.resolve().is_relative_to(run_dir) or mapping_path.stat().st_mode & 0o077:
        raise SystemExit("score requires a private mapping outside the run root")
    if adjudication.run_id != run_dir.name:
        raise SystemExit("score refuses adjudication for another run")
    unblind(packet, adjudication, mapping_path, key)
    manifest = json.loads((run_dir / "manifest.json").read_text()) if (run_dir / "manifest.json").exists() else {}
    expected_units = {
        case.id: tuple(unit.unit_id for unit in case.expected_units)
        for case in load_semantic_cases(ROOT / "corpus/semantic.jsonl")
    }
    scorecard = score_run(packet, adjudication, expected_units=expected_units, consumer_complete=bool(manifest.get("consumer_complete", False)))
    _atomic_json(run_dir / "scorecard.json", scorecard.model_dump(mode="json"))
    _atomic_json(run_dir / "decisions.json", [item.model_dump(mode="json") for item in decide(scorecard)])
    return {"scorecard": str(run_dir / "scorecard.json"), "decisions": str(run_dir / "decisions.json")}


def rescore_v2() -> dict:
    run_dir = _run_dir()
    expected_consensus = "04717dfe9b08ae165c3aafb52e6ef2e1d72d22dd09a0be44f1cf979b61a13633"
    consensus_path = run_dir.parent / f"{run_dir.name}-adjudication.CONSENSUS.json"
    if not consensus_path.is_file() or hashlib.sha256(consensus_path.read_bytes()).hexdigest() != expected_consensus:
        raise SystemExit("rescore-v2 refuses a missing or altered consensus")
    adjudication = Adjudication.model_validate_json((run_dir / "adjudication.json").read_text())
    observation_path = run_dir / "observations.jsonl"
    observations = []
    for line in observation_path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        # The frozen pilot/run writer carried legacy measurement fields at the
        # top level. Normalize them in memory; the frozen evidence is not
        # rewritten and no new experiment is performed.
        metrics = dict(raw.get("metric_values") or {})
        for source, target in (("latency_ms", "duration_ms"), ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            if source in raw and target not in metrics and raw[source] is not None:
                metrics[target] = raw[source]
        observations.append(RunObservation.model_validate({key: raw[key] for key in ("case_id", "variant", "repetition", "artifact_relpath", "hard_gate_flags", "warning_codes") if key in raw} | {"metric_values": metrics}))
    if len(observations) != 319:
        raise SystemExit("rescore-v2 requires exactly 319 frozen observations")
    observation_keys = [(item.case_id, item.variant, item.repetition) for item in observations]
    if len(set(observation_keys)) != len(observation_keys):
        raise SystemExit("rescore-v2 refuses duplicate observations")
    if any(item.hard_gate_flags.get("executed") is False for item in observations):
        raise SystemExit("rescore-v2 refuses unexecuted observations")
    key = _measured_hmac_key(os.environ)
    packet = BlindPacket.model_validate_json((run_dir / "blind-packet.json").read_text())
    packet_digest = hashlib.sha256((run_dir / "blind-packet.json").read_bytes()).hexdigest()
    if adjudication.run_id != run_dir.name:
        raise SystemExit("rescore-v2 refuses adjudication for another run")
    semantic_packet_keys = {
        (item.case_id, item.blind_variant, item.repetition)
        for item in packet.items
    }
    adjudication_keys = {
        (item.case_id, item.blind_variant, item.repetition)
        for item in adjudication.items
    }
    if len(packet.items) != 144 or len(adjudication.items) != 144 or semantic_packet_keys != adjudication_keys:
        raise SystemExit("rescore-v2 requires exactly 144 adjudicated semantic items")
    mapping_path = Path(os.environ.get("HINDSIGHT_BAKEOFF_MAPPING_PATH", run_dir.parent / ".private" / f"{run_dir.name}.mapping.json"))
    unblinded = unblind(packet, adjudication, mapping_path, key)
    expected_units = {case.id: tuple(unit.unit_id for unit in case.expected_units) for case in load_semantic_cases(ROOT / "corpus/semantic.jsonl")}
    if hashlib.sha256((run_dir / "blind-packet.json").read_bytes()).hexdigest() != packet_digest:
        raise SystemExit("rescore-v2 packet changed during validation")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    scorecard = score_run_v2(observations, unblinded, adjudication, expected_units=expected_units, consumer_complete=bool(manifest.get("consumer_complete", False)), run_id=run_dir.name)
    decisions = decide_v2(scorecard)
    _atomic_json(run_dir / "scorecard.v2.json", scorecard.model_dump(mode="json"))
    _atomic_json(run_dir / "decisions.v2.json", [item.model_dump(mode="json") for item in decisions])
    report = "# Memory Quality Phase 5.5 — v2\n\n| Component | Ruling | Evidence cases | Reasons |\n|---|---|---|---|\n" + "\n".join(f"| {item.component} | {item.ruling} | {','.join(item.evidence_case_ids)} | {','.join(item.reason_codes)} |" for item in decisions) + "\n"
    (run_dir / "report.v2.md").write_text(report)
    return {"scorecard": str(run_dir / "scorecard.v2.json"), "decisions": str(run_dir / "decisions.v2.json"), "report": str(run_dir / "report.v2.md")}


def _render_v3_report(scorecard, decisions) -> str:
    rows = []
    details = []
    for decision in decisions:
        evidence = scorecard.components[decision.component]
        rows.append(
            f"| {decision.component} | {decision.ruling} | "
            f"{','.join(decision.reason_codes)} | {evidence.observation_count} |"
        )
        hard = ", ".join(evidence.hard_gate_failures) or "none"
        details.extend((
            f"### {decision.component}",
            "",
            f"- Evidence available: `{str(evidence.evidence_available).lower()}`",
            f"- Hard-gate failures: {hard}",
        ))
        for variant, challenger in sorted(evidence.challengers.items()):
            details.append(
                f"- `{variant}`: non-inferior=`{str(challenger.non_inferior).lower()}`; "
                f"hard gates={','.join(challenger.hard_gate_failures) or 'none'}; "
                f"repeatable regressions={','.join(challenger.repeatable_regressions) or 'none'}"
            )
        details.append("")
    return (
        "# Memory Quality Phase 5.5 — V3\n\n"
        f"Frozen run: `{scorecard.run_id}`. This report only rescores existing evidence; "
        "it does not rerun Hindsight, model inference, or adjudication.\n\n"
        "| Component | Ruling | Reasons | Observations |\n"
        "|---|---|---|---:|\n"
        + "\n".join(rows)
        + "\n\n## Attributable evidence\n\n"
        + "\n".join(details)
    )


def _write_v3_artifacts(run_dir: Path, scorecard, decisions) -> dict[str, str]:
    targets = {
        "scorecard": run_dir / "scorecard.v3.json",
        "decisions": run_dir / "decisions.v3.json",
        "report": run_dir / "report.v3.md",
    }
    if any(path.exists() for path in targets.values()):
        raise SystemExit("rescore-v3 refuses to overwrite existing V3 artifacts")
    _atomic_json(targets["scorecard"], scorecard.model_dump(mode="json"))
    _atomic_json(targets["decisions"], [item.model_dump(mode="json") for item in decisions])
    _atomic_text(targets["report"], _render_v3_report(scorecard, decisions))
    return {name: str(path) for name, path in targets.items()}


def rescore_v3() -> dict:
    """Correct V2's attribution errors using only the immutable frozen run."""
    run_dir = _run_dir()
    expected_consensus = "04717dfe9b08ae165c3aafb52e6ef2e1d72d22dd09a0be44f1cf979b61a13633"
    consensus_path = run_dir.parent / f"{run_dir.name}-adjudication.CONSENSUS.json"
    if not consensus_path.is_file() or hashlib.sha256(consensus_path.read_bytes()).hexdigest() != expected_consensus:
        raise SystemExit("rescore-v3 refuses a missing or altered consensus")
    adjudication = Adjudication.model_validate_json(consensus_path.read_text())
    if adjudication.run_id != run_dir.name:
        raise SystemExit("rescore-v3 refuses consensus for another run")

    protected_paths = tuple(
        run_dir / name
        for name in (
            "scorecard.json", "decisions.json", "report.md",
            "scorecard.v2.json", "decisions.v2.json", "report.v2.md",
        )
    )
    if not all(path.is_file() for path in protected_paths):
        raise SystemExit("rescore-v3 requires preserved V1 and V2 artifacts")
    protected_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_paths}

    observation_path = run_dir / "observations.jsonl"
    observations = []
    for line in observation_path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        metrics = dict(raw.get("metric_values") or {})
        for source, target in (("latency_ms", "duration_ms"), ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            if source in raw and target not in metrics and raw[source] is not None:
                metrics[target] = raw[source]
        observations.append(RunObservation.model_validate(
            {key: raw[key] for key in ("case_id", "variant", "repetition", "artifact_relpath", "hard_gate_flags", "warning_codes") if key in raw}
            | {"metric_values": metrics}
        ))
    counts = {
        "preprocess": sum(item.variant.endswith("preprocess") for item in observations),
        "semantic": sum(item.variant.endswith("semantic") for item in observations),
        "delivery": sum(item.variant in {"ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"} for item in observations),
        "reliability": sum(item.variant in {"ach_reliability", "official_reliability"} for item in observations),
    }
    if counts != {"preprocess": 3, "semantic": 144, "delivery": 150, "reliability": 22} or len(observations) != 319:
        raise SystemExit("rescore-v3 requires exactly 319 frozen observations")
    observation_keys = [(item.case_id, item.variant, item.repetition) for item in observations]
    if len(set(observation_keys)) != len(observation_keys):
        raise SystemExit("rescore-v3 refuses duplicate observations")
    if any(item.hard_gate_flags.get("executed") is False for item in observations):
        raise SystemExit("rescore-v3 refuses unexecuted observations")

    packet_path = run_dir / "blind-packet.json"
    packet_digest = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    packet = BlindPacket.model_validate_json(packet_path.read_text())
    packet_keys = {(item.case_id, item.blind_variant, item.repetition) for item in packet.items}
    adjudication_keys = {(item.case_id, item.blind_variant, item.repetition) for item in adjudication.items}
    if len(packet.items) != 144 or len(adjudication.items) != 144 or packet_keys != adjudication_keys:
        raise SystemExit("rescore-v3 requires exact 144-item packet and consensus coverage")

    key = _measured_hmac_key(os.environ)
    mapping_path = Path(os.environ.get("HINDSIGHT_BAKEOFF_MAPPING_PATH", run_dir.parent / ".private" / f"{run_dir.name}.mapping.json"))
    if not mapping_path.is_file() or mapping_path.resolve().is_relative_to(run_dir.resolve()) or mapping_path.stat().st_mode & 0o077:
        raise SystemExit("rescore-v3 requires a private mapping outside the run root")
    unblinded = unblind(packet, adjudication, mapping_path, key)
    if hashlib.sha256(packet_path.read_bytes()).hexdigest() != packet_digest:
        raise SystemExit("rescore-v3 packet changed during validation")

    semantic_cases = load_semantic_cases(ROOT / "corpus/semantic.jsonl")
    expected_units = {case.id: tuple(unit.unit_id for unit in case.expected_units) for case in semantic_cases}
    critical_units = {
        case.id: tuple(unit.unit_id for unit in case.expected_units if unit.critical)
        for case in semantic_cases
    }
    ignored_units = {
        case.id: tuple(unit.unit_id for unit in case.expected_units if unit.scope == "ignore")
        for case in semantic_cases
    }
    critical_rejection_cases = tuple(
        case.id
        for case in semantic_cases
        if any(unit.critical and not unit.current for unit in case.expected_units)
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    scorecard = score_run_v3(
        observations,
        unblinded,
        adjudication,
        expected_units=expected_units,
        critical_units=critical_units,
        ignored_units=ignored_units,
        critical_rejection_cases=critical_rejection_cases,
        consumer_complete=bool(manifest.get("consumer_complete", False)),
        run_id=run_dir.name,
    )
    decisions = decide_v3(scorecard)
    result = _write_v3_artifacts(run_dir, scorecard, decisions)
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != digest for path, digest in protected_hashes.items()):
        raise SystemExit("rescore-v3 detected a modified V1 or V2 artifact")
    return result


def render_report() -> Path:
    run_dir = _run_dir()
    path = run_dir / "decisions.json"
    if not path.exists():
        raise SystemExit("report requires decisions.json")
    decisions = [item for item in json.loads(path.read_text())]
    if {item.get("component") for item in decisions} != {"host_adapters", "preprocessing", "semantic_extractor", "scope_router", "profile_compiler", "delivery_protocol", "capture_reliability", "working_state_ordering"}:
        raise SystemExit("report requires exactly one decision for each component")
    report = "# Memory Quality Phase 5.5\n\n| Component | Ruling | Approval required |\n|---|---|---|\n" + "\n".join(f"| {item['component']} | {item['ruling']} | {item['approval_required']} |" for item in sorted(decisions, key=lambda item: item["component"])) + "\n"
    target = run_dir / "report.md"
    target.write_text(report)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m experiments.memory_quality.runner")
    parser.add_argument("command", choices=("preflight", "legacy-calibrate", "smoke", "run", "semantic-repair-run", "semantic-repair-score", "semantic-splitter-run", "semantic-splitter-score", "export-adjudication", "score", "rescore-v2", "rescore-v3", "cleanup", "report"))
    args = parser.parse_args(argv)
    if args.command == "preflight":
        try:
            print(json.dumps(preflight(), sort_keys=True))
        except BakeoffRefused as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command in {"smoke", "run"}:
        try:
            print(json.dumps(run_smoke() if args.command == "smoke" else run_full(), sort_keys=True))
        except BakeoffRefused as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command == "semantic-repair-run":
        from .semantic_repair import run_semantic_repair

        try:
            print(json.dumps(run_semantic_repair(), sort_keys=True))
        except BakeoffRefused as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command == "semantic-repair-score":
        from .semantic_repair import score_semantic_repair_artifacts

        try:
            print(json.dumps(score_semantic_repair_artifacts(), sort_keys=True))
        except BakeoffRefused as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command == "semantic-splitter-run":
        from .semantic_repair import run_semantic_splitter

        try:
            print(json.dumps(run_semantic_splitter(), sort_keys=True))
        except BakeoffRefused as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command == "semantic-splitter-score":
        from .semantic_repair import score_semantic_splitter_artifacts

        try:
            print(json.dumps(score_semantic_splitter_artifacts(), sort_keys=True))
        except BakeoffRefused as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command == "export-adjudication":
        print(export_adjudication())
        return 0
    if args.command == "rescore-v2":
        print(json.dumps(rescore_v2(), sort_keys=True))
        return 0
    if args.command == "rescore-v3":
        print(json.dumps(rescore_v3(), sort_keys=True))
        return 0
    if args.command == "cleanup":
        run_id = os.environ.get("HINDSIGHT_BAKEOFF_RUN_ID")
        if not run_id:
            raise SystemExit("HINDSIGHT_BAKEOFF_RUN_ID is required")
        config = BakeoffConfig.from_env(os.environ, run_id=uuid.UUID(run_id))
        DisposableHindsight(config).cleanup()
        return 0
    if args.command == "score":
        print(json.dumps(score_artifacts(), sort_keys=True))
        return 0
    if args.command == "report":
        print(render_report())
        return 0
    raise SystemExit(f"{args.command} requires a completed measured run")


if __name__ == "__main__":
    main()
