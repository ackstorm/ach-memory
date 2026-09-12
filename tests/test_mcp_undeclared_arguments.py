"""A tool refuses an argument it does not declare.

The SDK generates each tool's argument model from its signature and leaves
pydantic's default `extra="ignore"`. Measured on the live surface before
this was closed: `recall(tags_filter=["fid:u01"])` returned 2 hits and
`recall(tags=["fid:u01"])` -- the same intent with one wrong name --
returned 8, with no error. The caller believed its filter had applied.

The REST surface has always refused these (`extra="forbid"` on every request
model), so the two doors used to disagree about what the same call meant.
"""

import pytest
from pydantic import ValidationError

from memory.mcp.server import build_mcp
from memory.mcp.tools import register


@pytest.fixture(scope="module")
def tools():
    mcp = build_mcp()
    register(mcp)
    return {tool.name: tool for tool in mcp._tool_manager.list_tools()}


def test_the_wrong_name_for_a_real_parameter_is_refused(tools):
    """`tags` is what `retain` calls them; on `recall` they are
    `tags_filter`. That mismatch is the one that reached production."""
    model = tools["recall"].fn_metadata.arg_model

    with pytest.raises(ValidationError, match="tags"):
        model.model_validate({"scope": "user", "query": "q", "tags": ["repo:acme/api"]})


def test_the_declared_name_still_works(tools):
    model = tools["recall"].fn_metadata.arg_model

    validated = model.model_validate(
        {"scope": "user", "query": "q", "tags_filter": ["repo:acme/api"]}
    )

    assert validated.tags_filter == ["repo:acme/api"]


def test_every_tool_is_hardened_not_just_recall(tools):
    """The defect is every optional parameter of all 28 tools, not `tags`."""
    assert len(tools) == 28
    for name, tool in tools.items():
        assert tool.fn_metadata.arg_model.model_config.get("extra") == "forbid", name


def test_hardening_a_server_with_no_tools_fails_loudly():
    """A silent no-op here would restore exactly the silence this removes,
    so a move in the SDK's internals has to surface as a crash at startup."""
    from memory.mcp.tools import _forbid_undeclared_arguments

    with pytest.raises(RuntimeError, match="tool manager internals"):
        _forbid_undeclared_arguments(build_mcp())
