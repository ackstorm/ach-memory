import pytest

from memory.errors import ContentRejectedBySanitizer, ContentTooLarge
from memory.sanitization import normalize_claim, sanitize_evidence
from memory.v040_contracts import RetainEvidence


def test_claim_allows_only_mechanical_normalization():
    # "Café" is the NFD form (e + combining acute); normalize_claim
    # must fold it to NFC "Café" while collapsing \r\n and inner whitespace.
    decomposed = "Café\r\nuses   spaces  \n"
    assert normalize_claim(decomposed) == "Café\nuses spaces\n"


#: A value with the shape of a credential: 16 mixed-case alphanumerics,
#: Shannon entropy 4.0. The previous fixture, `secret-value`, scores 3.19 --
#: below the 3.5 gate the keyword rule now shares with gitleaks -- and was
#: only ever caught because the rule accepted any non-space run as a value.
SECRET = "API_TOKEN=q7Hx2mZp9LkT4vRw"


def test_claim_with_secret_is_rejected_not_rewritten():
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim(f"Deploy with {SECRET}")


@pytest.mark.parametrize(
    "claim",
    [
        # Every one of these was a real retain rejected on 2026-09-11.
        "Set max_tokens=1 on the recall request so only the ids arrive.",
        "_RECALL_MAX_TOKENS = 32768 covers roughly 600 claims.",
        "api_key: rotated monthly, the value lives in Vault.",
        "The token: 2026-09-11T14:20:58Z stamp is upstream's, not ours.",
    ],
)
def test_configuration_talk_with_a_keyword_is_not_a_secret(claim):
    """`keyword=value` is only a secret when the VALUE looks like one: a
    small integer, a word or a timestamp does not, however the key is named."""
    assert normalize_claim(claim) == claim


@pytest.mark.parametrize(
    "claim",
    [
        "secret=3f9a1c7e2b8d4f6a9c0e1b2d3f4a5c6e",  # 32 hex, entropy 3.91
        "password: 'dGhpcyBpcyBhIHRlc3Q='",  # base64, entropy 3.68
        "auth_token = ghp_x7Fq9Lm2Zt4Rv8Kw1Jn6Hp3Yb5Cd0Aa",  # entropy 5.07
    ],
)
def test_a_credential_shaped_value_is_still_rejected(claim):
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim(claim)


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
        RetainEvidence(kind="tool_result", raw=f"PASS {SECRET}"),
        RetainEvidence(kind="user_quote", raw="Keep the public decision."),
    ))
    assert kept[0].raw == "PASS [redacted]"
    assert kept[1].raw == "Keep the public decision."
    with pytest.raises(ContentRejectedBySanitizer):
        sanitize_evidence((RetainEvidence(kind="tool_result", raw=SECRET),))


def test_evidence_keeps_configuration_talk_unredacted():
    kept = sanitize_evidence((
        RetainEvidence(kind="tool_result", raw="max_tokens=1 | 97 results | 97 w/ ids"),
    ))
    assert kept[0].raw == "max_tokens=1 | 97 results | 97 w/ ids"


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
