"""The MCP surface's shared pipeline.

Every tool goes through `tool_session`. A REST route gets its Principal and its
Session from FastAPI dependencies; a tool gets neither, only raw headers — so
the pipeline is reassembled once, here. SPEC §11.1 requires authentication,
scope resolution, authorization and bank resolution to be centralized, and a
tool that parsed its own header or opened its own session would end that.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, resolve_principal
from memory.db import session_scope
from memory.errors import Forbidden


class HasHeaders(Protocol):
    """What the pipeline needs from an mcp Context: nothing but headers."""

    headers: Mapping[str, str] | None


@dataclass
class ToolContext:
    principal: Principal
    db: Session


@contextmanager
def tool_session(ctx: HasHeaders) -> Iterator[ToolContext]:
    """Authenticate the caller and open a session for one tool call.

    The header is client-supplied input and is treated as a credential to be
    verified, never as an identity assertion — `resolve_principal` is the same
    function the REST surface uses, so an MCP caller cannot become anyone a
    REST caller could not.
    """
    headers = ctx.headers or {}
    authorization = headers.get("authorization") or headers.get("Authorization")

    with session_scope() as db:
        principal = resolve_principal(authorization, db)
        if principal.is_master:
            # Invariant 22: the master key never resides in an ordinary agent
            # runtime, and an MCP client IS exactly that -- an LLM-driven
            # coding agent, not an ACH-controlled service. Measured live: a
            # master key over MCP reached ANY project in the tenant and
            # returned another user's private project memory, because
            # `_resolve_bank` bypasses ownership for `principal.is_master` by
            # design (§7) and MCP has no header equivalent of REST's
            # On-Behalf-Of -- `_run` hardcodes `on_behalf_of=None`, so a
            # master call here would also audit an anonymous delegation
            # (SPEC §20.3 unsatisfiable by construction). Refusing it here is
            # the one-line fix for both: no MCP delegation path exists to add
            # `on_behalf_of` to, so there is nothing left to support.
            raise Forbidden("the master key is not accepted over MCP")
        yield ToolContext(principal=principal, db=db)


def build_mcp() -> MCPServer:
    """The server, with no tools registered yet.

    Tools are added by `memory.mcp.tools.register(mcp)`, which the app calls.
    Keeping registration out of this module is what makes the exclusion test in
    Task 6 meaningful: the advertised set is one list in one place.
    """
    return MCPServer(
        name="ach-memory",
        instructions=(
            "Durable memory for coding agents. `scope` selects whose memory: "
            "'user' is your own, 'project' is the shared memory of the project "
            "named by project_slug. You never supply a bank id."
        ),
    )
