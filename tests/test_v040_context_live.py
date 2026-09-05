import os

import pytest


@pytest.mark.integration
def test_v040_context_live_gate_is_explicitly_guarded():
    if os.environ.get("HINDSIGHT_V040_CONFIRM") != "disposable-banks-only":
        pytest.skip("live context gate requires disposable-banks-only confirmation")
    pytest.skip("run the disposable loopback context latency harness in release CI")
