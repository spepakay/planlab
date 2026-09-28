"""State transitions: claim, release, verify-claim, move and worktree.

The races are real subprocesses; the worktree cases use a scratch git
repository, not a stub.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable

from support import Tree, make_repo, slurp

Plan = Callable[..., str]
PID = "api-cleanup"
STAMP = "%Y-%m-%dT%H:%M:%S"


def race(tree: Tree, *argv: str, n: int = 2) -> tuple[list[int], list[tuple[int, str, str]]]:
    """Start n identical commands at once. Returns (sorted exit codes, raw results)."""
    procs = [tree.popen(*argv, by=f"p{i}") for i in range(n)]
    results = []
    for p in procs:
        out, err = p.communicate()
        results.append((p.returncode, out, err))
    return sorted(r[0] for r in results), results


# ── claim ─────────────────────────────────────────────────────────────────


class TestClaim:
    def test_moves_next_to_open_and_records_who_and_when(self, tree: Tree, plan: Plan) -> None:
        plan("next", PID)
        rc, _, err = tree.run("claim", PID, by="worker-a")
        assert rc == 0, err
        assert tree.state_of(PID) == "open"
        fm = tree.fm_of(PID)
        assert fm["claimed_by"] == "worker-a"
        assert fm["claimed_at"]
        assert any(":claim" in w for w in fm["worked_by"])

    def test_two_processes_one_winner(self, tree: Tree, plan: Plan) -> None:
        plan("next", PID)
        codes, results = race(tree, "claim", PID)
        assert codes == [0, 3], results
        assert tree.state_of(PID) == "open"
        assert tree.listdir("next") == []
        _, out, err = next(r for r in results if r[0] == 3)
        assert "claimed" in (out + err).lower()

    def test_refused_when_a_dependency_is_unmet(self, tree: Tree, plan: Plan) -> None:
        plan("next", "dep")
        plan("next", "a", depends_on=["dep"])
        rc, out, err = tree.run("claim", "a", by="worker-a")
        assert rc == 1
        assert "dep" in out + err
        assert tree.state_of("a") == "next"

    def test_refused_when_a_dependency_is_discarded(self, tree: Tree, plan: Plan) -> None:
        plan("discarded", "gone", why="superseded")
        plan("next", "a", depends_on=["gone"])
        rc, out, err = tree.run("claim", "a", by="worker-a")
        assert rc == 1
        assert "discarded" in out + err

    def test_a_plan_not_in_next_cannot_be_claimed(self, tree: Tree, plan: Plan) -> None:
        plan("drafts", "idea")
        rc, out, err = tree.run("claim", "idea", by="worker-a")
        assert rc == 1
        assert "next/" in out + err


# ── races on every transition ─────────────────────────────────────────────


class TestTransitionRaces:
    """move and release are transitions too: the loser skips, never crashes."""

    def test_two_concurrent_moves_leave_one_winner_and_no_traceback(
        self, tree: Tree, plan: Plan
    ) -> None:
        plan("open", PID, claimed_by="worker-a")
        codes, results = race(tree, "move", PID, "done")
        assert codes == [0, 3], results
        assert tree.state_of(PID) == "done"
        assert tree.listdir("open") == []
        assert all("Traceback" not in err for _, _, err in results)

    def test_two_concurrent_releases_leave_one_winner(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        codes, results = race(tree, "release", PID, "--force")
        assert codes == [0, 3], results
        assert tree.state_of(PID) == "next"

    def test_the_loser_of_a_move_does_not_edit_the_winners_file(
        self, tree: Tree, plan: Plan
    ) -> None:
        # edit() used to run BEFORE the link and re-resolve across all states,
        # so the loser stamped worked_by on the file the winner had moved.
        plan("open", PID, claimed_by="worker-a")
        codes, results = race(tree, "move", PID, "discarded", "--why", "x", "--force")
        assert codes == [0, 3], results
        assert len(tree.fm_of(PID)["worked_by"]) == 1

    def test_a_claim_and_a_concurrent_edit_cannot_resurrect_next(
        self, tree: Tree, plan: Plan
    ) -> None:
        plan("next", PID)
        editor = tree.popen(
            "set", PID, "priority=P1", by="editor", env={"PLANLAB_TEST_LOCK_DELAY": "0.4"}
        )
        claimer = tree.popen("claim", PID, by="claimer")
        editor.communicate()
        claimer.communicate()
        assert tree.listdir("next") == []
        fm = tree.fm_of(PID)
        assert tree.state_of(PID) == "open"
        assert fm.get("priority") == "P1"
        assert fm.get("claimed_by") == "claimer"


# ── release ───────────────────────────────────────────────────────────────


class TestRelease:
    def test_returns_to_next_clears_the_claim_and_keeps_the_branch(
        self, tree: Tree, plan: Plan
    ) -> None:
        plan("next", PID)
        tree.run("claim", PID, by="worker-a")
        tree.run("set", PID, f"branch=feat/{PID}", by="t")
        rc, _, err = tree.run("release", PID, by="worker-a")
        assert rc == 0, err
        assert tree.state_of(PID) == "next"
        fm = tree.fm_of(PID)
        assert fm.get("claimed_by") is None
        assert fm.get("claimed_at") is None
        assert fm["branch"] == f"feat/{PID}"

    def test_release_clears_a_stale_verify_marker(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        tree.run("verify-claim", PID, by="sync")
        marker = os.path.join(tree.path, "open", f".verifying-{PID}")
        assert os.path.exists(marker)
        tree.run("release", PID, by="worker-a")
        assert not os.path.exists(marker)


# ── verify-claim ──────────────────────────────────────────────────────────


class TestVerifyClaim:
    def test_two_processes_one_winner(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        codes, results = race(tree, "verify-claim", PID)
        assert codes == [0, 3], results

    def test_marker_records_the_time_and_the_identity(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        tree.run("verify-claim", PID, by="verifier")
        text = slurp(os.path.join(tree.path, "open", f".verifying-{PID}"))
        assert "verifier" in text
        assert time.strftime("%Y-%m-%d", time.gmtime()) in text

    def test_a_marker_older_than_two_hours_is_flagged_stale(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        old = time.strftime(STAMP, time.gmtime(time.time() - 3 * 3600))
        tree.write("open", f".verifying-{PID}", f"{old} by=sync\n")
        warnings = " ".join(w["message"] for w in tree.status_json()["warnings"])
        assert "stale verification" in warnings


# ── move ──────────────────────────────────────────────────────────────────


def write_sync_cache(
    tree: Tree, prs: dict[str, dict[str, str]], synced_at: str | None = None
) -> None:
    when = synced_at or time.strftime(STAMP, time.gmtime())
    with open(os.path.join(tree.path, ".sync.json"), "w") as fh:
        json.dump({"synced_at": when, "prs": prs}, fh)


class TestMove:
    def test_discarded_without_why_is_refused(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        rc, out, err = tree.run("move", PID, "discarded", by="worker-a")
        assert rc == 1
        assert "--why" in out + err
        assert tree.state_of(PID) == "open"

    def test_discarded_with_why_records_it(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        rc, _, err = tree.run("move", PID, "discarded", "--why", "superseded by x", by="worker-a")
        assert rc == 0, err
        assert tree.state_of(PID) == "discarded"
        fm = tree.fm_of(PID)
        assert fm["why"] == "superseded by x"
        assert fm.get("claimed_by") is None

    def test_done_requires_every_pr_merged(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a", prs=[1, 2])
        write_sync_cache(tree, {"1": {"state": "MERGED"}, "2": {"state": "OPEN"}})
        rc, out, err = tree.run("move", PID, "done", by="t")
        assert rc == 1
        assert "#2" in out + err
        write_sync_cache(tree, {"1": {"state": "MERGED"}, "2": {"state": "MERGED"}})
        rc, _, err = tree.run("move", PID, "done", by="t")
        assert rc == 0, err
        assert tree.state_of(PID) == "done"

    def test_done_refused_on_an_unknown_classification(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a", prs=[1])
        write_sync_cache(tree, {"1": {"state": "UNKNOWN"}})
        rc, out, err = tree.run("move", PID, "done", by="t")
        assert rc == 1
        assert "unknown" in (out + err).lower()
        rc, _, err = tree.run("move", PID, "done", "--force", by="t")
        assert rc == 0, err

    def test_done_refused_on_a_cache_older_than_a_day(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a", prs=[1])
        old = time.strftime(STAMP, time.gmtime(time.time() - 30 * 3600))
        write_sync_cache(tree, {"1": {"state": "MERGED"}}, synced_at=old)
        rc, out, err = tree.run("move", PID, "done", by="t")
        assert rc == 1
        assert "stale" in (out + err).lower()
        rc, _, err = tree.run("move", PID, "done", "--force", by="t")
        assert rc == 0, err

    def test_done_with_no_prs_at_all_is_allowed(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")
        rc, _, err = tree.run("move", PID, "done", by="t")
        assert rc == 0, err

    def test_move_removes_the_verify_marker_and_the_worktree(self, tree: Tree, plan: Plan) -> None:
        make_repo(tree.repo, branches=(f"feat/{PID}",))
        plan("open", PID, claimed_by="worker-a", branch=f"feat/{PID}")
        tree.run("worktree", "add", PID, by="t")
        tree.run("verify-claim", PID, by="sync")
        wt = tree.worktree_dir(PID)
        assert os.path.isdir(wt)
        rc, _, err = tree.run("move", PID, "done", by="t")
        assert rc == 0, err
        assert not os.path.exists(wt)
        assert not os.path.exists(os.path.join(tree.path, "open", f".verifying-{PID}"))


# ── ownership ─────────────────────────────────────────────────────────────


class TestOwnership:
    """Releasing or discarding destroys a worktree that may hold uncommitted work."""

    def test_release_refuses_for_a_different_worker(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="workerA")
        rc, out, err = tree.run("release", PID, by="workerB")
        assert rc == 1
        assert "workerA" in out + err
        assert tree.state_of(PID) == "open"

    def test_release_is_allowed_for_the_claimant(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="workerA")
        assert tree.run("release", PID, by="workerA").rc == 0

    def test_force_is_the_documented_override(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="workerA")
        rc, _, err = tree.run("release", PID, "--force", by="workerB")
        assert rc == 0, err

    def test_discard_refuses_for_a_different_worker(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="workerA")
        rc, out, err = tree.run("move", PID, "discarded", "--why", "x", by="workerB")
        assert rc == 1
        assert "workerA" in out + err

    def test_done_is_open_to_the_verifier_whoever_claimed_it(self, tree: Tree, plan: Plan) -> None:
        # The done gates prove the work landed; verification runs under its own identity.
        plan("open", PID, claimed_by="workerA")
        rc, _, err = tree.run("move", PID, "done", by="verifier")
        assert rc == 0, err


# ── worktrees ─────────────────────────────────────────────────────────────


def current_branch(path: str) -> str:
    return subprocess.run(
        ["git", "-C", path, "branch", "--show-current"], capture_output=True, text=True
    ).stdout.strip()


class TestWorktree:
    def test_add_creates_the_branch_when_it_does_not_exist(self, tree: Tree, plan: Plan) -> None:
        make_repo(tree.repo)
        plan("open", PID, claimed_by="worker-a")
        rc, _, err = tree.run("worktree", "add", PID, by="t")
        assert rc == 0, err
        wt = tree.worktree_dir(PID)
        assert os.path.isdir(wt)
        assert current_branch(wt) == f"feat/{PID}"
        assert tree.fm_of(PID)["branch"] == f"feat/{PID}"

    def test_add_reuses_a_branch_that_already_exists(self, tree: Tree, plan: Plan) -> None:
        branch = f"feat/{PID}"
        make_repo(tree.repo, branches=(branch,))
        git = ["git", "-C", tree.repo]
        subprocess.run([*git, "checkout", "-q", branch], check=True)
        with open(os.path.join(tree.repo, "marker"), "w") as fh:
            fh.write("from the existing branch\n")
        subprocess.run([*git, "add", "-A"], capture_output=True)
        subprocess.run([*git, "commit", "-qm", "x"], capture_output=True)
        subprocess.run([*git, "checkout", "-q", "main"], check=True)
        plan("open", PID, claimed_by="worker-a", branch=branch)
        rc, _, err = tree.run("worktree", "add", PID, by="t")
        assert rc == 0, err
        assert os.path.exists(os.path.join(tree.worktree_dir(PID), "marker"))

    def test_add_is_idempotent(self, tree: Tree, plan: Plan) -> None:
        make_repo(tree.repo)
        plan("open", PID, claimed_by="worker-a")
        assert tree.run("worktree", "add", PID, by="t").rc == 0
        rc, _, err = tree.run("worktree", "add", PID, by="t")
        assert rc == 0, err

    def test_remove_on_a_missing_path_is_a_no_op(self, tree: Tree, plan: Plan) -> None:
        make_repo(tree.repo)
        plan("open", PID, claimed_by="worker-a")
        rc, _, err = tree.run("worktree", "remove", PID, by="t")
        assert rc == 0, err

    def test_git_failure_exits_one(self, tree: Tree, plan: Plan) -> None:
        plan("open", PID, claimed_by="worker-a")  # build_repo is not a repo
        rc, _, _ = tree.run("worktree", "add", PID, by="t")
        assert rc == 1
