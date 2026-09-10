"""The advertised MCP tool surface, pinned in one place.

Both `scripts/e2e.py` and `scripts/mcp-smoke.py` assert the set a live
server advertises, and stating it twice is exactly how they drifted:
mcp-smoke went on pinning "the exact fifteen of SPEC 11" for the whole of
v0.4.0, which added eleven more tools plus `transfer`. Its check then failed
against a perfectly healthy server while saying nothing about the surface --
a pin that only ever reports its own staleness. `LEAK_RE` lives in
scripts/leakscan.py for the same reason.

Deliberately a literal rather than a walk of the live registry: the point is
to state the contract independently of the code implementing it, so an
accidental registration -- or removal -- fails here instead of reaching an
agent. tests/test_mcp_surface_honesty.py checks the in-process registry and
its groupings; this is the over-the-wire statement of the same contract.
"""

EXPECTED_MCP_TOOLS = frozenset(
    {
        # memory
        "retain", "sync_retain", "recall", "reflect", "memory_history",
        "list_memories", "get_memory", "forget", "correct", "restore",
        "list_documents", "get_document", "delete_document",
        "get_operation", "list_operations", "cancel_operation",
        # mental models
        "create_mental_model", "list_mental_models", "get_mental_model",
        "update_mental_model", "refresh_mental_model", "delete_mental_model",
        # working state and standing context
        "start_working_session", "set_working_state", "clear_working_state",
        "load_context",
        # projects -- the only project route advertised over MCP
        "transfer",
    }
)
