import hashlib
import hmac
import json

import pytest

from experiments.memory_quality import runner as runner_module
from experiments.memory_quality import semantic_repair as repair_module
from experiments.memory_quality.contracts import RunObservation, load_semantic_cases
from experiments.memory_quality.hindsight import BakeoffRefused
from experiments.memory_quality.scoring import (
    Adjudication,
    AdjudicationItem,
    build_blind_packet,
)
from experiments.memory_quality.semantic_repair import (
    CORPUS,
    _validate_semantic_observations,
    run_semantic_repair,
    score_semantic_repair_artifacts,
    semantic_repair_matrix,
)


def test_semantic_repair_refuses_without_explicit_authority():
    with pytest.raises(BakeoffRefused):
        run_semantic_repair({})


def test_semantic_repair_refuses_to_overwrite_external_mapping(tmp_path, monkeypatch):
    mapping = tmp_path / "mapping.json"
    mapping.write_text("custodied evidence")
    monkeypatch.setattr(
        runner_module,
        "preflight",
        lambda env: {
            "run_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "hindsight_version": "0.9.2",
            "official_package_version": "0.5.1",
        },
    )
    monkeypatch.setattr(runner_module, "_measured_hmac_key", lambda env: b"a" * 32)

    with pytest.raises(BakeoffRefused, match="mapping already exists"):
        run_semantic_repair(
            {
                "HINDSIGHT_BAKEOFF_CONFIRM": "disposable-banks-only",
                "HINDSIGHT_BAKEOFF_MAPPING_PATH": str(mapping),
                "MEMORY_BAKEOFF_ARTIFACT_DIR": str(tmp_path / "artifacts"),
            }
        )

    assert mapping.read_text() == "custodied evidence"


def test_semantic_repair_matrix_is_exact_and_deterministic():
    cases = load_semantic_cases(CORPUS)

    keys = semantic_repair_matrix(cases)

    assert len(keys) == 144
    assert len(set(keys)) == 144
    assert keys == semantic_repair_matrix(cases)
    assert {variant for _, variant, _ in keys} == {
        "ach_semantic",
        "native_semantic",
        "hybrid_semantic",
    }
    assert all(repetition in {1, 2, 3} for _, _, repetition in keys)


def _observation(case_id: str, variant: str, repetition: int) -> RunObservation:
    return RunObservation(
        case_id=case_id,
        variant=variant,
        repetition=repetition,
        artifact_relpath=f"semantic/{case_id}/{variant}-{repetition}.json",
        hard_gate_flags={"executed": True, "no_canary": True},
        metric_values={},
    )


def test_semantic_observation_validation_rejects_duplicate_missing_and_unexecuted_rows():
    expected = (("S01", "ach_semantic", 1), ("S01", "ach_semantic", 2))
    first = _observation(*expected[0])
    second = _observation(*expected[1])

    _validate_semantic_observations((first, second), expected)
    with pytest.raises(BakeoffRefused, match="duplicate"):
        _validate_semantic_observations((first, first), expected)
    with pytest.raises(BakeoffRefused, match="exact matrix"):
        _validate_semantic_observations((first,), expected)
    unexecuted = first.model_copy(
        update={"hard_gate_flags": {"executed": False, "no_canary": True}}
    )
    with pytest.raises(BakeoffRefused, match="executed=false"):
        _validate_semantic_observations((unexecuted, second), expected)


def test_semantic_repair_lifecycle_writes_exact_atomic_outputs_and_cleans_up(
    tmp_path, monkeypatch
):
    run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    artifact_root = tmp_path / "artifacts"
    mapping_path = tmp_path / "private" / "mapping.json"
    cleanup_calls: list[bool] = []

    class FakeBanks:
        def __init__(self, config, *, artifact_root):
            self.artifact_dir = artifact_root / str(config.run_id)
            self.artifact_dir.mkdir(parents=True)

        def cleanup(self):
            cleanup_calls.append(True)

    def fake_preflight(env):
        return {
            "run_id": run_id,
            "hindsight_version": "0.9.2",
            "official_package_version": "0.5.1",
        }

    def fake_run(case, variant, repetition, banks, artifact_path):
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text('{"claims":[]}\n')
        return _observation(case.id, variant, repetition)

    monkeypatch.setattr(runner_module, "preflight", fake_preflight)
    monkeypatch.setattr(runner_module, "_measured_hmac_key", lambda env: b"a" * 32)
    monkeypatch.setattr(repair_module, "DisposableHindsight", FakeBanks)
    monkeypatch.setattr(repair_module, "run_semantic_case", fake_run)
    env = {
        "HINDSIGHT_BAKEOFF_CONFIRM": "disposable-banks-only",
        "HINDSIGHT_BAKEOFF_RUN_ID": run_id,
        "HINDSIGHT_BAKEOFF_MAPPING_PATH": str(mapping_path),
        "MEMORY_BAKEOFF_ARTIFACT_DIR": str(artifact_root),
    }

    result = run_semantic_repair(env)

    run_dir = artifact_root / run_id
    observations = [
        json.loads(line)
        for line in (run_dir / "observations.jsonl").read_text().splitlines()
    ]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert result["observation_count"] == 144
    assert len(observations) == 144
    assert len({(row["case_id"], row["variant"], row["repetition"]) for row in observations}) == 144
    assert len(json.loads((run_dir / "blind-packet.json").read_text())["items"]) == 144
    assert manifest["observation_count"] == 144
    assert manifest["disposable_bank_count"] == 144
    assert manifest["native_retain_count"] == 48
    assert manifest["mutating_requests"] == 336
    assert manifest["cleanup_complete"] is True
    assert cleanup_calls == [True]
    assert mapping_path.stat().st_mode & 0o077 == 0
    assert not tuple(run_dir.rglob(".*.tmp"))
    assert all(
        path.stat().st_mode & 0o077 == 0
        for path in run_dir.rglob("*")
        if path.is_file()
    )


