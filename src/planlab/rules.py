"""What makes a tree valid, and what blocks a claim.

These are policy: dependency resolution, cycle detection, the drift gate,
stale claims and stale verification markers. The store owns files and writes;
this module owns the rules those files have to satisfy.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from planlab import store as S
from planlab.errors import PlanlabError
from planlab.store import Plan, Problem

MID_CLAIM_MINUTES = 5

Report = dict[str, list[Problem]]


def transient_pairs(plans: list[Plan]) -> tuple[set[int], list[Plan]]:
    """(inodes mid-claim, plans stuck half-claimed).

    link-then-unlink leaves a file in both next/ and open/ for microseconds.
    A crash leaves it there forever, and with no age bound `validate` called
    that clean while `claim` refused it as a lost race until a human noticed.
    """
    shared = {p.inode for p in plans if p.state == "next"} & {
        p.inode for p in plans if p.state == "open"
    }
    fresh: set[int] = set()
    stuck: list[Plan] = []
    cutoff = time.time() - MID_CLAIM_MINUTES * 60
    for p in plans:
        if p.inode not in shared or p.state != "open":
            continue
        try:
            recent = os.stat(p.path).st_mtime > cutoff
        except OSError:
            recent = True
        if recent:
            fresh.add(p.inode)
        else:
            stuck.append(p)
    return fresh, stuck


def sync_cache(tree: str) -> dict[str, Any]:
    """The last `sync` result. Absent or corrupt reads as empty, never raises."""
    path = os.path.join(tree, S.SYNC_CACHE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


# ── dependency logic ──────────────────────────────────────────────────────


def drift_blockers(plans: list[Plan]) -> set[str]:
    """ids of done/ plans whose drift drafts are not themselves done yet."""
    blocked: set[str] = set()
    for p in plans:
        if p.id is None or p.state != "done" or p.get("verdict") not in S.DRIFT_VERDICTS:
            continue
        pending = [
            q
            for q in plans
            if str(q.get("source") or "") == f"verify:{p.id}" and q.state not in S.TERMINAL_STATES
        ]
        if pending:
            blocked.add(p.id)
    return blocked


def dep_problems(
    plan: Plan, index: dict[str, Plan], drifted: set[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """(errors, unmet) for one plan's depends_on."""
    errors: list[str] = []
    unmet: list[dict[str, str]] = []
    for raw in plan.list_of("depends_on"):
        dep = str(raw)
        target = index.get(dep)
        if target is None:
            errors.append(f"depends_on names an unknown id: {dep}")
        elif target.state == "discarded":
            errors.append(f"blocked by discarded {dep}: edit depends_on or discard")
        elif dep in drifted:
            errors.append(f"blocked by drift of {dep}")
        elif target.state != "done":
            unmet.append({"id": dep, "state": target.state})
    return errors, unmet


def find_cycles(index: dict[str, Plan]) -> list[list[str]]:
    colour: dict[str, int] = {}
    cycles: list[list[str]] = []

    def walk(pid: str, trail: list[str]) -> None:
        if colour.get(pid) == 1:
            cycles.append([*trail[trail.index(pid) :], pid])
            return
        if colour.get(pid) == 2:
            return
        colour[pid] = 1
        plan = index.get(pid)
        for dep in plan.list_of("depends_on") if plan else []:
            if str(dep) in index:
                walk(str(dep), [*trail, pid])
        colour[pid] = 2

    for pid in sorted(index):
        walk(pid, [])
    return cycles


# ── validate ──────────────────────────────────────────────────────────────


def _plan_errors(
    p: Plan, rel: str, mid_claim: set[int], errors: list[Problem], warnings: list[Problem]
) -> None:
    """The per-file checks: identity, forbidden keys, orphans, stale claims."""
    if not p.id:
        errors.append({"id": None, "path": rel, "message": f"{p.name}: no id"})
    if not p.get("title"):
        errors.append({"id": p.id, "path": rel, "message": f"{p.name}: no title"})
    if p.id and S.id_from_name(p.name) != p.id:
        errors.append(
            {
                "id": p.id,
                "path": rel,
                "message": f"{p.id}: id does not match the filename ({p.name}); "
                "one of them is wrong",
            }
        )
    if "status" in p.fm:
        errors.append(
            {
                "id": p.id,
                "path": rel,
                "message": f"{p.id}: status: is not a lifecycle field; state is the directory",
            }
        )
    if p.state == "open" and not p.get("claimed_by") and p.inode not in mid_claim:
        errors.append(
            {"id": p.id, "path": rel, "message": f"{p.id}: orphan — in open/ with no claimed_by"}
        )
    if p.state == "open" and stale_claim(p):
        warnings.append(
            {
                "id": p.id,
                "path": rel,
                "message": f"{p.id}: claim older than {S.STALE_CLAIM_HOURS}h — release it",
            }
        )


