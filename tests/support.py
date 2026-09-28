"""Test support: drive the real CLI and seed or inspect a plan tree.

Every test runs `python -m planlab` as a subprocess, because that is the
surface callers actually use.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, NamedTuple

from planlab.frontmatter import parse_frontmatter, split_frontmatter

CLI = [sys.executable, "-m", "planlab"]
HOST = "testhost"
STATES = ("drafts", "next", "open", "done", "discarded")


class Result(NamedTuple):
    rc: int
    out: str
    err: str


def slurp(path: str) -> str:
    with open(path, newline="") as fh:
        return fh.read()


def invoke(
    base_env: dict[str, str],
    *args: object,
    tree: str | None = None,
    by: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> Result:
    """Run the CLI once and capture (rc, stdout, stderr)."""
    argv = list(CLI)
    if tree is not None:
        argv += ["--tree", tree]
    if by is not None:
        argv += ["--by", by]
    argv += [str(a) for a in args]
    proc = subprocess.run(
        argv, cwd=cwd, env={**base_env, **(env or {})}, input=stdin, capture_output=True, text=True
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


class Tree:
    """An initialised plan tree under a temp root, plus helpers to seed and inspect it."""

    def __init__(self, root: str, env: dict[str, str]) -> None:
        self.root = root
        self.env = env
        self.path = os.path.join(root, "tree")
        self.repo = os.path.join(root, "repo")
        self.state = env["PLANLAB_STATE_DIR"]

    # ── driving the CLI ───────────────────────────────────────────────────
    def run(self, *args: object, **kw: Any) -> Result:
        kw.setdefault("tree", self.path)
        return invoke(self.env, *args, **kw)

    def popen(
        self, *args: object, by: str, env: dict[str, str] | None = None, stdin: bool = False
    ) -> subprocess.Popen[str]:
        """Start the CLI without waiting, for the concurrency cases."""
        return subprocess.Popen(
            [*CLI, "--tree", self.path, "--by", by, *(str(a) for a in args)],
            env={**self.env, **(env or {})},
            stdin=subprocess.PIPE if stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def init(self, **over: str) -> Result:
        opts = {
            "track": "demo",
            "origin": "example/app",
            "gh-user": "someone",
            "base": "develop",
            "terminal": "main",
            "branch-prefix": "feat",
            "build-repo": self.repo,
            "host": HOST,
            **over,
        }
        args: list[str] = []
        for key, value in opts.items():
            args += ["--" + key, value]
        result = self.run("init", *args)
        assert result.rc == 0, result.err
        return result

    # ── seeding ───────────────────────────────────────────────────────────
    def write(self, state: str, name: str, text: str) -> str:
        """Drop a file into a state directory verbatim. Returns its path."""
        path = os.path.join(self.path, state, name)
        with open(path, "w", newline="") as fh:
            fh.write(text)
        return path

    def plan(
        self, state: str, pid: str, date: str = "2026-09-01", body: str = "\nBody.\n", **fm: Any
    ) -> str:
        """Write a well-formed plan and return its path."""
        title = fm.pop("title", pid.replace("-", " ").title())
        lines = ["---", f"id: {pid}", f"title: {title}", "type: plan", f"date: {date}"]
        for key, value in fm.items():
            rendered = json.dumps(value) if isinstance(value, (list, dict)) else value
            lines.append(f"{key}: {rendered}")
        lines.append("---")
        return self.write(state, f"{date}-{pid}.md", "\n".join(lines) + "\n" + body)

    # ── inspecting ────────────────────────────────────────────────────────
    def read(self, state: str, name: str) -> str:
        return slurp(os.path.join(self.path, state, name))

    def listdir(self, state: str) -> list[str]:
        return os.listdir(os.path.join(self.path, state))

    def find(self, pid: str) -> tuple[str | None, str | None]:
        """(state, filename) for an id, or (None, None)."""
        for state in STATES:
            for name in sorted(self.listdir(state)):
                if name.endswith(f"-{pid}.md"):
                    return state, name
        return None, None

    def state_of(self, pid: str) -> str | None:
        return self.find(pid)[0]

    def fm_of(self, pid: str) -> dict[str, Any]:
        """Parse an id's frontmatter with the CLI's own reader."""
        state, name = self.find(pid)
        assert state and name, f"no such plan: {pid}"
        return parse_frontmatter(split_frontmatter(self.read(state, name))[0])

    def status_json(self, *extra: str) -> dict[str, Any]:
        result = self.run("status", "--json", *extra)
        assert result.rc == 0, result.err
        data: dict[str, Any] = json.loads(result.out)
        return data

    def validate_json(self) -> tuple[int, dict[str, Any]]:
        result = self.run("validate", "--json")
        return result.rc, json.loads(result.out)

    def worktree_dir(self, pid: str) -> str:
        return os.path.join(self.state, "worktrees", "demo", pid)


def git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def make_repo(path: str, branches: tuple[str, ...] = ()) -> str:
    """A scratch git repository with an initial commit on main and a develop branch."""
    os.makedirs(path, exist_ok=True)
    e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q", "-b", "main", path], check=True, env=e)
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    with open(os.path.join(path, "README"), "w") as fh:
        fh.write("x\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    git(path, "branch", "develop")
    for branch in branches:
        git(path, "branch", branch)
    return path
