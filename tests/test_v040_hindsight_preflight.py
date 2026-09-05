import importlib.util
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from memory.hindsight.client import HindsightClient
from memory.retain_strategy import EXACT_RETAIN_STRATEGY


def _module():
    path = Path("scripts/v040-hindsight-preflight.py")
    spec = importlib.util.spec_from_file_location("v040_hindsight_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_reads_the_installed_named_strategy_without_running_extraction():
    module = _module()
    client = create_autospec(HindsightClient, instance=True)
    client.get_version.return_value = {"api_version": "0.9.2", "features": {}}
    client.get_bank_config.return_value = {
        "config": {"retain_strategies": {"ach-exact-v1": EXACT_RETAIN_STRATEGY}},
        "overrides": {"retain_strategies": {"ach-exact-v1": EXACT_RETAIN_STRATEGY}},
    }

    result = module.check(client, "internal-bank")

    assert result == {
        "hindsight_0_9_2": 1,
        "strategy_present": 1,
        "strategy_exact": 1,
    }
    client.dry_run_extract.assert_not_called()


def test_preflight_rejects_a_mismatched_installed_strategy():
    module = _module()
    client = create_autospec(HindsightClient, instance=True)
    client.get_version.return_value = {"api_version": "0.9.2", "features": {}}
    client.get_bank_config.return_value = {
        "config": {
            "retain_strategies": {
                "ach-exact-v1": {"retain_extraction_mode": "concise"}
            }
        },
        "overrides": {},
    }

    with pytest.raises(module.PreflightFailed, match="does not match"):
        module.check(client, "internal-bank")


def test_preflight_rejects_an_unvalidated_hindsight_version():
    module = _module()
    client = create_autospec(HindsightClient, instance=True)
    client.get_version.return_value = {"api_version": "0.9.3", "features": {}}

    with pytest.raises(module.PreflightFailed, match="0.9.2"):
        module.check(client, "internal-bank")

    client.get_bank_config.assert_not_called()
