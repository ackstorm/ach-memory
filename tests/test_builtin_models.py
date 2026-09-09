import pytest

from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT
from memory.mental_model_service import (
    PROJECT_ALWAYS_IN_CONTEXT_BUDGET,
    USER_ALWAYS_IN_CONTEXT_BUDGET,
)


@pytest.mark.parametrize(
    ("definition", "limit"),
    [
        (USER_CONTEXT, USER_ALWAYS_IN_CONTEXT_BUDGET),
        (PROJECT_CONTEXT, PROJECT_ALWAYS_IN_CONTEXT_BUDGET),
    ],
)
def test_a_builtin_definition_fits_its_scope_delivery_budget(definition, limit):
    """Every built-in is standing by definition, so its max_tokens is spent
    on every context load. This is the only place the budget can now be
    exceeded -- by us, raising a definition's max_tokens, not by a caller."""
    assert definition.max_tokens <= limit


def test_user_builtin_is_frozen_and_out_of_custom_namespace():
    assert USER_CONTEXT.key == "user-context"
    assert USER_CONTEXT.version == 3
    assert USER_CONTEXT.max_tokens == 2048
    assert USER_CONTEXT.source_tags == ("schema:ach-retain-v1", "validity:indefinite")
    assert USER_CONTEXT.tags_match == "all_strict"


def test_project_builtin_is_frozen():
    assert PROJECT_CONTEXT.key == "project-context"
    assert PROJECT_CONTEXT.version == 3
    assert PROJECT_CONTEXT.max_tokens == 2048
    assert "cheaply rediscoverable" in PROJECT_CONTEXT.source_query
    assert PROJECT_CONTEXT.tags_match == "all_strict"
