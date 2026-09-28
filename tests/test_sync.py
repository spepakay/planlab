"""GitHub reconciliation, entirely offline.

`gh` is replaced by a stub on PATH. The stub logs every invocation with the
GH_TOKEN it saw, so the account guard (every call carries a token for the
configured user) and the one-list-call budget are both assertable.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import pytest
from support import Result, Tree, slurp

Plan = Callable[..., str]

GH_STUB = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["GH_STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "token": os.environ.get("GH_TOKEN")}) + "\n")
fail = os.environ.get("GH_STUB_FAIL", "")
if fail and fail in " ".join(argv):
    sys.stderr.write("stub: refusing %s\n" % fail)
    sys.exit(1)
if argv[:1] == ["auth"]:
    who = argv[argv.index("-u") + 1] if "-u" in argv else "?"
    if who in os.environ.get("GH_STUB_NO_TOKEN_FOR", "").split(","):
        sys.stderr.write("no token for %s\n" % who); sys.exit(1)
    print("tok-" + who); sys.exit(0)
prs = json.load(open(os.environ["GH_STUB_PRS"]))
if argv[:2] == ["pr", "list"]:
    print(json.dumps([p for p in prs if not p.get("_hidden")])); sys.exit(0)
if argv[:2] == ["pr", "view"]:
    n = int(argv[2])
    for p in prs:
        if p["number"] == n:
            print(json.dumps(p)); sys.exit(0)
    sys.stderr.write("no pr %d\n" % n); sys.exit(1)
if argv[:1] == ["api"]:
    print("\n".join("refs/heads/" + b for b in
                    os.environ.get("GH_STUB_BRANCHES", "").split(",") if b))
    sys.exit(0)
sys.stderr.write("stub: unhandled %r\n" % argv); sys.exit(2)
"""


def pr(number: int, head: str, base: str, state: str, merged: bool = True) -> dict[str, Any]:
    return {
        "number": number,
        "headRefName": head,
        "baseRefName": base,
        "state": state,
        "mergedAt": "2026-09-01T00:00:00Z" if state == "MERGED" and merged else None,
        "url": f"https://github.com/example/app/pull/{number}",
        "title": f"PR {number}",
    }


