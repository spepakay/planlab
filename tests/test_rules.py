"""Validation: dependencies, cycles, duplicates, stray files and half-finished claims."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import pytest
from support import Tree

Plan = Callable[..., str]


def messages(rows: list[dict[str, Any]]) -> str:
    return " ".join(r["message"] for r in rows)


def ready_ids(tree: Tree) -> list[str]:
    return [r["id"] for r in tree.status_json()["ready"]]


def test_dangling_dependency_is_an_error(tree: Tree, plan: Plan) -> None:
    plan("next", "a", depends_on=["nope"])
    rc, v = tree.validate_json()
    assert rc == 1
    assert "nope" in messages(v["errors"])


def test_three_cycle_is_an_error(tree: Tree, plan: Plan) -> None:
    plan("next", "a", depends_on=["b"])
    plan("next", "b", depends_on=["c"])
    plan("next", "c", depends_on=["a"])
    rc, v = tree.validate_json()
    assert rc == 1
    assert "cycle" in messages(v["errors"]).lower()


def test_duplicate_id_is_an_error(tree: Tree, plan: Plan) -> None:
    plan("next", "dup", date="2026-09-01")
    plan("drafts", "dup", date="2026-09-02")
    rc, v = tree.validate_json()
    assert rc == 1
    assert "duplicate" in messages(v["errors"]).lower()


def test_same_inode_pair_across_next_and_open_is_one_plan(tree: Tree, plan: Plan) -> None:
    path = plan("next", "mid")
    os.link(path, os.path.join(tree.path, "open", os.path.basename(path)))
    rc, v = tree.validate_json()
    assert rc == 0, v
    assert "duplicate" not in messages(v["errors"]).lower()


def test_an_id_that_disagrees_with_the_filename_is_an_error(tree: Tree) -> None:
    tree.write("next", "2026-09-01-alpha.md", "---\nid: beta\ntitle: B\n---\nb\n")
    rc, v = tree.validate_json()
    assert rc == 1
    assert "does not match the filename" in messages(v["errors"])


def test_an_interrupted_write_leaves_no_permanent_problem(tree: Tree, plan: Plan) -> None:
    # edit() renames a temp into place; a crash between the two used to
    # leave a stray that validate reported forever.
    plan("next", "alpha")
    tree.write("next", ".tmp.999.2026-09-01-alpha.md", "half written")
    rc, v = tree.validate_json()
    assert v["problems"] == []
    assert rc == 0, v["errors"]


def test_an_unhashable_id_does_not_take_down_the_whole_tree(tree: Tree, plan: Plan) -> None:
    # by_id() puts the id in a dict; a list id used to raise TypeError and
    # break status, validate and claim for every OTHER plan too.
    tree.write("next", "2026-09-01-broken.md", "---\nid: [a, b]\ntitle: T\n---\nb\n")
    plan("next", "healthy")
    rc, v = tree.validate_json()
    assert rc == 1
    assert "id must be a string" in messages(v["errors"])
    assert "healthy" in ready_ids(tree)


def test_a_half_claimed_plan_is_reported_once_the_window_has_passed(tree: Tree, plan: Plan) -> None:
    # link-then-unlink leaves the file in both dirs for microseconds. A
    # crash leaves it there forever, and validate used to call that clean
    # while claim refused it as a lost race.
    path = plan("next", "ghost")
    twin = os.path.join(tree.path, "open", os.path.basename(path))
    os.link(path, twin)
    assert tree.validate_json()[0] == 0  # fresh: transient
    old = time.time() - 3600
    os.utime(twin, (old, old))
    rc, v = tree.validate_json()
    assert rc == 1
    assert "half-claimed" in messages(v["errors"])


def test_an_unreadable_plan_is_a_problem_not_a_silence(tree: Tree, plan: Plan) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a 000 file anyway")
    path = plan("open", "locked-out", claimed_by="s")
    os.chmod(path, 0o000)
    try:
        _, v = tree.validate_json()
    finally:
        os.chmod(path, 0o644)
    assert "unreadable" in messages(v["problems"])


def test_status_key_is_an_error(tree: Tree, plan: Plan) -> None:
    plan("next", "a", status="ACTIVE")
    rc, v = tree.validate_json()
    assert rc == 1
    assert "status" in messages(v["errors"])


def test_stray_filenames_are_problems_and_are_never_parsed(tree: Tree) -> None:
    tree.write("next", "2026-09-01-thing (conflicted copy 2026-09-02).md", "not yaml: [")
    tree.write("next", "2026-09-01-thing.md~", "---\nid: x\n---\n")
    _, v = tree.validate_json()
    names = " ".join(p["path"] for p in v["problems"])
    assert "conflicted copy" in names
    assert ".md~" in names
    assert "x" not in [e.get("id") for e in v["errors"]]


def test_verify_marker_is_not_a_stray(tree: Tree, plan: Plan) -> None:
    plan("open", "api-cleanup", claimed_by="s")
    tree.write("open", ".verifying-api-cleanup", "2026-09-22T00:00:00 by=t\n")
    _, v = tree.validate_json()
    assert [p for p in v["problems"] if "verifying" in p["path"]] == []


def test_corrupt_frontmatter_is_a_per_file_error_others_still_reported(
    tree: Tree, plan: Plan
) -> None:
    tree.write("next", "2026-09-01-broken.md", "---\nfoo: a: b\n---\nbody\n")
    plan("next", "good", depends_on=["missing"])
    rc, v = tree.validate_json()
    assert rc == 1
    msgs = messages(v["errors"])
    assert "broken.md" in msgs
    assert "missing" in msgs


def test_dependency_on_a_discarded_id_blocks_the_claim(tree: Tree, plan: Plan) -> None:
    plan("discarded", "gone", why="superseded")
    plan("next", "a", depends_on=["gone"])
    rc, v = tree.validate_json()
    assert rc == 1
    assert "blocked by discarded" in messages(v["errors"])
    assert "a" not in ready_ids(tree)


def test_dependency_on_a_drifted_done_plan_is_unmet_until_its_drafts_are_done(
    tree: Tree, plan: Plan
) -> None:
    plan("done", "shipped", verdict="drift")
    plan("drafts", "followup", source="verify:shipped")
    plan("next", "a", depends_on=["shipped"])
    _, v = tree.validate_json()
    assert "blocked by drift of shipped" in messages(v["errors"])
    assert "a" not in ready_ids(tree)
    # once the drift draft is itself done, the dependency is met
    _, name = tree.find("followup")
    assert name
    os.rename(os.path.join(tree.path, "drafts", name), os.path.join(tree.path, "done", name))
    _, v = tree.validate_json()
    assert "blocked by drift" not in messages(v["errors"])
    assert "a" in ready_ids(tree)


def test_a_clean_done_dependency_is_met(tree: Tree, plan: Plan) -> None:
    plan("done", "shipped", verdict="clean")
    plan("next", "a", depends_on=["shipped"])
    rc, v = tree.validate_json()
    assert rc == 0, v
    assert "a" in ready_ids(tree)


def test_an_unclaimed_plan_in_open_is_an_orphan(tree: Tree, plan: Plan) -> None:
    plan("open", "orphan")
    rc, v = tree.validate_json()
    assert rc == 1
    assert any("orphan" in e["message"] and e["id"] == "orphan" for e in v["errors"])
