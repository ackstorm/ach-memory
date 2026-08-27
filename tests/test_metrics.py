"""GET /metrics, and the label discipline that keeps it scrapeable.

Cardinality is the whole risk here: a label whose values come from caller
input turns one time series into unbounded thousands, and the failure lands
on the Prometheus, not on us.
"""


def test_metrics_exposes_build_info(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "memory_build_info" in response.text


def test_metrics_declares_every_collector(client):
    body = client.get("/metrics").text

    for name in (
        "memory_calls_total",
        "memory_call_duration_seconds",
        "memory_content_bytes_total",
        "memory_errors_total",
        "memory_hindsight_request_seconds",
        "memory_http_requests_total",
    ):
        assert name in body, name


def test_metrics_can_be_turned_off(configured_env, monkeypatch):
    """The route is not registered at all, rather than answering 403.

    A deployment that turns metrics off should not advertise that it has
    them.
    """
    from fastapi.testclient import TestClient

    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_METRICS_ENABLED", "false")
    get_settings.cache_clear()

    assert TestClient(create_app()).get("/metrics").status_code == 404


from prometheus_client import REGISTRY


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_http_requests_are_counted_by_route_template(client):
    before = _sample("memory_http_requests_total", route="/metrics", method="GET", status="200")

    client.get("/metrics")

    after = _sample("memory_http_requests_total", route="/metrics", method="GET", status="200")
    assert after == before + 1


def test_an_unmatched_path_cannot_mint_a_label(client):
    """A 404 has no route object, so it must collapse to one fixed label --
    otherwise every invented URL is a new time series."""
    client.get("/nope-a")
    client.get("/nope-b")

    assert _sample("memory_http_requests_total", route="unmatched", method="GET", status="404") >= 2


def test_a_domain_error_increments_its_code(client):
    before = _sample("memory_errors_total", code="UNAUTHORIZED")

    client.post("/v1/memory/recall", json={"scope": "user", "query": "x"})

    assert _sample("memory_errors_total", code="UNAUTHORIZED") == before + 1


def test_an_mcp_request_gets_its_own_route_label_not_unmatched(client):
    """The /mcp mount is a plain starlette.routing.Mount, which never sets
    scope["route"] (only FastAPI's APIRoute does) -- without the mount-prefix
    fallback in _route_label, every real MCP call would collapse into
    "unmatched" next to genuine 404 probing, and MCP is the surface where all
    fifteen tools are the same POST /mcp."""
    before = _sample("memory_http_requests_total", route="/mcp", method="POST", status="200")

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "127.0.0.1",
    }
    # The MCP session manager's task group only exists once the app's
    # lifespan has run -- entering the client as a context manager triggers
    # startup, same as test_the_mcp_endpoint_answers_the_host_it_is_configured_for.
    with client as c:
        response = c.post("/mcp/", json=body, headers=headers)

    assert response.status_code == 200
    after = _sample("memory_http_requests_total", route="/mcp", method="POST", status="200")
    assert after == before + 1