class GitHub:
    """The stubbed `gh`: set the PR list, run sync, read back every call it saw."""

    def __init__(self, tree: Tree) -> None:
        self.tree = tree
        bin_dir = os.path.join(tree.root, "bin")
        os.makedirs(bin_dir)
        stub = os.path.join(bin_dir, "gh")
        with open(stub, "w") as fh:
            fh.write(GH_STUB)
        os.chmod(stub, 0o755)
        self.log = os.path.join(tree.root, "gh.log")
        self.prs_file = os.path.join(tree.root, "prs.json")
        self.env = {
            "PATH": bin_dir + os.pathsep + os.environ["PATH"],
            "GH_STUB_LOG": self.log,
            "GH_STUB_PRS": self.prs_file,
        }
        self.set_prs([])

    def set_prs(self, prs: list[dict[str, Any]]) -> None:
        with open(self.prs_file, "w") as fh:
            json.dump(prs, fh)

    def sync(self, *extra: str, env: dict[str, str] | None = None) -> Result:
        return self.tree.run("sync", "--json", *extra, by="sync", env={**self.env, **(env or {})})

    def classify(self, *extra: str, env: dict[str, str] | None = None) -> dict[str, Any]:
        view: dict[str, Any] = json.loads(self.sync(*extra, env=env).out)
        return view

    def calls(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.log):
            return []
        return [json.loads(line) for line in slurp(self.log).splitlines() if line.strip()]

    def cache(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(slurp(os.path.join(self.tree.path, ".sync.json")))
        return data


@pytest.fixture
def gh(tree: Tree) -> GitHub:
    return GitHub(tree)


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [r["id"] for r in rows]


LANDED_A = [pr(10, "feat/a", "develop", "MERGED"), pr(11, "feat/a", "main", "MERGED")]


# ── stages and classification ─────────────────────────────────────────────


class TestStages:
    def test_every_stage_from_the_base_refs(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs(
            [
                *LANDED_A,
                pr(20, "feat/b", "develop", "MERGED"),
                pr(30, "feat/c", "develop", "OPEN"),
                pr(40, "feat/d", "develop", "MERGED"),
                pr(41, "feat/d", "main", "OPEN"),
            ]
        )
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        plan("open", "validating", claimed_by="s", branch="feat/b", prs=[20])
        plan("open", "inreview", claimed_by="s", branch="feat/c", prs=[30])
        plan("open", "promoting", claimed_by="s", branch="feat/d", prs=[40, 41])
        plan("open", "unbuilt", claimed_by="s", branch="feat/z")
        view = gh.classify(env={"GH_STUB_BRANCHES": "feat/z"})
        stages = {p["id"]: p["branches"] for p in view["plans"]}
        assert stages["landed"]["feat/a"] == "landed"
        assert stages["validating"]["feat/b"] == "validating"
        assert stages["inreview"]["feat/c"] == "in review"
        assert stages["promoting"]["feat/d"] == "promoting"
        assert stages["unbuilt"]["feat/z"] == "unbuilt"

    def test_one_landed_and_one_validating_branch_is_not_ready(
        self, gh: GitHub, plan: Plan
    ) -> None:
        gh.set_prs([*LANDED_A, pr(20, "feat/b", "develop", "MERGED")])
        plan("open", "mixed", claimed_by="s", branch="feat/a", prs=[10, 11, 20])
        view = gh.classify()
        row = view["plans"][0]
        assert row["classification"] == "in flight"
        assert row["id"] not in ids(view["ready_to_verify"])


class TestClassification:
    def test_ready_to_verify_when_every_branch_landed(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs(LANDED_A)
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        rc, out, err = gh.sync()
        assert rc == 0, err
        assert ids(json.loads(out)["ready_to_verify"]) == ["landed"]

    def test_needs_decision_when_a_pr_closed_unmerged_and_nothing_is_open(
        self, gh: GitHub, plan: Plan
    ) -> None:
        gh.set_prs([pr(50, "feat/e", "develop", "CLOSED", merged=False)])
        plan("open", "dropped", claimed_by="s", branch="feat/e", prs=[50])
        assert ids(gh.classify()["needs_decision"]) == ["dropped"]

    def test_a_reopened_branch_is_not_needs_decision(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs(
            [
                pr(50, "feat/e", "develop", "CLOSED", merged=False),
                pr(51, "feat/e", "develop", "OPEN"),
            ]
        )
        plan("open", "retried", claimed_by="s", branch="feat/e", prs=[50, 51])
        assert gh.classify()["needs_decision"] == []

    def test_stale_when_there_is_no_pr_and_no_branch_on_origin(
        self, gh: GitHub, plan: Plan
    ) -> None:
        plan("open", "ghost", claimed_by="s", branch="feat/gone")
        view = gh.classify(env={"GH_STUB_BRANCHES": "feat/other"})
        assert ids(view["stale"]) == ["ghost"]

    def test_not_stale_when_the_branch_exists_but_has_no_pr(self, gh: GitHub, plan: Plan) -> None:
        plan("open", "started", claimed_by="s", branch="feat/started")
        assert gh.classify(env={"GH_STUB_BRANCHES": "feat/started"})["stale"] == []


class TestPrecedence:
    def test_an_open_pr_outranks_an_older_merge_on_the_same_branch(
        self, gh: GitHub, plan: Plan
    ) -> None:
        # A reused branch: main PR #11 merged long ago, #12 is open now. The
        # branch is promoting, not landed, and the plan is not ready to verify.
        gh.set_prs([*LANDED_A, pr(12, "feat/a", "main", "OPEN")])
        plan("open", "reused", claimed_by="s", branch="feat/a", prs=[10, 11, 12])
        view = gh.classify()
        assert view["plans"][0]["branches"]["feat/a"] == "promoting"
        assert view["ready_to_verify"] == []

    def test_an_open_base_pr_outranks_a_merged_one(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs([pr(20, "feat/b", "develop", "MERGED"), pr(21, "feat/b", "develop", "OPEN")])
        plan("open", "more-work", claimed_by="s", branch="feat/b", prs=[20, 21])
        assert gh.classify()["plans"][0]["branches"]["feat/b"] == "in review"


# ── failure is never guessed past ─────────────────────────────────────────


class TestFailure:
    def test_gh_failure_makes_the_plan_unknown_and_exits_non_zero(
        self, gh: GitHub, plan: Plan
    ) -> None:
        gh.set_prs(LANDED_A)
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        plan("open", "missing", claimed_by="s", branch="feat/x", prs=[99])
        rc, out, _ = gh.sync()
        view = json.loads(out)
        assert rc == 1
        assert ids(view["unknown"]) == ["missing"]
        # the healthy plan is still classified
        assert ids(view["ready_to_verify"]) == ["landed"]

    def test_a_failed_list_leaves_the_cache_untouched(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        gh.set_prs(LANDED_A)
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        assert gh.sync().rc == 0
        before = slurp(os.path.join(tree.path, ".sync.json"))
        rc, out, err = gh.sync(env={"GH_STUB_FAIL": "pr list"})
        assert rc == 1
        assert "unknown" in out + err
        assert slurp(os.path.join(tree.path, ".sync.json")) == before

    def test_a_missing_token_for_the_configured_account_is_a_clean_refusal(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10])
        rc, out, err = gh.sync(env={"GH_STUB_NO_TOKEN_FOR": "someone"})
        assert rc == 1
        assert "someone" in out + err
        assert not os.path.exists(os.path.join(tree.path, ".sync.json"))

    def test_a_non_integer_pr_makes_only_that_plan_unknown(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        # `prs: ["#42"]` is an older hand-written shape. It used to traceback
        # out of the loop and lose every other plan's result with it.
        gh.set_prs(LANDED_A)
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        tree.write(
            "open",
            "2026-09-01-legacy.md",
            '---\nid: legacy\ntitle: L\nclaimed_by: s\nprs: ["#42"]\n---\nb\n',
        )
        rc, out, err = gh.sync()
        view = json.loads(out)
        assert rc == 1
        assert "Traceback" not in err
        assert ids(view["unknown"]) == ["legacy"]
        assert ids(view["ready_to_verify"]) == ["landed"]


# ── the call budget and the account guard ─────────────────────────────────


class TestBudget:
    def test_one_list_call_per_run_and_view_only_for_a_number_the_list_lacks(
        self, gh: GitHub, plan: Plan
    ) -> None:
        gh.set_prs([*LANDED_A, dict(pr(12, "feat/b", "develop", "MERGED"), _hidden=True)])
        plan("open", "one", claimed_by="s", branch="feat/a", prs=[10, 11])
        plan("open", "two", claimed_by="s", branch="feat/b", prs=[12])
        rc, _, err = gh.sync()
        assert rc == 0, err
        lists = [c for c in gh.calls() if c["argv"][:2] == ["pr", "list"]]
        views = [c for c in gh.calls() if c["argv"][:2] == ["pr", "view"]]
        assert len(lists) == 1
        assert [c["argv"][2] for c in views] == ["12"]

    def test_every_gh_invocation_received_gh_token(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "MERGED")])
        plan("open", "one", claimed_by="s", branch="feat/a", prs=[10])
        gh.sync()
        calls = [c for c in gh.calls() if c["argv"][:1] != ["auth"]]
        assert calls
        # the call that mints the token cannot carry it; every other one must
        assert all(c["token"] == "tok-someone" for c in calls), calls

    def test_every_gh_invocation_named_the_repository(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "MERGED")])
        plan("open", "one", claimed_by="s", branch="feat/a", prs=[10])
        gh.sync()
        for c in gh.calls():
            if c["argv"][:1] in (["auth"], ["api"]):
                continue
            assert "-R" in c["argv"], c["argv"]
            assert c["argv"][c["argv"].index("-R") + 1] == "example/app"


# ── the cache ─────────────────────────────────────────────────────────────


class TestCacheFreeze:
    """One permanently-unknown plan must not freeze the cache for the rest."""

    def test_a_healthy_plan_still_updates_when_another_is_unknown(
        self, gh: GitHub, plan: Plan
    ) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "OPEN")])
        plan("open", "healthy", claimed_by="s", branch="feat/a", prs=[10])
        assert gh.sync().rc == 0
        plan("open", "broken", claimed_by="s", branch="feat/x", prs=[99])
        gh.set_prs([pr(10, "feat/a", "develop", "MERGED")])
        assert gh.sync().rc == 1
        cache = gh.cache()
        assert cache["prs"]["10"]["state"] == "MERGED"
        assert cache["plans"]["healthy"]["classification"] == "in flight"
        assert "broken" not in cache["plans"]

    def test_an_unknown_plan_keeps_the_entry_the_last_good_sync_left(
        self, gh: GitHub, plan: Plan
    ) -> None:
        gh.set_prs(LANDED_A)
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        assert gh.sync().rc == 0
        before = gh.cache()
        gh.set_prs([])  # both PRs now unfindable
        assert gh.sync().rc == 1
        after = gh.cache()
        assert after["prs"] == before["prs"]
        assert after["plans"]["landed"] == before["plans"]["landed"]
        assert after["synced_at"] >= before["synced_at"]


