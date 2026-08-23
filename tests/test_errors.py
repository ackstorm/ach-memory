def test_every_error_code_is_in_the_spec_closed_list():
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

    declared = {
        obj.code
        for obj in vars(errors).values()
        if isinstance(obj, type) and issubclass(obj, errors.DomainError)
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
