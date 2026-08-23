import pytest

from memory import slugs
from memory.errors import ProjectInvalidSlug


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:acme/payments-api.git",
        "https://github.com/acme/payments-api.git",
        "https://github.com/acme/payments-api",
        "ssh://git@github.com/acme/payments-api.git",
        "https://github.com/acme/payments-api/",
    ],
)
def test_every_remote_spelling_yields_one_locator(remote):
    assert slugs.canonical_locator(remote) == "github.com/acme/payments-api"


def test_locator_keeps_the_host_so_forges_do_not_collide():
    assert slugs.canonical_locator(
        "https://gitlab.com/customer/payments-api"
    ) == "gitlab.com/customer/payments-api"


def test_slug_flattens_the_whole_locator():
    assert slugs.slug_from_locator("git@github.com:acme/payments-api.git").startswith(
        "github.com-acme-payments-api-"
    )


def test_the_same_repository_always_yields_the_same_slug():
    spellings = [
        "git@github.com:acme/payments-api.git",
        "https://github.com/acme/payments-api",
        "ssh://git@github.com:22/acme/payments-api.git",
    ]
    assert len({slugs.slug_from_locator(s) for s in spellings}) == 1


def test_dots_survive_so_the_host_stays_readable():
    assert slugs.slug_from_locator("https://github.com/acme/api").startswith(
        "github.com-acme-api-"
    )


def test_differently_segmented_locators_do_not_collide():
    """The collision this module exists to prevent.

    normalize_slug collapses `/` and `-` to one separator, so without a
    disambiguator these two unrelated repositories would share a memory bank.
    """
    a = slugs.slug_from_locator("https://github.com/acme/payments-api")
    b = slugs.slug_from_locator("https://github.com/acme-payments/api")
    assert a != b


def test_nested_group_paths_are_kept_distinct():
    a = slugs.slug_from_locator("https://gitlab.com/group/subgroup/repo")
    b = slugs.slug_from_locator("https://gitlab.com/group-subgroup/repo")
    assert a != b


@pytest.mark.parametrize(
    "remote", ["https://github.com", "git@github.com:", "github.com"]
)
def test_a_remote_without_a_repository_path_is_rejected(remote):
    with pytest.raises(ProjectInvalidSlug):
        slugs.canonical_locator(remote)


def test_two_forges_with_the_same_repository_name_do_not_collide():
    """The reason the slug is not the basename (SPEC §8.2)."""
    a = slugs.slug_from_locator("https://github.com/acme/payments-api")
    b = slugs.slug_from_locator("https://gitlab.com/customer/payments-api")
    assert a != b


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("payments-api", "payments-api"),
        ("Payments API", "payments-api"),
        ("  padded  ", "padded"),
        ("under_scores", "under-scores"),
        ("dots.and.dashes", "dots.and.dashes"),
        ("multiple///separators", "multiple-separators"),
        ("--leading-and-trailing--", "leading-and-trailing"),
    ],
)
def test_normalize_slug(raw, expected):
    assert slugs.normalize_slug(raw) == expected


def test_normalize_slug_is_idempotent():
    once = slugs.normalize_slug("Payments API")
    assert slugs.normalize_slug(once) == once


@pytest.mark.parametrize("raw", ["", "   ", "---", "***", "...", ".-.", "x" * 200])
def test_unusable_slug_is_rejected(raw):
    with pytest.raises(ProjectInvalidSlug):
        slugs.normalize_slug(raw)


def test_the_spec_worked_examples_match_this_implementation():
    """SPEC §8.2's example block is generated from `slug_from_locator`, and
    nothing kept them in step before this test.

    They had already drifted: §8.2 showed digest-free slugs
    (`github-com-acme-payments-api`), under which `acme/payments-api` and
    `acme-payments/api` derive the SAME slug -- two unrelated repositories
    sharing one memory bank, which is the precise failure that section exists
    to prevent. §10 makes derivation the client's job, so a wrapper author
    following the spec literally would have shipped that collision.
    """
    import re
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[1] / "SPEC-v1.md").read_text()
    section = re.search(r"### 8\.2 Git-derived slug\n(.*?)\n### 8\.3", spec, re.DOTALL)
    assert section, "SPEC §8.2 heading moved -- update this guard"
    block = re.search(r"```text\n(git@github\.com.*?)```", section.group(1), re.DOTALL)
    assert block, "SPEC §8.2's worked-example block moved -- update this guard"

    pairs = [
        tuple(part.strip() for part in line.split("->"))
        for line in block.group(1).strip().splitlines()
        if "->" in line
    ]
    assert pairs, "no worked examples found in SPEC §8.2"
    for url, documented in pairs:
        assert slugs.slug_from_locator(url) == documented, url


def test_flattening_without_a_digest_would_collide():
    """The reason the digest is normative in §8.2, pinned so nobody 'simplifies'
    it away: these two unrelated repositories flatten to one slug without it."""
    a = "https://github.com/acme/payments-api"
    b = "https://github.com/acme-payments/api"
    flattened_a = slugs.normalize_slug(slugs.canonical_locator(a))
    flattened_b = slugs.normalize_slug(slugs.canonical_locator(b))
    assert flattened_a == flattened_b, "premise changed: flattening no longer collides"
    assert slugs.slug_from_locator(a) != slugs.slug_from_locator(b)
