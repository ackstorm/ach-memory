"""Desired Hindsight bank configuration under test (SPEC Phase 3), and the
read-only verification a later, separately-approved rollout runs before any
of it ever touches a live bank.

Nothing in this module calls PATCH, retains memory, or mutates state.
Applying the config, enabling the worker, or refreshing mental models is
outside this plan's authorized rollout (see the Phase 3 plan's completion
boundary).
"""

from dataclasses import dataclass
from typing import Any

from memory import activity
from memory.hindsight.client import HindsightClient

# entity_labels is user-bank only: entities_allow_free_form stays False
# there so the canonical vocabulary is exhaustive; the project bank has no
# fixed actor vocabulary to canonicalize (it names conventions and gotchas,
# not who is speaking).
ENTITY_LABELS: tuple[str, ...] = ("user", "agent")

RETAIN_MISSION = (
    "Each item is one already-extracted claim. Store it as one fact, as "
    "written. Do not split, merge, rephrase, infer, or add facts. Keep the "
    "metadata."
)

OBSERVATIONS_MISSION = (
    "Observations are current truth. Merge restatements and count distinct "
    "sessions/sources, not raw repetitions. On conflict keep the current "
    "belief; keep an older choice only when a short reason prevents a "
    "likely future mistake. Keep genealogy in evidence. Never drop "
    "explicit negation."
)

USER_PROFILE_ELIGIBLE_LIMIT = 15
PROJECT_PROFILE_ELIGIBLE_LIMIT = 25


@dataclass(frozen=True)
class DesiredBankConfig:
    """One bank's desired Phase 3 configuration. `as_dict()` is the shape a
    future, separately-approved PATCH would send -- this module never sends
    it."""

    profile_eligible_limit: int
    entity_labels: tuple[str, ...] | None = None
    entities_allow_free_form: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "retain_default_strategy": "candidate_verbatim",
            "retain_strategies": {
                "candidate_verbatim": {
                    "retain_extraction_mode": "verbatim",
                    "retain_mission": RETAIN_MISSION,
                }
            },
            "enable_observations": True,
            "observations_mission": OBSERVATIONS_MISSION,
            "observation_scope_limits": [
                {"scope": ["profile_eligible"], "limit": self.profile_eligible_limit}
            ],
        }
        if self.entity_labels is not None:
            body["entity_labels"] = list(self.entity_labels)
        if self.entities_allow_free_form is not None:
            body["entities_allow_free_form"] = self.entities_allow_free_form
        return body


def desired_user_config() -> DesiredBankConfig:
    return DesiredBankConfig(
        profile_eligible_limit=USER_PROFILE_ELIGIBLE_LIMIT,
        entity_labels=ENTITY_LABELS,
        entities_allow_free_form=False,
    )


def desired_project_config() -> DesiredBankConfig:
    return DesiredBankConfig(profile_eligible_limit=PROJECT_PROFILE_ELIGIBLE_LIMIT)


def diff_config(desired: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Only the desired keys that differ from current -- current may carry
    fields desired says nothing about, and those are not drift."""
    changed: dict[str, Any] = {}
    for key, desired_value in desired.items():
        current_value = current.get(key)
        if current_value != desired_value:
            changed[key] = {"desired": desired_value, "current": current_value}
    return changed


def redact_for_display(value: Any, bank_id: str) -> Any:
    """Bank IDs never appear in a printed diff (SPEC inv. 29): a config
    response can echo the bank's own id back, and printing it verbatim
    would defeat the fingerprinting every other caller-facing surface
    already applies."""
    fingerprint = activity.fingerprint(bank_id)
    if value == bank_id:
        return fingerprint
    if isinstance(value, dict):
        return {key: redact_for_display(item, bank_id) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_for_display(item, bank_id) for item in value]
    return value


VERBATIM_CHECK_CLAIM = "Do not run destructive Git commands without approval."


@dataclass(frozen=True)
class VerifyResult:
    extraction_ok: bool
    facts: list[str]
    config_drift: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.extraction_ok and not self.config_drift


def verify_bank(
    client: HindsightClient, bank_id: str, desired: DesiredBankConfig
) -> VerifyResult:
    """Read-only: proves the `candidate_verbatim` strategy would return
    exactly the input claim, once, with no additional fact, and reports any
    config drift against `desired`. Never PATCHes config or retains memory
    -- see this module's docstring."""
    extraction = client.dry_run_extract(
        bank_id, VERBATIM_CHECK_CLAIM, retain_extraction_mode="verbatim"
    )
    facts = [fact.get("text", "") for fact in extraction.get("facts", [])]
    extraction_ok = facts == [VERBATIM_CHECK_CLAIM]

    current = client.get_bank_config(bank_id)
    drift = diff_config(desired.as_dict(), current)

    return VerifyResult(extraction_ok=extraction_ok, facts=facts, config_drift=drift)
