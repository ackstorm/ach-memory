from memory.hindsight import paths


def test_the_upstream_tenant_segment_is_always_default():
    """hindsight-api 0.9.1 hardcodes /v1/default in all 83 bank routes; its
    own tenancy comes from the Authorization header. Deriving this segment
    from MEMORY_TENANT_ID made a plausible config value 404 every read and
    surface as DOCUMENT_NOT_FOUND (review finding I4) -- which is why these
    helpers take no tenant argument at all."""
    assert paths.bank("user_abc") == "/v1/default/banks/user_abc"


def test_config_is_bank_scoped():
    assert paths.config("user_abc") == "/v1/default/banks/user_abc/config"
