"""Built-in project mental models (SPEC §2, §6). `briefing` is read at session start; the others
are loaded on demand with `load_context(model=...)`."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinModel:
    key: str
    name: str
    prompt: str
    scope: str  # "user" | "project"
    # A model reads only memories carrying one of these tags (`tags_match` any_strict); empty
    # reads the whole bank.
    source_tags: tuple[str, ...] = ()
    tags_match: str = "any_strict"
    # None: printed at session start. Otherwise the index line telling an agent when to load it.
    load_when: str | None = None


_RULES = (
    "Currentness: when memories conflict, the most recent one wins; drop anything a later memory "
    "says was removed, replaced or superseded. Use only what the memories state; never invent. "
    "Leave out history (dates, merge request numbers, incident stories), a person's habits or "
    "tastes that are not about this project, secrets, and advice that would fit any project."
)

BRIEFING = BuiltinModel(
    key="briefing",
    name="Briefing",
    scope="project",
    source_tags=("type:briefing",),
    prompt=(
        "Write the Briefing: what an agent must know before doing anything in this project. It is "
        "built from notes that agents working here saved for exactly this purpose, because they "
        "wished they had known them at the start of a session. The project's own instruction "
        "file (AGENTS.md) is loaded next to it.\n\n"
        "First call recall with max_tokens 12000 and the query \"what an agent must know at the "
        "start of every session in this project\". Recall again, also with max_tokens 12000, if "
        "the notes seem incomplete.\n\n"
        "Output Markdown bullets with no headings and no preamble, at most 6,000 characters in "
        "total, ordered from most important and most general to least:\n"
        "1. rules that bind every task in this project;\n"
        "2. where and how the project runs, and how to reach it;\n"
        "3. how changes are tested, released and rolled out;\n"
        "4. anything else every session needs.\n\n"
        "One point per bullet, written as an instruction or a fact an agent can act on, with "
        "commands and names exactly as the notes give them. Merge notes that say the same thing; "
        "otherwise keep each note's own wording, since a rephrased note can come to mean the "
        "opposite. "
        "A note about one component, file, host tool or incident goes last. " + _RULES
    ),
)

TRAPS = BuiltinModel(
    key="traps",
    name="Traps",
    scope="project",
    source_tags=("type:gotcha",),
    load_when="before debugging, deploying or releasing",
    prompt=(
        "Write the Traps reference for this project: what an agent loads before debugging, "
        "deploying or releasing, so it does not repeat a known failure. It is built from this "
        "project's verified gotchas.\n\n"
        "Make these calls first, each with max_tokens 12000:\n"
        "1. recall with the query \"failures, errors and traps hit while working on this "
        "project\";\n"
        "2. recall with the query \"environment, tooling, test, release and deployment "
        "problems\".\n\n"
        "Output Markdown: at most 30 bullets under at most 8 headings. One \"## <area>\" heading "
        "per area of the project, named as the project names it, the area with most traps first. "
        "Each bullet is \"**trap** -> what to do (why, in a few words)\". One trap per bullet; "
        "merge memories that describe the same trap; keep the traps that cost the most time or "
        "break the most work and drop the rest. End with one line: \"Detail: call recall with "
        "the trap's wording.\"\n\n" + _RULES
    ),
)

DECISIONS = BuiltinModel(
    key="decisions",
    name="Decisions",
    scope="project",
    source_tags=("type:decision", "type:constraint", "type:preference"),
    load_when="before changing a design or a rule",
    prompt=(
        "Write the Decisions reference for this project: what an agent loads before changing a "
        "design or a rule, so it does not reopen a settled choice or break a standing rule. It "
        "is built from this project's accepted decisions, constraints and preferences.\n\n"
        "Make these calls first, each with max_tokens 12000:\n"
        "1. recall with the query \"accepted decisions, rules and constraints of this project and "
        "why\";\n"
        "2. recall with the query \"rejected alternatives and superseded choices\".\n\n"
        "Output Markdown: at most 30 bullets under at most 8 \"## <area>\" headings, the area "
        "with most entries first. Each bullet is \"**choice or rule** - why, in a few words\", "
        "followed by \"; rejected: <alternative>\" when a memory names one. Prefix a rule that "
        "must be obeyed with \"(rule)\". A superseded decision is dropped and its replacement "
        "kept.\n\n" + _RULES
    ),
)

HOW_WE_WORK = BuiltinModel(
    key="how-we-work",
    name="How we work",
    scope="project",
    source_tags=("type:convention", "type:fact"),
    load_when="before touching the environment, the tooling or the release process",
    prompt=(
        "Write the How-we-work reference for this project: what an agent loads before touching "
        "the environment, the tooling or the release process. It is built from this project's "
        "conventions and durable facts.\n\n"
        "Make these calls first, each with max_tokens 12000:\n"
        "1. recall with the query \"how work is done in this project: build, test, release, "
        "deploy, review\";\n"
        "2. recall with the query \"where things live and run: environments, hosts, services, "
        "access\".\n\n"
        "Output Markdown: at most 30 bullets under at most 8 \"## <area>\" headings, the area "
        "with most entries first. Each bullet is a procedure or a fact an agent can act on, with "
        "commands, paths and names exactly as the memories give them.\n\n" + _RULES
    ),
)

BUILTIN_MODELS = (BRIEFING, TRAPS, DECISIONS, HOW_WE_WORK)
