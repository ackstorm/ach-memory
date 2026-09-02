import pytest

from experiments.memory_quality.runner import _measured_hmac_key, export_adjudication, main


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
