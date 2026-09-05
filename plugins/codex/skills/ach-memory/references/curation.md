# Retain curation

## Decision tree

1. Is there one durable claim? If no, abstain or use Working State.
2. Is it accepted or directly verified, rather than proposed, quoted, rejected or inferred? If no,
   ask, verify or abstain.
3. Does it belong to this repository or implementation? Use Project. Use User only for explicitly
   personal or stable cross-project information.
4. Can the claim be corrected independently? If no, split it into separate calls.
5. Can minimal evidence be sent without a secret or irrelevant surrounding content? If no, abstain.
6. Is it true now? If it begins in the future, do not retain it yet. If it expires, supply the exact
   `valid_until`; otherwise leave it indefinite.

## Examples

Retain in Project:

- “We chose PostgreSQL advisory locks because two workers may claim the same job.” → `decision`,
  `human_explicit`, with the user's decision sentence as evidence.
- A failing integration test proves retries duplicate an operation when its ID changes. → `gotcha`,
  `agent_verified`; include the failure, cause and reproducible condition.
- “Never send project incident details to the User Bank.” → `constraint`, `human_explicit`.

Retain in User:

- “Across my projects, keep answers concise unless I ask for detail.” → `preference`,
  `human_explicit`.
- “My name is Ana.” → `fact`, `human_explicit`.

Do not retain:

- “Maybe we should use Redis.” It is a proposal, not a decision.
- “Pepe said ‘deploy on Friday’.” It is quoted evidence, not the user's adopted instruction.
- “The tests are probably flaky.” It is inference until verified.
- “I am editing `api.py` now.” It is transient Working State.
- A token, password, private key, full transcript, file or log.

Time:

- “Do not schedule meetings through Sunday” is retainable only when already active and Sunday can
  be resolved to an exact instant in the interactive user's/session's timezone.
- “Starting next Monday, do not schedule meetings” is rejected for now; retain it on Monday.

Retries and changes:

- Network outcome unknown after `retain`: retry the identical payload with the same `operation_id`.
- Typo in an existing claim: `correct` it.
- A decision changed: `forget` the old decision, then `retain` the new decision with a new
  `operation_id`.
