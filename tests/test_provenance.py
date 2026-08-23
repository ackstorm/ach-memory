import pytest

from memory import provenance
from memory.errors import InvalidMetadata


def test_extraction_metadata_keeps_the_six_extraction_fields():
    extraction = provenance.build(
        {"agent": "codex", "source": "pull-request", "git_branch": "feature/auth"},
        project_slug="payments-api",
    )

    assert extraction == {
        "agent": "codex",
        "source": "pull-request",
        "git_branch": "feature/auth",
        "project_slug": "payments-api",
    }


def test_client_metadata_survives_when_it_is_not_reserved():
    extraction = provenance.build(
        {"profile": "security", "pr": "382"}, project_slug="payments-api"
    )

    assert extraction["profile"] == "security"
    assert extraction["pr"] == "382"


@pytest.mark.parametrize(
    "key", ["tenant_id", "user_id", "project_slug", "memory_key", "on_behalf_of"]
)
def test_a_reserved_key_is_refused_and_nothing_is_written(key):
    with pytest.raises(InvalidMetadata) as caught:
        provenance.build({key: "attacker"}, project_slug="payments-api")

    assert caught.value.details["key"] == key


def test_agent_is_reserved_against_overwrite_but_settable_once():
    """§13.4 reserves `agent` so client metadata cannot overwrite an
    authoritative value. When the server has none, the client's is the only
    value there is and it is kept — in extraction, since `agent` is one of
    §13.2's six extraction fields."""
    extraction = provenance.build({"agent": "codex"}, project_slug="payments-api")

    assert extraction["agent"] == "codex"


def test_client_name_is_reserved_against_overwrite_but_excluded_from_extraction():
    """Same overwrite protection as `agent`, but `client_name` is
    client-reported identity, not one of §13.2's extraction six, so it must
    never reach extraction."""
    extraction = provenance.build(
        {"client_name": "vscode-extension"}, project_slug="payments-api"
    )

    assert "client_name" not in extraction


def test_reserved_keys_contains_agent_and_client_name():
    assert "agent" in provenance.RESERVED_KEYS
    assert "client_name" in provenance.RESERVED_KEYS


def test_audit_only_runtime_fields_stay_out_of_extraction():
    """SPEC §13.2: os/arch/client_version (audit/runtime context) must never
    reach the extraction mapping Hindsight sees, only the extraction six may.
    """
    extraction = provenance.build(
        {
            "agent": "codex",
            "os": "linux",
            "arch": "arm64",
            "client_version": "1.2.3",
        },
        project_slug=None,
    )

    assert extraction == {"agent": "codex"}


def test_audit_only_keys_are_stripped_from_extraction_regardless_of_case():
    """Measured live: a stored memory's extraction metadata carried
    OS/ARCH/Client_Name/Client_Version case variants while the lowercase
    spelling of `os` was correctly dropped. The reserved-key check normalizes
    (key.strip().lower()) for exactly this reason (Task 2); the extraction
    filter must match the raw key against a lowercased AUDIT_ONLY_KEYS the
    same way, or a near-miss spelling reaches Hindsight."""
    extraction = provenance.build(
        {
            "OS": "linux",
            "ARCH": "arm64",
            "Client_Name": "evilcli",
            "Client_Version": "9.9.9",
            "source": "pull-request",
        },
        project_slug=None,
    )

    assert extraction == {"source": "pull-request"}


def test_context_line_ignores_empty_string_fields():
    # source/agent are both present but empty (dict[str, str] allows "" as a
    # value) — must read as absent, not as a sentence starting with "on".
    assert (
        provenance.context_line({"source": "", "agent": "", "git_branch": "main"})
        is None
    )


@pytest.mark.parametrize(
    "key", ["User_Id", "USER_ID", " user_id", "user_id ", "tenant_ID"]
)
def test_reserved_key_check_is_case_and_whitespace_insensitive(key):
    with pytest.raises(InvalidMetadata) as caught:
        provenance.build({key: "attacker"}, project_slug="payments-api")

    # normalized for matching, but the original spelling is still reported
    assert caught.value.details["key"] == key


def test_empty_project_slug_is_treated_as_absent():
    extraction = provenance.build({"agent": "codex"}, project_slug="")

    assert "project_slug" not in extraction


def test_build_returns_a_mapping_independent_of_the_callers_dict():
    client_metadata = {"agent": "codex", "os": "linux"}

    extraction = provenance.build(client_metadata, project_slug="payments-api")

    # mutating the caller's dict or the returned mapping after the call must
    # not reach back into the other, or into what was already validated.
    client_metadata["agent"] = "tampered-after-the-call"
    extraction["agent"] = "tampered-in-extraction"

    assert client_metadata["agent"] == "tampered-after-the-call"


def test_no_client_metadata_yields_empty_extraction():
    assert provenance.build(None, project_slug=None) == {}


def test_context_line_reads_like_a_sentence():
    line = provenance.context_line(
        {"source": "interactive-coding", "agent": "codex", "git_branch": "feature/auth"}
    )

    assert line == "interactive-coding via codex on feature/auth"


def test_context_line_is_none_without_provenance():
    assert provenance.context_line({}) is None
