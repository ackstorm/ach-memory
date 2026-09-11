"""The MCP surface's shared pipeline.

Every tool goes through `tool_session`. A REST route gets its Principal and its
Session from FastAPI dependencies; a tool gets neither, only raw headers — so
the pipeline is reassembled once, here. SPEC §11.1 requires authentication,
scope resolution, authorization and bank resolution to be centralized, and a
tool that parsed its own header or opened its own session would end that.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Protocol

from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, resolve_principal
from memory.config import get_settings
from memory.db import session_scope


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
    # Lower-cased once: HTTP header names are case-insensitive, and the
    # previous `headers.get("authorization") or headers.get("Authorization")`
    # pair covered only two of the spellings a client may send.
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    authorization = headers.get("authorization")

    settings = get_settings()
    platform_token = None
    if settings.auth_platform_enabled:
        # First configured header present wins (order is priority), mirroring
        # the REST `_platform_token`: the ACH gateway sends `x-litellm-api-key`,
        # a local stdio client sends `Authorization` -- one service accepts both.
        for header in settings.incoming_headers:
            raw = headers.get(header)
            if not raw:
                continue
            value = raw.strip()
            if value.lower().startswith("bearer "):
                value = value[len("bearer ") :].strip()
            if value:
                platform_token = value
                break

    with session_scope() as db:
        principal = resolve_principal(
            authorization, db, platform_token=platform_token
        )
        # Invariant 22, enforced by withholding the authority rather than by
        # refusing the caller. Measured live: a master key over MCP reached
        # ANY project in the tenant and returned another user's private
        # project memory, because `_resolve_bank` bypasses ownership for
        # `principal.is_master` by design (§7) and MCP has no header
        # equivalent of REST's On-Behalf-Of -- `_run` hardcodes
        # `on_behalf_of=None`, so a privileged call here would also audit an
        # anonymous delegation (SPEC §20.3 unsatisfiable by construction).
        #
        # It used to raise Forbidden, which was right while authority WAS the
        # credential: a master key had no identity, so there was nothing left
        # to be once you took its authority away. Now authority is
        # configuration over an ordinary external identity, and refusing here
        # would lock every configured operator out of their own memory over
        # MCP -- an agent runtime is exactly where they use it. So the
        # operator arrives as themselves, with their own banks and their own
        # projects, and simply carries no authority on this surface. Both
        # properties the refusal protected still hold: no ownership bypass,
        # and no unattributable delegation.
        yield ToolContext(
            principal=replace(principal, authority_allowed=False), db=db
        )


INSTRUCTIONS = (
    "Durable memory across sessions and context resets: the system of record "
    "for what was decided, preferred or learned. `scope` selects whose memory: "
    "'user' is your own, 'project' is the shared memory of the project named by "
    "project_slug. You never supply a bank id. Never store credentials, tokens "
    "or keys. Write in English whatever language the conversation uses: "
    "retrieval reranks in English only."
)


def build_mcp() -> MCPServer:
    """The server, with no tools registered yet.

    Tools are added by `memory.mcp.tools.register(mcp)`, which the app calls.
    Keeping registration out of this module is what makes the exclusion test in
    Task 6 meaningful: the advertised set is one list in one place.

    `instructions` carries the universal safety floor because it is the only
    delivery that reaches every caller. Host activation text may add richer
    workflow guidance, while direct HTTP and stdio clients both receive this
    static contract through discovery.

    It is not written for coding agents. Any MCP client gets it, so the text
    names the read moment and the write moment in general terms and leaves the
    per-tool detail to the tool descriptions.
    """
    return MCPServer(
        name="ach-memory",
        instructions=INSTRUCTIONS,
    )
