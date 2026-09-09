from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations

from memory.context_service import load_context as load_context_service
from memory.mcp.server import tool_session
from memory.mcp.tools import REGISTRY
from memory.v040_contracts import LoadContextRequest


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description="Load authorized bounded standing context. It creates no project, bank, model or claim, and retains nothing.",
        # Genuinely read-only: unlike recall/reflect, load_context enqueues
        # no maintenance and never calls run_access_maintenance.
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def load_context(
        ctx: Context,
        project_slug: str | None = None,
        workspace_id: str | None = None,
        scope: Literal["user", "project", "both"] = "both",
    ):
        with tool_session(ctx) as tc:
            result = load_context_service(
                tc.db,
                tc.principal,
                LoadContextRequest(project_slug=project_slug, workspace_id=workspace_id, scope=scope),
            )
            tc.db.commit()
            return result.model_dump()

    REGISTRY["load_context"] = load_context
