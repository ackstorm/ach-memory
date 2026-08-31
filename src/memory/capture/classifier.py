"""Structural origin/kind/provenance normalization, eligibility and routing
(SPEC Phase 3 §7-§10, the "harness" steps documented on the extractor
envelope in the Phase 3 plan). Pure and semantic-free: this module never
reads free text for meaning, only the structural fields the model already
assigned -- kind, origin, subject, provenance and the gotcha evidence
fields. Nothing here calls Hindsight or the database.
"""

import re

from memory.capture.contracts import (
    CandidateEnvelope,
    CandidateKind,
    CandidateOrigin,
    Eligibility,
    NormalizedCandidate,
    WorkingStateEnvelope,
)


class CandidateRejected(ValueError):
    """A structurally valid envelope that the routing rules refuse to file:
    a personal-preference or personal-language candidate aimed at the
    project bank. Distinct from a pydantic ValidationError (a malformed
    envelope) -- this is a well-formed envelope the rules say must not
    become project truth.
    """


# A floor, not a promise (same reasoning as memory.capture.local.redact()):
# catches the obvious first-person phrasings without claiming to understand
# the sentence. Project candidates that read this way are rejected outright
# rather than silently rewritten into impersonal wording.
_PERSONAL_LANGUAGE_RE = re.compile(r"(?i)\b(i|i'm|i've|i'd|my|mine|myself)\b")


def classify(envelope: dict) -> NormalizedCandidate | WorkingStateEnvelope:
    """Route one raw extractor envelope. An unrecognized `record` value
    fails closed rather than guessing which shape was meant."""
    record = envelope.get("record")
    if record == "working_state":
        return WorkingStateEnvelope.model_validate(envelope)
    if record == "candidate":
        return _classify_candidate(CandidateEnvelope.model_validate(envelope))
    raise CandidateRejected(f"unrecognized record type: {record!r}")


def _classify_candidate(envelope: CandidateEnvelope) -> NormalizedCandidate:
    kind: CandidateKind = envelope.kind
    origin: CandidateOrigin = envelope.origin

    # Harness rule #3: observed without an artifact is not observed.
    if origin == "observed" and envelope.provenance is None:
        origin = "inferred"

    # Harness rule #4: a gotcha needs failure plus cause and/or reproduction,
    # else it is just an unevidenced technical claim.
    has_gotcha_evidence = bool(envelope.failure) and bool(
        envelope.cause or envelope.reproduction
    )
    if kind == "gotcha" and not has_gotcha_evidence:
        kind = "technical_claim"

    # Harness rule #6: project claims use impersonal wording. A preference
    # is definitionally personal, so routing one to the project bank is a
    # category error regardless of its text; anything else aimed at the
    # project bank still fails if its own wording reads as personal.
    if envelope.subject == "project":
        if kind == "preference":
            raise CandidateRejected(
                "a preference cannot be routed to the project bank"
            )
        if _PERSONAL_LANGUAGE_RE.search(envelope.text):
            raise CandidateRejected(
                "personal language cannot be preserved as project truth"
            )

    eligible = _eligibility(kind, origin)
    correction_scope = (envelope.subject, eligible) if envelope.correction else None

    return NormalizedCandidate(
        text=envelope.text,
        kind=kind,
        origin=origin,
        bank_kind=envelope.subject,
        eligible=eligible,
        tags=[f"kind:{kind}", eligible],
        observation_scopes=[[eligible]],
        negative=envelope.negative,
        correction=envelope.correction,
        correction_scope=correction_scope,
        provenance=envelope.provenance,
    )


def _eligibility(kind: CandidateKind, origin: CandidateOrigin) -> Eligibility:
    """Harness rule #7, the spec matrix: a technical claim is always
    evidence-only regardless of origin, and inferred evidence is always
    evidence-only regardless of kind. Everything else -- a stated,
    confirmed, or artifact-backed observed preference/decision/convention/
    evidenced-gotcha -- is profile eligible."""
    if kind == "technical_claim":
        return "evidence_only"
    if origin == "inferred":
        return "evidence_only"
    return "profile_eligible"
