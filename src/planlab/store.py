"""The plan tree's reader and its only writer.

Tree resolution, scanning, per-plan locks, new-id, set, append-section and
rewrite-body. The frontmatter codec lives in `planlab.frontmatter`, the
validity rules in `planlab.rules`, and the status view in `planlab.index`.

Everything that changes a plan file goes through `edit()` or `transition()`.
Both hold a per-plan flock kept OUTSIDE the plan tree (the tree may sit in a
synced folder, where lock files would be copied between machines) and record
one `worked_by` line per write. Workers never edit a plan file directly.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import fcntl
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import Any

import yaml

from planlab.errors import FrontmatterError, LostRace, PlanlabError
from planlab.frontmatter import (
    Frontmatter,
    lossy_keys,
    parse_frontmatter,
    render,
    split_frontmatter,
)

Config = dict[str, Any]
Problem = dict[str, Any]
# (frontmatter, body, state, path) -> (frontmatter, body). Runs under the lock.
Mutator = Callable[[Frontmatter, str, str, str], tuple[Frontmatter, str]]

STATES = ("drafts", "next", "open", "done", "discarded")
TERMINAL_STATES = ("done", "discarded")
FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*\.md$")
PLAN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DATE_PREFIX_LEN = len("YYYY-MM-DD-")
VERIFY_MARKER = ".verifying-"
CONFIG_NAME = ".planlab.yaml"
SYNC_CACHE = ".sync.json"

# `+=` is only defined for these; the value type is how a token is coerced.
LIST_KEYS: dict[str, type] = {"prs": int, "artifacts": str, "related": str, "depends_on": str}
# `id` is the filename, not a field: changing one without the other splits
# find_plan (matches the name) from by_id (reads the frontmatter) and silently
# dangles every depends_on pointing at the old value. `worked_by` is the audit
# trail, which is only ever appended to by the store itself; no caller has a
# legitimate reason to set it.
IMMUTABLE_KEYS = ("id", "worked_by")
LOST = "{pid}: lost the race — already claimed or moved into {state}/ by another process"
DRIFT_VERDICTS = ("drift", "partial")
STALE_CLAIM_HOURS = 24


def id_from_name(name: str) -> str:
    """The id a plan filename encodes: strip the date prefix and the suffix."""
    return name[DATE_PREFIX_LEN:-3]


def require_id(pid: object) -> str:
    """Ids reach os.path.join in four modules; validate once, trust after."""
    if not pid or not PLAN_ID_RE.match(str(pid)):
        raise PlanlabError(
            f"{pid!r} is not a plan id (expected {PLAN_ID_RE.pattern}); ids are path "
            "segments and are never taken on trust"
        )
    return str(pid)


def utc_now() -> datetime.datetime:
    """Naive UTC. datetime.utcnow() is deprecated and removal is scheduled."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def as_naive_utc(value: object) -> datetime.datetime:
    """A YAML timestamp may come back tz-aware; subtraction needs one kind."""
    if isinstance(value, datetime.datetime):
        when = value
    else:
        when = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.tzinfo is not None:
        when = when.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return when


def slurp(path: str) -> str:
    with open(path, newline="") as fh:
        return fh.read()


# ── config and tree resolution ────────────────────────────────────────────


def state_dir() -> str:
    """Locks and worktrees: $PLANLAB_STATE_DIR, else the XDG state dir."""
    explicit = os.environ.get("PLANLAB_STATE_DIR")
    if explicit:
        return explicit
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "planlab")


def plans_root() -> str:
    """Where a tree is looked up by repository name: $PLANLAB_ROOT, else ~/plans."""
    return os.path.expanduser(os.environ.get("PLANLAB_ROOT") or "~/plans")


def hostname() -> str:
    return os.environ.get("PLANLAB_HOST") or socket.gethostname()


def load_config(tree: str) -> Config:
    path = os.path.join(tree, CONFIG_NAME)
    if not os.path.exists(path):
        raise PlanlabError(f"not a plan tree (no {CONFIG_NAME}): {tree}")
    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise PlanlabError(f"{path} does not parse: {str(exc).splitlines()[0]}") from exc
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        raise PlanlabError(f"{path} is not a mapping (got {type(cfg).__name__})")
    return cfg


