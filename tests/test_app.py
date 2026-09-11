import pytest

from memory.api.app import create_app
from tests.conftest import IDENTITY_HEADER, OPERATOR_SUBJECT

# SPEC §11.6/§11.7's exclusions are enforced today solely by nobody having
# written the routes: get_bank, update_bank, get_bank_stats, list_banks,
# create_bank, dry-run-refresh, list_tags, retry_operation, delete_operation,
# and the whole Plugin/PluginMarketplace surface all have NOTHING here to stop
# them from being added by accident in a future change. Pinning the full,
# exact route set means any addition -- intentional or not -- has to touch
# this list, instead of silently shipping.
# clear_memories/delete_bank ARE below now, but only as the admin/master-key
# routes SPEC §11.7 blesses -- they are never advertised over MCP (see
# tests/test_mcp_tools.py's own frozen tool-surface list for that half).
# directives/mental-models ARE below too (SPEC §14): REST-only routes, real
# HTTP verbs on a path-param id rather than the all-POST data-plane shape,
# and -- like clear_memories/delete_bank -- absent from
# tests/test_mcp_tools.py's EXPECTED_TOOLS on purpose.
#
# /v1/users and /v1/groups are absent, and their absence is load-bearing: this
# service mints no identities any more. A user exists because an external
# provider asserted them, so there is no route to create one and no key to
# hand back -- re-adding either would restore a second, local source of
# identity beside the IdP.
EXPECTED_ROUTES = {
    ("POST", "/v1/projects"),
    ("GET", "/v1/projects"),
    ("GET", "/v1/projects/{project_slug}"),
    ("PATCH", "/v1/projects/{project_slug}"),
    ("PATCH", "/v1/projects/{project_slug}/owner"),
    ("POST", "/v1/memory/retain"),
    ("POST", "/v1/memory/sync_retain"),
    ("POST", "/v1/memory/recall"),
    ("POST", "/v1/read/recall"),
    ("POST", "/v1/read/history"),
    ("POST", "/v1/memory/reflect"),
    ("POST", "/v1/memory/list"),
    ("POST", "/v1/memory/get"),
    ("POST", "/v1/memory/forget"),
    ("POST", "/v1/memory/restore"),
    ("POST", "/v1/memory/correct"),
    ("POST", "/v1/memory/documents/list"),
    ("POST", "/v1/memory/documents/get"),
    ("POST", "/v1/memory/documents/delete"),
    ("POST", "/v1/memory/operations/list"),
    ("POST", "/v1/memory/operations/get"),
    ("POST", "/v1/memory/operations/cancel"),
    ("GET", "/v1/admin/audit"),
    # The activity trail (SPEC observability). /metrics and /admin/ui are
    # deliberately absent: both are include_in_schema=False, so they never
    # reach app.openapi() and this pin cannot see them.
    ("GET", "/v1/admin/activity"),
    ("GET", "/v1/admin/activity/summary"),
    ("POST", "/v1/admin/memory/{scope}/clear"),
    ("DELETE", "/v1/admin/memory/{scope}"),
    ("POST", "/v1/admin/slugs/{retired_slug}/release"),
    ("POST", "/v1/directives"),
    ("GET", "/v1/directives"),
    ("GET", "/v1/directives/{directive_id}"),
    ("PATCH", "/v1/directives/{directive_id}"),
    ("DELETE", "/v1/directives/{directive_id}"),
    ("POST", "/v1/mental-models"),
    ("GET", "/v1/mental-models"),
    ("GET", "/v1/mental-models/{model_key}"),
    ("PATCH", "/v1/mental-models/{model_key}"),
    ("DELETE", "/v1/mental-models/{model_key}"),
    ("POST", "/v1/mental-models/{model_key}/refresh"),
    ("POST", "/v1/working-state/sessions"),
    ("PUT", "/v1/working-state"),
    ("DELETE", "/v1/working-state"),
}


def test_the_route_set_is_exactly_the_documented_surface(configured_env):
    """Cheap and permanent: a test comparing app.routes to a frozen list.

    Uses the resolved OpenAPI schema (app.openapi()["paths"]) rather than
    walking app.routes directly -- FastAPI's router inclusion is lazy
    (_IncludedRouter), so app.routes itself does not expose a flat,
    already-merged (method, path) list the way the schema does.
    """
    spec = create_app().openapi()
    actual = {
        (method.upper(), path)
        for path, methods in spec["paths"].items()
        for method in methods
    }

    assert actual == EXPECTED_ROUTES


def _enable_platform(monkeypatch):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-litellm-api-key")
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_RESOLVER_URL", "https://api.example.com/v2/user/info"
    )
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "teams")
    get_settings.cache_clear()


def _capture_platform_calls(monkeypatch):
    """Stop at the provider boundary: this task is about plumbing, not
    resolution, so the provider records what it was handed and refuses."""
    from memory.auth.providers import platform
    from memory.errors import Unauthorized

    seen = {}

    def _fake(token, db):
        seen["token"] = token
        raise Unauthorized("stop here")

    monkeypatch.setattr(platform, "authenticate", _fake)
    return seen


