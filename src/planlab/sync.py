"""Everything that talks to GitHub.

One repository-wide PR list per run, indexed by number and by head branch;
`gh pr view` only for a number that list lacks. Every call runs as
`GH_TOKEN=$(gh auth token -u <gh_user>) gh … -R <origin>`, because
`gh auth switch` writes one shared hosts.yml and another process can flip the
active account mid-run — under the wrong account a private repository is a 404,
so `-R` alone is not enough.

Stages come from each PR's base ref, in a two-branch promotion flow (`base`,
then `terminal`): a branch is in review while its PR into `base` is open,
validating once that merged, promoting while its PR into `terminal` is open,
landed once that merged. A plan is ready to verify when every branch it names
has landed.

A `gh` failure is never guessed past: that plan is `unknown`, its cache entry is
left exactly as the last good sync wrote it, and the command exits non-zero.
Every other plan still gets a fresh entry — otherwise one dead PR number would
freeze `synced_at` and `move done` would refuse the whole tree as stale.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any

from planlab import rules as R
from planlab import store as S
from planlab.errors import PlanlabError
from planlab.store import Plan

PR_FIELDS = "number,state,headRefName,baseRefName,mergedAt,url,title"
LIST_LIMIT = 500
GH_TIMEOUT = 120
UNBUILT, IN_REVIEW, VALIDATING, PROMOTING, LANDED = (
    "unbuilt",
    "in review",
    "validating",
    "promoting",
    "landed",
)
READY, NEEDS_DECISION, STALE, UNKNOWN, IN_FLIGHT = (
    "ready to verify",
    "needs decision",
    "stale",
    "unknown",
    "in flight",
)

PR = dict[str, Any]
Row = dict[str, Any]


class GhError(PlanlabError):
    pass


def _token(gh_user: str | None) -> str:
    if not gh_user:
        raise GhError("no gh_user in .planlab.yaml; every gh call needs one")
    try:
        res = subprocess.run(
            ["gh", "auth", "token", "-u", gh_user],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh auth token timed out after {GH_TIMEOUT}s") from exc
    if res.returncode != 0 or not res.stdout.strip():
        why = res.stderr.strip() or "empty token"
        raise GhError(f"no gh token for {gh_user}: {why} — `gh auth login -u {gh_user}`")
    return res.stdout.strip()


def _gh_raw(argv: list[str], token: str) -> str:
    env = dict(os.environ, GH_TOKEN=token)
    what = " ".join(argv[:3])
    try:
        res = subprocess.run(
            ["gh", *argv], capture_output=True, text=True, env=env, timeout=GH_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh {what} timed out after {GH_TIMEOUT}s") from exc
    if res.returncode != 0:
        raise GhError(f"gh {what} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def _gh(argv: list[str], token: str) -> Any:
    out = _gh_raw(argv, token)
    try:
        return json.loads(out or "null")
    except ValueError as exc:
        raise GhError(f"gh {' '.join(argv[:3])} returned unparseable output") from exc


# ── the GitHub picture ────────────────────────────────────────────────────


class Remote:
    """One PR list per run, plus lazy per-number and branch lookups."""

    def __init__(self, origin: str, token: str) -> None:
        self.origin, self.token = origin, token
        self.by_number: dict[int, PR] = {}
        self.by_branch: dict[str, list[PR]] = {}
        self._branches: set[str] | None = None
        rows = (
            _gh(
                [
                    "pr", "list", "--state", "all", "-L", str(LIST_LIMIT),
                    "--json", PR_FIELDS, "-R", origin,
                ],
                token,
            )
            or []
        )  # fmt: skip
        # At exactly the limit the repository-wide list is no longer the whole
        # picture, and a plan whose PR fell off the end reads as stale.
        self.truncated = len(rows) >= LIST_LIMIT
        for row in rows:
            self._add(row)

    def _add(self, row: PR) -> None:
        self.by_number[int(row["number"])] = row
        self.by_branch.setdefault(row["headRefName"], []).append(row)

    def pr(self, number: int | str) -> PR:
        """The PR, fetched individually only when the list lacked it."""
        n = int(number)
        if n not in self.by_number:
            self._add(
                _gh(["pr", "view", str(n), "--json", PR_FIELDS, "-R", self.origin], self.token)
            )
        return self.by_number[n]

    def branches(self) -> set[str]:
        """Heads on origin. One call, and only when a plan has no PR at all."""
        if self._branches is None:
            out = _gh_raw(
                ["api", f"repos/{self.origin}/git/matching-refs/heads", "--jq", ".[].ref"],
                self.token,
            )
            self._branches = {
                line.strip().replace("refs/heads/", "") for line in out.splitlines() if line.strip()
            }
        return self._branches


def stage_of(prs: list[PR], base: str, terminal: str) -> str:
    """The branch's stage, from the base refs of the PRs opened from it."""
    if not prs:
        return UNBUILT
    into_base = [p for p in prs if p["baseRefName"] == base]
    into_term = [p for p in prs if p["baseRefName"] == terminal]
    # Open work outranks any historical merge: a reused branch with a merged PR
    # into main AND a newer open one is promoting, not landed.
    if any(p["state"] == "OPEN" for p in into_term):
        return PROMOTING
    if any(p["state"] == "OPEN" for p in into_base):
        return IN_REVIEW
    if any(p["state"] == "MERGED" for p in into_term):
        return LANDED
    if any(p["state"] == "MERGED" for p in into_base):
        return VALIDATING
    return UNBUILT


