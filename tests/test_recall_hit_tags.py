"""A recalled fact carries back the caller's own tags.

Filtering recall by `repo:group/app` is only half a feature if the answer
never says which repo a hit belongs to. `RecallHit` is `extra="forbid"`, so
tags are dropped by construction unless the model has a field for them --
un-stripping them in `mcp/compact.py` does nothing on this path, which never
calls `compact_payload`.
"""

from memory.read_service import _caller_tags_of, _normalize_hit


def _raw(**overrides):
    raw = {
        "id": "mem-1",
        "text": "The auth module signs EdDSA, never RS256.",
        "type": "world",
        "tags": [
            "type:convention",
            "basis:agent_verified",
            "schema:ach-retain-v1",
            "validity:indefinite",
            "repo:ackstorm/ach-runtime",
        ],
    }
    raw.update(overrides)
    return raw


def test_a_hit_carries_the_callers_own_tags():
    hit = _normalize_hit(_raw())

    assert hit is not None
    assert hit.tags == ("repo:ackstorm/ach-runtime",)


def test_server_derived_tags_are_not_forwarded():
    """type:/basis: already reach the caller as memory_type/basis;
    schema:/validity: are internal bookkeeping this surface has never
    exposed."""
    hit = _normalize_hit(_raw())

    assert hit.memory_type == "convention"
    assert hit.basis == "agent_verified"
    for tag in hit.tags:
        assert not tag.startswith(("type:", "basis:", "schema:", "validity:"))


def test_an_untagged_hit_has_no_tags():
    hit = _normalize_hit(_raw(tags=["schema:ach-retain-v1"]))

    assert hit is not None
    assert hit.tags == ()


def test_caller_tags_survive_a_malformed_upstream_tag_list():
    """Upstream shape is not ours to trust: a non-list, or a non-string
    inside one, must degrade to no tags rather than raise inside a read."""
    assert _caller_tags_of(None) == ()
    assert _caller_tags_of("repo:x") == ()
    assert _caller_tags_of(["repo:b", 7, None, "repo:a"]) == ("repo:a", "repo:b")
