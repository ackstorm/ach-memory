from memory.builtin_models import BUILTIN_MODELS, PROJECT_CONTEXT, USER_CONTEXT


def test_the_two_builtins_exist():
    assert BUILTIN_MODELS == (USER_CONTEXT, PROJECT_CONTEXT)


def test_builtin_keys_are_distinct():
    assert len({m.key for m in BUILTIN_MODELS}) == len(BUILTIN_MODELS)


def test_builtins_carry_the_schema_source_tag():
    for model in BUILTIN_MODELS:
        assert "schema:ach-retain-v1" in model.source_tags
