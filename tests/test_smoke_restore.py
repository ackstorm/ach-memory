"""Regression tests for restore active-set verification in scripts/smoke.sh."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_active_memory import ActiveSetError, verify_memory_active

MEMORY_ID = "world-later"


class SmokeRestoreTests(unittest.TestCase):
    def test_http_200_noop_restore_is_rejected_when_memory_stays_absent(self):
        after_noop_restore = {"result": {"items": []}}

        with self.assertRaisesRegex(ActiveSetError, "not in the active set"):
            verify_memory_active(after_noop_restore, MEMORY_ID)

    def test_restore_is_verified_when_memory_returns_to_active_set(self):
        after_restore = {
            "result": {
                "memories": [
                    {"id": MEMORY_ID, "fact_type": "world"},
                ]
            }
        }

        self.assertIsNone(verify_memory_active(after_restore, MEMORY_ID))
