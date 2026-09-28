"""planlab — the plan lifecycle's command surface.

Plan files move through drafts/ -> next/ -> open/ -> done/ or discarded/, and
several worker processes claim and update them concurrently in one shared
folder. Every caller goes through this one entry point:

    planlab <command> [--tree T] [--by WHO] ...

It resolves the tree, hands the work to the modules, and turns a PlanlabError
into one line on stderr and a documented exit code. It holds no logic of its
own beyond tree resolution.

Exit codes: 0 fine · 1 refused or invalid · 2 no tree resolved · 3 lost a race.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

import yaml

from planlab import flow
from planlab import index as X
from planlab import rules as R
from planlab import store as S
from planlab import sync as Y
from planlab.errors import PlanlabError

Command = Callable[[argparse.Namespace], int]

CONFIG_DEFAULTS: dict[str, Any] = {
    "track": None,
    "origin": None,
    "gh_user": None,
    "base": "develop",
    "terminal": "main",
    "branch_prefix": "feat",
    "build_repo": None,
    "host": None,
}


class Refused(PlanlabError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def tree_of(args: argparse.Namespace) -> str:
    try:
        return S.resolve_tree(getattr(args, "tree", None))
    except PlanlabError as exc:
        raise Refused(str(exc), 2) from exc


# ── commands that live here ───────────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    if not args.tree:
        # abspath("") is the cwd, so this has to be checked BEFORE expanding:
        # without it, `init` scaffolds five directories into whatever checkout
        # the caller happened to be standing in.
        raise Refused("init needs --tree; it will not scaffold into the cwd", 2)
    tree = os.path.abspath(os.path.expanduser(args.tree))
    for state in S.STATES:
        os.makedirs(os.path.join(tree, state), exist_ok=True)
    path = os.path.join(tree, S.CONFIG_NAME)
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(yaml.safe_load(fh) or {})
    for key in CONFIG_DEFAULTS:
        value = getattr(args, key, None)
        if value in (None, ""):
            continue
        cfg[key] = os.path.expanduser(value) if key == "build_repo" else value
    cfg["host"] = cfg["host"] or S.hostname()
    with open(path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=True, default_flow_style=False)
    print(tree)
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    print(tree_of(args))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    tree = tree_of(args)
    report = R.validate(tree)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for kind in ("errors", "warnings", "problems"):
            for row in report[kind]:
                print(f"{kind[:-1]:<8} {row['message']}")
        if not any(report.values()):
            print("clean")
    return 1 if report["errors"] else 0


def cmd_index(args: argparse.Namespace) -> int:
    tree = tree_of(args)
    view = X.status(tree)
    path = X.write_readme(tree, view)
    if not args.quiet:
        print(path)
    # status() already validated; a third scan of the tree buys nothing.
    return 1 if view["error_count"] else 0


def cmd_status(args: argparse.Namespace) -> int:
    view = X.status(tree_of(args), show_all=args.all)
    if args.json:
        print(json.dumps(view, indent=2, default=str))
    else:
        print(X.render_status(view), end="")
    return 0


def cmd_new_id(args: argparse.Namespace) -> int:
    print(S.new_id(tree_of(args), args.title, args.by, source=args.source))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    S.set_fields(tree_of(args), args.id, args.assignment, args.by)
    return 0


def cmd_append_section(args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    if not text.strip():
        raise Refused("append-section reads the section body on stdin; got nothing")
    S.append_section(tree_of(args), args.id, args.heading, text, args.by)
    return 0


def cmd_rewrite_body(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.source):
        raise Refused(
            f"no such file: {args.source} (rewrite-body reads the new body from a "
            "file you wrote first)"
        )
    S.rewrite_body(tree_of(args), args.id, S.slurp(args.source), args.by)
    return 0


# ── commands the other modules own ────────────────────────────────────────


def _with_tree(fn: Callable[[str, argparse.Namespace], int]) -> Command:
    def run(args: argparse.Namespace) -> int:
        return fn(tree_of(args), args)

    return run


# ── argument parsing ──────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="planlab", description=(__doc__ or "").splitlines()[0])
    p.add_argument("--tree", help="the plan tree; else $PLANLAB_TREE, else from cwd")
    p.add_argument("--by", default="cli", help="who is acting, for worked_by")

    # --tree and --by are accepted on either side of the subcommand. Callers
    # tend to write them after it, and SUPPRESS keeps an absent copy from
    # clobbering the value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tree", default=argparse.SUPPRESS)
    common.add_argument("--by", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="command", required=True)

    def command(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help)

    init = command("init", help="create the five state dirs and .planlab.yaml")
    for key in CONFIG_DEFAULTS:
        init.add_argument("--" + key.replace("_", "-"), dest=key)
    init.set_defaults(func=cmd_init)

    command("tree", help="print the resolved tree").set_defaults(func=cmd_tree)

    v = command("validate", help="parse every plan and report problems")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_validate)

    i = command("index", help="regenerate README.md")
    i.add_argument("--quiet", action="store_true")
    i.set_defaults(func=cmd_index)

    st = command("status", help="ready, blocked, in flight, drafts, problems")
    st.add_argument("--all", action="store_true", help="include hidden drafts")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    n = command("new-id", help="reserve a drafts/ file and print its path")
    n.add_argument("title")
    n.add_argument("--source", help="where this came from: a path, a ticket id, verify:<id>")
    n.set_defaults(func=cmd_new_id)

    s = command("set", help="frontmatter edit: k=v, or k+=v on a list key")
    s.add_argument("id")
    s.add_argument("assignment", nargs="+")
    s.set_defaults(func=cmd_set)

    a = command("append-section", help="append a ## section read from stdin")
    a.add_argument("id")
    a.add_argument("--heading", required=True)
    a.set_defaults(func=cmd_append_section)

    r = command("rewrite-body", help="replace the body, keep the frontmatter")
    r.add_argument("id")
    r.add_argument("--from", dest="source", required=True)
    r.set_defaults(func=cmd_rewrite_body)

    c = command("claim", help="next/ -> open/, atomically")
    c.add_argument("id")
    c.set_defaults(func=_with_tree(flow.claim))

    rel = command("release", help="open/ -> next/, clear the claim")
    rel.add_argument("id")
    rel.add_argument("--force", action="store_true", help="release a plan claimed by someone else")
    rel.set_defaults(func=_with_tree(flow.release))

    vc = command("verify-claim", help="take the verification marker")
    vc.add_argument("id")
    vc.set_defaults(func=_with_tree(flow.verify_claim))

    mv = command("move", help="terminal move to done/ or discarded/")
    mv.add_argument("id")
    mv.add_argument("target", choices=["done", "discarded"])
    mv.add_argument("--why")
    mv.add_argument("--force", action="store_true")
    mv.set_defaults(func=_with_tree(flow.move))

    w = command("worktree", help="create or remove the plan's worktree")
    w.add_argument("action", choices=["add", "remove"])
    w.add_argument("id")
    w.set_defaults(func=_with_tree(flow.worktree))

    sy = command("sync", help="reconcile open/ with GitHub")
    sy.add_argument("--dry-run", action="store_true")
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(func=_with_tree(Y.sync))

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func: Command = args.func
    try:
        return func(args) or 0
    except PlanlabError as exc:
        sys.stderr.write(f"planlab: {exc}\n")
        return exc.code
