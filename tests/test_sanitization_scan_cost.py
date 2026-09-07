"""The secret scan is quadratic; the size gate must precede it.

`normalize_claim` normalized and scanned the whole input and only then
checked the 4096-byte ceiling. `_SECRET_PATTERNS`' key=value rule ends in
`\\w*` either side of its literal, so a long run of word characters makes the
engine retry from every position: 4 KB 20 ms, 8 KB 79 ms, 16 KB 324 ms,
32 KB 1.3 s, which puts 1 MB near twenty minutes of CPU. Since
`TypedRetainRequest.content` has no max_length and the write budget is 60
per minute per credential, one authenticated caller could hold a worker
indefinitely.
"""

import time

import pytest

from memory.errors import ContentTooLarge
from memory.sanitization import _MAX_BYTES, _MAX_RAW_BYTES, normalize_claim


def test_a_megabyte_is_refused_promptly_instead_of_being_scanned():
    """The assertion that matters is the CLOCK, not the exception.

    Before the fix this raised too -- eventually. A pure correctness test
    passes against the pathological version, which is why the bound is
    timed. The margin is enormous (about twenty minutes versus one second),
    so this is not a tight benchmark masquerading as a test.
    """
    started = time.monotonic()
    with pytest.raises(ContentTooLarge):
        normalize_claim("x" * 1_000_000)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"refusal took {elapsed:.1f}s; the scan ran before the size gate"


def test_whitespace_heavy_input_over_the_final_limit_still_normalizes_through():
    """The raw gate is 4x the real ceiling precisely so this keeps working:
    horizontal whitespace collapses, so a body far above 4096 bytes can
    legitimately land under it."""
    content = "word" + (" " * 8_000) + "end"
    assert len(content.encode()) > _MAX_BYTES

    normalized = normalize_claim(content)

    assert normalized == "word end"


def test_the_post_normalization_ceiling_is_still_the_real_limit():
    """Between _MAX_BYTES and _MAX_RAW_BYTES, and incompressible: the raw
    gate lets it through, the original check still refuses it."""
    content = "abcd" * 2_000  # 8000 bytes, no whitespace to collapse
    assert _MAX_BYTES < len(content.encode()) <= _MAX_RAW_BYTES

    with pytest.raises(ContentTooLarge):
        normalize_claim(content)


def test_an_ordinary_claim_is_untouched_by_the_new_gate():
    assert normalize_claim("The staging cluster runs in eu-west-1.") == (
        "The staging cluster runs in eu-west-1."
    )
