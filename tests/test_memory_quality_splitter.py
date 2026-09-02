import json

import pytest
from pydantic import ValidationError

from experiments.memory_quality.contracts import (
    BankSnapshot,
    RetainReceipt,
    SemanticCase,
    SnapshotObject,
)
from experiments.memory_quality.splitter import SplitEnvelope, split_and_persist


@pytest.mark.parametrize(
    "payload",
    (
        {
            "destination": "durable_user",
            "subject": "user",
            "text": "concise replies",
            "current": True,
            "extra": "forbidden",
        },
        {
            "destination": "evidence",
            "subject": None,
            "text": "repository output",
            "current": False,
        },
        {
            "destination": "durable_user",
            "subject": "project",
            "text": "wrong scope",
            "current": True,
        },
    ),
)
def test_split_envelope_rejects_invalid_contracts(payload):
    with pytest.raises(ValidationError):
        SplitEnvelope.model_validate(payload)


def test_splitter_rejects_malformed_outer_facts():
    case = SemanticCase(
        id="S99",
        transcript=({"role": "user", "text": "safe"},),
        expected_units=(),
        secret_canaries=("SECRET",),
    )

    class Banks:
        def create_bank(self, purpose, repetition):
            return purpose

        def dry_run_extract(self, bank_id, content, **options):
            return {"facts": "not-a-list"}

    with pytest.raises(TypeError, match="facts"):
        split_and_persist(case, 1, Banks())


def test_splitter_rejects_more_than_one_working_state():
    facts = [
        {
            "text": json.dumps(
                {
                    "destination": "working_state",
                    "subject": None,
                    "text": value,
                    "current": True,
                }
            )
        }
        for value in ("first", "second")
    ]

    with pytest.raises(ValueError, match="working state"):
        split_and_persist(_case(), 1, _FakeBanks(facts))


def test_splitter_persists_only_exact_subject_projections():
    facts = [
        _fact("durable_user", "user", "concise replies", True),
        _fact("durable_project", "project", "use JSONL", True),
        _fact("evidence", "user", "personal evidence", False),
        _fact("evidence", "project", "project evidence", False),
        _fact("working_state", None, "run the delivery gate", True),
        _fact("discard", None, "transient chatter", False),
    ]
    banks = _FakeBanks(facts)

    result = split_and_persist(_case(), 1, banks)

    assert len(banks.created) == 2
    user_bank, project_bank = (item[0] for item in banks.created)
    assert [call[1] for call in banks.retains if call[0] == user_bank] == [
        "concise replies",
        "personal evidence",
    ]
    assert [call[1] for call in banks.retains if call[0] == project_bank] == [
        "use JSONL",
        "project evidence",
    ]
    assert all(call[3] == "verbatim" for call in banks.retains)
    assert all("I prefer concise replies; this project uses JSONL." not in call[1] for call in banks.retains)
    assert result.output.working_state == {"objective": "run the delivery gate"}
    assert result.user_bank_scope_clean is True
    assert result.project_bank_scope_clean is True
    assert result.no_shared_document is True


def test_splitter_derives_failed_isolation_from_bank_documents():
    banks = _FakeBanks([_fact("durable_user", "user", "concise replies", True)])
    banks.injected_document = "foreign project material"

    result = split_and_persist(_case(), 1, banks)

    assert result.user_bank_scope_clean is False
    assert result.no_shared_document is False


def _case() -> SemanticCase:
    return SemanticCase(
        id="S99",
        transcript=(
            {
                "role": "user",
                "text": "I prefer concise replies; this project uses JSONL.",
            },
        ),
        expected_units=(),
        secret_canaries=("SECRET",),
    )


def _fact(destination, subject, text, current):
    return {
        "text": json.dumps(
            {
                "destination": destination,
                "subject": subject,
                "text": text,
                "current": current,
            }
        )
    }


class _FakeBanks:
    def __init__(self, facts):
        self.facts = facts
        self.created = []
        self.retains = []
        self.persisted = {}
        self.injected_document = None

    def create_bank(self, purpose, repetition):
        bank = f"{purpose}-{repetition}"
        self.created.append((bank, purpose))
        self.persisted[bank] = []
        return bank

    def dry_run_extract(self, bank_id, content, **options):
        return {"facts": self.facts}

    def retain_and_wait(self, bank_id, content, *, document_id, strategy):
        self.retains.append((bank_id, content, document_id, strategy))
        self.persisted[bank_id].append(content)
        return RetainReceipt(
            document_id=document_id,
            operation_id=f"op-{document_id}",
            terminal_state="completed",
            duration_ms=1,
        )

    def list_bank_objects(self, bank_id):
        values = list(self.persisted[bank_id])
        if self.injected_document is not None and "hybrid-user" in bank_id:
            values.append(self.injected_document)
        return BankSnapshot(
            bank_id=bank_id,
            objects=tuple(
                SnapshotObject(
                    layer="document",
                    object_id=f"doc-{index}",
                    text="",
                    original_text=content,
                )
                for index, content in enumerate(values)
            ),
        )
