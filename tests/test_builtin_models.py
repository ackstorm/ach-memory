from memory.builtin_models import USER_CONTEXT_V1, PROJECT_CONTEXT_V1


def test_user_builtin_is_frozen_and_out_of_custom_namespace():
    assert USER_CONTEXT_V1.key == "user-context"
    assert USER_CONTEXT_V1.version == 1
    assert USER_CONTEXT_V1.max_tokens == 512
    assert USER_CONTEXT_V1.source_tags == ("schema:ach-retain-v1", "validity:indefinite")
    assert USER_CONTEXT_V1.always_in_context is True


def test_project_builtin_is_frozen():
    assert PROJECT_CONTEXT_V1.key == "project-context"
    assert PROJECT_CONTEXT_V1.version == 1
    assert PROJECT_CONTEXT_V1.max_tokens == 1024
    assert "cheaply rediscoverable" in PROJECT_CONTEXT_V1.source_query
