from memory.hindsight import paths


def test_the_upstream_tenant_segment_is_always_default():
    """hindsight-api 0.9.1 hardcodes /v1/default in all 83 bank routes; its
    own tenancy comes from the Authorization header. Deriving this segment
    from MEMORY_TENANT_ID made a plausible config value 404 every read and
    surface as DOCUMENT_NOT_FOUND (review finding I4)."""
    assert paths.bank("ignored-by-design", "user_abc") == "/v1/default/banks/user_abc"


def test_dry_run_extract_is_bank_scoped():
    assert (
        paths.dry_run_extract("ignored-by-design", "user_abc")
        == "/v1/default/banks/user_abc/memories/dry-run-extract"
    )


def test_config_is_bank_scoped():
    assert paths.config("ignored-by-design", "user_abc") == "/v1/default/banks/user_abc/config"
