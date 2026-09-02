"""Fenced semantic-only rerun for repairing the Phase 5.5 ACH baseline."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .contracts import (
    SEMANTIC_V2_CORPUS_VERSION,
    RunObservation,
    SemanticCase,
    SemanticRepairManifest,
    SemanticSplitterManifest,
    corpus_digest,
    load_delivery_cases,
    load_semantic_cases,
)
from .hindsight import BakeoffConfig, BakeoffRefused, DisposableHindsight
from .preprocessing import HOST_CANARIES, scan_canaries
from .scoring import (
    Adjudication,
    BlindPacket,
    _blind_packet_digest,
    build_blind_packet,
    decide_semantic_repair,
    decide_semantic_splitter,
    score_semantic_repair,
    unblind,
)
from .semantic import run_semantic_case

ROOT = Path(__file__).parent
CORPUS = ROOT / "corpus/semantic.jsonl"
SPLITTER_CORPUS = ROOT / "corpus/semantic-v2.jsonl"
VARIANTS = ("ach_semantic", "native_semantic", "hybrid_semantic")
PRODUCTION_FLAGS = (
    "MEMORY_CAPTURE_ENABLED",
    "MEMORY_CAPTURE_WORKER_ENABLED",
    "MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED",
)


def semantic_repair_matrix(cases: Sequence[SemanticCase]) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (case.id, variant, repetition)
        for case in cases
        for variant in VARIANTS
        for repetition in range(1, 4)
    )


def _validate_semantic_observations(
    observations: Sequence[RunObservation],
    expected: Sequence[tuple[str, str, int]],
) -> None:
    actual = tuple(
        (observation.case_id, observation.variant, observation.repetition)
        for observation in observations
    )
    if len(actual) != len(set(actual)):
        raise BakeoffRefused("semantic repair refuses duplicate observations")
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise BakeoffRefused("semantic repair requires the exact matrix")
    if any(
        not observation.hard_gate_flags.get("executed", True)
        for observation in observations
    ):
        raise BakeoffRefused("semantic repair refuses executed=false")


def _protect_artifacts(artifact_dir: Path) -> None:
    for path in artifact_dir.rglob("*"):
        if path.is_file():
            path.chmod(0o600)


def _assert_activation_off(env: Mapping[str, str]) -> None:
    enabled = [name for name in PRODUCTION_FLAGS if env.get(name, "false").casefold() != "false"]
    if env.get("MEMORY_PROFILE_DELIVERY_MODE", "legacy") != "legacy":
        enabled.append("MEMORY_PROFILE_DELIVERY_MODE")
    if enabled:
        raise BakeoffRefused("semantic repair requires every production activation flag off")


def run_semantic_repair(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Run exactly 144 semantic observations in fresh disposable banks."""
    selected_env = os.environ if env is None else env
    _assert_activation_off(selected_env)

    # Local imports avoid making the general runner depend on this optional
    # lifecycle while reusing its audited atomic/blinding boundaries.
    from .runner import (
        _atomic_json,
        _atomic_text,
        _blind_semantic_artifacts,
        _measured_hmac_key,
        preflight,
    )

    preflight_manifest = preflight(selected_env)
    run_id = uuid.UUID(preflight_manifest["run_id"])
    artifact_root = Path(selected_env.get("MEMORY_BAKEOFF_ARTIFACT_DIR", ".artifacts/memory-quality"))
    artifact_dir = artifact_root / str(run_id)
    if artifact_dir.exists():
        raise BakeoffRefused("semantic repair refuses an existing run directory")

    config = BakeoffConfig.from_env(selected_env, run_id=run_id)
    key = _measured_hmac_key(selected_env)
    mapping_path = Path(
        selected_env.get(
            "HINDSIGHT_BAKEOFF_MAPPING_PATH",
            artifact_root / ".private" / f"{run_id}.mapping.json",
        )
    )
    if mapping_path.resolve().is_relative_to(artifact_dir.resolve()):
        raise BakeoffRefused("semantic repair mapping must stay outside the run directory")
    if mapping_path.exists():
        raise BakeoffRefused("semantic repair mapping already exists")

    cases = load_semantic_cases(CORPUS)
    by_id = {case.id: case for case in cases}
    matrix = semantic_repair_matrix(cases)
    canaries = tuple(
        dict.fromkeys(
            HOST_CANARIES
            + tuple(
                canary
                for case in (
                    *cases,
                    *load_delivery_cases(ROOT / "corpus/delivery.jsonl"),
                )
                for canary in case.secret_canaries
            )
        )
    )
    observations: list[dict[str, object]] = []
    banks = DisposableHindsight(config, artifact_root=artifact_root)
    cleanup_complete = False
    try:
        for case_id, variant, repetition in matrix:
            artifact = artifact_dir / "semantic" / case_id / f"{variant}-{repetition}.json"
            observation = run_semantic_case(
                by_id[case_id], variant, repetition, banks, artifact
            )
            observations.append(observation.model_dump(mode="json"))
        typed = tuple(RunObservation.model_validate(item) for item in observations)
        _validate_semantic_observations(typed, matrix)
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("semantic repair artifact canary scan failed")
        _blind_semantic_artifacts(observations, artifact_dir, key, mapping_path)
        typed = tuple(RunObservation.model_validate(item) for item in observations)
        packet = build_blind_packet(typed, key)
        _atomic_json(artifact_dir / "blind-packet.json", packet.model_dump(mode="json"))
        _atomic_text(
            artifact_dir / "observations.jsonl",
            "\n".join(json.dumps(item, sort_keys=True, separators=(",", ":")) for item in observations)
            + "\n",
        )
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("semantic repair artifact canary scan failed")
    finally:
        try:
            banks.cleanup()
            cleanup_complete = True
        finally:
            _protect_artifacts(artifact_dir)

    manifest = SemanticRepairManifest(
        run_id=str(run_id),
        hindsight_version=preflight_manifest["hindsight_version"],
        official_package_version=preflight_manifest["official_package_version"],
        semantic_digest=corpus_digest(cases),
        observation_count=len(observations),
        disposable_bank_count=144,
        native_retain_count=48,
        cleanup_complete=cleanup_complete,
        # Persistent server mutations are 144 creates, 48 native retains and
        # 144 deletes. ACH/hybrid extraction calls are dry runs.
        mutating_requests=336,
    )
    _atomic_json(artifact_dir / "manifest.json", manifest.model_dump(mode="json"))
    _protect_artifacts(artifact_dir)
    return {
        "run_id": str(run_id),
        "observation_count": len(observations),
        "artifact_dir": str(artifact_dir),
        "cleanup_complete": cleanup_complete,
    }


