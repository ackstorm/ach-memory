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
