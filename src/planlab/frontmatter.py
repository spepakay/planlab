"""The frontmatter codec: split a plan file, parse it, render it back.

Pure functions, no file I/O. The store reads and writes files; this module
only turns text into a mapping and back, byte for byte where it can and loudly
where it cannot.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from planlab.errors import FrontmatterError

Frontmatter = dict[str, Any]

# Emitted in this order; anything else keeps its original order after them.
CANONICAL_KEYS = (
    "id", "title", "type", "date", "updated", "base",
    "depends_on", "revisit", "priority", "size", "owner", "source", "related",
    "supersedes", "artifacts", "why", "claimed_by", "claimed_at",
    "branch", "prs", "verified", "verdict", "worked_by",
)  # fmt: skip

_OPEN_RE = re.compile(r"\A---[ \t]*\r?\n")
_CLOSE_RE = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.M)


def split_frontmatter(raw: str) -> tuple[str | None, str]:
    """(frontmatter_text, body). Body is the remainder, byte for byte."""
    m = _OPEN_RE.match(raw)
    if not m:
        return None, raw
    close = _CLOSE_RE.search(raw, m.end())
    if not close:
        return None, raw
    return raw[m.end() : close.start()], raw[close.end() :]


def parse_frontmatter(text: str | None) -> Frontmatter:
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FrontmatterError(str(exc).splitlines()[0]) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a mapping")
    return data


def raw_field(text: str | None, key: str) -> str | None:
    """The scalar exactly as written, for values YAML would not round-trip.

    A ` #` opens a comment in a plain scalar, so `status: X; PR #12 ...`
    parses to `X; PR`. Reading the line rather than the parsed value is how
    `lossy_keys` notices that a write would drop text.
    """
    for line in (text or "").splitlines():
        m = re.match(rf"^{re.escape(key)}:[ \t]*(.*)$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def lossy_keys(fm_text: str | None, fm: Frontmatter) -> list[str]:
    """Keys whose written text will not survive a parse/dump round trip.

    An unquoted ` #` opens a comment in a plain YAML scalar, so
    `note: see issue #14` parses to `see issue`. Reading is harmless; WRITING
    that back is silent data loss on the ordinary claim path, so every write
    refuses until the operator quotes the value.
    """
    out = []
    for key, value in (fm or {}).items():
        if not isinstance(value, str):
            continue
        raw = raw_field(fm_text, key)
        if raw is not None and raw != value and raw.startswith(value):
            out.append(key)
    return out


def render(fm: Frontmatter, body: str) -> str:
    ordered = {k: fm[k] for k in CANONICAL_KEYS if k in fm}
    for k, v in fm.items():
        if k not in ordered:
            ordered[k] = v
    text = yaml.safe_dump(
        ordered,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10**6,
    )
    return "---\n" + text + "---\n" + body
