import os
import uuid

import pytest

from memory.hindsight.client import HindsightClient

pytestmark = pytest.mark.integration

HINDSIGHT_URL = os.environ.get("MEMORY_HINDSIGHT_URL", "http://localhost:8888")


@pytest.fixture
def live_client() -> HindsightClient:
    return HindsightClient(
        base_url=HINDSIGHT_URL,
        api_key=os.environ.get("MEMORY_HINDSIGHT_API_KEY", ""),
        tenant_id="default",
    )


def test_retain_then_recall_round_trip(live_client):
    bank_id = f"user_{uuid.uuid4()}"
    live_client.ensure_bank(bank_id)

    live_client.retain(
        bank_id,
        "This project pins its Python dependencies with uv, never with pip.",
        is_async=False,
    )
    result = live_client.recall(bank_id, "how are Python dependencies managed here")

    assert "uv" in str(result).lower()
