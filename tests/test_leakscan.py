"""scripts/leakscan.py's regex was previously \\b-anchored and could not see
a bank id embedded in a chunk_id (`project_<uuid>_doc7_3`)."""

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
    assert leakscan.find(f'{{"chunk_id": "{BANK}_doc7_3"}}') is not None


def test_a_literal_bank_id_key_is_caught():
    assert leakscan.find('{"bank_id": "whatever"}') is not None


def test_the_internal_project_id_is_caught():
    assert leakscan.find('{"x": "prj_67601bd645324bfebfd161eb411a802a"}') is not None


def test_the_exposed_ids_are_not_flagged():
    for exposed in ("usr_00c0f7", "grp_deadbeef", "key_cafebabe"):
        assert leakscan.find(f'{{"id": "{exposed}"}}') is None
