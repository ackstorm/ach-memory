import pytest

from memory.config import Settings, sqlalchemy_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_sqlalchemy_url(given, expected):
    assert sqlalchemy_url(given) == expected


def test_settings_accept_a_plain_postgresql_url():
    settings = Settings(database_url="postgresql://u:p@h/d", hindsight_url="http://hs")
    assert settings.database_url == "postgresql+psycopg://u:p@h/d"
