import hashlib
import re

from memory.errors import ProjectInvalidSlug

MAX_SLUG_LENGTH = 128

# scp-style remotes have no scheme and use a colon to separate host from path:
#   git@github.com:acme/payments-api.git
_SCP_STYLE = re.compile(r"^(?:[^/@]+@)?([^/:]+):(.+)$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_USERINFO = re.compile(r"^[^/@]+@")
_DOT_GIT = re.compile(r"\.git$")
# "." stays literal so derived slugs read as github.com-acme-payments-api-<digest>
# rather than github-com-... . It is safe where "/" is not: a dot is fine in a
# URL path segment, and unlike "-" it never doubles as the separator.
_NON_SLUG = re.compile(r"[^a-z0-9.]+")


def canonical_locator(remote_url: str) -> str:
    """One spelling for a Git remote: host/path, lowercase, no scheme or .git.

    The host is kept on purpose. Dropping it would make
    github.com/acme/payments-api and gitlab.com/customer/payments-api the same
    project, which is the collision SPEC §8.2 exists to prevent. The port is
    dropped for the opposite reason: ssh://git@host:22/acme/repo and
    https://host/acme/repo are the same repository and must not become two.
    """
    url = _DOT_GIT.sub("", remote_url.strip().rstrip("/"))

    if _SCHEME.match(url):
        url = _USERINFO.sub("", _SCHEME.sub("", url))
    else:
        scp = _SCP_STYLE.match(url)
        url = f"{scp.group(1)}/{scp.group(2)}" if scp else _USERINFO.sub("", url)

    host, _, path = url.partition("/")
    host = host.split(":", 1)[0]
    path = path.strip("/")
    if not host or not path:
        raise ProjectInvalidSlug(
            "a Git remote must name a host and a repository path"
        )
    return f"{host}/{path}".lower()


def normalize_slug(raw: str) -> str:
    """Lowercase alphanumerics, dots and hyphens. Idempotent.

    Deliberately lossy: it also normalizes human-supplied slugs like
    MEMORY_PROJECT=payments-api, where collapsing separators is what you want.
    A slug DERIVED from a Git remote must carry a digest (SPEC §8.2) precisely because this
    collapsing cannot tell a path separator from a literal hyphen.
    """
    slug = _NON_SLUG.sub("-", raw.strip().lower()).strip("-.")
    if not slug or len(slug) > MAX_SLUG_LENGTH or not any(c.isalnum() for c in slug):
        raise ProjectInvalidSlug(
            "a project slug must be 1 to "
            f"{MAX_SLUG_LENGTH} characters of letters, digits, dots and "
            "hyphens, and must contain at least one letter or digit"
        )
    return slug


def slug_from_locator(remote_url: str) -> str:
    """Reference implementation of SPEC §8.2. NOT called by this service.

    Deriving a slug from a Git remote is the CLIENT's job (§8.2, §10) and this
    repository ships no client, so nothing in `src/` calls this. It is kept,
    and tested, for one concrete reason: SPEC §8.2's worked examples are
    generated from it (`tests/test_slugs.py` asserts they still match), and
    they were WRONG before that guard existed -- the spec showed digest-free
    slugs, under which `acme/payments-api` and `acme-payments/api` collide
    into one memory bank, the exact failure §8.2 exists to prevent. A prose
    rule with no executable counterpart drifts; this is the counterpart.

    The whole locator flattened, never the repository basename (inv. 10).

    The digest suffix is not decoration. normalize_slug collapses `/`, `.` and
    `-` to one separator, so without it acme/payments-api and acme-payments/api
    would both become github-com-acme-payments-api — two unrelated repositories
    sharing one memory bank, which is the exact failure this function exists to
    prevent. The digest is taken over the canonical locator, so the same
    repository always yields the same slug however its remote is spelled.
    """
    locator = canonical_locator(remote_url)
    digest = hashlib.sha256(locator.encode("utf-8")).hexdigest()[:8]
    return normalize_slug(f"{locator}-{digest}")
