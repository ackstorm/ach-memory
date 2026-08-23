from memory.auth import keys


def test_generated_key_has_prefix_and_entropy():
    key = keys.generate_key()
    assert key.startswith("mem_")
    assert len(key) > 40


def test_generated_keys_are_unique():
    assert len({keys.generate_key() for _ in range(1000)}) == 1000


def test_hash_is_hex_sha256():
    digest = keys.hash_key("mem_abc")
    assert len(digest) == 64
    assert int(digest, 16) >= 0


def test_verify_accepts_the_right_key():
    key = keys.generate_key()
    assert keys.verify_key(key, keys.hash_key(key)) is True


def test_verify_rejects_a_different_key():
    stored = keys.hash_key(keys.generate_key())
    assert keys.verify_key(keys.generate_key(), stored) is False


def test_hash_never_equals_plaintext():
    key = keys.generate_key()
    assert keys.hash_key(key) != key
