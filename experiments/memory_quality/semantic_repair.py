"""Fenced semantic-only rerun for repairing the Phase 5.5 ACH baseline."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from .contracts import (
    RunObservation,
    SemanticCase,
    SemanticRepairManifest,
    corpus_digest,
    load_delivery_cases,
    load_semantic_cases,
)
from .hindsight import BakeoffConfig, BakeoffRefused, DisposableHindsight
from .preprocessing import HOST_CANARIES, scan_canaries
from .scoring import build_blind_packet
from .semantic import run_semantic_case

ROOT = Path(__file__).parent
CORPUS = ROOT / "corpus/semantic.jsonl"
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