def resolve_tree(explicit: str | None = None) -> str:
    """--tree, else $PLANLAB_TREE, else the cwd's origin basename under plans_root()."""
    for cand in (explicit, os.environ.get("PLANLAB_TREE")):
        if cand:
            cand = os.path.abspath(os.path.expanduser(cand))
            if os.path.isdir(cand):
                return cand
            raise PlanlabError(f"no plan tree at {cand}")
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    if url:
        name = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1])
        cand = os.path.join(plans_root(), name)
        if os.path.isdir(cand):
            return cand
    raise PlanlabError(
        "no plan tree resolved: pass --tree, set $PLANLAB_TREE, or run inside a "
        f"checkout whose origin has a tree under {plans_root()}/"
    )


def require_host(tree: str, cfg: Config | None = None) -> None:
    cfg = cfg or load_config(tree)
    want = cfg.get("host")
    have = hostname()
    if want and want != have:
        raise PlanlabError(
            f"refusing to write from {have}: this tree is owned by {want} (host: in {CONFIG_NAME})"
        )


# ── plan loading ──────────────────────────────────────────────────────────


class Plan:
    __slots__ = ("body", "error", "fm", "id", "inode", "path", "state")

    def __init__(
        self,
        path: str,
        state: str,
        fm: Frontmatter,
        body: str,
        inode: int,
        error: str | None = None,
    ) -> None:
        self.path, self.state, self.fm = path, state, fm
        self.body, self.inode, self.error = body, inode, error
        self.id: str | None = fm.get("id")

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.fm.get(key, default)

    def list_of(self, key: str) -> list[Any]:
        v = self.fm.get(key) or []
        return v if isinstance(v, list) else [v]


def scan(tree: str) -> tuple[list[Plan], list[Problem]]:
    """(plans, problems). Anything not matching FILENAME_RE is a problem."""
    plans: list[Plan] = []
    problems: list[Problem] = []
    for state in STATES:
        d = os.path.join(tree, state)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            if name.startswith("."):
                continue  # verify markers and interrupted writes, never plans
            if not FILENAME_RE.match(name):
                problems.append(
                    {"path": f"{state}/{name}", "message": "not a plan filename; never parsed"}
                )
                continue
            try:
                raw = slurp(path)
                inode = os.stat(path).st_ino
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue  # claimed out from under us; the other copy stands
                problems.append(
                    {"path": f"{state}/{name}", "message": f"unreadable: {exc.strerror}"}
                )
                continue
            fm_text, body = split_frontmatter(raw)
            err: str | None
            try:
                fm = parse_frontmatter(fm_text)
                err = None
            except FrontmatterError as exc:
                fm, err = {}, str(exc)
            if err is None and "id" in fm and not isinstance(fm["id"], str):
                # A list or mapping id is unhashable and takes down by_id — and
                # with it status, validate and claim for every other plan.
                fm, err = {}, f"id must be a string, got {fm['id']!r}"
            plans.append(Plan(path, state, fm, body, inode, err))
    return plans, problems


def by_id(plans: Iterable[Plan]) -> dict[str, Plan]:
    """id -> plan, collapsing a same-inode next//open pair into the open one."""
    out: dict[str, Plan] = {}
    for p in plans:
        if not p.id:
            continue
        prev = out.get(p.id)
        if prev is None or (prev.inode == p.inode and p.state == "open"):
            out[p.id] = p
    return out


def duplicates(plans: Iterable[Plan]) -> list[tuple[Plan, Plan]]:
    seen: dict[str, Plan] = {}
    dupes: list[tuple[Plan, Plan]] = []
    for p in plans:
        if not p.id:
            continue
        prev = seen.get(p.id)
        if prev is None:
            seen[p.id] = p
        elif prev.inode != p.inode:
            dupes.append((p, prev))
    return dupes


# ── locking and mutation ──────────────────────────────────────────────────


def lock_path(tree: str, cfg: Config, pid: str) -> str:
    require_id(pid)
    d = os.path.join(state_dir(), "locks", cfg.get("track") or "default")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{pid}.lock")


def write_atomic(path: str, text: str) -> None:
    """Write through a private temp in the same directory, then rename.

    O_EXCL plus the pid keeps a planted symlink at a predictable name from
    turning this into a write to somewhere else, and keeps two writers from
    sharing one inode and publishing each other's half.
    """
    tmp = os.path.join(os.path.dirname(path) or ".", f".tmp.{os.getpid()}.{os.path.basename(path)}")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # A process killed between open and rename left this behind, and pids
        # get reused. It is ours by name and worthless by definition.
        os.unlink(tmp)
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def find_plan(tree: str, pid: str, states: Iterable[str] = STATES) -> tuple[str, str]:
    require_id(pid)
    for state in states:
        d = os.path.join(tree, state)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if FILENAME_RE.match(name) and id_from_name(name) == pid:
                return os.path.join(d, name), state
    raise PlanlabError(f"no plan with id {pid} in {tree}")


