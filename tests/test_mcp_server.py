import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from memory.mcp import server as mcp_server

MASTER_PLAINTEXT = "mem_master_secret_for_tests"


def test_the_static_policy_leaves_room_for_memory():
    """A fixed 1428-char policy displaced the dynamic project orientation."""
    assert len(mcp_server.INSTRUCTIONS) <= 600


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """`resolve_principal` and `session_scope` both call `get_settings()`.

    Tests that go through the `client`/`app` fixture already get this via
    conftest's monkeypatch. The two auth-failure tests below call
    `tool_session` directly with only `tenant` in scope, so without this they
    hit a `pydantic` ValidationError (missing required settings) before ever
    reaching the `Unauthorized` they're asserting on. Mirrors the pattern in
    test_principal.py's `_settings` fixture.
    """
    from memory.auth import keys
    from memory.config import get_settings
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_build_mcp_returns_a_server_with_no_tools_of_its_own():
    """Scaffolding only. The tools land via register() (Task 2 and after),
    not inside build_mcp() itself -- asserted directly, not just via
    `isinstance`. The bare isinstance check used to stay green even with a
    tool registered straight inside build_mcp(); only the separate
    suite-level pinning test in test_mcp_tools.py caught that, which made
    THIS test's name a promise it didn't keep."""
    mcp = mcp_server.build_mcp()

    assert isinstance(mcp, MCPServer)
    assert await mcp.list_tools() == []


def test_a_missing_authorization_header_is_unauthorized(tenant):
    from memory.errors import Unauthorized

    with pytest.raises(Unauthorized), mcp_server.tool_session(_headers({})):
        pass


def test_a_master_key_is_refused_over_mcp(tenant):
    """Invariant 22: the master key never resides in an ordinary agent
    runtime, and MCP is exactly that. Measured live before this fix: a
    master key over MCP reached ANY project in the tenant and returned
    another user's private project memory, with `on_behalf_of` hardcoded to
    None the whole time (SPEC §20.3 unsatisfiable over MCP). See
    test_mcp_tools.py::test_a_master_key_is_refused_by_a_real_tool_call for
    the same refusal proven through `_run`'s MCPToolError wrapping."""
    from memory.errors import Forbidden

    with pytest.raises(Forbidden), mcp_server.tool_session(
        _headers({"authorization": f"Bearer {MASTER_PLAINTEXT}"})
    ):
        pass


def test_a_bad_key_is_unauthorized(tenant):
    from memory.errors import Unauthorized

    with pytest.raises(Unauthorized), mcp_server.tool_session(
        _headers({"authorization": "Bearer nope"})
    ):
        pass


def test_the_api_key_header_authenticates_over_mcp(client, master_headers, tenant):
    """The MCP surface reads `x-ach-memory-key` too, not just Authorization.

    This is the header an agent can set without fighting whatever LiteLLM or a
    gateway has already put in Authorization.
    """
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    with mcp_server.tool_session(_headers({"x-ach-memory-key": key})) as tc:
        assert tc.principal.user_id == user_id
        assert tc.principal.is_master is False


def test_the_api_key_header_beats_authorization_over_mcp(
    client, master_headers, tenant
):
    """A master key parked in Authorization must not win. Over MCP the master
    key is refused outright (Invariant 22), so if precedence regressed this
    would raise Forbidden instead of resolving the user."""
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    with mcp_server.tool_session(
        _headers(
            {
                "authorization": f"Bearer {MASTER_PLAINTEXT}",
                "x-ach-memory-key": key,
            }
        )
    ) as tc:
        assert tc.principal.user_id == user_id


