from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BuiltinModelDefinition:
    key: Literal["user-context", "project-context"]
    scope: Literal["user", "project"]
    version: int
    name: str
    source_query: str
    source_tags: tuple[str, str]
    tags_match: Literal["all"]
    max_tokens: int
    trigger: Mapping[str, object]
    always_in_context: bool = True


USER_CONTEXT_V1 = BuiltinModelDefinition(
    key="user-context",
    scope="user",
    version=1,
    name="User Context",
    source_query=(
        "Summarize durable user context that an authorized agent should always know. "
        "Include explicitly retained identity, relationships, preferences, constraints, "
        "conventions, facts, and accepted decisions useful across agents. Distinguish "
        "imperative constraints from defeasible preferences and preserve whether support "
        "is human-explicit or agent-verified. Omit project-specific material, speculation, "
        "transient work, secrets, unsupported inference, and duplicate statements. "
        "Present only current indefinite knowledge."
    ),
    source_tags=("schema:ach-retain-v1", "validity:indefinite"),
    tags_match="all",
    max_tokens=512,
    trigger={
        "mode": "delta",
        "refresh_after_consolidation": True,
        "min_refresh_interval_seconds": 300,
    },
    always_in_context=True,
)

PROJECT_CONTEXT_V1 = BuiltinModelDefinition(
    key="project-context",
    scope="project",
    version=1,
    name="Project Context",
    source_query=(
        "Summarize durable, impersonal project context that is not cheaply rediscoverable "
        "from the repository: accepted decisions and useful rationale, constraints, "
        "conventions, non-obvious facts and history, and verified gotchas. Preserve "
        "prohibitions and distinguish rejected or superseded alternatives from active "
        "decisions. Omit personal user context, current task progress, secrets, unsupported "
        "inference, duplicate statements, and cheap source-code facts. Present only current "
        "indefinite knowledge."
    ),
    source_tags=("schema:ach-retain-v1", "validity:indefinite"),
    tags_match="all",
    max_tokens=1024,
    trigger={
        "mode": "delta",
        "refresh_after_consolidation": True,
        "min_refresh_interval_seconds": 300,
    },
    always_in_context=True,
)
