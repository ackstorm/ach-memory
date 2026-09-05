from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations

from memory.context_service import load_context
from memory.mcp.server import tool_session
from memory.mcp.tools import REGISTRY
from memory.v040_contracts import LoadContextRequest


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description="Load authorized bounded standing context. It creates no project, bank, model or claim; it may enqueue one existing safety repair.",
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
    )
    def load_context_tool(project_slug: str | None, workspace_id: str | None, ctx: Context):
        with tool_session(ctx) as tc:
            result = load_context(tc.db, tc.principal, LoadContextRequest(project_slug=project_slug, workspace_id=workspace_id))
            tc.db.commit()
            return result.model_dump()

    REGISTRY["load_context"] = load_context_tool
