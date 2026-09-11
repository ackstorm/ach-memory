"""A hit's timestamp says what it is.

`RecallHit.occurred_at` was filled from `occurred_start or mentioned_at`. Retain
takes no "when did this happen" input, so for every fact this service writes
`occurred_start` is never set and the field always carried the retain time --
under the name of the event time. Measured 2026-09-11: no raw recall result
carried an `occurred_start` key at all.
"""

from memory.read_service import _normalize_hit


def _raw(**overrides):
    raw = {
        "id": "mem-1",
        "text": "The auth module signs EdDSA, never RS256.",
        "type": "world",
        "tags": ["type:convention", "basis:agent_verified", "schema:ach-retain-v1"],
        "mentioned_at": "2026-09-11T10:14:31.026616+00:00",
    }
    raw.update(overrides)
    return raw


def test_a_hit_reports_when_it_was_retained_under_that_name():
    hit = _normalize_hit(_raw())

    assert hit is not None
    assert hit.mentioned_at == "2026-09-11T10:14:31.026616+00:00"
    assert not hasattr(hit, "occurred_at")


def test_an_event_time_is_not_passed_off_as_the_retain_time():
    """Should upstream ever infer an event date, it must not be handed to the
    caller as `mentioned_at`. The two answer different questions; a caller
    that wants the event time gets it when retain can carry one, under its
    own name."""
    hit = _normalize_hit(_raw(occurred_start="2024-01-15T10:30:00Z"))

    assert hit is not None
    assert hit.mentioned_at == "2026-09-11T10:14:31.026616+00:00"


def test_a_hit_with_no_timestamp_says_so():
    hit = _normalize_hit(_raw(mentioned_at=None))

    assert hit is not None
    assert hit.mentioned_at is None
