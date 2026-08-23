def test_every_error_code_is_in_the_spec_closed_list(configured_env):
    """§18 is declared CLOSED and the codebase reasons about it that way.
    Five codes were missing when this test was written (review finding I7).

    Checks both ways, against §18's actual list (the first fenced ```text```
    block under the "## 18. Error model" heading) rather than the whole SPEC:
    every DomainError.code the codebase declares must appear in that list
    (forward), and every code in that list must be either a declared
    DomainError or one of the MCP-only literal codes `_run` raises directly
    (backward) -- `INVALID_REQUEST` has no DomainError subclass by design
    (§18 prose: "REST has no equivalent DomainError for this class of
    failure"), so it is folded in from `mcp/tools.py`'s own `MCPToolError(
    "...")` call sites instead of hardcoded here.

    Deleting a code from §18's list while leaving it in §18's prose turns
    this red (review mutant M15): the earlier version matched the whole SPEC
    file with one regex, so a code named anywhere in prose -- not just the
    closed list -- satisfied it. Adding a phantom code to §18's list with no
    backing declaration also turns this red (M14), which the previous
    one-directional assertion could not catch at all.
    """
    import re
    from pathlib import Path

    from memory import errors, mcp

    root = Path(__file__).resolve().parents[1]
    spec_text = (root / "SPEC-v1.md").read_text()

    section = re.search(
        r"## 18\. Error model\n(.*?)\n## 19", spec_text, re.DOTALL
    ).group(1)
    closed_list = re.search(r"```text\n(.*?)\n```", section, re.DOTALL).group(1)
    spec_codes = set(closed_list.split())

    # Enumerate SUBCLASSES, not vars(errors). The previous version only saw
    # classes declared in errors.py itself, so `UserAlreadyExists` -- the one
    # DomainError declared in a route module -- was invisible to a test whose
    # entire purpose is catching exactly that (2026-08-23 review, R1-#2/R2-I7).
    # create_app() is called first so every route module is imported and any
    # subclass declared outside errors.py is registered before we look.
    from memory.api.app import create_app

    create_app()

    def _subclasses(cls) -> set[type]:
        found = set()
        for sub in cls.__subclasses__():
            found.add(sub)
            found |= _subclasses(sub)
        return found

    declared = {
        obj.code for obj in _subclasses(errors.DomainError) | {errors.DomainError}
    }
    mcp_tools_source = (
        Path(mcp.__file__).parent / "tools.py"
    ).read_text()
    mcp_only_codes = set(
        re.findall(r'MCPToolError\(\s*"([A-Z_]+)"', mcp_tools_source)
    )
    codebase_codes = declared | mcp_only_codes

    assert codebase_codes == spec_codes, (
        f"declared but missing from SPEC §18: {sorted(codebase_codes - spec_codes)}; "
        f"in SPEC §18 but not implemented: {sorted(spec_codes - codebase_codes)}"
    )
