"""The state transitions: claim, release, verify-claim, move, worktree.

Every one of these is a mutation, so every one runs behind the host guard and,
where it touches frontmatter, behind the store's per-plan lock.

The claim is a hardlink, never `mv -n`: `os.rename` overwrites on POSIX, which
would silently let two workers win the same plan. `os.link` fails with EEXIST
atomically, the loser exits 3, and the brief window where the file is in both
next/ and open/ is one plan to `validate`, not a duplicate id.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import shutil
import subprocess
from collections.abc import Iterable
from typing import Any, NoReturn

from planlab import rules as R
from planlab import store as S
from planlab.errors import LostRace, PlanlabError
from planlab.frontmatter import Frontmatter, parse_frontmatter, split_frontmatter
from planlab.store import Config

SYNC_STALE_HOURS = 24
GIT_TIMEOUT = 120
MERGED = "MERGED"


def _refuse(message: str) -> NoReturn:
    raise PlanlabError(message)


def load(tree: str, pid: str, states: Iterable[str] = S.STATES) -> tuple[str, str, Frontmatter]:
    path, state = S.find_plan(tree, pid, states)
    fm_text, _ = split_frontmatter(S.slurp(path))
    return path, state, parse_frontmatter(fm_text)


def worktree_path(cfg: Config, pid: str) -> str:
    # An id reaches shutil.rmtree through here. os.path.join absorbs an absolute
    # segment entirely and "../" walks out, so the id is validated, not trusted.
    S.require_id(pid)
    return os.path.join(S.state_dir(), "worktrees", cfg.get("track") or "default", pid)


def branch_for(cfg: Config, fm: Frontmatter, pid: str) -> str:
    return str(fm.get("branch") or f"{cfg.get('branch_prefix') or 'feat'}/{pid}")


def _now() -> str:
    return S.utc_now().strftime("%Y-%m-%dT%H:%M:%S")


# ── claim and release ─────────────────────────────────────────────────────


def claim(tree: str, args: argparse.Namespace) -> int:
    cfg = S.load_config(tree)
    S.require_host(tree, cfg)
    try:
        S.find_plan(tree, args.id, ("next",))
    except PlanlabError:
        # Two workers, and the winner already unlinked next/. That is the
        # same lost race as EEXIST below, so it gets the same skip code.
        try:
            S.find_plan(tree, args.id, ("open",))
        except PlanlabError:
            _refuse(f"{args.id} is not in next/; only a next/ plan can be claimed")
        raise LostRace(f"{args.id} is already claimed (it is in open/)") from None

    plans, _ = S.scan(tree)
    index = S.by_id(plans)
    me = index.get(args.id)
    if me is None:
        _refuse(f"{args.id}: the plan in next/ did not parse; run `planlab validate`")
    errs, unmet = R.dep_problems(me, index, R.drift_blockers(plans))
    if errs:
        _refuse(f"{args.id}: {'; '.join(errs)}")
    if unmet:
        waiting = ", ".join(f"{u['id']} ({u['state']})" for u in unmet)
        _refuse(f"{args.id}: dependencies not done: {waiting}")

    def mutate(fm: Frontmatter, body: str, state: str, path: str) -> tuple[Frontmatter, str]:
        fm["claimed_by"] = args.by
        fm["claimed_at"] = _now()
        return fm, body

    print(S.transition(tree, cfg, args.id, ("next",), "open", "claim", args.by, mutate))
    return 0


def _require_owner(tree: str, pid: str, args: argparse.Namespace, verb: str) -> None:
    """A worktree removal is destructive; only its claimant may trigger one."""
    _, _, fm = load(tree, pid)
    owner = fm.get("claimed_by")
    if owner and owner != args.by and not getattr(args, "force", False):
        _refuse(
            f"{pid} is claimed by {owner}, not {args.by} — refusing to {verb} it. Its "
            "worktree may hold uncommitted work. Pass --force if you are certain."
        )


def _clear_claim(fm: Frontmatter) -> None:
    fm.pop("claimed_by", None)
    fm.pop("claimed_at", None)


def release(tree: str, args: argparse.Namespace) -> int:
    cfg = S.load_config(tree)
    S.require_host(tree, cfg)
    _require_owner(tree, args.id, args, "release")

    def mutate(fm: Frontmatter, body: str, state: str, path: str) -> tuple[Frontmatter, str]:
        _clear_claim(fm)
        return fm, body

    dst = S.transition(tree, cfg, args.id, ("open",), "next", "release", args.by, mutate)
    # Only after the move has actually landed: a failed transition must not
    # leave the plan claimed but its worktree gone.
    _drop_worktree(tree, cfg, args.id)
    _clear_marker(tree, args.id)
    print(dst)
    return 0


# ── verification claim ────────────────────────────────────────────────────


def marker_path(tree: str, pid: str) -> str:
    return os.path.join(tree, "open", f"{S.VERIFY_MARKER}{pid}")


def _clear_marker(tree: str, pid: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(marker_path(tree, pid))


def verify_claim(tree: str, args: argparse.Namespace) -> int:
    cfg = S.load_config(tree)
    S.require_host(tree, cfg)
    S.find_plan(tree, args.id, ("open",))
    path = marker_path(tree, args.id)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise LostRace(
                f"{args.id} is already being verified ({os.path.basename(path)})"
            ) from exc
        raise
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{_now()} by={args.by}\n")
    print(path)
    return 0


# ── terminal moves ────────────────────────────────────────────────────────


def _sync_age_hours(pid: str, cache: dict[str, Any]) -> float | None:
    entry = (cache.get("plans") or {}).get(pid) or {}
    when = entry.get("synced_at") or cache.get("synced_at")
    if not when:
        return None
    try:
        stamp = S.as_naive_utc(when)
    except ValueError:
        return None
    return (S.utc_now() - stamp).total_seconds() / 3600.0


def _check_done_allowed(tree: str, pid: str, fm: Frontmatter, force: bool) -> None:
    prs = fm.get("prs") or []
    prs = prs if isinstance(prs, list) else [prs]
    if not prs or force:
        return
    cache = R.sync_cache(tree)
    age = _sync_age_hours(pid, cache)
    states = cache.get("prs", {})
    if age is None or age > SYNC_STALE_HOURS:
        how_old = "never synced" if age is None else f"{age:.0f}h old"
        _refuse(
            f"{pid}: the sync cache is stale or missing ({how_old}); run `planlab sync`, or --force"
        )
    unknown = [n for n in prs if states.get(str(n), {}).get("state", "UNKNOWN") == "UNKNOWN"]
    if unknown:
        numbers = ", ".join(f"#{n}" for n in unknown)
        _refuse(f"{pid}: unknown PR state for {numbers}; run `planlab sync`, or --force")
    unmerged = [n for n in prs if states.get(str(n), {}).get("state") != MERGED]
    if unmerged:
        _refuse(f"{pid}: not every PR has merged: {', '.join(f'#{n}' for n in unmerged)}")
    # Merged is not landed. sync classifies a branch whose only merge went into
    # `base` as validating, and the done gate has to agree with it or work that
    # never reached the terminal branch gets marked verified-complete.
    branches = ((cache.get("plans") or {}).get(pid) or {}).get("branches") or {}
    unlanded = sorted(b for b, stage in branches.items() if stage != "landed")
    if unlanded:
        stages = ", ".join(branches.get(b, "?") for b in unlanded)
        _refuse(
            f"{pid}: {', '.join(unlanded)} has not landed on the terminal branch "
            f"({stages}); run `planlab sync`, or --force"
        )


def move(tree: str, args: argparse.Namespace) -> int:
    cfg = S.load_config(tree)
    S.require_host(tree, cfg)
    _, _, fm = load(tree, args.id)
    if args.target == "discarded" and not args.why:
        _refuse(f"move {args.id} discarded needs --why; a discard without a reason is a deletion")
    if args.target == "discarded":
        # done/ is reached after verification, under the verifier's own identity,
        # and its gates already prove the work landed. discarded/ abandons work
        # that may still be in flight, so it belongs to the claimant.
        _require_owner(tree, args.id, args, "discard")
    if args.target == "done":
        _check_done_allowed(tree, args.id, fm, args.force)

    def mutate(f: Frontmatter, body: str, st: str, path: str) -> tuple[Frontmatter, str]:
        _clear_claim(f)
        if args.why and args.target == "discarded":
            f["why"] = args.why
        return f, body

    dst = S.transition(
        tree,
        cfg,
        args.id,
        ("open", "next", "drafts"),
        args.target,
        f"move {args.target}",
        args.by,
        mutate,
    )
    _drop_worktree(tree, cfg, args.id)
    _clear_marker(tree, args.id)
    print(dst)
    return 0


# ── worktrees ─────────────────────────────────────────────────────────────


def _git(repo: str, *argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", repo, *argv], capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        _refuse(f"git {' '.join(argv[:2])} timed out after {GIT_TIMEOUT}s in {repo}")


def _drop_worktree(tree: str, cfg: Config, pid: str) -> None:
    path = worktree_path(cfg, pid)
    repo = cfg.get("build_repo")
    if repo and os.path.isdir(repo):
        _git(repo, "worktree", "remove", "--force", path)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    # Prune LAST: while the directory still exists it is a no-op, and skipping
    # it leaves a registration that makes the next `worktree add` fail with
    # "missing but already registered".
    if repo and os.path.isdir(repo):
        _git(repo, "worktree", "prune")


def _has_ref(repo: str, ref: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def _add_worktree(repo: str, cfg: Config, path: str, branch: str) -> None:
    """Check out `branch` at `path`: the local branch, else origin's, else a new one."""
    if _has_ref(repo, branch):
        res = _git(repo, "worktree", "add", path, branch)
    elif _has_ref(repo, f"origin/{branch}"):
        res = _git(repo, "worktree", "add", "--track", "-b", branch, path, f"origin/{branch}")
    else:
        base = cfg.get("base") or "develop"
        start = f"origin/{base}" if _has_ref(repo, f"origin/{base}") else base
        res = _git(repo, "worktree", "add", "-b", branch, path, start)
    if res.returncode != 0:
        _refuse(f"git worktree add failed: {res.stderr.strip() or res.stdout.strip()}")


def worktree(tree: str, args: argparse.Namespace) -> int:
    cfg = S.load_config(tree)
    S.require_host(tree, cfg)
    path = worktree_path(cfg, args.id)
    if args.action == "remove":
        _drop_worktree(tree, cfg, args.id)
        print(path)
        return 0

    _, _, fm = load(tree, args.id)
    repo = cfg.get("build_repo")
    branch = branch_for(cfg, fm, args.id)
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        _refuse(f"build_repo is not a git checkout: {repo}")

    def record_branch(f: Frontmatter, body: str, st: str, p: str) -> tuple[Frontmatter, str]:
        f["branch"] = branch
        return f, body

    if os.path.isdir(path):
        if _git(path, "rev-parse", "--is-inside-work-tree").returncode != 0:
            _refuse(
                f"{path} exists but is not a git worktree; remove it first "
                f"(`planlab worktree remove {args.id}`)"
            )
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _add_worktree(repo, cfg, path, branch)

    S.edit(tree, args.id, "worktree add", args.by, record_branch, cfg)
    print(path)
    return 0
