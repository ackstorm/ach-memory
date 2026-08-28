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

from memory.auth.principal import API_KEY_HEADER, Principal, resolve_principal
from memory.config import get_settings
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
    # Lower-cased once: HTTP header names are case-insensitive, and the
    # previous `headers.get("authorization") or headers.get("Authorization")`
    # pair covered only two of the spellings a client may send.
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    authorization = headers.get("authorization")
    # Same precedence as the REST surface: when present, this is the only
    # credential considered. It exists because everything that fronts this
    # service has its own claim on Authorization (SPEC §5.1).
    api_key = headers.get(API_KEY_HEADER)

    settings = get_settings()
    platform_token = None
    if settings.auth_platform_enabled and settings.auth_platform_incoming_header:
        raw = headers.get(settings.auth_platform_incoming_header.lower())
        if raw:
            value = raw.strip()
            if value.lower().startswith("bearer "):
                value = value[len("bearer ") :].strip()
            platform_token = value or None

    with session_scope() as db:
        principal = resolve_principal(
            authorization, db, api_key=api_key, platform_token=platform_token
        )
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


INSTRUCTIONS = (
    "Durable memory across sessions and context resets: the system of "
    "record for what was decided, preferred or learned, which would "
    "otherwise be lost when this conversation ends. An agent's own "
    "notes, a host memory directory, a MEMORY.md index or a scratch "
    "file are invisible here -- whatever is written there is lost to "
    "every future session.\n\n"
    "READ before acting. Recall whenever the work depends on something "
    "settled earlier: a preference, a convention, a constraint, a "
    "name, an approach already tried and rejected. Searching here "
    "beats reconstructing the past from files or transcripts, which "
    "record what was done and never why. `recall` returns matching "
    "facts; `reflect` answers a question from them.\n\n"
    "WRITE the moment a fact becomes durable -- when it will still "
    "hold after this task: a decision confirmed or rejected, a stated "
    "preference, a convention, a constraint, a gotcha and its cause, "
    "whatever a context summary establishes. Store each as a "
    "standalone statement that makes sense to a reader with none of "
    "this conversation: name the subject, state the fact, say why it "
    "holds. Skip what is routine, already stored or easily re-derived. "
    "Never store credentials, tokens or keys. Write in English "
    "whatever language the conversation uses: retrieval reranks in "
    "English only.\n\n"
    "`scope` selects whose memory: 'user' is your own, 'project' is "
    "the shared memory of the project named by project_slug. You never "
    "supply a bank id."
)


def build_mcp() -> MCPServer:
    """The server, with no tools registered yet.

    Tools are added by `memory.mcp.tools.register(mcp)`, which the app calls.
    Keeping registration out of this module is what makes the exclusion test in
    Task 6 meaningful: the advertised set is one list in one place.

    `instructions` carries the policy because it is the only delivery that
    reaches every caller. activation.txt reaches a host whose SessionStart hook
    runs, which codex's never does (measured, test_agent_bundle), and reaches
    nobody who wires the endpoint by hand. This string is returned by
    `initialize`, so it lands on every host, HTTP and stdio alike -- the proxy
    forwards it verbatim when it advertises none of its own -- and it changes
    on deploy, with no plugin update and no version bump anywhere.

    It is not written for coding agents. Any MCP client gets it, so the text
    names the read moment and the write moment in general terms and leaves the
    per-tool detail to the tool descriptions.
    """
    return MCPServer(
        name="ach-memory",
        instructions=INSTRUCTIONS,
    )