class TestFreshness:
    def test_another_plans_successful_sync_does_not_vouch_for_a_failed_one(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        gh.set_prs(
            [*LANDED_A, pr(20, "feat/b", "develop", "MERGED"), pr(21, "feat/b", "main", "MERGED")]
        )
        plan("open", "old", claimed_by="s", branch="feat/a", prs=[10, 11])
        plan("open", "fresh", claimed_by="s", branch="feat/b", prs=[20, 21])
        assert gh.sync().rc == 0
        # Age `old`'s own entry by years; `fresh` keeps syncing cleanly.
        cache = gh.cache()
        cache["plans"]["old"]["synced_at"] = "2020-01-01T00:00:00"
        with open(os.path.join(tree.path, ".sync.json"), "w") as fh:
            json.dump(cache, fh)
        rc, out, err = tree.run("move", "old", "done", by="t")
        assert rc == 1
        assert "stale" in (out + err).lower()
        rc, _, err = tree.run("move", "fresh", "done", by="t")
        assert rc == 0, err


class TestDoneGate:
    def test_a_pr_merged_only_into_base_is_not_done(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        # sync calls that branch `validating`; the done gate used to test only
        # state == MERGED and marked unlanded work verified-complete.
        gh.set_prs([pr(30, "feat/c", "develop", "MERGED")])
        plan("open", "devonly", claimed_by="s", branch="feat/c", prs=[30])
        assert gh.sync().rc == 0
        rc, out, err = tree.run("move", "devonly", "done", by="verifier")
        assert rc == 1
        assert "has not landed" in out + err
        assert tree.state_of("devonly") == "open"

    def test_the_same_branch_landed_on_main_is_done(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        gh.set_prs([pr(30, "feat/c", "develop", "MERGED"), pr(31, "feat/c", "main", "MERGED")])
        plan("open", "landed", claimed_by="s", branch="feat/c", prs=[30, 31])
        assert gh.sync().rc == 0
        rc, _, err = tree.run("move", "landed", "done", by="verifier")
        assert rc == 0, err


class TestTruncation:
    def test_a_full_pr_list_is_reported_as_possibly_truncated(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs([pr(n, f"feat/b{n}", "develop", "OPEN") for n in range(500)])
        plan("open", "one", claimed_by="s", branch="feat/b0", prs=[0])
        assert gh.classify()["truncated"]

    def test_a_short_list_is_not(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "OPEN")])
        plan("open", "one", claimed_by="s", branch="feat/a", prs=[10])
        assert not gh.classify()["truncated"]


class TestDiscovery:
    def test_a_pr_on_the_branch_not_in_prs_is_reported_and_never_appended(
        self, gh: GitHub, tree: Tree, plan: Plan
    ) -> None:
        gh.set_prs([*LANDED_A, pr(60, "feat/a", "develop", "OPEN")])
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        view = gh.classify()
        assert [d["number"] for d in view["discovered"]] == [60]
        assert tree.fm_of("landed")["prs"] == [10, 11]


class TestCache:
    def test_cache_records_synced_at_and_per_pr_state(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs(LANDED_A)
        plan("open", "landed", claimed_by="s", branch="feat/a", prs=[10, 11])
        assert gh.sync().rc == 0
        cache = gh.cache()
        assert cache["synced_at"]
        assert cache["prs"]["10"]["state"] == "MERGED"
        assert cache["prs"]["11"]["base"] == "main"
        assert cache["plans"]["landed"]["classification"] == "ready to verify"

    def test_dry_run_writes_nothing(self, gh: GitHub, tree: Tree, plan: Plan) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "MERGED")])
        plan("open", "one", claimed_by="s", branch="feat/a", prs=[10])
        assert gh.sync("--dry-run").rc == 0
        assert not os.path.exists(os.path.join(tree.path, ".sync.json"))

    def test_status_shows_the_cached_pr_state(self, gh: GitHub, tree: Tree, plan: Plan) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "OPEN")])
        plan("open", "one", claimed_by="s", branch="feat/a", prs=[10])
        gh.sync()
        row = tree.status_json()["in_flight"][0]
        assert row["prs"] == [{"number": 10, "state": "OPEN"}]

    def test_sync_only_looks_at_open_plans(self, gh: GitHub, plan: Plan) -> None:
        gh.set_prs([pr(10, "feat/a", "develop", "MERGED")])
        plan("drafts", "idea")
        plan("done", "old", prs=[10])
        assert gh.classify()["plans"] == []
