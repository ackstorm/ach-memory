"""The sole semantic extractor (SPEC Phase 3 §7-§8). One `dry-run-extract`
call per slice with a strict JSON-envelope prompt, and structural validation
of every returned fact through `memory.capture.classifier`. Never partial:
a malformed envelope, an adversarial field, or more than one Working State
record anywhere in the batch fails the whole extraction rather than filing
whatever happened to parse.
"""

import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from memory.capture.classifier import CandidateRejected, classify
from memory.capture.contracts import NormalizedCandidate, WorkingStateEnvelope
from memory.hindsight.client import HindsightClient

EXTRACTION_PROMPT = """\
Extract only semantic claims worth remembering from this transcript slice: \
stated or observed user preferences, project conventions, decisions, \
gotchas and technical facts -- plus, at most once, a Working State update \
(objective, current direction, recent decisions, open questions, next \
steps).

Exclude greetings, small talk, session logistics, canaries, and anything \
cheaply reproduced by reading the repository (file contents, directory \
listings, command output already visible in the slice).

Emit exactly one minified JSON object per line, matching one of these two \
shapes -- nothing else:

{"record":"candidate","text":"...","kind":"preference|decision|convention\
|gotcha|technical_claim","origin":"stated|confirmed|observed|inferred",\
"subject":"user|project","provenance":{"type":"transcript","start":N,\
"end":N} or omitted,"negative":true or false,"correction":true or false,\
"failure":"..." or omitted,"cause":"..." or omitted,"reproduction":"..." \
or omitted}

{"record":"working_state","objective":"...","current_direction":"..." or \
omitted,"recent_decisions":[...],"open_questions":[...],"next_steps":[...]}

Never set bank, profile_eligible, tags, or observation_scopes -- those are \
derived, not proposed. Use "observed" only when provenance names the exact \
transcript span. Use "gotcha" only when failure and (cause or \
reproduction) are also given. At most one working_state object total.\
"""


class ExtractionFailed(Exception):
    """The whole slice's extraction is rejected. Never files a partial
    result: a caller catching this discards every candidate this call
    produced, not just the one that failed to parse or classify."""


@dataclass(frozen=True)
class ExtractionResult:
    candidates: list[NormalizedCandidate] = field(default_factory=list)
    working_state: WorkingStateEnvelope | None = None


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
        except (ValidationError, CandidateRejected) as exc:
            raise ExtractionFailed(str(exc)) from exc

        if isinstance(classified, WorkingStateEnvelope):
            if working_state is not None:
                raise ExtractionFailed("more than one working_state record")
            working_state = classified
        else:
            candidates.append(classified)

    return ExtractionResult(candidates=candidates, working_state=working_state)
