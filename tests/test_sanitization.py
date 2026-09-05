import pytest

from memory.errors import ContentRejectedBySanitizer, ContentTooLarge
from memory.sanitization import normalize_claim, sanitize_evidence
from memory.v040_contracts import RetainEvidence


def test_claim_allows_only_mechanical_normalization():
    # "Café" is the NFD form (e + combining acute); normalize_claim
    # must fold it to NFC "Café" while collapsing \r\n and inner whitespace.
    decomposed = "Café\r\nuses   spaces  \n"
    assert normalize_claim(decomposed) == "Café\nuses spaces\n"


def test_claim_with_secret_is_rejected_not_rewritten():
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim("Deploy with API_TOKEN=secret-value")


def test_claim_rejects_blank_content():
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim("   \n\t  ")


def test_claim_rejects_oversize_content():
    with pytest.raises(ContentTooLarge):
        normalize_claim("a" * 4097)


def test_claim_allows_exactly_the_byte_ceiling():
    assert normalize_claim("a" * 4096) == "a" * 4096


def test_evidence_redacts_independently_and_requires_meaning():
    kept = sanitize_evidence((
        RetainEvidence(kind="tool_result", raw="PASS API_TOKEN=secret-value"),
        RetainEvidence(kind="user_quote", raw="Keep the public decision."),
    ))
    assert kept[0].raw == "PASS [redacted]"
    assert kept[1].raw == "Keep the public decision."
    with pytest.raises(ContentRejectedBySanitizer):
        sanitize_evidence((RetainEvidence(kind="tool_result", raw="API_TOKEN=secret-value"),))


def test_evidence_drops_whitespace_only_survivors_but_keeps_the_rest():
    kept = sanitize_evidence((
        RetainEvidence(kind="tool_result", raw="   "),
        RetainEvidence(kind="user_quote", raw="Still meaningful."),
    ))
    assert len(kept) == 1
    assert kept[0].raw == "Still meaningful."


def test_evidence_source_ref_with_control_character_is_dropped_not_rejected():
    kept = sanitize_evidence((
        RetainEvidence(kind="user_quote", raw="Public statement.", source_ref="line\x0012"),
    ))
    assert kept[0].source_ref is None


def test_evidence_total_size_over_ceiling_is_rejected():
    items = tuple(
        RetainEvidence(kind="tool_result", raw="x" * 1024) for _ in range(4)
    )
    with pytest.raises(ContentTooLarge):
        sanitize_evidence(items)
