# ach-memory v0.4.0 mental-model governance — verification result

Status: **NON-LIVE AND LIVE HINDSIGHT 0.9.2 GATES PASSED**

## Non-live governance suite (executed)

```
rtk uv run pytest tests/test_builtin_models.py tests/test_mental_model_service.py \
  tests/test_mental_models_api.py tests/test_model_refresh.py tests/test_bootstrap.py \
  tests/test_mcp_tools.py -q
```

The original plan run passed 143 tests. After the live compatibility corrections, the complete
non-integration repository suite passed 2,239 tests (5 skipped, 6 live tests deselected); the
focused changed-contract suite passed 157 tests.

```
rtk uv run ruff check src tests
```

Result: all checks passed (repository-wide, not just this plan's files).

These cover, across Tasks 1-7 of this plan: exact frozen built-in prompts/budgets; transactional
custom-model create/list/get/update/delete/refresh with the five-custom quota, the always-in-context
delivery-token budget, exact `operation_id` idempotency and crash-recovery resume; the logical-key
REST surface; REST/MCP parity and the closed 24-tool advertised surface; bootstrap (User/Project
built-in provisioning, MCP-bootstrap project creation and its audit/warning, opt-out,
lifecycle-disabled and version-upgrade built-in reconciliation, refusal to adopt a colliding unknown
upstream model); and exact refresh-operation-identity completion plus bounded, backoff-gated repair.

## Live disposable-bank run

The plan calls for a guarded live proof against a real Hindsight instance
(`HINDSIGHT_V040_CONFIRM=disposable-banks-only`), covering:

```
bootstrap creates exactly one built-in and a second bootstrap is idempotent
five custom models succeed while the sixth fails before an upstream request
always_in_context false survives update and built-in version reconciliation
refresh output is withheld until the exact operation completes
delete by ACH model_key removes only its mapped upstream model
an externally created unknown model is reported but not adopted or counted
```

`tests/test_v040_mental_models_live.py` implements all six as two disposable-bank test functions,
guarded by:

- skip unless `HINDSIGHT_V040_CONFIRM=disposable-banks-only` is set;
- refusal (`pytest.fail`, not a silent skip) against any Hindsight host that is not loopback and not
  explicitly named in `HINDSIGHT_V040_ALLOW_HOSTS`;
- a fresh, uniquely-named disposable bank per run, registered before use and deleted in `finally`
  regardless of outcome.

The two tests passed against the real loopback Hindsight 0.9.2 deployment on 2026-09-05. In the
combined retain/model live run they completed in 1.42 seconds and 0.22 seconds respectively.

The first attempt exposed two upstream-contract mismatches that mocks had hidden: create returns
`mental_model_id` plus `operation_id`, and manual refresh is represented by omitting the trigger,
not by `mode: manual`. Both contracts are now pinned in production code, unit tests and the SPEC.
Fresh model output is withheld against the returned create operation until its terminal state is
observed. The live lifecycle also verifies that changing an automatic model back to manual clears
the upstream consolidation trigger instead of merely changing ACH's registry.

No production model, bank or credential was touched. Cleanup was independently verified after the
combined live run: no bank with a v0.4.0 test or diagnostic prefix remained.