def test_a_valid_user_key_yields_its_own_principal(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    with mcp_server.tool_session(_headers({"authorization": f"Bearer {key}"})) as tc:
        assert tc.principal.user_id == user_id
        assert tc.principal.is_master is False


def test_the_session_is_closed_when_the_tool_returns(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    with mcp_server.tool_session(_headers({"authorization": f"Bearer {key}"})) as tc:
        session = tc.db
        assert session.get_transaction() is not None

    # `is_active or get_bind() is not None` would be true whether or not close()
    # ran. A closed Session has released its transaction, and that is the thing
    # a leak would keep hold of.
    assert session.get_transaction() is None


def test_the_session_is_closed_even_when_the_tool_raises(
    client, master_headers, tenant
):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    session = None
    with pytest.raises(RuntimeError), mcp_server.tool_session(
        _headers({"authorization": f"Bearer {key}"})
    ) as tc:
        session = tc.db
        raise RuntimeError("the tool blew up")

    assert session is not None
    assert session.get_transaction() is None


def _headers(mapping: dict[str, str]):
    """A stand-in for mcp.Context, which carries only what the pipeline reads."""

    class _Ctx:
        headers = mapping

    return _Ctx()


@pytest.mark.anyio
async def test_the_mcp_endpoint_answers_the_host_it_is_configured_for(
    monkeypatch, configured_env
):
    """The SDK enables DNS-rebinding protection and allows only 127.0.0.1 by
    default, so a deployed service behind an ingress answers 421 to every MCP
    call. Configured, not disabled -- the check is worth keeping, it just has
    to know the hostname it runs under."""
    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MCP_ALLOWED_HOSTS", "127.0.0.1,memory.example.com")
    get_settings.cache_clear()

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
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "server/discover",
    }

    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://memory.example.com"
    ) as client:
        allowed = await client.post(
            "/mcp/", json=body, headers={**headers, "Host": "memory.example.com"}
        )
        origin_refused = await client.post(
            "/mcp/",
            json=body,
            headers={
                **headers,
                "Host": "memory.example.com",
                "Origin": "https://memory.example.com",
            },
        )
        refused = await client.post(
            "/mcp/", json=body, headers={**headers, "Host": "evil.example.com"}
        )

    assert allowed.status_code == 200
    assert origin_refused.status_code == 403
    assert refused.status_code == 421


# The three revisions LiteLLM v1.99.1 can request: its `MCPSpecVersion` has
# exactly these members and no newer one, so if the endpoint does not serve
# them it cannot be reached from the gateway at all -- which is what happened,
# and what "Failed to fetch MCP tools" meant on the caller's side.
LITELLM_SPEC_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


@pytest.mark.anyio
@pytest.mark.parametrize("requested", LITELLM_SPEC_VERSIONS)
async def test_the_mcp_endpoint_serves_the_initialize_era(
    requested, monkeypatch, configured_env
):
    """A handshake client must get through `initialize` without a session.

    Both halves matter. Serving the era is what unblocks every mcp 1.x
    gateway; answering it with no `mcp-session-id` is what keeps the endpoint
    horizontally scalable, which was the reason the newest-only gate looked
    safe to add in the first place.
    """
    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MCP_ALLOWED_HOSTS", "memory.example.com")
    get_settings.cache_clear()
    app = create_app()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "memory.example.com",
    }
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://memory.example.com"
    ) as client:
        # No MCP-Protocol-Version header: a client cannot name a revision it
        # has not negotiated yet, and requiring one here is what broke it.
        handshake = await client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": requested,
                    "capabilities": {},
                    "clientInfo": {"name": "litellm", "version": "1.99.1"},
                },
            },
        )
        negotiated = handshake.json()["result"]["protocolVersion"]
        listing = await client.post(
            "/mcp/",
            headers={**headers, "MCP-Protocol-Version": negotiated},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert handshake.status_code == 200
    assert negotiated == requested
    assert "mcp-session-id" not in {k.lower() for k in handshake.headers}
    assert listing.status_code == 200
    assert listing.json()["result"]["tools"], "the handshake era listed no tools"


@pytest.mark.anyio
async def test_the_mcp_endpoint_refuses_a_revision_the_sdk_cannot_serve(
    monkeypatch, configured_env
):
    """An unknown revision fails loudly rather than being negotiated down.

    The alternative is silence: the SDK would answer with whatever it does
    support, and a client that asked for something else would never learn its
    request was not honoured.
    """
    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MCP_ALLOWED_HOSTS", "memory.example.com")
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://memory.example.com"
    ) as client:
        response = await client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Host": "memory.example.com",
                "MCP-Protocol-Version": "1999-01-01",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32020
    assert "2026-07-28" in error["data"]["supported"]
    assert set(LITELLM_SPEC_VERSIONS) <= set(error["data"]["supported"])


def test_mcp_transport_security_does_not_treat_hosts_as_origins(
    monkeypatch, configured_env
):
    """v1 keeps browser-origin MCP unsupported and native clients Origin-free."""
    from mcp.server.mcpserver import MCPServer

    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MCP_ALLOWED_HOSTS", "127.0.0.1,memory.example.com")
    get_settings.cache_clear()
    captured: dict[str, object] = {}
    original = MCPServer.streamable_http_app

    def capture_security(self, *args, **kwargs):
        captured["security"] = kwargs["transport_security"]
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MCPServer, "streamable_http_app", capture_security)
    create_app()

    security = captured["security"]
    assert security.allowed_hosts == ["127.0.0.1", "memory.example.com"]
    assert security.allowed_origins == []
