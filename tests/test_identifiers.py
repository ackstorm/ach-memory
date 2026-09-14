import pytest

from memory.identifiers import has_control_character


@pytest.mark.parametrize("bad", ["a\x00b", "ping\x07pong", "red\x1bpop", "del\x7f"])
def test_control_characters_are_detected(bad):
    assert has_control_character(bad)


@pytest.mark.parametrize("ok", ["", "plain text", "punctuation: it's fine!"])
def test_ordinary_text_is_not_flagged(ok):
    assert not has_control_character(ok)
