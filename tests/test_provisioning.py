from memory.auth import provisioning
from memory.models import ExternalIdentity


def test_credential_id_is_stable_bounded_and_prefixed():
    a = provisioning.credential_id_for("https://ach.example.com", "alice@example.com")
    b = provisioning.credential_id_for("https://ach.example.com", "alice@example.com")
    assert a == b
    assert a.startswith("ext_")
    assert len(a) <= 64


def test_credential_id_cannot_be_confused_across_the_separator():
    """Concatenating issuer+subject without a separator would make ("ab", "c")
    and ("a", "bc") the same credential."""
    assert provisioning.credential_id_for("ab", "c") != provisioning.credential_id_for(
        "a", "bc"
    )


def test_first_sight_creates_a_user_with_its_own_deterministic_bank(db):
    user = provisioning.link_identity(
        db, issuer="https://ach.example.com", subject="alice@example.com"
    )
    assert user.id.startswith("usr_")
    assert user.bank_id == f"user_{user.id}"

    identity = db.get(ExternalIdentity, ("https://ach.example.com", "alice@example.com"))
    assert identity is not None
    assert identity.user_id == user.id
    assert identity.credential_id.startswith("ext_")


def test_second_sight_returns_the_same_user(db):
    first = provisioning.link_identity(
        db, issuer="https://ach.example.com", subject="alice@example.com"
    )
    second = provisioning.link_identity(
        db, issuer="https://ach.example.com", subject="alice@example.com"
    )
    assert first.id == second.id
    assert db.query(ExternalIdentity).count() == 1


def test_the_same_subject_from_another_issuer_is_another_user(db):
    from_ach = provisioning.link_identity(
        db, issuer="https://ach.example.com", subject="alice@example.com"
    )
    from_dex = provisioning.link_identity(
        db, issuer="https://auth.example.com", subject="alice@example.com"
    )
    assert from_ach.id != from_dex.id


def test_the_user_id_is_deterministic_across_sessions(db):
    """Pinned because the id has to be reproducible without a row lookup:
    Principal.user_id must equal it on every subsequent authentication, not
    just the one that minted the row."""
    user = provisioning.link_identity(
        db, issuer="https://ach.example.com", subject="alice@example.com"
    )
    assert user.id == provisioning._user_id_for("https://ach.example.com", "alice@example.com")
