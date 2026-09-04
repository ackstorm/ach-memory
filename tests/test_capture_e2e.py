"""Phase 3's delivery gate: local sanitization, durable replay safety, one
semantic extraction, exact candidate mapping and automatic Working State
delivery, driven end to end through the real HTTP API and the real worker
state machine -- only Hindsight is mocked.

This is not authorization to enable anything: MEMORY_CAPTURE_WORKER_ENABLED
and the local hook's MEMORY_CAPTURE_ENABLED both default false, no route
here PATCHes bank config or touches mental models, and nothing here proves
more than what this one mocked, local scenario shows.
"""

import hashlib
import json

import httpx
import respx

from memory.capture import local, worker
from memory.capture.contracts import CheckpointSubmission
from memory.hindsight.client import HindsightClient
from memory.models import Project, ProjectSlug, User

BASE = "http://hindsight.test"
WS = "ws_" + "e" * 32

# Secret and repository-file canaries: proven absent from every downstream
# surface, never just "not asserted".
SECRET_CANARY = "sk-CANARYE2ESECRET1234567890"
FILE_CANARY = "CANARY_FILE_BODY_MUST_NEVER_SURVIVE"
PATH_CANARY = "/home/user/.ssh/id_rsa_CANARYPATH"

TRANSCRIPT = "\n".join(
    json.dumps(record)
    for record in [
        {
            "type": "user",
            "message": {"role": "user", "content": "I prefer tabs over spaces for indentation."},
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": f"Here's a token if you need it: Bearer {SECRET_CANARY}",
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check that file."},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": PATH_CANARY}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": f"-----BEGIN RSA PRIVATE KEY-----\n{FILE_CANARY}\n-----END RSA PRIVATE KEY-----",
                        "is_error": False,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "CI runs ruff and pytest on every push -- I can see the workflow file.",
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "The service probably listens on port 8080, based on the Dockerfile.",
            },
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "Do not squash commits, ever."},
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Actually, we use ruff now, not flake8 -- that changed last sprint.",
            },
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "Also, I like dark mode in my editor."},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "Next I'll wire the capture CLI."},
        },
    ]
) + "\n"

ENVELOPES = [
    {
        "record": "candidate",
        "text": "Prefers tabs over spaces for indentation.",
        "kind": "preference",
        "origin": "stated",
        "subject": "user",
    },
    {
        "record": "candidate",
        "text": "CI runs ruff and pytest on every push.",
        "kind": "convention",
        "origin": "observed",
        "subject": "project",
        "provenance": {"type": "transcript", "start": 10, "end": 60},
    },
    {
        "record": "candidate",
        "text": "The service listens on port 8080.",
        "kind": "technical_claim",
        "origin": "inferred",
        "subject": "project",
    },
    {
        "record": "candidate",
        "text": "Do not squash commits.",
        "kind": "convention",
        "origin": "stated",
        "subject": "project",
        "negative": True,
    },
    {
        "record": "candidate",
        "text": "The project uses ruff, not flake8.",
        "kind": "convention",
        "origin": "stated",
        "subject": "project",
        "correction": True,
    },
    {
        "record": "candidate",
        "text": "Prefers dark mode in the editor.",
        "kind": "preference",
        "origin": "stated",
        "subject": "user",
    },
    {
        "record": "working_state",
        "objective": "Ship the capture pipeline",
        "next_steps": ["Wire the capture CLI"],
    },
]


def _create_user_and_project(client, master_headers, tenant, session):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    client.post("/v1/projects", json={"project_slug": "acme-e2e"}, headers=headers)
    mapping = session.query(ProjectSlug).filter_by(slug="acme-e2e").one()
    project = session.get(Project, mapping.project_internal_id)
    user = session.get(User, user_id)
    return headers, project, user


