import pytest
from pydantic import ValidationError

from memory.config import Settings

REQUIRED = {
    "MEMORY_DATABASE_URL": "postgresql+psycopg://memory:memory@localhost:5432/memory",
    "MEMORY_MASTER_KEY_HASH": "0" * 64,
    "MEMORY_HINDSIGHT_URL": "http://localhost:8888",
}


def _clear(monkeypatch):
    for key in list(REQUIRED) + ["MEMORY_TENANT_ID", "MEMORY_MAX_CONTENT_BYTES"]:
        monkeypatch.delenv(key, raising=False)


def test_settings_read_from_environment(monkeypatch):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    settings = Settings()

    assert settings.database_url == REQUIRED["MEMORY_DATABASE_URL"]
    assert settings.hindsight_url == "http://localhost:8888"


def test_tenant_id_defaults_to_hindsight_default_segment(monkeypatch):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    assert Settings().tenant_id == "default"


def test_missing_required_setting_fails_loudly(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMORY_DATABASE_URL", REQUIRED["MEMORY_DATABASE_URL"])

    with pytest.raises(ValidationError):
        Settings()


def test_a_zero_write_limit_is_refused(monkeypatch):
    """MEMORY_WRITE_LIMIT=0 is the natural spelling of "block all writes" and
    made Limiter.check evaluate len(hits) >= 0 -> True on an empty deque, then
    IndexError on hits[0] -> 500 on every write instead of 429."""
    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_a_master_hash_with_stray_whitespace_still_authenticates(monkeypatch):
    """`echo -n k | sha256sum` appends "  -"; a hash read from a mounted Secret
    carries "\\n"; PowerShell's Get-FileHash is uppercase. Each silently
    produced a master key that authenticates nothing, indistinguishable from a
    wrong key -- on the one credential whose failure blocks all provisioning."""
    from memory.auth import keys

    real = keys.hash_key("some-master-key")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", f"  {real.upper()}\n")
    monkeypatch.setenv("MEMORY_DATABASE_URL", REQUIRED["MEMORY_DATABASE_URL"])
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", REQUIRED["MEMORY_HINDSIGHT_URL"])

    assert keys.verify_key("some-master-key", Settings().master_key_hash)


def test_a_zero_or_negative_write_window_is_refused(monkeypatch):
    """A window of 0 makes `cutoff = now - window` evict every hit immediately,
    so the limiter never fires again -- SPEC §20's MUST silently bypassed with
    no error and no log. Quieter than the write_limit=0 crash beside it, which
    at least announced itself as a 500."""
    import pytest
    from pydantic import ValidationError

    from memory.config import Settings

    for value in ("0", "-5"):
        monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings()


def test_the_readme_documents_every_setting():
    """The README's Configuration table is the only place an operator
    deploying outside Helm/Compose learns which variables exist and which are
    required. Nothing kept it in step with `Settings`, and five of these were
    undocumented before it was written.

    Same guard as tests/test_slugs.py's SPEC §8.2 check, for the same reason:
    a prose rule with no executable counterpart drifts, and this project has
    already shipped one that did.
    """
    import re
    from pathlib import Path

    from memory.config import Settings

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    section = re.search(
        r"## Configuration\n(.*?)\nThe three required variables", readme, re.DOTALL
    )
    assert section, "README's Configuration section moved -- update this guard"

    documented = set(re.findall(r"\| `(MEMORY_[A-Z_]+)`", section.group(1)))
    actual = {f"MEMORY_{name.upper()}" for name in Settings.model_fields}
    assert documented == actual, (
        f"undocumented: {sorted(actual - documented)}; "
        f"documented but not real: {sorted(documented - actual)}"
    )
