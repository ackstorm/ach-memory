"""GET /metrics, and the label discipline that keeps it scrapeable.

Cardinality is the whole risk here: a label whose values come from caller
input turns one time series into unbounded thousands, and the failure lands
on the Prometheus, not on us.
"""

import httpx
import pytest


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
        "memory_capture_stage_total",
        "memory_capture_stage_duration_seconds",
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


@pytest.mark.anyio
async def test_an_mcp_request_gets_its_own_route_label_not_unmatched(app):
    """The /mcp mount is a plain starlette.routing.Mount, which never sets
    scope["route"] (only FastAPI's APIRoute does) -- without the mount-prefix
    fallback in _route_label, every real MCP call would collapse into
    "unmatched" next to genuine 404 probing, and MCP is the surface where all
    fifteen tools are the same POST /mcp."""
    before = _sample("memory_http_requests_total", route="/mcp", method="POST", status="200")

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "probe",
                    "version": "0",
                },
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "127.0.0.1",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "server/discover",
    }
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post("/mcp/", json=body, headers=headers)

    assert response.status_code == 200
    after = _sample("memory_http_requests_total", route="/mcp", method="POST", status="200")
    assert after == before + 1


# ---------------------------------------------------------------------------
# Task 8: capture worker stage/outcome/duration -- content-free by
# construction (label values are a closed set the source code chooses, never
# a caller-supplied session/project id, hash or bank id).
# ---------------------------------------------------------------------------


def test_capture_stage_labels_are_a_closed_set_with_no_identity_in_them():
    """The metric declaration itself, not a live increment: proves the
    label SET is exactly stage/outcome -- never a session id, project slug,
    content hash or bank id, regardless of what any future caller does."""
    from memory import metrics

    assert metrics.CAPTURE_STAGE._labelnames == ("stage", "outcome")
    assert metrics.CAPTURE_STAGE_DURATION._labelnames == ("stage",)


def test_a_capture_extraction_stage_increments_the_stage_counter(session, tenant):
    import respx
    from httpx import Response

    from memory import ids
    from memory.auth.principal import Principal
    from memory.capture import repository, worker
    from memory.hindsight.client import HindsightClient
    from memory.models import Project, User

    user = User(id="usr_cap_m", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        project_slug="acme-metrics",
        owner_type="user",
        owner_id="usr_cap_m",
        bank_id=ids.new_project_bank_id(),
    )
    session.add_all([user, project])
    session.flush()
    principal = Principal(
        tenant_id=tenant, user_id="usr_cap_m", is_master=False, key_id="k", credential_id="k"
    )
    result = repository.accept_checkpoint(
        session,
        principal,
        host="claude-code",
        session_id="sess-metrics",
        project_slug="acme-metrics",
        git_locator=None,
        workspace_id="ws_" + "a" * 32,
        start_offset=0,
        end_offset=100,
        content_hash="a" * 64,
        sanitized_hash="b" * 64,
        content="user: hello",
    )
    session.commit()

    before = _sample("memory_capture_stage_total", stage="extract", outcome="advanced")

    # Transitions are owner-fenced, so the worker only ever processes a row
    # it holds the lease on.
    owner = repository.new_lease_owner("test")
    repository.acquire_lease(session, owner=owner, lease_seconds=60)
    session.commit()

    client = HindsightClient(base_url="http://hindsight.test", api_key="k", tenant_id="default")
    with respx.mock:
        respx.post(
            f"http://hindsight.test/v1/default/banks/{project.bank_id}/memories/dry-run-extract"
        ).mock(return_value=Response(200, json={"facts": []}))
        worker.process_row(
            session,
            client,
            result.row,
            owner=owner,
            lease_seconds=60,
            max_attempts=8,
            correction_refresh_enabled=False,
            profile_delivery_mode="legacy",
        )
    session.commit()

    after = _sample("memory_capture_stage_total", stage="extract", outcome="advanced")
    assert after == before + 1
