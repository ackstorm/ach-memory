from typing import Literal

MemoryType = Literal[
    "preference", "constraint", "decision", "convention", "fact", "gotcha"
]
EvidenceBasis = Literal["human_explicit", "agent_verified"]
RetainTrigger = Literal["user_requested", "agent_proactive"]
EvidenceKind = Literal["user_quote", "tool_result", "artifact_excerpt"]
Lifecycle = Literal["active", "expired", "forgotten"]
ModelOrigin = Literal["builtin", "user"]
DeliveryState = Literal["ready", "withheld"]
