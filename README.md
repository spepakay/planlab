# planlab

A command-line tool that moves plan files through a lifecycle — `drafts/` →
`next/` → `open/` → `done/` or `discarded/` — for several worker processes that
claim and update them at the same time, in one shared folder. Each plan is a
Markdown file with YAML frontmatter; its state is the directory it sits in.
Correctness under concurrency is the core problem: two workers must never both
win a claim, an edit must never race a move, and no write may silently lose
text.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Requires Python 3.10+ on a POSIX system (it uses `fcntl.flock` and hard links).

## Usage

```bash
planlab init --tree ~/plans/app --track app --origin example/app --gh-user me --build-repo ~/src/app

planlab --tree ~/plans/app new-id "Tighten the retry policy"   # reserve drafts/<date>-<id>.md
planlab --tree ~/plans/app set tighten-the-retry-policy priority=P1 prs+=42
echo "Found two more call sites." | planlab --tree ~/plans/app append-section tighten-the-retry-policy --heading Notes

planlab --tree ~/plans/app claim tighten-the-retry-policy --by worker-a   # next/ -> open/, atomically
planlab --tree ~/plans/app worktree add tighten-the-retry-policy          # a git worktree for the plan's branch
planlab --tree ~/plans/app sync                                           # reconcile open/ with GitHub
planlab --tree ~/plans/app move tighten-the-retry-policy done

planlab --tree ~/plans/app status      # ready, blocked, in flight, drafts, problems
planlab --tree ~/plans/app validate    # dependency cycles, duplicates, orphans, stray files
planlab --tree ~/plans/app index       # regenerate the tree's README.md
```

`--tree` and `--by` are accepted before or after the subcommand. Without
`--tree`, the tree comes from `$PLANLAB_TREE`, else from the current checkout's
origin name under `$PLANLAB_ROOT` (default `~/plans`). Locks and worktrees live
under `$PLANLAB_STATE_DIR` (default `$XDG_STATE_HOME/planlab`).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | Refused or invalid: the tree said no, or the input was wrong |
| 2 | No plan tree could be resolved |
| 3 | Lost a race: another process owns this transition, so skip it |

Every refusal is one line on stderr, prefixed `planlab:`. A traceback means a
bug in planlab, never a refusal.

## Design notes

- **One writer.** Every change to a plan file goes through `store.edit()` or
  `store.transition()`. Nothing else opens a plan for writing, so the locking
  and audit rules live in exactly one place, and every write appends one
  `worked_by` line recording host, caller, time and action.
- **Per-plan locks outside the tree.** Each plan has its own `flock`, kept
  under the state directory rather than beside the plans: the tree may live in
  a synced folder, where lock files would be copied between machines.
- **Claims are hard links: link, then unlink, then edit.** `os.rename`
  overwrites on POSIX, so two workers could both "win". `os.link` fails with
  `EEXIST` atomically; the loser exits 3 and never touches a plan it did not
  win. Preconditions are checked before the link, because refusing after it
  would leave a plan moved but not updated.
- **Atomic writes.** Each write goes to a temp file opened with `O_EXCL` in the
  same directory, then `os.replace`s the original. `O_EXCL` stops a planted
  symlink redirecting the write, and a failed write deletes its temp file.
- **One exception type, documented exit codes.** Everything the operator should
  see is a `PlanlabError`; the CLI renders it as one line with the exit code
  its class carries. Losing a race is its own subclass because it is an
  expected outcome that callers skip, not a failure.
- **No silent YAML loss.** An unquoted ` #` starts a YAML comment, so
  `note: see issue #14` parses as `see issue`. Reading that is harmless;
  writing it back would drop text, so every write refuses until the value is
  quoted.

## Checks

```bash
.venv/bin/pytest            # the suite drives the real CLI as a subprocess
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy              # strict, over src/
```

The concurrency tests start real competing processes. The test-only
`PLANLAB_TEST_LOCK_DELAY` variable holds the lock longer so the processes
genuinely contend instead of finishing one after the other.

## License

MIT. See [LICENSE](LICENSE).
