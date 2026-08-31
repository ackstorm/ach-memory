"""Constrained aliases reused by REST's Pydantic request models and the
public FastMCP tool signatures.

A bound enforced only on a Pydantic request model is invisible to the model
calling an MCP tool: the SDK derives each tool's advertised JSON Schema from
the function SIGNATURE, not from what a route's body_factory() builds inside
it (SPEC §11.4's `update_mode` precedent -- `append` was already accepted at
runtime long before anything in the advertised schema said it existed). These
aliases are the single place a workspace/session/Working-State-line bound is
declared, so REST and MCP can never advertise two different shapes for the
identical value.
"""

import re
from typing import Annotated

from pydantic import AfterValidator, Field

from memory.identifiers import has_control_character

WORKSPACE_ID_PATTERN = r"^ws_[0-9a-f]{32}$"
_WORKSPACE_ID_RE = re.compile(WORKSPACE_ID_PATTERN)


def _well_formed_workspace_id(value: str) -> str:
    # fullmatch, not match/search: `$` alone matches just before a trailing
    # newline, so `.match()` against this same pattern let
    # "ws_" + 32 hex + "\n" through.
    if not _WORKSPACE_ID_RE.fullmatch(value):
        raise ValueError("workspace_id must be 'ws_' followed by 32 hex characters")
    return value


def _no_blank_or_control(value: str) -> str:
    if not value.strip() or has_control_character(value):
        raise ValueError("must not be blank or contain control characters")
    return value


WorkspaceId = Annotated[
    str,
    Field(pattern=WORKSPACE_ID_PATTERN),
    AfterValidator(_well_formed_workspace_id),
]

SessionId = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(_no_blank_or_control),
]

SessionEpoch = Annotated[int, Field(ge=0)]
CheckpointSeq = Annotated[int, Field(ge=0)]

WorkingStateLine = Annotated[
    str,
    Field(min_length=1, max_length=512),
    AfterValidator(_no_blank_or_control),
]

WorkingStateLines = Annotated[list[WorkingStateLine], Field(max_length=10)]
