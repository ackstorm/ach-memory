from memory.api.app import create_app

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
EXPECTED_ROUTES = {
    ("POST", "/v1/users"),
    ("GET", "/v1/users"),
    ("GET", "/v1/users/{user_id}"),
    ("POST", "/v1/users/{user_id}/keys"),
    ("GET", "/v1/users/{user_id}/keys"),
    ("DELETE", "/v1/users/{user_id}/keys/{key_id}"),
    ("POST", "/v1/groups"),
    ("GET", "/v1/groups"),
    ("GET", "/v1/groups/{group_id}"),
    ("PUT", "/v1/groups/{group_id}/members/{user_id}"),
    ("DELETE", "/v1/groups/{group_id}/members/{user_id}"),
    ("POST", "/v1/projects"),
    ("GET", "/v1/projects"),
    ("GET", "/v1/projects/{project_slug}"),
    ("PATCH", "/v1/projects/{project_slug}"),
    ("PATCH", "/v1/projects/{project_slug}/owner"),
    ("POST", "/v1/memory/retain"),
    ("POST", "/v1/memory/sync_retain"),
    ("POST", "/v1/memory/recall"),
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
    ("GET", "/v1/mental-models/{mental_model_id}"),
    ("PATCH", "/v1/mental-models/{mental_model_id}"),
    ("DELETE", "/v1/mental-models/{mental_model_id}"),
    ("POST", "/v1/mental-models/{mental_model_id}/refresh"),
    ("POST", "/v1/mental-models/{mental_model_id}/clear"),
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


def test_the_platform_header_is_ignored_when_the_provider_is_off(client, monkeypatch):
    """A stray header on a deployment that never enabled the provider is not a
    credential -- it must not reach the resolver at all."""
    seen = _capture_platform_calls(monkeypatch)

    response = client.get("/v1/projects", headers={"x-litellm-api-key": "sk-abc"})

    assert seen == {}
    assert response.status_code == 401
