"""The store: reservations, frontmatter writes, locks, the host guard, ids and flags."""

from __future__ import annotations

import os
import pathlib
import time
from collections.abc import Callable

import pytest
import yaml
from support import Result, Tree, slurp

from planlab import store

PID = "api-cleanup"

# ── new-id ────────────────────────────────────────────────────────────────


class TestNewId:
    def test_reserves_with_o_excl_and_prints_the_path(self, tree: Tree) -> None:
        rc, out, err = tree.run("new-id", "Fix two README inaccuracies", by="t")
        assert rc == 0, err
        path = out.strip()
        assert os.path.exists(path)
        assert path.endswith("-fix-two-readme-inaccuracies.md")
        assert "/drafts/" in path
        text = slurp(path)
        assert "id: fix-two-readme-inaccuracies" in text
        assert "title: Fix two README inaccuracies" in text
        assert "type: plan" in text
        assert f"date: {time.strftime('%Y-%m-%d')}" in text

    def test_a_reservation_validates_on_its_own(self, tree: Tree) -> None:
        # A capture worker can only rewrite the body, so the reservation has to
        # carry every field `validate` requires or every capture is an error.
        tree.run("new-id", "Something worth doing", by="t")
        rc, v = tree.validate_json()
        assert rc == 0, v["errors"]

    def test_source_can_be_set_at_reservation(self, tree: Tree) -> None:
        rc, out, err = tree.run(
            "new-id", "Drifted thing", "--source", "verify:shipped", by="worker:verifier"
        )
        assert rc == 0, err
        assert "source: verify:shipped" in slurp(out.strip())

    def test_collision_against_an_id_in_done_yields_dash_two(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("done", "widget")
        rc, out, err = tree.run("new-id", "Widget", by="t")
        assert rc == 0, err
        assert out.strip().endswith("-widget-2.md")

    def test_second_reservation_of_the_same_title_does_not_collide(self, tree: Tree) -> None:
        first = tree.run("new-id", "Same Thing", by="t")
        second = tree.run("new-id", "Same Thing", by="t")
        assert (first.rc, second.rc) == (0, 0)
        assert first.out.strip() != second.out.strip()
        assert second.out.strip().endswith("-same-thing-2.md")


# ── set ───────────────────────────────────────────────────────────────────


class TestSet:
    def test_typed_list_append_without_duplicates(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, claimed_by="worker-a")
        for assignment in ("prs+=42", "prs+=42", "prs+=43"):
            tree.run("set", PID, assignment, by="t")
        assert tree.fm_of(PID)["prs"] == [42, 43]

    def test_a_wikilink_appended_to_related_survives_verbatim(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        # Found when a verifier wired its write-up into related:.
        # `[[notes/reference/x]]` is a nested YAML flow sequence, so coercing
        # before casting to str stored "[['notes/reference/x']]".
        plan("open", PID, claimed_by="s")
        link = "[[notes/reference/2026-09-23-x-what-shipped]]"
        tree.run("set", PID, f"related+={link}", by="worker:verifier")
        assert tree.fm_of(PID)["related"] == [link]

    def test_a_path_appended_to_related_is_not_retyped(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, claimed_by="s")
        tree.run("set", PID, "related+=2026-09-23", by="t")
        assert tree.fm_of(PID)["related"] == ["2026-09-23"]

    def test_append_to_an_untyped_key_is_an_error(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, claimed_by="worker-a")
        rc, out, err = tree.run("set", PID, "title+=x", by="t")
        assert rc != 0
        assert "not a list key" in (out + err).lower()

    def test_the_audit_trail_cannot_be_set(self, tree: Tree, plan: Callable[..., str]) -> None:
        # worked_by is only ever appended to by the store; a caller clearing it
        # would erase the record of who touched the plan.
        plan("open", PID, claimed_by="s")
        tree.run("set", PID, "verdict=clean", by="t")
        assert tree.run("set", PID, "worked_by=[]", by="evil").rc != 0
        assert len(tree.fm_of(PID)["worked_by"]) == 1

    @pytest.mark.parametrize("bad", ["prs+=abc", "prs+="])
    def test_a_non_integer_pr_is_refused_not_a_traceback(
        self, tree: Tree, plan: Callable[..., str], bad: str
    ) -> None:
        plan("open", PID, claimed_by="s")
        rc, _, err = tree.run("set", PID, bad, by="t")
        assert rc != 0
        assert "Traceback" not in err
        assert "planlab:" in err

    def test_a_hash_in_a_set_value_survives(self, tree: Tree, plan: Callable[..., str]) -> None:
        # Found in a live smoke run: `set title=see PR #39` stored `see PR`,
        # because safe_load reads an unquoted ` #` as a comment.
        plan("open", PID, claimed_by="s")
        tree.run("set", PID, "title=Close out after PR #39 and #37", by="t")
        assert tree.fm_of(PID)["title"] == "Close out after PR #39 and #37"

    def test_typed_values_are_still_typed(self, tree: Tree, plan: Callable[..., str]) -> None:
        plan("open", PID, claimed_by="s")
        tree.run("set", PID, "prs+=42", "verified=2026-09-22", by="t")
        fm = tree.fm_of(PID)
        assert fm["prs"] == [42]
        assert str(fm["verified"]) == "2026-09-22"

    def test_setting_the_id_is_refused(self, tree: Tree, plan: Callable[..., str]) -> None:
        # The id IS the filename. Changing one without the other splits
        # find_plan from by_id and dangles every depends_on silently.
        plan("next", "alpha")
        rc, out, err = tree.run("set", "alpha", "id=beta", by="t")
        assert rc != 0
        assert "identity" in (out + err).lower()
        assert tree.fm_of("alpha")["id"] == "alpha"

    def test_scalar_assignment_and_worked_by_growth(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, claimed_by="worker-a")
        tree.run("set", PID, "verdict=clean", by="opnd")
        fm = tree.fm_of(PID)
        assert fm["verdict"] == "clean"
        assert len(fm["worked_by"]) == 1
        assert "opnd" in fm["worked_by"][0]
        assert ":set" in fm["worked_by"][0]

    def test_two_concurrent_set_calls_both_land(self, tree: Tree, plan: Callable[..., str]) -> None:
        plan("open", PID, claimed_by="worker-a")
        delay = {"PLANLAB_TEST_LOCK_DELAY": "0.3"}
        procs = [tree.popen("set", PID, f"prs+={400 + i}", by=f"p{i}", env=delay) for i in range(2)]
        outs = [p.communicate() for p in procs]
        assert [p.returncode for p in procs] == [0, 0], outs
        assert sorted(tree.fm_of(PID)["prs"]) == [400, 401]

    def test_a_write_refuses_rather_than_truncate_an_unquoted_hash(self, tree: Tree) -> None:
        # safe_load/safe_dump silently drops everything after an unquoted ` #`.
        # Reading is harmless; writing it back is data loss on the claim path.
        tree.write(
            "next",
            "2026-09-01-hashy.md",
            "---\nid: hashy\ntitle: T\nnote: see issue #14 in the backlog\n---\nb\n",
        )
        rc, out, err = tree.run("claim", "hashy", by="t")
        assert rc != 0
        assert "note" in out + err
        assert "#14 in the backlog" in tree.read("next", "2026-09-01-hashy.md")

    def test_a_quoted_hash_writes_normally(self, tree: Tree) -> None:
        tree.write(
            "next",
            "2026-09-01-quoted.md",
            "---\nid: quoted\ntitle: T\nnote: 'see issue #14'\n---\nb\n",
        )
        rc, _, err = tree.run("claim", "quoted", by="t")
        assert rc == 0, err
        assert tree.fm_of("quoted")["note"] == "see issue #14"

    def test_a_scalar_worked_by_does_not_wedge_every_write(self, tree: Tree) -> None:
        tree.write(
            "open",
            "2026-09-01-scalarwb.md",
            "---\nid: scalarwb\ntitle: T\nclaimed_by: s\nworked_by: someone\n---\nb\n",
        )
        rc, _, err = tree.run("set", "scalarwb", "verdict=clean", by="t")
        assert rc == 0, err
        assert len(tree.fm_of("scalarwb")["worked_by"]) == 2

    def test_a_stale_write_temp_does_not_wedge_the_plan(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("next", "wedged")
        state, name = tree.find("wedged")
        assert state and name
        tree.write(state, f".tmp.{os.getpid() + 1}.{name}", "half a write")
        tree.write(state, f".tmp.{os.getpid()}.{name}", "half a write")
        rc, _, err = tree.run("set", "wedged", "priority=P1", by="t")
        assert rc == 0, err

    def test_a_leftover_temp_with_our_own_pid_is_replaced_not_fatal(
        self, tmp_path: pathlib.Path
    ) -> None:
        # The CLI case above runs in a child process with a different pid, so
        # it cannot plant the exact name. This one calls write_atomic in-process,
        # where os.getpid() is the pid the temp name is built from.
        target = tmp_path / "plan.md"
        target.write_text("old\n")
        stale = tmp_path / f".tmp.{os.getpid()}.plan.md"
        stale.write_text("half a write")
        store.write_atomic(str(target), "new\n")
        assert target.read_text() == "new\n"
        assert not stale.exists()

    def test_a_failed_write_leaves_no_temp_behind(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "plan.md"
        target.write_text("old\n")

        def refuse(src: str, dst: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            store.write_atomic(str(target), "new\n")
        assert target.read_text() == "old\n"
        assert [p.name for p in tmp_path.iterdir()] == ["plan.md"]

    def test_a_corrupt_config_is_one_line_not_a_traceback(self, tree: Tree) -> None:
        with open(os.path.join(tree.path, ".planlab.yaml"), "w") as fh:
            fh.write("track: t\n  bad: indentation\n")
        rc, out, err = tree.run("validate")
        assert rc == 1
        assert "Traceback" not in err
        assert "does not parse" in out + err
        # and the operator's own diagnostic command says so rather than
        # reporting a healthy tree while every write is broken
        rc, out, err = tree.run("status")
        assert "does not parse" in out + err

    def test_body_is_untouched_by_a_frontmatter_write(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        path = plan("open", PID, body="\n## Keep me\n\n  indented  \n", claimed_by="s")
        before = slurp(path).split("---\n", 2)[2]
        tree.run("set", PID, "verdict=clean", by="t")
        assert slurp(path).split("---\n", 2)[2] == before


# ── body writers ──────────────────────────────────────────────────────────


class TestWriteCommands:
    def test_append_section_adds_one_section_and_one_worked_by_line(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, body="\nOriginal body.\n", claimed_by="s")
        rc, _, err = tree.run(
            "append-section",
            PID,
            "--heading",
            "Build log 2026-09-22",
            by="worker:builder",
            stdin="two things changed\n",
        )
        assert rc == 0, err
        state, name = tree.find(PID)
        assert state and name
        text = tree.read(state, name)
        assert "## Build log 2026-09-22" in text
        assert "two things changed" in text
        assert "Original body." in text
        assert len(tree.fm_of(PID)["worked_by"]) == 1

    def test_rewrite_body_keeps_frontmatter_byte_identical(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        path = plan("drafts", "idea", body="\nold\n")
        before_fm = slurp(path).split("---\n")[1]
        scratch = os.path.join(tree.root, "new.md")
        with open(scratch, "w") as fh:
            fh.write("# New\n\nbrand new body\n")
        rc, _, err = tree.run("rewrite-body", "idea", "--from", scratch, by="t")
        assert rc == 0, err
        after = slurp(path)
        assert "brand new body" in after
        assert "old" not in after.split("---\n", 2)[2]
        # frontmatter keeps every original key, plus worked_by
        for line in before_fm.strip().splitlines():
            assert line.split(":")[0] in after.split("---\n")[1]

    def test_two_concurrent_append_sections_both_land(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, claimed_by="s")
        delay = {"PLANLAB_TEST_LOCK_DELAY": "0.3"}
        procs = [
            tree.popen(
                "append-section", PID, "--heading", f"H{i}", by=f"p{i}", env=delay, stdin=True
            )
            for i in range(2)
        ]
        for i, p in enumerate(procs):
            p.communicate(f"body {i}\n")
        state, name = tree.find(PID)
        assert state and name
        text = tree.read(state, name)
        assert "## H0" in text
        assert "## H1" in text
        assert len(tree.fm_of(PID)["worked_by"]) == 2


# ── the host guard ────────────────────────────────────────────────────────


class TestHostGuard:
    def test_mutating_command_refused_on_another_host(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("open", PID, claimed_by="s")
        rc, out, err = tree.run("set", PID, "verdict=clean", by="t", env={"PLANLAB_HOST": "laptop"})
        assert rc != 0
        assert "laptop" in out + err
        assert "testhost" in out + err

    def test_reads_work_on_any_host(self, tree: Tree, plan: Callable[..., str]) -> None:
        plan("open", PID, claimed_by="s")
        rc, _, err = tree.run("status", env={"PLANLAB_HOST": "laptop"})
        assert rc == 0, err


# ── ids are path segments ─────────────────────────────────────────────────


class TestIdentifiers:
    """An id is a path segment in four modules. It is never taken on trust."""

    def test_a_traversing_id_never_reaches_the_filesystem(self, tree: Tree) -> None:
        victim = os.path.join(tree.root, "victim")
        os.makedirs(victim)
        with open(os.path.join(victim, "keep"), "w") as fh:
            fh.write("do not delete me\n")
        for pid in ("../../victim", victim):
            rc, out, err = tree.run("worktree", "remove", pid, by="t")
            assert rc != 0, pid
            assert "not a plan id" in out + err, pid
        assert os.path.exists(os.path.join(victim, "keep"))

    def test_a_traversing_id_creates_no_lock_file(self, tree: Tree) -> None:
        target = os.path.join(tree.root, "lockbait")
        os.makedirs(target)
        rc, _, _ = tree.run("set", os.path.join(target, "planted"), "x=1", by="t")
        assert rc != 0
        assert os.listdir(target) == []

    @pytest.mark.parametrize(
        "argv",
        [
            ["claim", "../escape"],
            ["release", "../escape"],
            ["verify-claim", "../escape"],
            ["worktree", "add", "../escape"],
        ],
        ids=lambda argv: argv[0],
    )
    def test_every_id_taking_command_validates(self, tree: Tree, argv: list[str]) -> None:
        rc, _, err = tree.run(*argv, by="t")
        assert rc != 0
        assert "Traceback" not in err


# ── flag placement ────────────────────────────────────────────────────────


class TestFlagPlacement:
    """--tree and --by work on either side of the subcommand."""

    def test_tree_after_the_subcommand(
        self, tree: Tree, plan: Callable[..., str], run: Callable[..., Result]
    ) -> None:
        plan("open", PID, claimed_by="s")
        rc, out, err = run("status", "--tree", tree.path)
        assert rc == 0, err
        assert PID in out

    def test_by_after_the_subcommand(
        self, tree: Tree, plan: Callable[..., str], run: Callable[..., Result]
    ) -> None:
        plan("open", PID, claimed_by="s")
        rc, _, err = run(
            "set", PID, "verdict=clean", "--tree", tree.path, "--by", "worker:verifier"
        )
        assert rc == 0, err
        assert "worker:verifier" in tree.fm_of(PID)["worked_by"][0]

    def test_init_without_a_tree_refuses_instead_of_scaffolding_the_cwd(
        self, tree: Tree, run: Callable[..., Result]
    ) -> None:
        rc, out, err = run("init", "--track", "t", cwd=tree.root)
        assert rc == 2
        assert "will not scaffold into the cwd" in out + err
        assert not os.path.exists(os.path.join(tree.root, "drafts"))

    def test_rewrite_body_from_a_missing_file_refuses(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("drafts", "idea")
        rc, out, err = tree.run("rewrite-body", "idea", "--from", "/nope/missing.md", by="t")
        assert rc != 0
        assert "Traceback" not in err
        assert "no such file" in (out + err).lower()

    def test_init_expands_a_tilde_in_every_path_it_stores(
        self, tree: Tree, run: Callable[..., Result]
    ) -> None:
        fresh = os.path.join(tree.root, "tilde")
        rc, _, err = run(
            "init", "--tree", fresh, "--track", "t", "--host", "testhost", "--build-repo", "~/r"
        )
        assert rc == 0, err
        cfg = yaml.safe_load(slurp(os.path.join(fresh, ".planlab.yaml")))
        assert not cfg["build_repo"].startswith("~")

    def test_init_takes_tree_after_the_subcommand(
        self, tree: Tree, run: Callable[..., Result]
    ) -> None:
        fresh = os.path.join(tree.root, "fresh")
        rc, _, err = run("init", "--tree", fresh, "--track", "t", "--host", "testhost")
        assert rc == 0, err
        assert os.path.isdir(os.path.join(fresh, "drafts"))

    def test_the_flag_before_the_subcommand_still_wins_when_the_other_is_absent(
        self, tree: Tree, plan: Callable[..., str]
    ) -> None:
        plan("drafts", "idea")
        rc, out, err = tree.run("status", by="before")
        assert rc == 0, err
        assert "idea" in out


# ── tree resolution ───────────────────────────────────────────────────────


class TestTreeResolution:
    def test_explicit_flag_beats_environment(self, tree: Tree) -> None:
        rc, out, err = tree.run("tree", env={"PLANLAB_TREE": "/nope"})
        assert rc == 0, err
        assert out.strip() == tree.path

    def test_environment_is_used_when_no_flag(self, tree: Tree, run: Callable[..., Result]) -> None:
        rc, out, err = run("tree", env={"PLANLAB_TREE": tree.path})
        assert rc == 0, err
        assert out.strip() == tree.path

    def test_no_tree_exits_two(self, tree: Tree, run: Callable[..., Result]) -> None:
        rc, out, err = run("tree", cwd=tree.root)
        assert rc == 2
        assert "no plan tree" in (out + err).lower()