def test_semantic_repair_refuses_serialized_canary_and_still_cleans_up(
    tmp_path, monkeypatch
):
    run_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    artifact_root = tmp_path / "artifacts"
    cleanup_calls: list[bool] = []
    canary = load_semantic_cases(CORPUS)[0].secret_canaries[0]

    class FakeBanks:
        def __init__(self, config, *, artifact_root):
            self.artifact_dir = artifact_root / str(config.run_id)
            self.artifact_dir.mkdir(parents=True)

        def cleanup(self):
            cleanup_calls.append(True)

    def fake_run(case, variant, repetition, banks, artifact_path):
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps({"claim": canary}))
        return _observation(case.id, variant, repetition)

    monkeypatch.setattr(
        runner_module,
        "preflight",
        lambda env: {
            "run_id": run_id,
            "hindsight_version": "0.9.2",
            "official_package_version": "0.5.1",
        },
    )
    monkeypatch.setattr(runner_module, "_measured_hmac_key", lambda env: b"a" * 32)
    monkeypatch.setattr(repair_module, "DisposableHindsight", FakeBanks)
    monkeypatch.setattr(repair_module, "run_semantic_case", fake_run)

    with pytest.raises(RuntimeError, match="canary"):
        run_semantic_repair(
            {
                "HINDSIGHT_BAKEOFF_CONFIRM": "disposable-banks-only",
                "HINDSIGHT_BAKEOFF_RUN_ID": run_id,
                "HINDSIGHT_BAKEOFF_MAPPING_PATH": str(tmp_path / "mapping.json"),
                "MEMORY_BAKEOFF_ARTIFACT_DIR": str(artifact_root),
            }
        )

    assert cleanup_calls == [True]
    run_dir = artifact_root / run_id
    assert all(
        path.stat().st_mode & 0o077 == 0
        for path in run_dir.rglob("*")
        if path.is_file()
    )


def _semantic_scoring_fixture(
    tmp_path,
    *,
    sorted_observation_keys=False,
    sorted_packet_keys=False,
    legacy_packet_digest=False,
):
    run_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    artifact_root = tmp_path / "artifacts"
    run_dir = artifact_root / run_id
    run_dir.mkdir(parents=True)
    key = b"k" * 32
    cases = load_semantic_cases(CORPUS)
    observations = []
    mapping = {}
    for index, (case_id, variant, repetition) in enumerate(
        semantic_repair_matrix(cases)
    ):
        opaque = f"blind/outputs/o-{index:03d}.json"
        target = run_dir / opaque
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"claims":[]}')
        observation = _observation(case_id, variant, repetition).model_copy(
            update={
                "artifact_relpath": opaque,
                "hard_gate_flags": {
                    "no_shared_document": variant != "native_semantic",
                    "no_canary": True,
                    "executed": True,
                },
            }
        )
        observations.append(observation)
        mapping[opaque] = {
            "case_id": case_id,
            "variant": variant,
            "repetition": repetition,
        }
    packet = build_blind_packet(observations, key)
    if legacy_packet_digest:
        legacy_digest = hashlib.sha256(
            "\n".join(item.model_dump_json() for item in packet.items).encode()
        ).hexdigest()
        packet = packet.model_copy(update={"corpus_digest": legacy_digest})
    units = {
        case.id: tuple(
            unit.unit_id for unit in case.expected_units if unit.scope != "ignore"
        )
        for case in cases
    }
    adjudication = Adjudication(
        run_id=run_id,
        items=tuple(
            AdjudicationItem(
                case_id=item.case_id,
                blind_variant=item.blind_variant,
                repetition=item.repetition,
                required_units_met=units[item.case_id],
                unsupported_current_claims=0,
                wrong_scope_claims=0,
                notes_code="NONE",
            )
            for item in packet.items
        ),
    )
    consensus_path = artifact_root / f"{run_id}-adjudication.CONSENSUS.json"
    consensus_path.write_text(adjudication.model_dump_json())
    packet_payload = (
        json.dumps(
            packet.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        if sorted_packet_keys
        else packet.model_dump_json()
    )
    (run_dir / "blind-packet.json").write_text(packet_payload)
    if sorted_observation_keys:
        observation_lines = (
            json.dumps(
                item.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in observations
        )
    else:
        observation_lines = (item.model_dump_json() for item in observations)
    (run_dir / "observations.jsonl").write_text("\n".join(observation_lines) + "\n")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": "semantic_repair",
                "hindsight_version": "0.9.2",
                "official_package_version": "0.5.1",
                "semantic_digest": "fixture",
                "observation_count": 144,
                "disposable_bank_count": 144,
                "native_retain_count": 48,
                "cleanup_complete": True,
                "mutating_requests": 336,
            }
        )
    )
    encoded = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "mapping": mapping,
                "seal": hmac.new(key, encoded, hashlib.sha256).hexdigest(),
            }
        )
    )
    mapping_path.chmod(0o600)
    env = {
        "MEMORY_BAKEOFF_ARTIFACT_DIR": str(artifact_root),
        "HINDSIGHT_BAKEOFF_RUN_ID": run_id,
        "HINDSIGHT_BAKEOFF_HMAC_KEY": key.hex(),
        "HINDSIGHT_BAKEOFF_MAPPING_PATH": str(mapping_path),
        "HINDSIGHT_ADJUDICATION_CONSENSUS_SHA256": hashlib.sha256(
            consensus_path.read_bytes()
        ).hexdigest(),
    }
    return env, run_dir, consensus_path


