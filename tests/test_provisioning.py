from memory.auth import provisioning
from memory.models import ExternalIdentity, User


def test_two_identities_never_share_a_minted_user_id():
    """An issuer URL is full of colons, so a ":" join would make ("a:b", "c")
    and ("a", "b:c") one user id -- and the loser would then fail the users PK
    on every provisioning attempt, forever."""
    assert provisioning._user_id_for("a:b", "c") != provisioning._user_id_for("a", "b:c")
    assert provisioning._user_id_for("https://idp.test", "bob:alice") != provisioning._user_id_for(
        "https://idp.test:bob", "alice"
    )


def test_an_existing_identity_is_looked_up_never_rehashed(db):
    """The hash only mints; `(issuer, subject)` is the lookup key. A user whose
    id does not match today's hash must still resolve to itself -- this is what
    makes changing how the id is minted safe for users already provisioned."""
    db.add(User(id="usr_minted_by_an_older_rule"))
    db.flush()
    db.add(ExternalIdentity(issuer="https://idp.test", subject="alice",
                            user_id="usr_minted_by_an_older_rule"))
    db.flush()

    user = provisioning.link_identity(db, issuer="https://idp.test", subject="alice")

    assert user.id == "usr_minted_by_an_older_rule"
    assert user.id != provisioning._user_id_for("https://idp.test", "alice")


def test_first_sight_creates_a_user_with_its_own_deterministic_bank(db):
    user = provisioning.link_identity(
        db, issuer="https://ach.example.com", subject="alice@example.com"
    )
    assert user.id.startswith("usr_")

    identity = db.get(ExternalIdentity, ("https://ach.example.com", "alice@example.com"))
    assert identity is not None
    assert identity.user_id == user.id


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