def run_semantic_splitter(
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Run the Phase 5.7 matrix with measured projection persistence."""
    selected_env = os.environ if env is None else env
    _assert_activation_off(selected_env)

    from .runner import (
        _atomic_json,
        _atomic_text,
        _blind_semantic_artifacts,
        _measured_hmac_key,
        preflight,
    )

    preflight_manifest = preflight(selected_env)
    run_id = uuid.UUID(preflight_manifest["run_id"])
    artifact_root = Path(
        selected_env.get(
            "MEMORY_BAKEOFF_ARTIFACT_DIR", ".artifacts/memory-quality"
        )
    )
    artifact_dir = artifact_root / str(run_id)
    if artifact_dir.exists():
        raise BakeoffRefused("semantic splitter refuses an existing run directory")

    config = BakeoffConfig.from_env(selected_env, run_id=run_id)
    key = _measured_hmac_key(selected_env)
    mapping_path = Path(
        selected_env.get(
            "HINDSIGHT_BAKEOFF_MAPPING_PATH",
            artifact_root / ".private" / f"{run_id}.mapping.json",
        )
    )
    if mapping_path.resolve().is_relative_to(artifact_dir.resolve()):
        raise BakeoffRefused("semantic splitter mapping must stay outside the run directory")
    if mapping_path.exists():
        raise BakeoffRefused("semantic splitter mapping already exists")

    cases = load_semantic_cases(SPLITTER_CORPUS)
    by_id = {case.id: case for case in cases}
    matrix = semantic_repair_matrix(cases)
    canaries = tuple(
        dict.fromkeys(
            HOST_CANARIES
            + tuple(
                canary
                for case in (
                    *cases,
                    *load_delivery_cases(ROOT / "corpus/delivery.jsonl"),
                )
                for canary in case.secret_canaries
            )
        )
    )
    observations: list[dict[str, object]] = []
    banks = DisposableHindsight(config, artifact_root=artifact_root)
    cleanup_complete = False
    cleanup_bank_count = 0
    disposable_bank_count = 0
    try:
        for case_id, variant, repetition in matrix:
            artifact = (
                artifact_dir
                / "semantic"
                / case_id
                / f"{variant}-{repetition}.json"
            )
            observation = run_semantic_case(
                by_id[case_id], variant, repetition, banks, artifact
            )
            observations.append(observation.model_dump(mode="json"))
        typed = tuple(RunObservation.model_validate(item) for item in observations)
        _validate_semantic_observations(typed, matrix)
        disposable_bank_count = banks.registered_bank_count
        if disposable_bank_count != 192:
            raise BakeoffRefused("semantic splitter requires exactly 192 disposable banks")
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("semantic splitter artifact canary scan failed")
        _blind_semantic_artifacts(observations, artifact_dir, key, mapping_path)
        typed = tuple(RunObservation.model_validate(item) for item in observations)
        packet = build_blind_packet(typed, key)
        _atomic_json(
            artifact_dir / "blind-packet.json", packet.model_dump(mode="json")
        )
        _atomic_text(
            artifact_dir / "observations.jsonl",
            "\n".join(
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in observations
            )
            + "\n",
        )
        if scan_canaries((artifact_dir,), canaries):
            raise RuntimeError("semantic splitter artifact canary scan failed")
    finally:
        try:
            cleanup_bank_count = banks.cleanup()
            cleanup_complete = cleanup_bank_count == disposable_bank_count
        finally:
            _protect_artifacts(artifact_dir)

    projection_retain_count = sum(
        int(item.get("metric_values", {}).get("claim_count", 0))
        for item in observations
        if item.get("variant") == "hybrid_semantic"
    )
    manifest = SemanticSplitterManifest(
        run_id=str(run_id),
        hindsight_version=preflight_manifest["hindsight_version"],
        official_package_version=preflight_manifest["official_package_version"],
        corpus_version=SEMANTIC_V2_CORPUS_VERSION,
        semantic_digest=corpus_digest(cases),
        observation_count=len(observations),
        disposable_bank_count=disposable_bank_count,
        cleanup_bank_count=cleanup_bank_count,
        native_retain_count=48,
        projection_retain_count=projection_retain_count,
        cleanup_complete=cleanup_complete,
        mutating_requests=(
            disposable_bank_count * 2 + 48 + projection_retain_count
        ),
    )
    _atomic_json(artifact_dir / "manifest.json", manifest.model_dump(mode="json"))
    _protect_artifacts(artifact_dir)
    return {
        "run_id": str(run_id),
        "observation_count": len(observations),
        "artifact_dir": str(artifact_dir),
        "cleanup_complete": cleanup_complete,
    }


def _semantic_repair_report(
    scorecard, decisions, *, title: str = "Repaired semantic baseline"
) -> str:
    rows = "\n".join(
        f"| {decision.component} | {decision.ruling} | "
        f"{','.join(decision.reason_codes)} |"
        for decision in decisions
    )
    return (
        f"# Memory Quality — {title}\n\n"
        f"Run: `{scorecard.run_id}`. Only semantic extraction and scope routing "
        "were measured. No production architecture was changed automatically.\n\n"
        "| Component | Ruling | Reasons |\n"
        "|---|---|---|\n"
        f"{rows}\n"
    )


def _assert_safe_score_payloads(payloads: Sequence[str], canaries: Sequence[str]) -> None:
    forbidden = (
        *canaries,
        "mq55-",
        '"bank_id"',
        '"mapping"',
        '"seal"',
        '"hmac_key"',
        '"raw_content"',
        '"transcript"',
    )
    if any(value in payload for payload in payloads for value in forbidden):
        raise BakeoffRefused("semantic repair score contains forbidden material")


def score_semantic_repair_artifacts(
    env: Mapping[str, str] | None = None,
    *,
    corpus_path: Path = CORPUS,
    manifest_model: type[SemanticRepairManifest | SemanticSplitterManifest] = SemanticRepairManifest,
    output_suffix: str = "semantic-repair",
    report_title: str = "Phase 5.6 repaired semantic baseline",
    decision_function: Callable = decide_semantic_repair,
) -> dict[str, str]:
    """Unblind and score one complete semantic-repair consensus exactly once."""
    selected_env = os.environ if env is None else env
    run_id = selected_env.get("HINDSIGHT_BAKEOFF_RUN_ID")
    if not run_id:
        raise BakeoffRefused("semantic repair score requires a run ID")
    artifact_root = Path(
        selected_env.get("MEMORY_BAKEOFF_ARTIFACT_DIR", ".artifacts/memory-quality")
    )
    run_dir = artifact_root / run_id
    targets = {
        "scorecard": run_dir / f"scorecard.{output_suffix}.json",
        "decisions": run_dir / f"decisions.{output_suffix}.json",
        "report": run_dir / f"report.{output_suffix}.md",
    }
    if any(path.exists() for path in targets.values()):
        raise BakeoffRefused("semantic repair score refuses to overwrite outputs")

    consensus_path = artifact_root / f"{run_id}-adjudication.CONSENSUS.json"
    expected_consensus = selected_env.get("HINDSIGHT_ADJUDICATION_CONSENSUS_SHA256")
    if (
        not expected_consensus
        or not consensus_path.is_file()
        or hashlib.sha256(consensus_path.read_bytes()).hexdigest()
        != expected_consensus
    ):
        raise BakeoffRefused("semantic repair score refuses a missing or altered consensus")
    adjudication = Adjudication.model_validate_json(consensus_path.read_text())
    if adjudication.run_id != run_id:
        raise BakeoffRefused("semantic repair score refuses consensus for another run")

    manifest = manifest_model.model_validate_json((run_dir / "manifest.json").read_text())
    if manifest.run_id != run_id or not manifest.cleanup_complete:
        raise BakeoffRefused("semantic repair score requires completed cleanup")
    observations = tuple(
        RunObservation.model_validate_json(line)
        for line in (run_dir / "observations.jsonl").read_text().splitlines()
        if line.strip()
    )
    cases = load_semantic_cases(corpus_path)
    _validate_semantic_observations(observations, semantic_repair_matrix(cases))

    packet_path = run_dir / "blind-packet.json"
    packet_before = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    packet = BlindPacket.model_validate_json(packet_path.read_text())
    packet_keys = {
        (item.case_id, item.blind_variant, item.repetition) for item in packet.items
    }
    adjudication_keys = {
        (item.case_id, item.blind_variant, item.repetition)
        for item in adjudication.items
    }
    if (
        len(packet.items) != 144
        or len(packet_keys) != 144
        or len(adjudication.items) != 144
        or len(adjudication_keys) != 144
        or packet_keys != adjudication_keys
    ):
        raise BakeoffRefused("semantic repair score requires exact 144-item coverage")

    from .runner import _atomic_json, _atomic_text, _measured_hmac_key

    key = _measured_hmac_key(selected_env)
    rebuilt = build_blind_packet(observations, key)
    accepted_digests = {
        _blind_packet_digest(packet.items, canonical=True),
        _blind_packet_digest(packet.items, canonical=False),
    }
    expected_packet_sha = selected_env.get("HINDSIGHT_BAKEOFF_PACKET_SHA256")
    if expected_packet_sha is not None and packet_before != expected_packet_sha:
        raise BakeoffRefused("semantic repair score refuses an altered blind packet")
    digest_is_verifiable = (
        packet.corpus_digest in accepted_digests or expected_packet_sha is not None
    )
    if rebuilt.items != packet.items or not digest_is_verifiable:
        raise BakeoffRefused("semantic repair score refuses an altered blind packet")
    mapping_value = selected_env.get("HINDSIGHT_BAKEOFF_MAPPING_PATH")
    if not mapping_value:
        raise BakeoffRefused("semantic repair score requires the external mapping")
    mapping_path = Path(mapping_value)
    if (
        not mapping_path.is_file()
        or mapping_path.resolve().is_relative_to(run_dir.resolve())
        or mapping_path.stat().st_mode & 0o077
    ):
        raise BakeoffRefused("semantic repair score requires a private external mapping")
    unblinded = unblind(packet, adjudication, mapping_path, key)
    if hashlib.sha256(packet_path.read_bytes()).hexdigest() != packet_before:
        raise BakeoffRefused("semantic repair packet changed during validation")

    expected_units = {
        case.id: tuple(unit.unit_id for unit in case.expected_units) for case in cases
    }
    critical_units = {
        case.id: tuple(unit.unit_id for unit in case.expected_units if unit.critical)
        for case in cases
    }
    ignored_units = {
        case.id: tuple(
            unit.unit_id for unit in case.expected_units if unit.scope == "ignore"
        )
        for case in cases
    }
    critical_rejection_cases = tuple(
        case.id
        for case in cases
        if any(unit.critical and not unit.current for unit in case.expected_units)
    )
    scorecard = score_semantic_repair(
        observations,
        unblinded,
        adjudication,
        expected_units=expected_units,
        critical_units=critical_units,
        ignored_units=ignored_units,
        critical_rejection_cases=critical_rejection_cases,
        run_id=run_id,
    )
    decisions = decision_function(scorecard)
    score_payload = json.dumps(
        scorecard.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    decision_payload = json.dumps(
        [item.model_dump(mode="json") for item in decisions],
        sort_keys=True,
        separators=(",", ":"),
    )
    report_payload = _semantic_repair_report(
        scorecard, decisions, title=report_title
    )
    canaries = tuple(
        dict.fromkeys(
            HOST_CANARIES
            + tuple(canary for case in cases for canary in case.secret_canaries)
        )
    )
    _assert_safe_score_payloads(
        (score_payload, decision_payload, report_payload), canaries
    )
    _atomic_json(targets["scorecard"], scorecard.model_dump(mode="json"))
    _atomic_json(
        targets["decisions"],
        [item.model_dump(mode="json") for item in decisions],
    )
    _atomic_text(targets["report"], report_payload)
    _protect_artifacts(run_dir)
    return {name: str(path) for name, path in targets.items()}


def score_semantic_splitter_artifacts(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Score the measured Phase 5.7 corpus without changing older runs."""
    return score_semantic_repair_artifacts(
        env,
        corpus_path=SPLITTER_CORPUS,
        manifest_model=SemanticSplitterManifest,
        output_suffix="semantic-splitter.v2",
        report_title="Phase 5.7 measured semantic splitter",
        decision_function=decide_semantic_splitter,
    )
