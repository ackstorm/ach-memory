from unittest.mock import create_autospec

import pytest

from memory.errors import HindsightError
from memory.hindsight.client import HindsightClient
from memory.retain_strategy import EXACT_RETAIN_STRATEGY, ensure_exact_retain_strategy


def _config(strategies):
    return {
        "bank_id": "redacted-by-client-boundary",
        "config": {"retain_strategies": strategies},
        "overrides": {},
    }


def test_missing_exact_strategy_is_added_without_removing_existing_strategies():
    client = create_autospec(HindsightClient, instance=True)
    existing = {"conversation": {"retain_extraction_mode": "concise"}}
    configured = {**existing, "ach-exact-v1": EXACT_RETAIN_STRATEGY}
    client.get_bank_config.side_effect = [_config(existing), _config(configured)]

    ensure_exact_retain_strategy(client, "internal-bank")

    client.ensure_bank.assert_called_once_with("internal-bank")
    client.update_bank_config.assert_called_once_with(
        "internal-bank", {"retain_strategies": configured}
    )


def test_matching_exact_strategy_needs_no_config_write():
    client = create_autospec(HindsightClient, instance=True)
    client.get_bank_config.return_value = _config({"ach-exact-v1": EXACT_RETAIN_STRATEGY})

    ensure_exact_retain_strategy(client, "internal-bank")

    client.update_bank_config.assert_not_called()


def test_unverifiable_exact_strategy_blocks_retain_instead_of_falling_back():
    client = create_autospec(HindsightClient, instance=True)
    wrong = {"ach-exact-v1": {"retain_extraction_mode": "concise"}}
    client.get_bank_config.side_effect = [_config(wrong), _config(wrong)]

    with pytest.raises(HindsightError, match="retain strategy could not be verified"):
        ensure_exact_retain_strategy(client, "internal-bank")


def test_a_second_call_reverifies_because_a_bank_can_be_deleted_under_us():
    """No per-process memo here, however hot this path is.

    `DELETE /v1/admin/memory/{scope}` tears a bank down and deliberately
    leaves its id in place for the next retain to auto-create -- and auto-
    creation brings it back with Hindsight's DEFAULT config. A cached
    "already verified" verdict outlives the teardown, so the recreated bank
    is never repaired and every later retain is silently stored under the
    default extraction strategy. This asserts the upstream check actually
    happens again; it fails the moment someone reintroduces the memo.
    """
    client = create_autospec(HindsightClient, instance=True)
    client.get_bank_config.return_value = _config({"ach-exact-v1": EXACT_RETAIN_STRATEGY})

    ensure_exact_retain_strategy(client, "internal-bank")
    ensure_exact_retain_strategy(client, "internal-bank")

    assert client.ensure_bank.call_count == 2
    assert client.get_bank_config.call_count == 2
