import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from memory.mcp import server as mcp_server
from tests.conftest import OPERATOR_SUBJECT


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
    from memory.config import get_settings
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
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


def test_an_operator_reaches_mcp_as_an_ordinary_user_with_no_authority(
    client, master_headers, tenant
):
    """Invariant 22, now enforced by withholding the authority rather than by
    refusing the caller.

    It used to raise Forbidden, which was right while authority WAS the
    credential: take the authority off a master key and nothing was left to
    be. Authority is configuration over an ordinary external identity now, so
    refusing here would lock a configured operator out of their own memory on
    the one surface an agent runtime actually uses. What must still hold is
    that the authority does not travel: `_resolve_bank` bypasses ownership for
    `is_master` (§7) and `_run` hardcodes `on_behalf_of=None`, so an operator
    who kept it here would read another user's project and audit the
    delegation to nobody.

    See test_mcp_tools.py::test_an_operator_cannot_reach_another_users_project_over_mcp
    for the same rule proven through a real tool call.
    """
    from memory.auth.principal import is_operator
    from memory.config import get_settings

    with mcp_server.tool_session(_headers(master_headers)) as tc:
        # The same identity, unchanged: still themselves, still their own bank.
        assert tc.principal.subject == OPERATOR_SUBJECT
        assert tc.principal.user_id is not None
        # Configuration still names them an operator -- what MCP withholds is
        # the exercise of it, not the grant. Asserting both halves is the
        # point: `is_master is False` alone would stay green if the operator
        # config had simply stopped matching.
        assert is_operator(tc.principal, get_settings()) is True
        assert tc.principal.authority_allowed is False
        assert tc.principal.is_master is False


def test_a_token_no_provider_accepts_is_unauthorized(tenant):
    """No local keys left, so there is nothing here to be a *wrong* key: a
    token is refused because no configured issuer minted it."""
    from memory.errors import Unauthorized

    with pytest.raises(Unauthorized), mcp_server.tool_session(
        _headers({"authorization": "Bearer nope"})
    ):
        pass


def test_an_external_identity_yields_its_own_principal(new_user, tenant):
    user = new_user()

    with mcp_server.tool_session(_headers(user["headers"])) as tc:
        assert tc.principal.user_id == user["user_id"]
        assert tc.principal.subject == user["subject"]
        assert tc.principal.is_master is False


def test_the_session_is_closed_when_the_tool_returns(new_user, tenant):
    with mcp_server.tool_session(_headers(new_user()["headers"])) as tc:
        session = tc.db
        assert session.get_transaction() is not None

    # `is_active or get_bind() is not None` would be true whether or not close()
    # ran. A closed Session has released its transaction, and that is the thing
    # a leak would keep hold of.
    assert session.get_transaction() is None


def test_the_session_is_closed_even_when_the_tool_raises(new_user, tenant):
    session = None
    with pytest.raises(RuntimeError), mcp_server.tool_session(
        _headers(new_user()["headers"])
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
