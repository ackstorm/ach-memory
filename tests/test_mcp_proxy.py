"""The proxy's own logic: SPEC §8 resolution done client-side, and the
injection rule that must never override what the model passed.

The forwarding itself is FastMCP's create_proxy and is not re-tested here;
scripts/mcp-smoke.py --proxy exercises it end to end against a live stack.
"""

import subprocess
from typing import ClassVar

import pytest

from memory.mcp.proxy import (
    ProjectContextMiddleware,
    fill_project_arguments,
    resolve_project_context,
)


def _git_repo(tmp_path, origin: str | None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if origin:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", origin],
            check=True,
        )
    return tmp_path


def test_memory_project_env_wins_over_git(tmp_path, monkeypatch):
    # SPEC §8: MEMORY_PROJECT is checked before Git, even inside a repo.
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    assert resolve_project_context(str(repo)) == ("payments-api", None)


def test_git_origin_becomes_a_derived_slug_and_the_locator(tmp_path, monkeypatch):
    """SPEC §8.2 derivation, pinned to its literal output.

    The digest is the point: normalize_slug collapses `/`, `.` and `-` alike,
    so acme/payments-api and acme-payments/api both flatten to
    github.com-acme-payments-api and would share one bank without it.

    Sending the locator alone instead of a slug does not work -- measured
    against production 2026-08-27, PROJECT_CONTEXT_UNAVAILABLE -- because a
    locator never resolves identity (inv. 11) and is not unique (§17).
    """
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (
        "github.com-acme-payments-api-dab6719d",
        "git@github.com:acme/payments-api.git",
    )


def test_remote_that_names_no_repository_resolves_nothing(tmp_path, monkeypatch):
    """A local-path remote identifies no project, and must not crash startup.

    canonical_locator raises ProjectInvalidSlug for a remote with no host and
    path. Letting that escape would kill the stdio server at launch -- before
    the host ever calls a tool -- over a repository the agent may never ask
    about.
    """
    repo = _git_repo(tmp_path, str(tmp_path / "bare-clone"))
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (None, None)


def test_repo_without_origin_resolves_nothing(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, None)
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (None, None)


def test_no_repo_resolves_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(tmp_path)) == (None, None)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # A locator with no slug is what the server rejects, so this shape is
        # never produced by resolve_project_context -- kept to pin that the
        # injector reports what it was given rather than inventing a slug.
        ({"scope": "project"}, {"scope": "project", "git_locator": "L"}),
        # Explicit values from the model are never overridden.
        (
            {"scope": "project", "project_slug": "theirs"},
            {"scope": "project", "project_slug": "theirs"},
        ),
        (
            {"scope": "project", "git_locator": "theirs"},
            {"scope": "project", "git_locator": "theirs"},
        ),
        # scope=user is untouched -- injecting here would attach a project
        # to a user-bank call the server would then reject or misroute.
        ({"scope": "user"}, {"scope": "user"}),
        # Tools with no scope argument (get_operation etc.) are untouched:
        # injecting an argument their schema lacks fails validation upstream.
        ({"operation_id": "op_1"}, {"operation_id": "op_1"}),
    ],
)
def test_fill_project_arguments_locator(arguments, expected):
    fill_project_arguments(arguments, None, "L")
    assert arguments == expected


def test_fill_sends_the_slug_and_the_locator_together():
    """Both, because they do different jobs: the slug resolves the project and
    the locator binds it to the repository on first touch (§8.3) and refuses a
    mismatch later (§8.4)."""
    arguments = {"scope": "project"}
    fill_project_arguments(arguments, "payments-api", "L")
    assert arguments == {
        "scope": "project",
        "project_slug": "payments-api",
        "git_locator": "L",
    }


@pytest.mark.anyio
async def test_middleware_injects_into_call_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    middleware = ProjectContextMiddleware()

    seen = {}

    async def call_next(context):
        seen.update(context.message.arguments)
        return "result"

    class Message:
        name = "list_memories"
        arguments: ClassVar = {"scope": "project"}

    class Context:
        message = Message()

    assert await middleware.on_call_tool(Context(), call_next) == "result"
    assert seen == {"scope": "project", "project_slug": "payments-api"}
