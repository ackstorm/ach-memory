from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT


def test_user_builtin_is_frozen_and_out_of_custom_namespace():
    assert USER_CONTEXT.key == "user-context"
    assert USER_CONTEXT.version == 2
    assert USER_CONTEXT.max_tokens == 2048
    assert USER_CONTEXT.source_tags == ("schema:ach-retain-v1", "validity:indefinite")
    assert USER_CONTEXT.always_in_context is True


def test_project_builtin_is_frozen():
    assert PROJECT_CONTEXT.key == "project-context"
    assert PROJECT_CONTEXT.version == 2
    assert PROJECT_CONTEXT.max_tokens == 2048
    assert "cheaply rediscoverable" in PROJECT_CONTEXT.source_query
