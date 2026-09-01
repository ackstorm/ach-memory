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

# The mirror of the rule above, for the other bank (SPEC §6.5): "local
# wording such as `here`, `in this repo`, `for this project` or
# task-specific instructions must not widen into user scope". A claim about
# how one repository works is project truth however it was phrased, and
# filing it as user truth would apply it to every other repository.
_LOCAL_SCOPE_RE = re.compile(
    r"(?i)\b(here|in this (?:repo|repository|project|codebase)"
    r"|for this (?:repo|repository|project|codebase)|on this project)\b"
)

# An explicit negative claim, for validating `negative` (SPEC §7.5:
# "preserve explicit negative constraints as negative constraints"). The
# flag is what routes a prohibition, so a model that sets it without a
# prohibition in the text has mislabelled the claim -- fail closed rather
# than file "use X" as if it read "never use X".
_NEGATION_RE = re.compile(
    r"(?i)(\bnot\b|\bno\b|\bnever\b|\bnone\b|\bavoid(?:s|ed|ing)?\b|\bwithout\b"
    r"|\bstop\b|\bprohibit(?:s|ed)?\b|\bforbid(?:s|den)?\b|\bdisallow(?:s|ed)?\b"
    r"|\bdon't\b|\bdoesn't\b|\bdidn't\b|\bcan't\b|\bcannot\b|\bwon't\b|\bshouldn't\b"
    r"|\bmustn't\b|\bisn't\b|\baren't\b|n't\b)"
)


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

    # §6.5, the other direction: repository-local wording must not widen
    # into user scope. "Always run make check here" is how this project
    # works, not how this person works, and filing it as user truth would
    # carry it into every other repository they touch.
    if envelope.subject == "user" and _LOCAL_SCOPE_RE.search(envelope.text):
        raise CandidateRejected(
            "project-local wording cannot widen into user scope"
        )

    # §7.5: a negative constraint stays a negative constraint. The flag is
    # what preserves it, so it must be backed by an actual prohibition in
    # the text -- otherwise "do not X" and "prefer X" become indistinguishable
    # downstream, in whichever direction the model got it wrong.
    if envelope.negative and not _NEGATION_RE.search(envelope.text):
        raise CandidateRejected(
            "negative=true does not match an explicit negative claim"
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


# SPEC §6.4, transcribed cell by cell rather than summarized. The previous
# summary ("everything that is neither a technical claim nor inferred is
# eligible") read one row wrong: an OBSERVED preference and an OBSERVED
# decision are evidence, not profile truth. Watching someone do a thing
# twice is not the same as their saying they want it done that way, and
# §6.6 is explicit that observed preferences remain evidence.
#
# | kind             | stated   | confirmed | observed | inferred |
# | preference       | profile  | profile   | evidence | evidence |
# | decision         | profile  | profile   | evidence | evidence |
# | convention       | profile  | profile   | profile  | evidence |
# | gotcha           | profile  | profile   | profile  | evidence |
# | technical_claim  | evidence | evidence  | evidence | evidence |
#
# `working_state` has no row here: §6.4 routes it by kind before bank
# retention and it never becomes a Hindsight fact at all, so it never
# reaches this function -- WorkingStateEnvelope is a separate shape.
_PROFILE = "profile_eligible"
_EVIDENCE = "evidence_only"

_DURABILITY: dict[tuple[CandidateKind, CandidateOrigin], Eligibility] = {
    ("preference", "stated"): _PROFILE,
    ("preference", "confirmed"): _PROFILE,
    ("preference", "observed"): _EVIDENCE,
    ("preference", "inferred"): _EVIDENCE,
    ("decision", "stated"): _PROFILE,
    ("decision", "confirmed"): _PROFILE,
    ("decision", "observed"): _EVIDENCE,
    ("decision", "inferred"): _EVIDENCE,
    ("convention", "stated"): _PROFILE,
    ("convention", "confirmed"): _PROFILE,
    ("convention", "observed"): _PROFILE,
    ("convention", "inferred"): _EVIDENCE,
    ("gotcha", "stated"): _PROFILE,
    ("gotcha", "confirmed"): _PROFILE,
    ("gotcha", "observed"): _PROFILE,
    ("gotcha", "inferred"): _EVIDENCE,
    ("technical_claim", "stated"): _EVIDENCE,
    ("technical_claim", "confirmed"): _EVIDENCE,
    ("technical_claim", "observed"): _EVIDENCE,
    ("technical_claim", "inferred"): _EVIDENCE,
}


def _eligibility(kind: CandidateKind, origin: CandidateOrigin) -> Eligibility:
    """Harness rule #7: durability is looked up in §6.4's matrix, never
    inferred from a rule of thumb about it. A pair missing from the table is
    a kind/origin this harness does not know how to place, so it is evidence
    -- the safe half of the matrix."""
    return _DURABILITY.get((kind, origin), _EVIDENCE)
