import uuid

import pytest

from experiments.memory_quality.hindsight import (
    BakeoffConfig,
    BakeoffRefused,
    DisposableHindsight,
    bank_id,
)


def bakeoff_env(**overrides):
    env = {
        "HINDSIGHT_BAKEOFF_CONFIRM": "disposable-banks-only",
        "HINDSIGHT_BAKEOFF_TENANT": "test",
    }
    env.update(overrides)
    return env


def test_live_config_requires_explicit_disposable_authority():
    env = bakeoff_env()
    env.pop("HINDSIGHT_BAKEOFF_CONFIRM")
    with pytest.raises(BakeoffRefused):
        BakeoffConfig.from_env(env)


def test_missing_url_defaults_to_the_local_service():
    env = bakeoff_env()
    assert BakeoffConfig.from_env(env).base_url == "http://127.0.0.1:8888"


def test_non_loopback_production_url_is_rejected():
    env = bakeoff_env(HINDSIGHT_BAKEOFF_URL="https://hindsight.example.invalid")
    env["MEMORY_HINDSIGHT_URL"] = env["HINDSIGHT_BAKEOFF_URL"]
    with pytest.raises(BakeoffRefused, match="production URL"):
        BakeoffConfig.from_env(env)


def test_confirmed_loopback_service_is_allowed():
    env = bakeoff_env(HINDSIGHT_BAKEOFF_URL="http://127.0.0.1:8888")
    assert BakeoffConfig.from_env(env).base_url == "http://127.0.0.1:8888"


def test_private_non_loopback_requires_explicit_allowlist():
    env = bakeoff_env(HINDSIGHT_BAKEOFF_URL="http://10.0.0.2:8888")
    with pytest.raises(BakeoffRefused):
        BakeoffConfig.from_env(env)


def test_bank_ids_are_run_scoped():
    run = uuid.uuid4()
    assert bank_id(run, "Semantic User", 1).startswith(f"mq55-{run.hex[:12]}-")


def test_cleanup_validates_entire_registry_before_deleting(tmp_path, respx_mock):
    run = uuid.uuid4()
    root = tmp_path / str(run)
    root.mkdir()
    (root / "banks.json").write_text(
        f'["mq55-{run.hex[:12]}-good-001", "mq55-foreign-bad-001"]'
    )
    config = BakeoffConfig.from_env(bakeoff_env(), run_id=run)
    client = DisposableHindsight(config, artifact_root=tmp_path)
    with pytest.raises(BakeoffRefused):
        client.cleanup()
    assert not respx_mock.calls