def stamp(by: str | None, action: str) -> str:
    when = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    return f"{hostname()}/{by or 'unknown'}@{when}:{action}"


def _refuse_if_lossy(path: str, fm_text: str | None, fm: Frontmatter) -> None:
    lossy = lossy_keys(fm_text, fm)
    if lossy:
        raise PlanlabError(
            f"{os.path.basename(path)}: {', '.join(sorted(lossy))} would lose text on "
            "write — an unquoted ' #' starts a YAML comment. Quote the value in the "
            "plan file, then retry."
        )


def check_writable(path: str) -> None:
    """Every precondition that must hold BEFORE a plan is moved or edited."""
    fm_text, _ = split_frontmatter(slurp(path))
    _refuse_if_lossy(path, fm_text, parse_frontmatter(fm_text))


def _apply(path: str, state: str, mutate: Mutator, by: str | None, action: str) -> None:
    """Read, mutate, stamp worked_by, write. Always called under the lock."""
    fm_text, body = split_frontmatter(slurp(path))
    fm = parse_frontmatter(fm_text)
    _refuse_if_lossy(path, fm_text, fm)
    fm, body = mutate(fm, body, state, path)
    history = fm.get("worked_by")
    if not isinstance(history, list):
        # A hand edit or a file-sync merge can leave a scalar here, and .append
        # on it used to traceback on EVERY write to that plan, permanently.
        history = [history] if history else []
    history.append(stamp(by, action))
    fm["worked_by"] = history
    # Test-only: widens the held-lock window so the concurrency cases actually
    # contend instead of finishing one after the other.
    delay = os.environ.get("PLANLAB_TEST_LOCK_DELAY")
    if delay:
        time.sleep(float(delay))
    write_atomic(path, render(fm, body))


def edit(
    tree: str,
    pid: str,
    action: str,
    by: str | None,
    mutate: Mutator,
    cfg: Config | None = None,
) -> tuple[str, str]:
    """Read, mutate (fm, body) -> (fm, body), write atomically, under flock."""
    cfg = cfg or load_config(tree)
    require_host(tree, cfg)
    lock = lock_path(tree, cfg, pid)
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            path, state = find_plan(tree, pid)
            _apply(path, state, mutate, by, action)
            return path, state
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def transition(
    tree: str,
    cfg: Config,
    pid: str,
    src_states: Iterable[str],
    dst_state: str,
    action: str,
    by: str | None,
    mutate: Mutator,
) -> str:
    """Move a plan between state directories, under the plan's own lock.

    The lock is the same one `edit()` takes, and that matters: without it an
    editor can read next/ while a claimer links the file into open/ and unlinks
    the source, and the editor's rename then RESURRECTS next/ with fresh content
    beside a stale open/ — both operations reporting success.

    Inside the lock the order is link, unlink, then edit. The link is what
    claims the destination, so a loser never mutates a plan it did not win.
    """
    require_host(tree, cfg)
    lock = lock_path(tree, cfg, pid)
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                src, state = find_plan(tree, pid, src_states)
            except PlanlabError:
                # The winner finished while we waited for the lock. That is the
                # same lost race as EEXIST below, not an invalid request. If the
                # plan is not at the destination either, that error stands.
                find_plan(tree, pid, (dst_state,))
                raise LostRace(LOST.format(pid=pid, state=dst_state)) from None
            # Preconditions run BEFORE the link: a refusal after it would leave
            # the plan moved but unedited, which is worse than either outcome.
            check_writable(src)
            dst = os.path.join(tree, dst_state, os.path.basename(src))
            try:
                os.link(src, dst)
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOENT):
                    raise LostRace(LOST.format(pid=pid, state=dst_state)) from exc
                raise
            os.unlink(src)
            _apply(dst, state, mutate, by, action)
            return dst
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ── writers ───────────────────────────────────────────────────────────────


def kebab(title: object) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return slug or "untitled"


def known_ids(tree: str) -> set[str]:
    """Every id the tree holds, from the filenames alone — no parse needed.

    An id that disagrees with its filename is a hard validate error, so reading
    the names is both sufficient and ~150 file opens cheaper on the capture path.
    """
    ids = set()
    for state in STATES:
        d = os.path.join(tree, state)
        for name in os.listdir(d) if os.path.isdir(d) else []:
            if FILENAME_RE.match(name):
                ids.add(id_from_name(name))
    return ids


