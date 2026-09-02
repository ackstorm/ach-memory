import pytest

from experiments.memory_quality import runner as runner_module
from experiments.memory_quality.contracts import RunObservation
from experiments.memory_quality.runner import _measured_hmac_key, export_adjudication, main
from experiments.memory_quality.scoring import build_blind_packet


def test_run_refuses_without_measured_authority(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_BAKEOFF_CONFIRM", raising=False)
    with pytest.raises(SystemExit):
        main(["run"])


def test_report_refuses_before_completed_measured_run():
    with pytest.raises(SystemExit):
        main(["report"])


def test_measured_hmac_key_rejects_development_default():
    with pytest.raises(Exception, match="HINDSIGHT_BAKEOFF_HMAC_KEY"):
        _measured_hmac_key({"HINDSIGHT_BAKEOFF_HMAC_KEY": "development-only"})


def test_measured_hmac_key_requires_32_random_bytes():
    with pytest.raises(Exception, match="32 random bytes"):
        _measured_hmac_key({"HINDSIGHT_BAKEOFF_HMAC_KEY": "aa" * 31})


def test_export_adjudication_rejects_non_opaque_packet(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    items = [{"case_id": f"S{i:02d}", "blind_variant": "V-abc", "repetition": 1, "artifact_relpath": f"semantic/S{i:02d}/ach_semantic-1.json"} for i in range(144)]
    (run / "blind-packet.json").write_text(__import__("json").dumps({"items": items, "corpus_digest": "x"}))
    monkeypatch.setenv("MEMORY_BAKEOFF_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_BAKEOFF_RUN_ID", "run")
    with pytest.raises(SystemExit, match="opaque"):
        export_adjudication()


def test_export_contains_only_packet_outputs_and_closed_manifest(tmp_path, monkeypatch):
    run = tmp_path / "run"
    outputs = run / "blind" / "outputs"
    outputs.mkdir(parents=True)
    observations = []
    for index in range(144):
        relative = f"blind/outputs/o-{index:03d}.json"
        (run / relative).write_text('{"claims":[]}')
        observations.append(RunObservation(case_id=f"S{index:02d}", variant="ach_semantic", repetition=1, artifact_relpath=relative, hard_gate_flags={}, metric_values={}))
    (run / "blind-packet.json").write_text(build_blind_packet(observations, b"a" * 32).model_dump_json())
    monkeypatch.setenv("MEMORY_BAKEOFF_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_BAKEOFF_RUN_ID", "run")
    target = export_adjudication()
    names = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
    assert names == {"blind-packet.json", "export-manifest.json", *(f"blind/outputs/o-{i:03d}.json" for i in range(144))}
    assert not (target / ".blind-mapping.json").exists()


def test_v3_writer_preserves_prior_score_artifacts_and_refuses_overwrite(tmp_path):
    scorecard = runner_module.score_run_v3(
        (),
        (),
        runner_module.Adjudication(run_id="r", items=()),
        expected_units={},
        critical_units={},
        consumer_complete=False,
    )
    decisions = runner_module.decide_v3(scorecard)
    prior = {
        "scorecard.json": "v1-score",
        "decisions.json": "v1-decisions",
        "report.md": "v1-report",
        "scorecard.v2.json": "v2-score",
        "decisions.v2.json": "v2-decisions",
        "report.v2.md": "v2-report",
    }
    for name, content in prior.items():
        (tmp_path / name).write_text(content)

    result = runner_module._write_v3_artifacts(tmp_path, scorecard, decisions)

    assert all((tmp_path / name).read_text() == content for name, content in prior.items())
    assert set(result) == {"scorecard", "decisions", "report"}
    assert (tmp_path / "scorecard.v3.json").exists()
    assert "NO_COMPARATIVE_HOST_ADAPTER_EVIDENCE" in (tmp_path / "report.v3.md").read_text()
    with pytest.raises(SystemExit, match="refuses to overwrite"):
        runner_module._write_v3_artifacts(tmp_path, scorecard, decisions)


def test_main_dispatches_rescore_v3(monkeypatch, capsys):
    monkeypatch.setattr(runner_module, "rescore_v3", lambda: {"scorecard": "scorecard.v3.json"})

    assert main(["rescore-v3"]) == 0
    assert "scorecard.v3.json" in capsys.readouterr().out
