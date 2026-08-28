def test_the_console_is_served(client):
    response = client.get("/admin/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ach-memory" in response.text


def test_the_console_loads_no_third_party_code(client):
    """The page holds the master key. A CDN script tag would put that key one
    supply-chain compromise away from an attacker."""
    body = client.get("/admin/ui").text

    assert "http://" not in body
    assert "https://" not in body


def test_peek_reads_the_field_names_hindsight_actually_returns(client):
    """Reading a field that does not exist fails silently, and did.

    Peek asked each item for `type`, `created_at`, `metadata.agent` and
    `metadata.git_locator`. Hindsight returns `fact_type` and `date`, and
    returns no agent and no git_locator at all -- so every row rendered
    "memory" with two em-dashes while the payload underneath held the type,
    the timestamp, the state and the proof count. No error, no empty page,
    just a panel that quietly said nothing.
    """
    body = client.get("/admin/ui").text

    assert "item.fact_type" in body
    assert "item.date" in body
    assert "item.state" in body
    assert "item.memory_type" not in body
    assert "item.created_at" not in body
    assert "metadata?.agent" not in body
    assert "metadata?.git_locator" not in body


def test_the_console_can_be_turned_off(configured_env, monkeypatch):
    from fastapi.testclient import TestClient

    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_ADMIN_UI_ENABLED", "false")
    get_settings.cache_clear()

    assert TestClient(create_app()).get("/admin/ui").status_code == 404