def classify(stages: dict[str, str], prs: list[PR], branch_on_origin: bool) -> str:
    """One plan's state, from its branches' stages and its PRs."""
    if not prs:
        return STALE if not branch_on_origin else IN_FLIGHT
    if stages and all(s == LANDED for s in stages.values()):
        return READY
    dead = [p for p in prs if p["state"] == "CLOSED" and not p.get("mergedAt")]
    if dead:
        alive = {p["headRefName"] for p in prs if p["state"] == "OPEN" or p.get("mergedAt")}
        if any(p["headRefName"] not in alive for p in dead):
            return NEEDS_DECISION
    return IN_FLIGHT


# ── the run ───────────────────────────────────────────────────────────────


def _open_plans(tree: str) -> list[Plan]:
    plans, _ = S.scan(tree)
    return [p for p in plans if p.state == "open" and p.id and not p.error]


def _unknown(plan: Plan, why: str, prs: list[int] | None = None, stages: Any = None) -> Row:
    return {
        "id": plan.id,
        "classification": UNKNOWN,
        "branches": stages or {},
        "prs": prs or [],
        "why": why,
    }


def _plan_row(plan: Plan, remote: Remote, base: str, terminal: str) -> Row:
    """Classify one open plan. A gh failure makes only this plan unknown."""
    branch = plan.get("branch") or ""
    try:
        declared = [int(n) for n in plan.list_of("prs")]
    except (TypeError, ValueError):
        return _unknown(plan, f"prs: holds a non-integer entry ({plan.get('prs')!r}); fix the plan")
    try:
        prs = [remote.pr(n) for n in declared]
    except GhError as exc:
        return _unknown(plan, str(exc), declared)

    heads: dict[str, list[PR]] = {}
    for p in prs:
        heads.setdefault(p["headRefName"], []).append(p)
    if branch and branch not in heads:
        heads[branch] = list(remote.by_branch.get(branch, []))
    stages = {b: stage_of(ps, base, terminal) for b, ps in heads.items()}

    on_origin = True
    if not prs:
        try:
            on_origin = branch in remote.branches() if branch else False
        except GhError as exc:
            return _unknown(plan, str(exc), declared, stages)

    discovered = [
        {
            "number": p["number"],
            "branch": p["headRefName"],
            "state": p["state"],
            "plan": plan.id,
            "url": p.get("url"),
        }
        for p in remote.by_branch.get(branch, [])
        if branch and int(p["number"]) not in declared
    ]
    return {
        "id": plan.id,
        "title": plan.get("title"),
        "branch": branch,
        "classification": classify(stages, prs, on_origin),
        "branches": stages,
        "prs": declared,
        "pr_states": {
            str(p["number"]): {
                "state": p["state"],
                "base": p["baseRefName"],
                "head": p["headRefName"],
                "url": p.get("url"),
            }
            for p in prs
        },
        "discovered": discovered,
    }