def test_semantic_repair_scoring_validates_consensus_and_writes_only_semantic_decisions(
    tmp_path,
):
    env, run_dir, _ = _semantic_scoring_fixture(tmp_path)

    result = score_semantic_repair_artifacts(env)

    decisions = json.loads((run_dir / "decisions.semantic-repair.json").read_text())
    scorecard = json.loads((run_dir / "scorecard.semantic-repair.json").read_text())
    assert set(result) == {"scorecard", "decisions", "report"}
    assert set(scorecard["components"]) == {"semantic_extractor", "scope_router"}
    assert {item["component"] for item in decisions} == {
        "semantic_extractor",
        "scope_router",
    }
    with pytest.raises(BakeoffRefused, match="refuses to overwrite"):
        score_semantic_repair_artifacts(env)


def test_semantic_repair_scoring_accepts_equivalent_gate_key_order(tmp_path):
    """Catch order-sensitive packet digests after sorted observation persistence."""
    env, run_dir, _ = _semantic_scoring_fixture(
        tmp_path,
        sorted_observation_keys=True,
        sorted_packet_keys=True,
        legacy_packet_digest=True,
    )
    packet_path = run_dir / "blind-packet.json"
    env["HINDSIGHT_BAKEOFF_PACKET_SHA256"] = hashlib.sha256(
        packet_path.read_bytes()
    ).hexdigest()

    score_semantic_repair_artifacts(env)

    assert (run_dir / "scorecard.semantic-repair.json").is_file()


def test_semantic_repair_scoring_refuses_a_forged_packet_digest(tmp_path):
    env, run_dir, _ = _semantic_scoring_fixture(tmp_path)
    packet_path = run_dir / "blind-packet.json"
    packet = json.loads(packet_path.read_text())
    packet["corpus_digest"] = "0" * 64
    packet_path.write_text(json.dumps(packet))

    with pytest.raises(BakeoffRefused, match="altered blind packet"):
        score_semantic_repair_artifacts(env)


def test_semantic_repair_scoring_refuses_external_packet_sha_mismatch(tmp_path):
    env, _, _ = _semantic_scoring_fixture(tmp_path)
    env["HINDSIGHT_BAKEOFF_PACKET_SHA256"] = "0" * 64

    with pytest.raises(BakeoffRefused, match="altered blind packet"):
        score_semantic_repair_artifacts(env)


def test_semantic_repair_scoring_refuses_consensus_sha_mismatch(tmp_path):
    env, _, _ = _semantic_scoring_fixture(tmp_path)
    env["HINDSIGHT_ADJUDICATION_CONSENSUS_SHA256"] = "0" * 64

    with pytest.raises(BakeoffRefused, match="consensus"):
        score_semantic_repair_artifacts(env)


def test_semantic_repair_scoring_refuses_incomplete_consensus(tmp_path):
    env, _, consensus_path = _semantic_scoring_fixture(tmp_path)
    payload = json.loads(consensus_path.read_text())
    payload["items"].pop()
    consensus_path.write_text(json.dumps(payload))
    env["HINDSIGHT_ADJUDICATION_CONSENSUS_SHA256"] = hashlib.sha256(
        consensus_path.read_bytes()
    ).hexdigest()

    with pytest.raises(BakeoffRefused, match="144-item"):
        score_semantic_repair_artifacts(env)
