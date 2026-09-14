import pytest

from memory.errors import ContentRejectedBySanitizer
from memory.sanitization import normalize_claim


def test_claim_allows_only_mechanical_normalization():
    # "Café" is the NFD form (e + combining acute); normalize_claim must
    # fold it to NFC "Café" while collapsing \r\n and inner whitespace.
    decomposed = "Café\r\nuses   spaces  \n"
    assert normalize_claim(decomposed) == "Café\nuses spaces\n"


#: 16 mixed-case alphanumerics, Shannon entropy 4.0 -- above the 3.5 gate.
SECRET = "API_TOKEN=q7Hx2mZp9LkT4vRw"


def test_claim_with_secret_is_rejected_not_rewritten():
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim(f"Deploy with {SECRET}")


@pytest.mark.parametrize(
    "claim",
    [
        "Set max_tokens=1 on the recall request so only the ids arrive.",
        "_RECALL_MAX_TOKENS = 32768 covers roughly 600 claims.",
        "api_key: rotated monthly, the value lives in Vault.",
        "The token: 2026-09-11T14:20:58Z stamp is upstream's, not ours.",
    ],
)
def test_configuration_talk_with_a_keyword_is_not_a_secret(claim):
    """`keyword=value` is only a secret when the VALUE looks like one."""
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
