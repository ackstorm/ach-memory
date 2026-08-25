import pytest

from memory.auth import provisioning
from memory.models import User


def test_credential_id_is_stable_bounded_and_prefixed():
    a = provisioning.credential_id_for("https://ach.example.com", "alice@example.com")
    b = provisioning.credential_id_for("https://ach.example.com", "alice@example.com")
    assert a == b
    assert a.startswith("ext_")
    assert len(a) <= 64


def test_credential_id_cannot_be_confused_across_the_separator():
    """Concatenating issuer+subject without a separator made ("ab", "c") and
    ("a", "bc") the same credential -- one identity's rate-limit bucket and
    audit rows silently shared with another's."""
    assert provisioning.credential_id_for("ab", "c") != provisioning.credential_id_for(
        "a", "bc"
    )


def test_first_sight_creates_a_user_with_its_own_bank(session, tenant):
    user_id, credential_id = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    user = session.get(User, user_id)
    assert user is not None and user.bank_id
    assert credential_id.startswith("ext_")


def test_second_sight_returns_the_same_user(session, tenant):
    first = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    second = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    assert first == second
    assert session.query(User).count() == 1


def test_the_same_subject_from_another_issuer_is_another_user(session, tenant):
    alice_ach, _ = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    alice_dex, _ = provisioning.link_identity(
        session, issuer="https://auth.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    assert alice_ach != alice_dex


def test_an_identity_from_another_tenant_is_refused(session, tenant):
    from memory.errors import Unauthorized

    provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    with pytest.raises(Unauthorized):
        provisioning.link_identity(
            session, issuer="https://ach.example.com", subject="alice@example.com",
            tenant_id="other",
        )
