import pytest

from memory.errors import InvalidTag
from memory.tags import RESERVED_PREFIXES, normalize_caller_tags


def test_tags_are_lowercased_stripped_deduped_and_sorted():
    """Normalisation is total and applied on BOTH write and read, so the
    agent cannot desynchronise what it stored from what it searches."""
    assert normalize_caller_tags([" Repo:Group/App ", "repo:group/app", "kind:ci"]) == (
        "kind:ci", "repo:group/app",
    )


def test_none_and_empty_normalize_to_no_tags():
    assert normalize_caller_tags(None) == ()
    assert normalize_caller_tags([]) == ()


@pytest.mark.parametrize("prefix", sorted(RESERVED_PREFIXES))
def test_a_server_owned_prefix_is_refused(prefix):
    """type:, basis:, schema: and validity: are derived server-side. A caller
    tag in those namespaces would corrupt typed curation and the mental-model
    source filter, so it is refused rather than merged."""
    with pytest.raises(InvalidTag):
        normalize_caller_tags([f"{prefix}anything"])


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "a" * 65, "repo:a b", "repo:a\nb", "repo:ünïcode", "x" * 3 + ":" * 2],
)
def test_a_malformed_tag_is_refused(bad):
    with pytest.raises(InvalidTag):
        normalize_caller_tags([bad])


def test_too_many_tags_are_refused():
    with pytest.raises(InvalidTag):
        normalize_caller_tags([f"kind:{n}" for n in range(9)])


@pytest.mark.parametrize(
    "bad",
    [5, "repo:x", {"repo": "x"}, [1, 2], ["repo:x", 7], [None], [["repo:x"]]],
)
def test_a_non_list_of_strings_is_a_typed_rejection_not_a_crash(bad):
    """These models run normalize_caller_tags as a `mode="before"` validator,
    so the value arrives un-type-checked. A TypeError or AttributeError here
    is not a DomainError, escapes pydantic, and is reported to the caller as
    a 500 -- a malformed request body dressed up as a server fault."""
    with pytest.raises(InvalidTag):
        normalize_caller_tags(bad)


def test_none_is_the_only_falsy_input_that_is_not_an_error():
    assert normalize_caller_tags(None) == ()
    assert normalize_caller_tags([]) == ()
    assert normalize_caller_tags(()) == ()
    with pytest.raises(InvalidTag):
        normalize_caller_tags(0)
    with pytest.raises(InvalidTag):
        normalize_caller_tags("")
