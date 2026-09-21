"""Mechanical, non-semantic sanitization for retained claim content.

Content is REJECTED, never rewritten, when it would need secret redaction: a
claim is one durable, independently-correctable statement, and silently
editing it would make the stored text diverge from what the caller believes
it retained.
"""

import math
import re
import unicodedata
from collections import Counter

from memory.errors import ContentRejectedBySanitizer
from memory.identifiers import has_control_character

_HORIZONTAL_WS = re.compile(r"[ \t]+")

# Structural, not semantic: fixed patterns for shapes secrets commonly take.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"\b(?:sk|gh[oprsu]|mem|xox[baprs])[-_][A-Za-z0-9]{10,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
    ),
    # Userinfo WITH a password. Userinfo alone is not a credential: an ssh git
    # remote carries a username, and rejecting it cost a real claim its home.
    # A token used as the username still trips the token patterns above.
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@\S+"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),  # JWT
    re.compile(r"\b[sr]k[-_](?:live|test)[-_][A-Za-z0-9]{8,}\b"),  # Stripe sk_/rk_ live|test
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),  # GitLab PAT
    re.compile(r"\bxapp-[0-9]-[A-Z0-9]+-[A-Za-z0-9-]+\b"),  # Slack app token
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),  # Slack webhook
]

# `keyword = value` where the VALUE has the shape of a credential: 10-150
# characters and Shannon entropy of at least 3.5 bits/char (gitleaks'
# generic-api-key rule), so config talk like "max_tokens=1" is not flagged.
_KEYWORD_ASSIGNMENT = re.compile(
    r"(?i)\b\w*(?:secret|password|passwd|token|api[_-]?key)\w*"
    r"\s*[=:]\s*[\"'`]?([A-Za-z0-9._=+/-]{10,150})"
)
_KEYWORD_VALUE_MIN_ENTROPY = 3.5


def _shannon_entropy(value: str) -> float:
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in Counter(value).values())


def _looks_like_credential(match: re.Match[str]) -> bool:
    return _shannon_entropy(match.group(1)) >= _KEYWORD_VALUE_MIN_ENTROPY


def contains_secret(text: str) -> bool:
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return True
    return any(_looks_like_credential(m) for m in _KEYWORD_ASSIGNMENT.finditer(text))


def normalize_claim(content: str) -> str:
    """Canonicalize one durable claim. Rejects rather than rewrites a secret."""
    normalized = unicodedata.normalize("NFC", content).replace("\r\n", "\n")
    normalized = "\n".join(
        _HORIZONTAL_WS.sub(" ", line).rstrip() for line in normalized.split("\n")
    )
    if not normalized.strip():
        raise ContentRejectedBySanitizer("content is empty after normalization")
    # Newline is the one control character a claim may carry; tabs were
    # already folded into spaces above.
    if has_control_character(normalized.replace("\n", "")):
        raise ContentRejectedBySanitizer("content contains control characters")
    if contains_secret(normalized):
        raise ContentRejectedBySanitizer("canonical content cannot be stored safely")
    return normalized
