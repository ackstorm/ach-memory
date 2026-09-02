"""Measured five-destination splitter for the Phase 5.7 challenger."""

from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import SemanticCase
from .semantic import NormalizedClaim, SemanticOutput

SplitDestination = Literal[
    "durable_user",
    "durable_project",
    "evidence",
    "working_state",
    "discard",
]


class SplitEnvelope(BaseModel):
    """The complete semantic contract of the minimal challenger."""

    model_config = ConfigDict(extra="forbid")

    destination: SplitDestination
    subject: Literal["user", "project"] | None
    text: str = Field(min_length=1)
    current: bool

    @model_validator(mode="after")
    def validate_destination_subject(self) -> SplitEnvelope:
        if self.destination == "durable_user" and (
            self.subject != "user" or not self.current
        ):
            raise ValueError("durable_user requires a current user subject")
        if self.destination == "durable_project" and (
            self.subject != "project" or not self.current
        ):
            raise ValueError("durable_project requires a current project subject")
        if self.destination == "evidence" and self.subject is None:
            raise ValueError("evidence requires an explicit subject")
        if self.destination in {"working_state", "discard"} and self.subject is not None:
            raise ValueError("non-bank destinations cannot carry a subject")
        if self.destination == "working_state" and not self.current:
            raise ValueError("working state must describe current work")
        return self


class SplitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: SemanticOutput
    routes: tuple[SplitEnvelope, ...]
    user_projection: tuple[str, ...]
    project_projection: tuple[str, ...]
    user_bank_scope_clean: bool
    project_bank_scope_clean: bool
    no_shared_document: bool


_OUTER_EXAMPLE = json.dumps(
    {
        "facts": [
            {
                "what": json.dumps(
                    {
                        "destination": "durable_user",
                        "subject": "user",
                        "text": "concise replies",
                        "current": True,
                    },
                    separators=(",", ":"),
                ),
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

SPLITTER_PROMPT = f"""\
Return exactly one outer object shaped like {_OUTER_EXAMPLE}. Each fact's
`what` is one escaped minified JSON object and no other prose. Emit the
minimum number of semantic claims. Every inner object has exactly these fields:
destination, subject, text and current.

Choose one destination:
- durable_user: a stable personal preference or cross-project convention;
  subject must be user and current must be true.
- durable_project: an accepted current project decision, constraint,
  convention, gotcha or durable technical fact; subject must be project and
  current must be true.
- evidence: useful rejected, superseded, observed or reproducible support that
  is not active profile truth; subject must explicitly be user or project.
- working_state: current objective, authorized work, open question or next
  step; subject must be null. Emit at most one.
- discard: greetings, trivia, secrets, unsupported inference and content cheap
  to recover from the repository; subject must be null.

Permission to implement or test is working_state, not a decision. A personal
statement and a project statement in one sentence are separate claims. Keep
negative constraints negative. Repository-local claims are project-scoped.
An unconfirmed inference is discard. Never copy raw transcript framing,
credentials, canaries or source markers. Do not emit kind, origin, eligibility,
ranking, displacement, tags or proof counts.
"""


def _parse_routes(response: dict) -> tuple[SplitEnvelope, ...]:
    facts = response.get("facts")
    if not isinstance(facts, list):
        raise TypeError("splitter response facts must be a list")
    routes = []
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("text"), str):
            raise TypeError("splitter fact must carry text")
        try:
            payload = json.loads(fact["text"])
        except json.JSONDecodeError as exc:
            raise ValueError("splitter fact is not valid JSON") from exc
        routes.append(SplitEnvelope.model_validate(payload))
    if sum(route.destination == "working_state" for route in routes) > 1:
        raise ValueError("splitter emitted more than one working state")
    return tuple(routes)


def _snapshot_texts(snapshot) -> tuple[str, ...]:
    return tuple(
        item.original_text
        for item in snapshot.objects
        if item.layer == "document" and item.original_text is not None
    )


def split_and_persist(case: SemanticCase, repetition: int, banks) -> SplitResult:
    """Extract routes, persist exact projections, then measure both banks."""
    from .semantic import _canonical

    content = _canonical(case)
    user_bank = banks.create_bank(f"semantic-{case.id}-hybrid-user", repetition)
    project_bank = banks.create_bank(
        f"semantic-{case.id}-hybrid-project", repetition
    )
    started = time.monotonic()
    response = banks.dry_run_extract(
        project_bank,
        content,
        retain_extraction_mode="custom",
        retain_mission=SPLITTER_PROMPT,
    )
    routes = _parse_routes(response)

    user_routes = tuple(
        route
        for route in routes
        if route.destination == "durable_user"
        or (route.destination == "evidence" and route.subject == "user")
    )
    project_routes = tuple(
        route
        for route in routes
        if route.destination == "durable_project"
        or (route.destination == "evidence" and route.subject == "project")
    )
    user_projection = tuple(route.text for route in user_routes)
    project_projection = tuple(route.text for route in project_routes)

    for scope, bank, projection in (
        ("user", user_bank, user_projection),
        ("project", project_bank, project_projection),
    ):
        for index, text in enumerate(projection, start=1):
            banks.retain_and_wait(
                bank,
                text,
                document_id=f"mq57:{case.id}:{repetition}:{scope}:{index}",
                strategy="verbatim",
            )

    user_documents = _snapshot_texts(banks.list_bank_objects(user_bank))
    project_documents = _snapshot_texts(banks.list_bank_objects(project_bank))
    user_clean = sorted(user_documents) == sorted(user_projection)
    project_clean = sorted(project_documents) == sorted(project_projection)
    no_raw = all(
        content not in document
        for document in (*user_documents, *project_documents)
    )

    claims = tuple(
        NormalizedClaim(
            text=route.text,
            scope=route.subject,
            current=route.current,
            source_ids=(),
        )
        for route in (*user_routes, *project_routes)
        if route.subject is not None
    )
    working = next(
        (route for route in routes if route.destination == "working_state"),
        None,
    )
    output = SemanticOutput(
        claims=claims,
        working_state={"objective": working.text} if working else None,
        document_scopes=tuple(
            scope
            for scope, projection in (
                ("user", user_projection),
                ("project", project_projection),
            )
            if projection
        ),
        input_tokens=None,
        output_tokens=None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return SplitResult(
        output=output,
        routes=routes,
        user_projection=user_projection,
        project_projection=project_projection,
        user_bank_scope_clean=user_clean,
        project_bank_scope_clean=project_clean,
        no_shared_document=user_clean and project_clean and no_raw,
    )
