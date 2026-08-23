import re

from memory import ids


def test_user_id_shape():
    assert re.fullmatch(r"usr_[0-9a-f]{32}", ids.new_user_id())


def test_key_id_shape():
    assert re.fullmatch(r"key_[0-9a-f]{32}", ids.new_key_id())


def test_bank_ids_carry_only_a_type_prefix():
    assert re.fullmatch(
        r"user_[0-9a-f-]{36}", ids.new_user_bank_id()
    )
    assert re.fullmatch(
        r"project_[0-9a-f-]{36}", ids.new_project_bank_id()
    )


def test_ids_are_unique():
    generated = {ids.new_user_bank_id() for _ in range(1000)}
    assert len(generated) == 1000
