import uuid


def new_user_id() -> str:
    return f"usr_{uuid.uuid4().hex}"


def new_group_id() -> str:
    return f"grp_{uuid.uuid4().hex}"


def new_key_id() -> str:
    return f"key_{uuid.uuid4().hex}"


def new_user_bank_id() -> str:
    """Opaque bank ID. The prefix is a diagnostic hint and nothing more:
    it must never encode tenant, user, project or repository names (SPEC §4.7).
    """
    return f"user_{uuid.uuid4()}"


def new_project_bank_id() -> str:
    return f"project_{uuid.uuid4()}"


def new_project_internal_id() -> str:
    """Internal only. SPEC inv. 34: never required by an ordinary client."""
    return f"prj_{uuid.uuid4().hex}"


def new_audit_id() -> str:
    return f"aud_{uuid.uuid4().hex}"
