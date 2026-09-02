"""The sole semantic extractor (SPEC Phase 3 §7-§8). One `dry-run-extract`
call per slice with a strict JSON-envelope prompt, and structural validation
of every returned fact through `memory.capture.classifier`. Never partial:
a malformed envelope, an adversarial field, or more than one Working State
record anywhere in the batch fails the whole extraction rather than filing
whatever happened to parse.
"""

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from memory.capture.classifier import CandidateDropped, CandidateRejected, classify
from memory.capture.contracts import NormalizedCandidate, WorkingStateEnvelope
from memory.hindsight.client import HindsightClient

_EXTRACTION_INTRO = """\
Extract only semantic claims worth remembering from this transcript slice: \
stated or observed user preferences, project conventions, decisions, \
gotchas and technical facts -- plus, at most once, a Working State update \
(objective, current direction, recent decisions, open questions, next \
steps).

Exclude greetings, small talk, session logistics, canaries, and anything \
cheaply reproduced by reading the repository (file contents, directory \
listings, command output already visible in the slice).
"""

_CANDIDATE_SHAPE = """\
{"record":"candidate","text":"...","kind":"preference|decision|convention\
|gotcha|technical_claim","origin":"stated|confirmed|observed|inferred",\
"subject":"user|project","provenance":{"type":"transcript","start":N,\
"end":N} or omitted,"negative":true or false,"correction":true or false,\
"failure":"..." or omitted,"cause":"..." or omitted,"reproduction":"..." \
or omitted}"""

_WORKING_STATE_SHAPE = """\
{"record":"working_state","objective":"...","current_direction":"..." or \
omitted,"recent_decisions":[...],"open_questions":[...],"next_steps":[...]}
"""

_HINDSIGHT_CANDIDATE_EXAMPLE = json.dumps(
    {
        "record": "candidate",
        "text": "claim",
        "kind": "preference",
        "origin": "stated",
        "subject": "user",
    },
    separators=(",", ":"),
)
_HINDSIGHT_OUTER_EXAMPLE = json.dumps(
    {
        "facts": [
            {
                "what": _HINDSIGHT_CANDIDATE_EXAMPLE,
                "when": "N/A",
                "where": "N/A",
                "who": "N/A",
                "why": "N/A",
                "fact_type": "world",
            }
        ]
    },
    separators=(",", ":"),
)

EXTRACTION_RULES = """\
Plans, proposals, research output and next steps are Working State, never \
decisions. Permission to execute, test or explore a proposal is \
authorization, not confirmation of a decision: use "confirmed" only when \
the human accepted the decision itself, and then only for the gist they \
accepted, not for every detail of the proposal around it.

Set "correction":true only for an explicit human correction of an earlier \
claim, and route it to the scope it corrects -- a correction about this \
repository is subject "project", a correction about how the person works \
is subject "user". Repository-local wording ("here", "in this repo", "for \
this project") is always subject "project"; never widen it to "user".

Preserve a negative constraint as a negative constraint: keep the \
prohibition in "text" and set "negative":true. Never weaken "do not X" \
into "prefers Y". Write project claims impersonally -- no "I", "my" or \
"we".

Lines of the form [raw N:M) are markers written by the harness, naming the \
original byte range each following record came from. They are not \
transcript content: never quote them, never emit one, and never treat one \
as a claim.

Never set bank, profile_eligible, tags, or observation_scopes -- those are \
derived, not proposed. Use "observed" only when provenance names the exact \
transcript span, as character offsets into this slice text (not the byte \
numbers in the markers). Omit provenance for stated, confirmed and inferred \
claims. Use "gotcha" only when failure and (cause or \
reproduction) are also given. At most one working_state object total.\
"""

_JSONL_ENVELOPE = f"""\
Emit exactly one minified JSON object per line, matching one of these two \
shapes -- nothing else:

{_CANDIDATE_SHAPE}

{_WORKING_STATE_SHAPE}\
"""

_HINDSIGHT_OBJECT_ENVELOPE = f"""\
Emit exactly one outer object shaped like {_HINDSIGHT_OUTER_EXAMPLE} -- \
nothing else. Replace the example claim with a claim from the input and add \
one fact per claim. Each fact's `what` is a string containing one escaped, \
minified ACH envelope matching one of these two shapes; it is never a nested \
object:

{_CANDIDATE_SHAPE}

{_WORKING_STATE_SHAPE}\
"""

