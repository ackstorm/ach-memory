from memory.builtin_models import BRIEFING, BUILTIN_MODELS, DECISIONS, HOW_WE_WORK, TRAPS


def test_builtins_are_the_briefing_and_three_on_demand_models():
    assert BUILTIN_MODELS == (BRIEFING, TRAPS, DECISIONS, HOW_WE_WORK)
    assert BRIEFING.load_when is None
    assert all(m.load_when for m in (TRAPS, DECISIONS, HOW_WE_WORK))


def test_builtins_are_project_models_with_unique_keys():
    assert {m.scope for m in BUILTIN_MODELS} == {"project"}
    assert len({m.key for m in BUILTIN_MODELS}) == len(BUILTIN_MODELS)


def test_each_builtin_reads_only_its_memory_types():
    assert BRIEFING.source_tags == ("type:briefing",)
    assert TRAPS.source_tags == ("type:gotcha",)
    assert set(DECISIONS.source_tags) == {"type:decision", "type:constraint", "type:preference"}
    assert set(HOW_WE_WORK.source_tags) == {"type:convention", "type:fact"}
    assert all(m.tags_match == "any_strict" for m in BUILTIN_MODELS)


def test_every_prompt_asks_recall_for_12000_tokens():
    # Hindsight 1.2.1's recall tool defaults to 2,048 tokens and the refresh agent passes that
    # itself, ignoring trigger.recall_max_tokens: only the prompt reaches it.
    for model in BUILTIN_MODELS:
        assert "max_tokens 12000" in model.prompt


def test_the_briefing_keeps_each_note_in_its_own_words():
    # A rewrite once turned "judged only in production: e2e uses a mock LLM" into its opposite.
    assert "keep each note's own wording" in BRIEFING.prompt


def test_no_prompt_drops_the_preferences_decisions_reads():
    assert all("personal preferences of the user" not in m.prompt for m in BUILTIN_MODELS)