def test_the_platform_header_reaches_the_resolver(client, monkeypatch):
    """The header name comes from configuration, so it cannot be a named
    parameter -- it has to be read off the Request."""
    _enable_platform(monkeypatch)
    seen = _capture_platform_calls(monkeypatch)

    client.get("/v1/projects", headers={"x-litellm-api-key": "sk-abc"})

    assert seen["token"] == "sk-abc"


def test_a_bearer_prefixed_platform_token_is_stripped(client, monkeypatch):
    """LiteLLM's own header requires the "Bearer " prefix; the resolver must
    receive the bare key."""
    _enable_platform(monkeypatch)
    seen = _capture_platform_calls(monkeypatch)

    client.get("/v1/projects", headers={"x-litellm-api-key": "Bearer sk-abc"})

    assert seen["token"] == "sk-abc"


def test_a_list_of_incoming_headers_tries_each_in_order(client, monkeypatch):
    """One deployment serves a gateway (x-litellm-api-key) and a local stdio
    client (Authorization) at once. First header present wins."""
    _enable_platform(monkeypatch)
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key,authorization"
    )
    from memory.config import get_settings

    get_settings.cache_clear()
    seen = _capture_platform_calls(monkeypatch)

    client.get("/v1/projects", headers={"Authorization": "Bearer sk-fallback"})

    assert seen["token"] == "sk-fallback"


def test_the_first_listed_header_wins_when_both_are_present(client, monkeypatch):
    """Order is priority: the gateway's own header is listed first so a stray
    Authorization cannot override it."""
    _enable_platform(monkeypatch)
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key,authorization"
    )
    from memory.config import get_settings

    get_settings.cache_clear()
    seen = _capture_platform_calls(monkeypatch)

    client.get(
        "/v1/projects",
        headers={"x-litellm-api-key": "sk-gateway", "Authorization": "Bearer sk-local"},
    )

    assert seen["token"] == "sk-gateway"


def test_a_non_jwt_bearer_on_authorization_reaches_the_platform_resolver(
    client, monkeypatch
):
    """The motivating case: a LiteLLM key sent on Authorization used to 401,
    because the platform provider never read Authorization. With it listed,
    an opaque bearer there resolves -- while a JWT still routes to provider 1
    by shape (it never reaches the platform resolver)."""
    _enable_platform(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "authorization")
    from memory.config import get_settings

    get_settings.cache_clear()
    seen = _capture_platform_calls(monkeypatch)

    client.get("/v1/projects", headers={"Authorization": "Bearer sk-opaque"})

    assert seen["token"] == "sk-opaque"


def test_the_platform_header_is_ignored_when_the_provider_is_off(client, monkeypatch):
    """A stray header on a deployment that never enabled the provider is not a
    credential -- it must not reach the resolver at all.

    The suite now enables the platform provider for every test (it is how the
    whole suite authenticates), so the header this sends is the one that WOULD
    be a credential -- turning the provider off is what has to make it inert.
    `_platform_token` reads the settings per request, so clearing the cache
    here is enough; the app need not be rebuilt."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "false")
    get_settings.cache_clear()
    seen = _capture_platform_calls(monkeypatch)

    response = client.get("/v1/projects", headers={IDENTITY_HEADER: OPERATOR_SUBJECT})

    assert seen == {}
    assert response.status_code == 401


def test_create_app_refuses_a_master_config_that_could_over_grant(
    configured_env, monkeypatch
):
    """Over-granting is the one misconfiguration that announces itself
    nowhere: an empty entry in the granted set matches a principal with no
    identity, and every request after that looks perfectly ordinary.

    `config._id_set` discards empty entries, so the assertion in `create_app`
    is unreachable through configuration alone -- which is exactly why it
    needs a test that reaches it. Loosening that parsing must stop the
    container from booting, not quietly hand every bank to everyone.
    """
    from memory import config

    monkeypatch.setattr(config, "_id_set", lambda raw: frozenset(raw.split(",")))
    monkeypatch.setenv("MEMORY_MASTER_USERS", "")
    config.get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="Refusing to start"):
        create_app()


def test_create_app_refuses_ambiguous_operator_config_across_two_providers(monkeypatch):
    """Two providers enabled, operator authority granted, and nothing saying
    which provider may grant it: either one's assertion would match, so the
    grant is ambiguous in the direction that hands out the admin plane.
    Refusing to start is the only signal loud enough."""
    from memory.api.app import create_app
    from memory.config import get_settings
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", "mcp:ach-memory")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "authorization")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "authorization")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_URL", "http://litellm/whoami")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "team_id")
    monkeypatch.setenv("MEMORY_MASTER_USERS", "jc@example.com")
    monkeypatch.delenv("MEMORY_MASTER_ISSUER", raising=False)
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="MEMORY_MASTER_ISSUER"):
        create_app()

    # Naming the issuer resolves it.
    monkeypatch.setenv("MEMORY_MASTER_ISSUER", "https://idp.example.com")
    get_settings.cache_clear()
    create_app()
    get_settings.cache_clear()
