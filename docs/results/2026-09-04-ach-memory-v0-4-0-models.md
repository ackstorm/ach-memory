# ach-memory v0.4.0 mental-model governance — verification result

Status: **NON-LIVE GATE COMPLETE — LIVE DISPOSABLE-BANK RUN NOT EXECUTED (no reachable instance)**

## Non-live governance suite (executed)

```
rtk uv run pytest tests/test_builtin_models.py tests/test_mental_model_service.py \
  tests/test_mental_models_api.py tests/test_model_refresh.py tests/test_bootstrap.py \
  tests/test_mcp_tools.py -q
```

Result: 143 passed, 0 failed.

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

## Live disposable-bank run (not executed)

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

Running it in this environment (`rtk env HINDSIGHT_V040_CONFIRM=disposable-banks-only uv run pytest
tests/test_v040_mental_models_live.py -q`) exercised exactly that guard: the configured
`MEMORY_HINDSIGHT_URL` here is the test suite's own mock hostname (`hindsight.test`, never a real
service), which is neither loopback nor allow-listed, so both tests correctly failed at the host
check rather than silently skipping or reaching an unintended endpoint. This environment has no
running Hindsight instance and no configured LLM-provider credentials for one (`docker-compose.yml`'s
`hindsight` service requires `HINDSIGHT_LLM_BASE_URL`/`HINDSIGHT_LLM_API_KEY`, neither of which is
available here), so the actual disposable-bank proof could not be executed as part of this session.

No production model, bank or credential was touched by this verification. Running the live proof for
real is an operator action: point `MEMORY_HINDSIGHT_URL` at a loopback or explicitly allow-listed
disposable Hindsight 0.9.2 instance with real LLM-provider credentials, then run the command above.
