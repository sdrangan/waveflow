"""Timing events: what ran, inside what, and for how long.

Every build step, toolchain run and MCP tool call is a **span**: a named interval with a kind, a
start, an elapsed time, an outcome and a parent.  Spans nest the way the work does -- a ``csynth``
step holds one inner step per top, each holding the ``vitis-run`` it launched -- because the
parent is whatever span is open when a new one starts.  Nothing that opens a span needs to know
who is around it, so a :class:`~waveflow.build.build.BuildDag` run from inside another DAG's step
nests under that step with no code of its own.

Each span is appended, as one JSON line, to the **project's** event log::

    <project>/.waveflow/events.jsonl

The project is the ``root_dir`` of the outermost ``BuildDag.run`` (see :func:`logging_to`), or,
outside any build -- an MCP tool call -- the current directory.  Two projects keep two logs; two
builds of one project, or a build and the MCP server, append to the same one -- each line in one
write, under a file lock (:func:`_append_line` says why append mode alone is not enough on
Windows).  ``.jsonl`` (JSON Lines) is one JSON object per line: an event is added by appending a
line, never by rewriting the file, and a crash costs at most the last line.

Reading them back: :func:`load_events` and :func:`analyze_events` (where the time went, by
category, and per-name statistics -- what a pysim costs against an RTL simulation, for an agent
deciding how often it can afford the latter), :func:`format_tree` for one build's nested timings.
A caller can also take the events of one block of work directly, with :func:`collect`.

Environment:

``WAVEFLOW_EVENTS=off``
    Record nothing (the test suite sets this, so test runs leave no logs behind).
``WAVEFLOW_EVENTS_FILE``
    Log here instead of the project's file -- also how a child process joins its parent's log.
``WAVEFLOW_EVENT_PARENT``
    The span a child process's spans nest under; :func:`child_env` sets both for a subprocess.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

__all__ = [
    "EVENTS_FILE",
    "CATEGORIES",
    "span",
    "record",
    "collect",
    "logging_to",
    "log_path",
    "child_env",
    "load_events",
    "category",
    "analyze_events",
    "format_analysis",
    "format_tree",
]

#: The log, relative to the project directory.
EVENTS_FILE = Path(".waveflow") / "events.jsonl"

ENV_OFF = "WAVEFLOW_EVENTS"
ENV_FILE = "WAVEFLOW_EVENTS_FILE"
ENV_PARENT = "WAVEFLOW_EVENT_PARENT"

_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "waveflow_event_parent", default=os.environ.get(ENV_PARENT) or None)
_log: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "waveflow_event_log", default=None)
_sinks: contextvars.ContextVar[tuple[list, ...]] = contextvars.ContextVar(
    "waveflow_event_sinks", default=())


# ---------------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------------


def _off() -> bool:
    return os.environ.get(ENV_OFF, "").strip().lower() in ("off", "0", "false", "no")


def log_path() -> Path | None:
    """Where spans go now, or ``None`` when logging is off."""
    if _off():
        return None
    chosen = _log.get()
    if chosen is not None:
        return chosen
    env = os.environ.get(ENV_FILE)
    return Path(env) if env else Path.cwd() / EVENTS_FILE


@contextlib.contextmanager
def logging_to(project_dir: str | os.PathLike[str]) -> Iterator[None]:
    """Log the spans opened inside to *project_dir*'s event log.

    A no-op when an outer scope (or ``WAVEFLOW_EVENTS_FILE``) already chose a log: a nested
    build's spans belong in the log of the build that started it.
    """
    if _log.get() is not None or os.environ.get(ENV_FILE):
        yield
        return
    token = _log.set(Path(project_dir).resolve() / EVENTS_FILE)
    try:
        yield
    finally:
        _log.reset(token)


def _emit(event: dict[str, Any]) -> None:
    for sink in _sinks.get():
        sink.append(event)
    path = log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_line(path, (json.dumps(event, default=str) + "\n").encode("utf-8"))
    except OSError:
        pass                     # a timing log must never fail the work it is timing


#: Serializes this process's writers; the file lock in :func:`_append_line` serializes processes.
_write_lock = threading.Lock()

#: How long a writer waits for another process's lock before dropping its event.
LOCK_TIMEOUT_S = 10.0


def _append_line(path: Path, data: bytes) -> None:
    """Append *data* (one whole line) to *path*, safely against other threads and processes.

    Append mode alone is not enough.  POSIX makes an ``O_APPEND`` write land at the end
    atomically, but Windows emulates append as seek-to-end then write -- two steps, so two
    processes (the MCP server and a build, or two builds) can both seek to the same end and the
    second write overwrites the first.  And a buffered text file may split one long line into
    several writes, which other threads can interleave with.  So: one unbuffered ``os.write`` of
    the whole line, under a thread lock and an OS file lock held on byte 0 of the log.  If the
    lock cannot be had within :data:`LOCK_TIMEOUT_S`, the event is dropped -- never the build.
    """
    with _write_lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
                     0o644)
        try:
            if not _lock(fd):
                return
            try:
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, data)
            finally:
                _unlock(fd)
        finally:
            os.close(fd)


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> bool:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.005)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(fd: int) -> bool:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.005)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def span(kind: str, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Time the block as one span; spans opened inside it are its children.

    Yields the event dict, so the block can add to it (``ev["ok"] = False`` for a failure it
    caught itself, ``ev["attrs"]["cycles"] = n``).  An exception escaping the block marks the
    span failed and propagates.
    """
    sid = uuid.uuid4().hex[:12]
    event: dict[str, Any] = {"id": sid, "parent": _parent.get(), "kind": kind, "name": name,
                             "start": time.time(), "pid": os.getpid(), "attrs": dict(attrs)}
    token = _parent.set(sid)
    started = time.perf_counter()
    failed = False
    try:
        yield event
    except BaseException:
        failed = True
        raise
    finally:
        _parent.reset(token)
        event["elapsed"] = time.perf_counter() - started
        event["end"] = event["start"] + event["elapsed"]
        event.setdefault("ok", not failed)
        if not event["attrs"]:
            del event["attrs"]
        _emit(event)