def new_id(
    tree: str,
    title: str,
    by: str | None,
    today: str | None = None,
    source: str | None = None,
) -> str:
    """Reserve drafts/<date>-<id>.md with O_EXCL. Returns the path."""
    cfg = load_config(tree)
    require_host(tree, cfg)
    date = today or datetime.date.today().isoformat()
    base, taken, n = kebab(title), known_ids(tree), 1
    pid = base
    while True:
        path = os.path.join(tree, "drafts", f"{date}-{pid}.md")
        if pid not in taken:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            else:
                with os.fdopen(fd, "w") as fh:
                    # The reservation is a valid plan on its own: title, type and
                    # date are all knowable here, and a capture worker's only
                    # body-writing verb is rewrite-body, which cannot set them.
                    fm: Frontmatter = {
                        "id": pid,
                        "title": str(title),
                        "type": "plan",
                        "date": datetime.date.fromisoformat(date),
                    }
                    if source:
                        fm["source"] = source
                    fm["worked_by"] = [stamp(by, "new-id")]
                    fh.write(render(fm, "\n"))
                return path
        n += 1
        pid = f"{base}-{n}"


def coerce(value: str) -> Any:
    """Type a `set` value: 425 -> int, true -> bool, a date -> date.

    Anything that parses back as a STRING is returned raw. YAML string parsing
    is exactly where the lossy behaviour lives — `set note=see PR #39` used to
    store `see PR`, because an unquoted ` #` opens a comment. Nothing is gained
    by re-parsing a string the caller already typed, and a field can be lost.
    """
    try:
        out = yaml.safe_load(value)
    except yaml.YAMLError:
        return value
    if out is None or isinstance(out, str):
        return value
    return out


def _append_to_list(fm: Frontmatter, key: str, value: str) -> None:
    """`key+=value`: append to a typed list key, skipping duplicates."""
    if key not in LIST_KEYS:
        raise PlanlabError(
            f"{key} is not a list key; `+=` is defined for {', '.join(sorted(LIST_KEYS))}"
        )
    cast = LIST_KEYS[key]
    item: int | str
    try:
        # Only `prs` is typed. For the string-valued list keys the raw token IS
        # the value — coercing first mangles anything YAML reads as structure,
        # e.g. a wikilink `[[a/b]]` parses as a nested flow sequence and
        # stringifies as "[['a/b']]".
        item = int(coerce(value)) if cast is int else str(value)
    except (TypeError, ValueError):
        raise PlanlabError(f"{key} takes {cast.__name__} values; {value!r} is not one") from None
    current = fm.get(key) or []
    if not isinstance(current, list):
        current = [current]
    if item not in current:
        current.append(item)
    fm[key] = current


def set_fields(tree: str, pid: str, assignments: Iterable[str], by: str | None) -> tuple[str, str]:
    """k=v replaces; k+=v appends to a typed list without duplicates."""

    def mutate(fm: Frontmatter, body: str, state: str, path: str) -> tuple[Frontmatter, str]:
        for raw in assignments:
            if "+=" in raw and raw.index("+=") < raw.index("=") + 1:
                key, value = raw.split("+=", 1)
                _append_to_list(fm, key, value)
            elif "=" in raw:
                key, value = raw.split("=", 1)
                if key in IMMUTABLE_KEYS:
                    raise PlanlabError(
                        f"{key} is part of the plan's identity or its audit trail; it "
                        "cannot be set. Discard the plan and capture a new one."
                    )
                fm[key] = coerce(value)
            else:
                raise PlanlabError(f"expected k=v or k+=v, got {raw!r}")
        return fm, body

    return edit(tree, pid, "set", by, mutate)


def append_section(tree: str, pid: str, heading: str, text: str, by: str | None) -> tuple[str, str]:
    def mutate(fm: Frontmatter, body: str, state: str, path: str) -> tuple[Frontmatter, str]:
        block = f"## {heading}\n\n" + text.rstrip("\n") + "\n"
        if body.endswith("\n\n"):
            sep = ""
        elif body.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        return fm, body + sep + block

    return edit(tree, pid, "append-section", by, mutate)


def rewrite_body(tree: str, pid: str, text: str, by: str | None) -> tuple[str, str]:
    def mutate(fm: Frontmatter, body: str, state: str, path: str) -> tuple[Frontmatter, str]:
        return fm, text

    return edit(tree, pid, "rewrite-body", by, mutate)
