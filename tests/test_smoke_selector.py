"""Regression tests for the memory selected by scripts/smoke.sh."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from select_curatable_memory import SelectionError, select_curatable_memory


class SmokeSelectorTests(unittest.TestCase):
    def test_runbook_selection_skips_observation_sibling(self):
        marker = "docs/runbooks/deploy.md"
        listing = {
            "result": {
                "items": [
                    {
                        "id": "observation-first",
                        "fact_type": "observation",
                        "content": "The deploy runbook lives in docs/runbooks/deploy.md.",
                    },
                    {
                        "id": "unrelated-world",
                        "fact_type": "world",
                        "content": "The deploy runbook lives in docs/runbooks/other.md.",
                    },
                    {
                        "id": "world-later",
                        "fact_type": "world",
                        "content": (
                            "The deploy runbook is located at "
                            "docs/runbooks/deploy.md. | When: 2026-08-24"
                        ),
                    },
                ]
            }
        }

        self.assertEqual(
            select_curatable_memory(
                listing, marker
            ),
            "world-later",
        )

    def test_unrelated_world_fact_does_not_match_runbook_marker(self):
        marker = "docs/runbooks/deploy.md"
        listing = {
            "result": {
                "items": [
                    {
                        "id": "unrelated-world",
                        "fact_type": "world",
                        "content": "The deploy runbook lives in docs/runbooks/other.md.",
                    }
                ]
            }
        }

        with self.assertRaises(SelectionError):
            select_curatable_memory(listing, marker)

    def test_runbook_selection_fails_when_no_curatable_match_exists(self):
        marker = "docs/runbooks/deploy.md"
        listing = {
            "result": {
                "memories": [
                    {
                        "id": "observation-only",
                        "fact_type": "observation",
                        "content": "The deploy runbook lives in docs/runbooks/deploy.md.",
                        "metadata": {"token": "do-not-print"},
                    }
                ]
            }
        }

        with self.assertRaisesRegex(SelectionError, "fact_type must be world or experience") as ctx:
            select_curatable_memory(listing, marker)
        self.assertIn("observation-only", str(ctx.exception))
        self.assertNotIn("do-not-print", str(ctx.exception))
