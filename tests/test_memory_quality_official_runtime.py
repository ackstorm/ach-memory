from pathlib import Path

import pytest

from experiments.memory_quality.upstream import SourceDrift, verify_official_source


def test_missing_official_file_fails_before_runtime(tmp_path):
    with pytest.raises(SourceDrift):
        verify_official_source(tmp_path)


def test_real_pinned_checkout_passes_when_present():
    root = Path(__file__).parents[2] / "hindsight/hindsight-integrations/coding-agents"
    if not (root / "package.json").exists():
        pytest.skip("sibling upstream checkout is unavailable")
    source = verify_official_source(root)
    assert source.package_version == "0.5.1"
    assert source.release_commit == "c61c4e7d7"