@respx.mock
def test_the_phase_3_delivery_gate(client, master_headers, tenant, session, tmp_path, monkeypatch):
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_CAPTURE_WORKER_ENABLED", "true")
    get_settings.cache_clear()

    headers, project, user = _create_user_and_project(client, master_headers, tenant, session)

    # --- Local preprocessing: exactly what the Stop/PreCompact hook does ---
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(TRANSCRIPT)
    raw_slice = local.read_new_slice(transcript_path, 0)
    assert raw_slice.records, "fixture produced no complete records"
    batch = local.build_batch(raw_slice)
    sanitized = batch.content

    for canary in (SECRET_CANARY, FILE_CANARY, PATH_CANARY, "BEGIN RSA PRIVATE KEY"):
        assert canary not in sanitized, f"{canary!r} leaked into the sanitized slice"

    # The body is the local client's own model, filled from the local
    # client's own batch -- no field is supplied here that the hook does not
    # supply itself. `project_slug` used to be patched in at this line
    # because the hook never sent one and the route requires it; that repair
    # is what hid Phase 3 review finding 1, so the model is built for real.
    body = CheckpointSubmission(
        host="claude-code",
        session_id="sess-e2e",
        project_slug="acme-e2e",
        workspace_id=WS,
        start_offset=batch.start_offset,
        end_offset=batch.end_offset,
        content_hash=hashlib.sha256(batch.raw).hexdigest(),
        sanitized_hash=hashlib.sha256(sanitized.encode()).hexdigest(),
        content=sanitized,
    ).model_dump(exclude_none=True)

    # --- Submit twice: the second must be the identical row, not a new one ---
    first = client.post("/v1/capture/checkpoints", json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["duplicate"] is False
    capture_id = first.json()["capture_id"]

    second = client.post("/v1/capture/checkpoints", json=body, headers=headers)
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["capture_id"] == capture_id

    for canary in (SECRET_CANARY, FILE_CANARY, PATH_CANARY):
        assert canary not in json.dumps(first.json())
        assert canary not in json.dumps(second.json())

    # --- Mock Hindsight: extraction plus one retain per resolved bank ---
    extract_route = respx.post(
        f"{BASE}/v1/default/banks/{project.bank_id}/memories/dry-run-extract"
    ).mock(
        return_value=httpx.Response(
            200, json={"facts": [{"text": json.dumps(e)} for e in ENVELOPES]}
        )
    )
    user_bank_id = user.bank_id

    user_op_id = worker.filer.operation_id(capture_id, "user")
    project_op_id = worker.filer.operation_id(capture_id, "project")
    user_retain_route = respx.post(f"{BASE}/v1/default/banks/{user_bank_id}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": user_op_id})
    )
    project_retain_route = respx.post(
        f"{BASE}/v1/default/banks/{project.bank_id}/memories"
    ).mock(return_value=httpx.Response(200, json={"operation_id": project_op_id}))
    respx.get(f"{BASE}/v1/default/banks/{user_bank_id}/operations/{user_op_id}").mock(
        return_value=httpx.Response(200, json={"operation_id": user_op_id, "status": "completed"})
    )
    respx.get(f"{BASE}/v1/default/banks/{project.bank_id}/operations/{project_op_id}").mock(
        return_value=httpx.Response(
            200, json={"operation_id": project_op_id, "status": "completed"}
        )
    )

    # --- Drain the worker through every stage ---
    hindsight_client = HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")
    guard = 0
    while worker.run_once(session, hindsight_client) and guard < 20:
        guard += 1
    get_settings.cache_clear()

    assert extract_route.call_count == 1, "one semantic extraction, never re-run on replay"
    assert user_retain_route.call_count == 1
    assert project_retain_route.call_count == 1

    # --- Exact subject banks, kind, eligibility tags/scopes, negative wording ---
    user_payload = json.loads(user_retain_route.calls.last.request.read())
    project_payload = json.loads(project_retain_route.calls.last.request.read())
    assert user_payload["async"] is True
    assert project_payload["async"] is True

    user_items = {item["content"]: item for item in user_payload["items"]}
    project_items = {item["content"]: item for item in project_payload["items"]}

    assert user_items["Prefers tabs over spaces for indentation."]["tags"] == [
        "kind:preference", "profile_eligible",
    ]
    assert user_items["Prefers tabs over spaces for indentation."]["observation_scopes"] == [
        ["profile_eligible"]
    ]
    assert user_items["Prefers dark mode in the editor."]["tags"] == [
        "kind:preference", "profile_eligible",
    ]

    convention = project_items["CI runs ruff and pytest on every push."]
    assert convention["tags"] == ["kind:convention", "profile_eligible"]
    assert convention["metadata"]["provenance"] == {"type": "transcript", "start": 10, "end": 60}
    assert convention["metadata"]["origin"] == "observed"

    # Inferred evidence: recallable (filed) but never profile eligible.
    inferred = project_items["The service listens on port 8080."]
    assert inferred["tags"] == ["kind:technical_claim", "evidence_only"]
    assert inferred["observation_scopes"] == [["evidence_only"]]

    negative = project_items["Do not squash commits."]
    assert negative["metadata"]["negative"] is True
    assert negative["content"] == "Do not squash commits."  # negation and wording intact

    correction = project_items["The project uses ruff, not flake8."]
    assert correction["metadata"]["correction"] is True
    # The correction is confined to the project bank/scope: it must not
    # appear among the user bank's filed items at all.
    assert "The project uses ruff, not flake8." not in user_items

    for canary in (SECRET_CANARY, FILE_CANARY, PATH_CANARY):
        assert canary not in json.dumps(user_payload)
        assert canary not in json.dumps(project_payload)

    # --- One effective Working State advancement, visible in the next brief ---
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )
    full = client.get(
        "/v1/session-brief",
        params={"scope": "user", "project_slug": "acme-e2e", "workspace_id": WS, "tier": "full"},
        headers=headers,
    ).json()
    assert "objective: Ship the capture pipeline" in full["instructions"]
    assert "source session: sess-e2e (epoch" in full["instructions"]
    for canary in (SECRET_CANARY, FILE_CANARY, PATH_CANARY):
        assert canary not in full["instructions"]

    # --- Replaying the completed checkpoint files nothing again ---
    third = client.post("/v1/capture/checkpoints", json=body, headers=headers)
    assert third.status_code == 202
    assert third.json()["duplicate"] is True

    monkeypatch.setenv("MEMORY_CAPTURE_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    worker.run_once(session, hindsight_client)
    get_settings.cache_clear()

    assert extract_route.call_count == 1
    assert user_retain_route.call_count == 1
    assert project_retain_route.call_count == 1

    # --- Still production-disabled: no config PATCH route is mocked, so
    # any attempt anywhere above would have raised on the unmocked request
    # rather than silently succeeding. Confirmed here as a direct assertion
    # too, on the flags this whole scenario ran under. ---
    from memory.config import Settings

    assert Settings.model_fields["capture_worker_enabled"].default is False
    assert Settings.model_fields["capture_correction_refresh_enabled"].default is False
