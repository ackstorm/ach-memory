from memory.hindsight import paths


def test_the_upstream_tenant_segment_is_always_default():
    """hindsight-api 0.9.1 hardcodes /v1/default in all 83 bank routes; its
    own tenancy comes from the Authorization header. Deriving this segment
    from MEMORY_TENANT_ID made a plausible config value 404 every read and
    surface as DOCUMENT_NOT_FOUND (review finding I4)."""
    assert paths.bank("ignored-by-design", "user_abc") == "/v1/default/banks/user_abc"
