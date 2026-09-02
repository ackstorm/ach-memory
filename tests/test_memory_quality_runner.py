import pytest

from experiments.memory_quality.runner import _measured_hmac_key, main


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
