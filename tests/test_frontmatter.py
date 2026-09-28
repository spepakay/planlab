"""The frontmatter codec: pure functions, tested without touching the disk."""

from __future__ import annotations

from typing import Any

import pytest

from planlab.errors import FrontmatterError
from planlab.frontmatter import parse_frontmatter, raw_field, render, split_frontmatter


def round_trip(raw: str) -> tuple[dict[str, Any], str, str]:
    fm_text, body = split_frontmatter(raw)
    fm = parse_frontmatter(fm_text)
    return fm, body, render(fm, body)


def test_real_world_shapes_round_trip() -> None:
    raw = (
        "---\n"
        'pr: ["#8"]\n'
        "created: 2026-07-11\n"
        "status: 'VALIDATED — stage refresh consolidated; PR #12 closed unmerged'\n"
        "related:\n"
        "  - notes/plans/2026-08-25-launch\n"
        "  - notes/reference/2026-09-01-thing\n"
        "---\n"
        "# Title\n\nBody text.\n"
    )
    fm, body, out = round_trip(raw)
    assert fm["pr"] == ["#8"]
    assert str(fm["created"]) == "2026-07-11"
    assert "PR #12 closed unmerged" in fm["status"]
    assert len(fm["related"]) == 2
    fm2, body2 = split_frontmatter(out)
    assert parse_frontmatter(fm2)["pr"] == ["#8"]
    assert body2 == body


def test_an_unquoted_hash_truncates_the_scalar_as_yaml_says_it_does() -> None:
    # Pinned so nobody reads the loss as a bug in the reader: ` #` opens a
    # comment in a plain YAML scalar. raw_field reads the line as written,
    # which is how a write notices it would drop text.
    raw = "---\nstatus: VALIDATED; PR #12 closed unmerged\n---\nb\n"
    fm_text = split_frontmatter(raw)[0]
    assert parse_frontmatter(fm_text)["status"] == "VALIDATED; PR"
    assert raw_field(fm_text, "status") == "VALIDATED; PR #12 closed unmerged"


def test_no_frontmatter_is_not_an_error() -> None:
    raw = "# Just a heading\n\nNo frontmatter at all.\n"
    fm_text, body = split_frontmatter(raw)
    assert fm_text is None
    assert body == raw
    assert parse_frontmatter(fm_text) == {}


@pytest.mark.parametrize(
    ("raw", "body"),
    [
        ("---\nid: x\n---\r\nline one\r\nline two\r\n", "line one\r\nline two\r\n"),
        ("---\nid: x\n---\nno trailing newline", "no trailing newline"),
    ],
    ids=["crlf", "no-final-newline"],
)
def test_body_is_byte_identical(raw: str, body: str) -> None:
    _, got, out = round_trip(raw)
    assert got == body
    assert out.endswith(body)


def test_title_with_colon_is_quoted_and_reads_back_equal() -> None:
    out = render({"id": "x", "title": "Phase 1: the state machine"}, "\nbody\n")
    assert "title: 'Phase 1: the state machine'" in out
    assert parse_frontmatter(split_frontmatter(out)[0])["title"] == "Phase 1: the state machine"


def test_canonical_key_order_unknown_keys_keep_original_order() -> None:
    raw = "---\nzeta: 1\nprs: [8]\nalpha: 2\ntitle: T\nid: x\n---\nbody\n"
    _, _, out = round_trip(raw)
    block = split_frontmatter(out)[0] or ""
    keys = [
        line.split(":")[0]
        for line in block.splitlines()
        if line and not line.startswith(("-", " "))
    ]
    assert keys == ["id", "title", "prs", "zeta", "alpha"]


def test_parse_failure_is_raised_as_a_frontmatter_error() -> None:
    fm_text, _ = split_frontmatter("---\ndescription: a: b\n---\nbody\n")
    with pytest.raises(FrontmatterError):
        parse_frontmatter(fm_text)
