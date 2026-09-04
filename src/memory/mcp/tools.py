"""Shared MCP tool contracts and product registration aggregation."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ValidationError, model_serializer

# Populated by the product registrars. Tests call through it, so an
# unregistered tool fails there exactly as it would over the wire.
REGISTRY: dict[str, Any] = {}


class ToolResult(BaseModel):
    """A BaseModel, not a dict: the SDK only emits structured output for one."""

    result: dict[str, Any]
    # Set only when the caller used a retired project slug (SPEC §8.6).
    project_slug: str | None = None
    resolved_from: str | None = None
    notice: str | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict[str, Any]:
        """Emit only the fields that carry something.

        The three slug fields are null on every call that did not follow a
        rename, which is nearly all of them, and the SDK serializes this model
        twice per response — once as structured output and once as the text
        block that mirrors it (`func_metadata.convert_result`). Dropping nulls
        is lossless and applies to all eighteen tools, `verbose` included.

        Safe against the advertised contract: the SDK builds outputSchema with
        `model_json_schema()` in validation mode, which a serializer does not
        touch, and every field it omits is optional there.
        """
        emitted: dict[str, Any] = {"result": self.result}
        for name in ("project_slug", "resolved_from", "notice"):
            value = getattr(self, name)
            if value is not None:
                emitted[name] = value
        return emitted


class MCPToolError(Exception):
    """The only exception shape a tool pipeline lets escape.

    An MCP client ultimately only sees `str(exc)` — the SDK's dispatcher
    wraps a raised exception as `f"Error executing tool {name}: {e}"` and
    drops every other attribute — so `code`/`details` are encoded into the
    text itself, the only channel that survives that wrapping. `.code`,
    `.message` and `.details` also stay as real attributes for anything that
    can see the exception object directly (tests included).
    """

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        text = f"{code}: {message}"
        if self.details:
            text = f"{text} {self.details}"
        super().__init__(text)


def _validation_message(exc: ValidationError) -> str:
    """Render only safe validation messages about caller-authored input."""
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in exc.errors()
    )


def _invalid_request(exc: ValidationError) -> MCPToolError:
    return MCPToolError("INVALID_REQUEST", _validation_message(exc))


def _internal_error() -> MCPToolError:
    return MCPToolError("INTERNAL_ERROR", "internal error")


def register(mcp: MCPServer) -> None:
    """Register each product-owned MCP surface on one server."""
    # Kept local so product modules can import the shared contracts above
    # without creating an import-time cycle back through this aggregator.
    from memory.mcp import memory_tools, working_state_tools

    memory_tools.register(mcp)
    working_state_tools.register(mcp)
