from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinModel:
    key: str
    name: str
    prompt: str
    scope: str  # "user" | "project"
    source_tags: tuple[str, ...] = ("schema:ach-retain-v1",)
    tags_match: str = "all"


USER_CONTEXT = BuiltinModel(
    key="user-context",
    name="User Context",
    scope="user",
    prompt=(
        "Summarize durable user context that an authorized agent should always know. "
        "Include explicitly retained identity, relationships, preferences, constraints, "
        "conventions, facts, and accepted decisions useful across agents. Distinguish "
        "imperative constraints from defeasible preferences and preserve whether support "
        "is human-explicit or agent-verified. Omit project-specific material, speculation, "
        "transient work, secrets, unsupported inference, and duplicate statements. "
        "Present only current indefinite knowledge."
    ),
)

PROJECT_CONTEXT = BuiltinModel(
    key="project-context",
    name="Project Context",
    scope="project",
    prompt=(
        "Summarize durable, impersonal project context that is not cheaply rediscoverable "
        "from the repository: accepted decisions and useful rationale, constraints, "
        "conventions, non-obvious facts and history, and verified gotchas. Preserve "
        "prohibitions and distinguish rejected or superseded alternatives from active "
        "decisions. Omit personal user context, current task progress, secrets, unsupported "
        "inference, duplicate statements, and cheap source-code facts. Present only current "
        "indefinite knowledge."
    ),
)

BUILTIN_MODELS = (USER_CONTEXT, PROJECT_CONTEXT)