def record(kind: str, name: str, *, elapsed: float = 0.0, ok: bool = True,
           start: float | None = None, **attrs: Any) -> None:
    """One span that already happened (or took no time: a build step that was up to date).

    It ends now unless *start* says when it began; its parent is the span open now.
    """
    begin = time.time() - elapsed if start is None else start
    event: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "parent": _parent.get(), "kind": kind,
                             "name": name, "start": begin, "end": begin + elapsed,
                             "elapsed": elapsed, "ok": ok, "pid": os.getpid()}
    if attrs:
        event["attrs"] = attrs
    _emit(event)


@contextlib.contextmanager
def collect() -> Iterator[list[dict[str, Any]]]:
    """Hand the caller every span that closes inside the block, nested ones included."""
    out: list[dict[str, Any]] = []
    token = _sinks.set(_sinks.get() + (out,))
    try:
        yield out
    finally:
        _sinks.reset(token)


def child_env() -> dict[str, str]:
    """Environment for a subprocess whose own spans should join this log, under this span."""
    env: dict[str, str] = {}
    path = log_path()
    if path is None:
        env[ENV_OFF] = "off"
        return env
    env[ENV_FILE] = str(path)
    if _parent.get():
        env[ENV_PARENT] = _parent.get()          # type: ignore[assignment]
    return env


# ---------------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------------

#: The categories, most specific first: where two overlap in time, the earlier one is charged.
CATEGORIES = ("synth", "rtl sim", "pysim", "other build", "mcp")

#: A span's own category, from its name; a span that matches none takes its parent's.  csim is
#: C simulation, so "other build"; a ``vitis-run`` inside ``check_cosim`` is RTL simulation.
_RULES = (
    ("rtl sim", r"xsi|cosim|rtlsim|rtl_sim|xsim|xelab|rtl_timing"),
    ("synth", r"csynth|synth|system_rtl|impl|vivado"),
    ("pysim", r"pysim|py_sim"),
)