def sync(tree: str, args: argparse.Namespace) -> int:
    cfg = S.load_config(tree)
    origin = cfg.get("origin")
    base, terminal = cfg.get("base") or "develop", cfg.get("terminal") or "main"
    if not origin:
        raise PlanlabError("no origin in .planlab.yaml")

    token = _token(cfg.get("gh_user"))
    try:
        remote = Remote(origin, token)
    except GhError as exc:
        # The list is the whole picture; without it every plan is unknown and
        # the cache stays exactly as it was.
        rows = [_unknown(p, str(exc)) for p in _open_plans(tree)]
        return _report(tree, args, rows, write=False)

    rows = [_plan_row(plan, remote, base, terminal) for plan in _open_plans(tree)]
    return _report(tree, args, rows, write=not args.dry_run, truncated=remote.truncated)


def all_unknown(rows: list[Row]) -> bool:
    """Nothing usable came back at all — write nothing rather than a blank."""
    return bool(rows) and all(r["classification"] == UNKNOWN for r in rows)


def _write_cache(tree: str, view: dict[str, Any], rows: list[Row]) -> None:
    """Merge, never replace, and stamp each plan with its OWN sync time."""
    prior = R.sync_cache(tree)
    prs = dict(prior.get("prs") or {})
    plans = dict(prior.get("plans") or {})
    for r in rows:
        if r["classification"] == UNKNOWN:
            continue
        prs.update(r.get("pr_states") or {})
        plans[r["id"]] = {
            "classification": r["classification"],
            "branches": r["branches"],
            "synced_at": view["synced_at"],
        }
    S.write_atomic(
        os.path.join(tree, S.SYNC_CACHE),
        json.dumps(
            {"synced_at": view["synced_at"], "prs": prs, "plans": plans},
            indent=2,
            sort_keys=True,
        ),
    )


def _report(
    tree: str, args: argparse.Namespace, rows: list[Row], write: bool, truncated: bool = False
) -> int:
    buckets = {
        k: [r for r in rows if r["classification"] == k]
        for k in (READY, NEEDS_DECISION, STALE, UNKNOWN)
    }
    view: dict[str, Any] = {
        "tree": tree,
        "synced_at": S.utc_now().strftime("%Y-%m-%dT%H:%M:%S"),
        "plans": rows,
        "ready_to_verify": buckets[READY],
        "needs_decision": buckets[NEEDS_DECISION],
        "stale": buckets[STALE],
        "unknown": buckets[UNKNOWN],
        "discovered": [d for r in rows for d in r.get("discovered", [])],
        "truncated": truncated,
    }
    if write and not all_unknown(rows):
        _write_cache(tree, view, rows)

    if args.json:
        print(json.dumps(view, indent=2, default=str))
    else:
        print(_text(view))
    return 1 if buckets[UNKNOWN] else 0


def _text(view: dict[str, Any]) -> str:
    out = [f"synced {view['synced_at']}"]
    if view.get("truncated"):
        out.append(
            f"WARNING: the PR list hit its {LIST_LIMIT}-row limit — a plan whose PR "
            "fell off the end will read as stale."
        )
    for label, key in (
        ("READY TO VERIFY", "ready_to_verify"),
        ("NEEDS DECISION", "needs_decision"),
        ("STALE", "stale"),
        ("UNKNOWN", "unknown"),
    ):
        rows = view[key]
        if not rows:
            continue
        out.append("")
        out.append(f"{label} ({len(rows)})")
        for r in rows:
            detail = (
                r.get("why")
                or ", ".join(f"{b} {s}" for b, s in sorted(r["branches"].items()))
                or "no branch"
            )
            out.append(f"  {r['id']:<32} {detail}")
    if view["discovered"]:
        out.append("")
        out.append("PRs on a plan's branch that prs: does not list (reported, never added):")
        for d in view["discovered"]:
            out.append(f"  #{d['number']:<6} {d['plan']:<24} {d['branch']}")
    return "\n".join(out)
