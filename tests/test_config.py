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
