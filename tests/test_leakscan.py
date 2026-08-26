"""The three scripts' leak scanners were three divergent regexes.

scripts/e2e.py's was \\b-anchored, so it could not see a bank id embedded in
a chunk_id (`project_<uuid>_doc7_3`) -- the exact shape of the leak that
already shipped once. It is also the scanner with the widest coverage (~70
scenarios, all 15 tools), so that anchor made the broadest gate the blindest
one (2026-08-23 review, R4-C2).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import leakscan

BANK = "project_ba378411-348d-4eb2-9c74-ef0c9da982cc"
USER_BANK = "user_ba378411-348d-4eb2-9c74-ef0c9da982cc"


def test_a_bare_bank_id_is_caught():
    assert leakscan.find(f'{{"x": "{BANK}"}}') is not None
    assert leakscan.find(f'{{"x": "{USER_BANK}"}}') is not None


def test_a_bank_id_embedded_in_a_chunk_id_is_caught():
    """The \\b anchor made this pass. A word character follows the final hex
    group, so there is no boundary to match."""
    assert leakscan.find(f'{{"chunk_id": "{BANK}_doc7_3"}}') is not None


def test_a_literal_bank_id_key_is_caught():
    assert leakscan.find('{"bank_id": "whatever"}') is not None


def test_the_internal_project_id_is_caught():
    """prj_ is Project.internal_id (SPEC inv. 34), asserted by
    tests/test_projects_api.py and tests/test_admin_api.py to never leave the
    API -- but e2e.py's comment claimed it was "meant to be visible"."""
    assert leakscan.find('{"x": "prj_67601bd645324bfebfd161eb411a802a"}') is not None


def test_the_exposed_ids_are_not_flagged():
    """usr_/grp_/key_ ARE meant to be visible; flagging them would make every
    successful provisioning response a false positive."""
    for exposed in ("usr_00c0f7", "grp_deadbeef", "key_cafebabe"):
        assert leakscan.find(f'{{"id": "{exposed}"}}') is None


def test_a_bank_fingerprint_is_not_a_bank_id():
    """sha256(bank_id)[:12] is what the activity trail stores in place of the
    bank id (SPEC inv. 29). It must not itself match the scanner -- if it did,
    every activity response would be a false positive and the gate would be
    switched off in frustration rather than fixed."""
    from memory.activity import fingerprint

    assert leakscan.find(fingerprint(BANK)) is None
    assert leakscan.find(fingerprint(USER_BANK)) is None


def test_the_activity_read_surfaces_leak_no_bank_id(client, master_headers, seeded_activity):
    """The two routes that expose per-call telemetry, held to the same gate as
    every other response in this service. /metrics is checked too: it is
    unauthenticated, so anything that reached its labels would be public."""
    for path in ("/metrics", "/v1/admin/activity", "/v1/admin/activity/summary"):
        assert leakscan.find(client.get(path, headers=master_headers).text) is None