ExtractionTransport = Literal["jsonl", "hindsight_object"]


def build_extraction_prompt(transport: ExtractionTransport) -> str:
    """Render one semantic contract through a transport-specific envelope."""
    if transport == "jsonl":
        envelope = _JSONL_ENVELOPE
    elif transport == "hindsight_object":
        envelope = _HINDSIGHT_OBJECT_ENVELOPE
    else:
        raise ValueError(f"unsupported extraction transport: {transport}")
    return f"{_EXTRACTION_INTRO}\n{envelope}\n\n{EXTRACTION_RULES}"


EXTRACTION_PROMPT = build_extraction_prompt("jsonl")


class ExtractionFailed(Exception):
    """The whole slice's extraction is rejected. Never files a partial
    result: a caller catching this discards every candidate this call
    produced, not just the one that failed to parse or classify."""


@dataclass(frozen=True)
class ExtractionResult:
    candidates: list[NormalizedCandidate] = field(default_factory=list)
    working_state: WorkingStateEnvelope | None = None
    #: How many candidates a semantic rule dropped (CandidateDropped). A
    #: count, never the claims themselves: this number is safe to log or
    #: meter, the text behind it is not.
    dropped: int = 0


def extract(
    client: HindsightClient, bank_id: str, sanitized_content: str
) -> ExtractionResult:
    """Read-only against Hindsight (`dry-run-extract` stores nothing);
    parsing and classification happen entirely in this process."""
    response = client.dry_run_extract(
        bank_id,
        sanitized_content,
        retain_extraction_mode="custom",
        retain_mission=EXTRACTION_PROMPT,
    )
    # Hindsight may also suggest entities alongside facts; discarded on
    # purpose -- canonical routing belongs to the harness (classify()),
    # never to Hindsight's own entity resolution.
    facts = response.get("facts", [])

    candidates: list[NormalizedCandidate] = []
    working_state: WorkingStateEnvelope | None = None
    dropped = 0

    for fact in facts:
        text = fact.get("text") if isinstance(fact, dict) else None
        if not isinstance(text, str):
            raise ExtractionFailed("a fact carried no text")

        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExtractionFailed(f"malformed envelope JSON: {exc}") from exc
        if not isinstance(envelope, dict):
            raise ExtractionFailed("envelope was not a JSON object")

        try:
            classified = classify(envelope)
        except CandidateDropped:
            # One claim this harness disagrees with, not a misread slice.
            # Dropped alone: failing the whole extraction here would discard
            # every good candidate beside it and then retry the identical
            # prompt to the attempt limit, losing the slice for good.
            dropped += 1
            continue
        except (ValidationError, CandidateRejected) as exc:
            raise ExtractionFailed(str(exc)) from exc

        if isinstance(classified, WorkingStateEnvelope):
            if working_state is not None:
                raise ExtractionFailed("more than one working_state record")
            working_state = classified
        else:
            classified = _without_invalid_provenance(
                envelope,
                classified,
                len(sanitized_content),
            )
            candidates.append(classified)

    return ExtractionResult(
        candidates=candidates, working_state=working_state, dropped=dropped
    )


def _without_invalid_provenance(
    envelope: dict,
    candidate: NormalizedCandidate,
    slice_length: int,
) -> NormalizedCandidate:
    """Remove a false span without discarding sibling candidates.

    `observed` is the origin that survives on the strength of its artifact
    (SPEC §6.1, harness rule #3), so a span that runs off the end of the
    slice is not a citation. Reclassification after removing the span makes
    an `observed` claim inferred and recalculates its eligibility. Other
    origins keep their structural meaning, and the rest of the slice survives.
    """
    span = candidate.provenance
    if span is None:
        return candidate
    if span.start < slice_length and span.end <= slice_length:
        return candidate
    repaired = dict(envelope)
    repaired.pop("provenance", None)
    reclassified = classify(repaired)
    if isinstance(reclassified, WorkingStateEnvelope):
        raise TypeError("a candidate changed record type during reclassification")
    return reclassified
