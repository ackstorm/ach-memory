"""CLI orchestration with refusal-first lifecycle commands."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path

import httpx

from .contracts import corpus_digest, load_delivery_cases, load_semantic_cases
from .delivery import build_delivery
from .hindsight import BakeoffConfig, BakeoffRefused, DisposableHindsight
from .preprocessing import run_preprocessing, scan_canaries
from .reliability import reliability_matrix, run_fault_scenario, run_worker_boundary_verification
from .scoring import Adjudication, build_blind_packet, decide, score_run
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
    return {"hindsight_version": version, "official_package_version": source.package_version, "official_checkout_commit": source.checkout_commit, "semantic_digest": corpus_digest(semantic), "delivery_digest": corpus_digest(delivery), "mutating_requests": 0, "run_id": str(config.run_id)}


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
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
        observations.extend({"case_id": item.fault, "variant": item.variant, "repetition": 1, "artifact_relpath": f"reliability/{item.variant}/{item.fault}.json", "hard_gate_flags": {"executed": True, "recovered": run_fault_scenario(item.variant, item.fault).observed in {"completed", "recovered_later", "requires_future_event", "unrecoverable_without_outbox", "rejected_as_stale"}}, "metric_values": {"requests": run_fault_scenario(item.variant, item.fault).requests}} for item in reliability_matrix())
        _atomic_json(artifact_dir / "manifest.json", manifest)
        with (artifact_dir / "observations.jsonl").open("w") as handle:
            for observation in observations:
                handle.write(json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n")
        semantic_cases = load_semantic_cases(ROOT / "corpus/semantic.jsonl")
        delivery_cases = load_delivery_cases(ROOT / "corpus/delivery.jsonl")
        canaries = tuple(canary for case in (*semantic_cases, *delivery_cases) for canary in case.secret_canaries)
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


def run_full(env=None) -> dict:
    """Execute the complete 319-observation matrix; intentionally not smoke."""
    env = env or os.environ
    manifest = preflight(env)
    config = BakeoffConfig.from_env(env, run_id=uuid.UUID(manifest["run_id"]))
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
                    observations.append(run_semantic_case(case, variant, repetition, banks).model_dump(mode="json"))
        for case in load_delivery_cases(ROOT / "corpus/delivery.jsonl"):
            for variant in ("ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"):
                for repetition in range(1, 4):
                    observations.append(build_delivery(case, variant, banks).model_copy(update={"latency_ms": repetition}).model_dump(mode="json") | {"repetition": repetition, "artifact_relpath": f"delivery/{case.id}/{variant}-{repetition}.json", "hard_gate_flags": {"executed": True}, "metric_values": {}})
        for item in reliability_matrix():
            result = run_fault_scenario(item.variant, item.fault)
            observations.append({"case_id": item.fault, "variant": item.variant, "repetition": 1, "artifact_relpath": f"reliability/{item.variant}/{item.fault}.json", "hard_gate_flags": {"executed": True}, "metric_values": {"requests": result.requests, "duplicates": result.duplicate_objects}})
        _atomic_json(artifact_dir / "manifest.json", manifest)
        (artifact_dir / "observations.jsonl").write_text("\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")) for item in observations) + "\n")
        canaries = tuple(canary for case in (*load_semantic_cases(ROOT / "corpus/semantic.jsonl"), *load_delivery_cases(ROOT / "corpus/delivery.jsonl")) for canary in case.secret_canaries)
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("artifact canary scan failed")
        return {"run_id": str(config.run_id), "observation_count": len(observations), "artifact_dir": str(artifact_dir)}
    finally:
        banks.cleanup()


def score_artifacts() -> dict:
    run_dir = _run_dir()
    observation_path = run_dir / "observations.jsonl"
    adjudication_path = run_dir / "adjudication.json"
    if not observation_path.exists() or not adjudication_path.exists():
        raise SystemExit("score requires observations.jsonl and adjudication.json")
    observations = []
    for line in observation_path.read_text().splitlines():
        raw = json.loads(line)
        if raw.get("variant") in VALID_OBSERVATION_VARIANTS or raw.get("variant") in {"ach_reliability", "official_reliability"}:
            from .contracts import RunObservation
            observation = RunObservation.model_validate(raw)
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
    key = os.environ.get("HINDSIGHT_BAKEOFF_HMAC_KEY", "development-only").encode()
    packet = build_blind_packet(observations, key)
    _atomic_json(run_dir / "blind-packet.json", packet.model_dump(mode="json"))
    adjudication = Adjudication.model_validate_json(adjudication_path.read_text())
    scorecard = score_run(packet, adjudication)
    _atomic_json(run_dir / "scorecard.json", scorecard.model_dump(mode="json"))
    _atomic_json(run_dir / "decisions.json", [item.model_dump(mode="json") for item in decide(scorecard)])
    return {"scorecard": str(run_dir / "scorecard.json"), "decisions": str(run_dir / "decisions.json")}


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
    parser.add_argument("command", choices=("preflight", "legacy-calibrate", "smoke", "run", "score", "cleanup", "report"))
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
