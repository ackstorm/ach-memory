import pytest

from experiments.memory_quality.runner import main


def test_run_refuses_without_measured_authority(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_BAKEOFF_CONFIRM", raising=False)
    with pytest.raises(SystemExit):
        main(["run"])


def test_report_refuses_before_completed_measured_run():
    with pytest.raises(SystemExit):
        main(["report"])