def validate(tree: str, scanned: tuple[list[Plan], list[Problem]] | None = None) -> Report:
    """`scanned` lets a caller that already walked the tree skip a second pass.

    status() does exactly that: without it, `status` opened every plan file
    twice per invocation and `index` three times.
    """
    plans, problems = scanned if scanned is not None else S.scan(tree)
    index = S.by_id(plans)
    errors: list[Problem] = []
    warnings: list[Problem] = []
    try:
        S.load_config(tree)
    except PlanlabError as exc:
        errors.append({"id": None, "path": S.CONFIG_NAME, "message": str(exc)})
    # Between the link and the unlink a claim leaves the file in both next/
    # and open/. That pair is one plan mid-flight, not an orphan.
    mid_claim, stuck = transient_pairs(plans)
    for pair in stuck:
        errors.append(
            {
                "id": pair.id,
                "path": f"{pair.state}/{pair.name}",
                "message": f"{pair.id}: half-claimed — the same file is in next/ "
                f"and open/ and has been for over {MID_CLAIM_MINUTES} minutes; "
                "an interrupted claim. Delete one copy.",
            }
        )

    for p in plans:
        rel = f"{p.state}/{p.name}"
        if p.error:
            errors.append(
                {
                    "id": None,
                    "path": rel,
                    "message": f"{p.name}: frontmatter did not parse ({p.error})",
                }
            )
            continue
        _plan_errors(p, rel, mid_claim, errors, warnings)

    for pid, age in stale_markers(tree):
        warnings.append(
            {
                "id": pid,
                "path": f"open/{S.VERIFY_MARKER}{pid}",
                "message": f"{pid}: stale verification claim, {age:.0f}h old — release it",
            }
        )

    for p, prev in S.duplicates(plans):
        errors.append(
            {
                "id": p.id,
                "path": f"{p.state}/{p.name}",
                "message": f"duplicate id {p.id} (also {prev.state}/{prev.name})",
            }
        )

    drifted = drift_blockers(plans)
    for p in plans:
        if p.error or not p.id:
            continue
        errs, _ = dep_problems(p, index, drifted)
        for msg in errs:
            errors.append({"id": p.id, "path": f"{p.state}/{p.name}", "message": f"{p.id}: {msg}"})

    for cycle in find_cycles(index):
        errors.append(
            {"id": cycle[0], "path": None, "message": f"dependency cycle: {' -> '.join(cycle)}"}
        )

    return {"errors": errors, "warnings": warnings, "problems": problems}


def stale_markers(tree: str, hours: float = 2) -> list[tuple[str, float]]:
    """(id, age_hours) for verification markers nobody cleared."""
    out: list[tuple[str, float]] = []
    d = os.path.join(tree, "open")
    for name in sorted(os.listdir(d) if os.path.isdir(d) else []):
        if not name.startswith(S.VERIFY_MARKER):
            continue
        full = os.path.join(d, name)
        try:
            with open(full) as fh:
                when = S.as_naive_utc(fh.read().split()[0])
            age = (S.utc_now() - when).total_seconds() / 3600.0
        except (OSError, ValueError, IndexError):
            age = (time.time() - os.stat(full).st_mtime) / 3600.0
        if age > hours:
            out.append((name[len(S.VERIFY_MARKER) :], age))
    return out


# ── claims ────────────────────────────────────────────────────────────────


def stale_claim(plan: Plan, hours: float = S.STALE_CLAIM_HOURS) -> bool:
    """A claim nobody released. Claims never touch it; the operator does."""
    raw = plan.get("claimed_at")
    if not raw:
        return False
    try:
        when = S.as_naive_utc(raw)
    except ValueError:
        return False
    return (S.utc_now() - when).total_seconds() > hours * 3600
