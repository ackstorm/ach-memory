"""Secret shapes QA F-06/F-26 found passing, and control characters (F-07)."""

import pytest

from memory.errors import ContentRejectedBySanitizer
from memory.sanitization import contains_secret, normalize_claim


@pytest.mark.parametrize(
    "text",
    [
        "AWS access key AKIAIOSFODNN7EXAMPLE and secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY.",
        "aws_secret_access_key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "google key AIzaSyA-1234567890abcdefghijklmnopqrst_",
        "jwt eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "stripe sk_live_51H8xYzAbCdEfGh",
        "deploy key sk-live-AKIA4XPLTESTFAKE9931",
        "restricted rk_test_51H8xYzAbCdEfGh",
        "gitlab glpat-abcdefghijklmnopqrst",
        "slack app token xapp-1-A0123456789-abcdefghij",
        "hook https://hooks.slack.com/services/TQA0FIXTURE/BQA0FIXTURE/qa-fixture-not-a-token",
    ],
)
def test_common_credential_shapes_are_rejected(text):
    assert contains_secret(text)
    with pytest.raises(ContentRejectedBySanitizer):
        normalize_claim(text)


@pytest.mark.parametrize(
    "text",
    [
        "The AKIA prefix marks an AWS access key id.",  # prefix alone, too short
        "Use uv, not pip, for Python installs.",
        "Release v0.7.1 shipped on 2026-09-11.",
        "The digest 5f4dcc3b5aa765d61d8327deb882cf99 names the image.",  # 32 hex, not a key shape
    ],
)
def test_ordinary_prose_still_passes(text):
    assert not contains_secret(text)
    assert normalize_claim(text) == text


@pytest.mark.parametrize("bad", ["ping\x07pong", "red\x1b[31m text", "nul\x00byte", "del\x7f"])
def test_control_characters_are_rejected(bad):
    with pytest.raises(ContentRejectedBySanitizer, match="control"):
        normalize_claim(bad)


def test_newline_and_tab_remain_legal():
    assert normalize_claim("line one\n\tline two") == "line one\n line two"


def test_blank_content_says_so_instead_of_blaming_a_secret():
    with pytest.raises(ContentRejectedBySanitizer, match="empty"):
        normalize_claim("   \n\t ")