def load_events(project_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every span in every event log under *project_dir* (its own and its subprojects')."""
    root = Path(project_dir)
    logs = [root / EVENTS_FILE] if (root / EVENTS_FILE).is_file() else []
    logs += [p for p in root.rglob("events.jsonl")
             if p.parent.name == ".waveflow" and p != root / EVENTS_FILE]
    out: list[dict[str, Any]] = []
    for log in logs:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and "start" in ev and "end" in ev:
                out.append(ev)
    out.sort(key=lambda e: e["start"])
    return out


def category(event: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    """The category *event* is charged to: its own name's, else its nearest ancestor's."""
    seen = set()
    ev: dict[str, Any] | None = event
    while ev is not None and ev["id"] not in seen:
        seen.add(ev["id"])
        if ev.get("kind") == "mcp":
            return "mcp"
        for cat, pattern in _RULES:
            if re.search(pattern, str(ev.get("name", "")), re.IGNORECASE):
                return cat
        ev = by_id.get(ev.get("parent") or "")
    return "other build"


def analyze_events(project_dir: str | os.PathLike[str] | None = None, *,
                   events: list[dict[str, Any]] | None = None,
                   since: float | None = None, until: float | None = None) -> dict[str, Any]:
    """Where the time went, from the event logs under *project_dir* (or from *events*).

    Returns:

    ``categories``
        Seconds per category, each second charged once, to the most specific category running
        then (:data:`CATEGORIES`) -- so a build step and the toolchain run inside it are not
        counted twice.
    ``by_name``
        Per ``(kind, name)``: how many ran, and their total, mean, min and max seconds -- the cost
        of one pysim against one RTL simulation, read from the project's own history.  Steps that
        were up to date are counted apart, as ``skipped``.
    ``first``, ``last``, ``n_events``
        The span of time covered, and how many spans.
    """
    evs = load_events(project_dir) if events is None else list(events)
    if since is not None:
        evs = [e for e in evs if e["end"] >= since]
    if until is not None:
        evs = [e for e in evs if e["start"] <= until]
    by_id = {e["id"]: e for e in evs if "id" in e}

    intervals = [(e["start"], e["end"], category(e, by_id)) for e in evs if e["end"] > e["start"]]
    seconds = {c: 0.0 for c in CATEGORIES}
    edges = sorted({x for a, b, _ in intervals for x in (a, b)})
    for a, b in zip(edges, edges[1:]):
        live = [c for s, t, c in intervals if s <= a and t >= b]
        if live:
            seconds[min(live, key=CATEGORIES.index)] += b - a

    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for e in evs:
        key = (str(e.get("kind")), str(e.get("name")))
        s = stats.setdefault(key, {"kind": key[0], "name": key[1], "category": category(e, by_id),
                                   "count": 0, "skipped": 0, "failed": 0, "total": 0.0,
                                   "min": None, "max": None})
        if (e.get("attrs") or {}).get("skipped"):
            s["skipped"] += 1
            continue
        s["count"] += 1
        s["failed"] += 0 if e.get("ok", True) else 1
        s["total"] += e["elapsed"]
        s["min"] = e["elapsed"] if s["min"] is None else min(s["min"], e["elapsed"])
        s["max"] = e["elapsed"] if s["max"] is None else max(s["max"], e["elapsed"])
    for s in stats.values():
        s["mean"] = s["total"] / s["count"] if s["count"] else None

    return {
        "categories": seconds,
        "by_name": sorted(stats.values(), key=lambda s: -s["total"]),
        "first": evs[0]["start"] if evs else None,
        "last": max(e["end"] for e in evs) if evs else None,
        "n_events": len(evs),
    }


def format_analysis(analysis: dict[str, Any], *, top: int = 15) -> str:
    """:func:`analyze_events`' result as Markdown: the categories, then the costliest names."""
    lines = ["| category | minutes |", "| --- | --- |"]
    for cat, sec in analysis["categories"].items():
        if sec > 0:
            lines.append(f"| {cat} | {sec / 60:.1f} |")
    lines += ["", "| kind | name | category | runs | mean s | min s | max s | total min |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in analysis["by_name"][:top]:
        if not s["count"]:
            continue
        lines.append(f"| {s['kind']} | `{s['name']}` | {s['category']} | {s['count']} | "
                     f"{s['mean']:.1f} | {s['min']:.1f} | {s['max']:.1f} | {s['total'] / 60:.1f} |")
    return "\n".join(lines)


def format_tree(events: list[dict[str, Any]], *, min_seconds: float = 0.0) -> str:
    """The spans of one build as an indented tree, children under their parents, in start order."""
    by_id = {e["id"]: e for e in events}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for e in sorted(events, key=lambda e: e["start"]):
        parent = e.get("parent") if e.get("parent") in by_id else None
        children.setdefault(parent, []).append(e)

    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for e in children.get(parent, []):
            skipped = (e.get("attrs") or {}).get("skipped")
            if not skipped and e["elapsed"] < min_seconds:
                continue
            status = "up to date" if skipped else (f"{e['elapsed']:.1f} s"
                                                   + ("" if e.get("ok", True) else "  FAILED"))
            lines.append(f"{'  ' * depth}{e['name']:<{max(1, 36 - 2 * depth)}} {status}")
            walk(e["id"], depth + 1)

    walk(None, 0)
    return "\n".join(lines)
