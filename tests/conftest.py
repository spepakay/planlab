"""Fixtures: a fresh, isolated plan tree per test.

Locks, worktrees and the tree itself live under pytest's tmp_path, and the
environment is scrubbed of PLANLAB_* variables, so a test never touches the
real state directory or an ambient tree.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support import HOST, Result, Tree, invoke


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """An isolated environment: private state dir, fixed host, no ambient tree."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("PLANLAB_")}
    e.update(
        PLANLAB_STATE_DIR=str(tmp_path / "state"),
        PLANLAB_ROOT=str(tmp_path / "plans"),
        PLANLAB_HOST=HOST,
    )
    return e


@pytest.fixture
def run(env: dict[str, str]) -> Callable[..., Result]:
    """Invoke the CLI with no default tree: run(*args, tree=, by=, cwd=, env=, stdin=)."""

    def _run(*args: object, **kw: Any) -> Result:
        return invoke(env, *args, **kw)

    return _run


@pytest.fixture
def tree(tmp_path: Path, env: dict[str, str]) -> Tree:
    t = Tree(str(tmp_path), env)
    t.init()
    return t


@pytest.fixture
def plan(tree: Tree) -> Callable[..., str]:
    """plan(state, pid, date=..., body=..., **frontmatter) -> path."""
    return tree.plan
