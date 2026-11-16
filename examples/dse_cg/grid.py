"""grid.py -- plan step 4: measure the grid G1 on the live backend (``plans/dse_cg.md``).

    python -m examples.dse_cg.grid shared               # the parts every root shares, first
    python -m examples.dse_cg.grid measure 0 2          # process 0 of 2 (the lab machine's two slots)
    python -m examples.dse_cg.grid status               # what is measured
    python -m examples.dse_cg.grid table                # G1's table, for the replay backend
    python -m examples.dse_cg.grid finish               # pysim and golden over the grid, into it
    python -m examples.dse_cg.grid off                  # the RTL runs not timed at G1's condition
    python -m examples.dse_cg.grid retime 0 2           # re-time them: process 0 of 2, started together
    python -m examples.dse_cg.grid retimed              # how the re-timing went

Every legal design of ``ltbf_cg_mem.GRID`` with every timing workload (``t``): each root's csynth
(``hls_estimate`` of its kernels), each part's synthesis, each RTL run, through the adaptor as the live
backend calls it.  A row per call is appended to ``BUILD_DIR/grid/g1_<process>.jsonl`` with the tool
time it took, and a call already recorded in any of those files is skipped, so a run that stops
resumes where it stopped -- the adaptor's own caches (synthesis by content, csynth per root, RTL runs
by stamp) would answer it again, but without the time it took the first time.

The processes are split by the systolic part: the two roots that share one (they differ only in
``L``) go to the same process, so no part is synthesized twice at once; the parts every root shares
(the vector unit's, the crossbar's, the hub's) are synthesized first, by ``shared``.  ``pysim`` and
``golden`` are not measured here: they take seconds, and are computed over the grid when it is scored.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from examples.dse_cg.ltbf_cg_mem import (
    BUILD_DIR,
    BUILT,
    GRID,
    NODE_TOPS,
    LtbfCgMemAdaptor,
)

OUT = BUILD_DIR / "grid"


def designs(a: LtbfCgMemAdaptor) -> list[dict]:
    """Every legal design of the grid, in a fixed order."""
    names = [k for k, v in GRID.items() if v[0] == "design"]
    out = []
    for combo in itertools.product(*(GRID[k][1] for k in names)):
        p = dict(zip(names, combo, strict=True))
        if a.legal(p) is None:
            out.append(p)
    return out


def workloads() -> list[dict]:
    """The timing workloads: one sector's long-term update at each ``t``."""
    return [{"name": "long_term", "t": t} for t in GRID["t"][1]]


def _done() -> set[str]:
    keys: set[str] = set()
    for f in OUT.glob("g1_*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r["row"].get("status") in ("ok", "mismatch"):
                    keys.add(r["key"])
    return keys


def _record(proc: str, key: str, fn: str, params: dict, extra: dict, row: dict, wall: float) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"g1_{proc}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "fn": fn, "params": params, **extra, "row": row,
                             "wall_s": wall, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                            default=str) + "\n")


def _call(proc: str, done: set[str], key: str, fn: str, params: dict, extra: dict, run) -> None:
    if key in done:
        return
    t0 = time.perf_counter()
    try:
        row = run()
    except Exception as e:  # noqa: BLE001 -- a failure is a row, as the service makes it
        row = {"status": f"failed: {type(e).__name__}: {str(e)[-1500:]}"}
    wall = time.perf_counter() - t0
    _record(proc, key, fn, params, extra, row, wall)
    print(f"{time.strftime('%H:%M:%S')} {fn} {key[:40]} {row.get('status')} {wall:.0f} s", flush=True)
    if row.get("status") in ("ok", "mismatch"):
        done.add(key)


def measure_root(a: LtbfCgMemAdaptor, proc: str, points: list[dict], done: set[str]) -> None:
    """One root's points (one HLS configuration): csynth, its parts, then every RTL run."""
    p0 = points[0]
    kids = a.hls_identities(p0)
    for node in sorted(NODE_TOPS):
        if node in BUILT:
            for top in NODE_TOPS[node]:
                _call(proc, done, f"hls:{a.hls_key(p0)}:{top}", "hls_estimate", p0,
                      {"kernel": top, "identity": kids.get(top)}, lambda t=top: a.hls_estimate(t, p0))
    for p in points:
        ids = a.identities(p)
        for node in sorted(BUILT):
            _call(proc, done, f"synth:{node}:{ids[node]}", "synth", p, {"node": node, "identity": ids[node]},
                  lambda n=node, q=p: a.synth(n, q))
    for p in points:
        for w in workloads():
            sid = a.sim_identity(p, w)
            _call(proc, done, f"rtlsim:{sid['run']}", "rtlsim", p, {"workload": w, "sim_identity": sid},
                  lambda q=p, ww=w: a.rtlsim(q, ww))


def roots(a: LtbfCgMemAdaptor) -> dict[str, list[dict]]:
    """The grid's designs by root (``hls_key``), in a fixed order."""
    out: dict[str, list[dict]] = {}
    for p in designs(a):
        out.setdefault(a.hls_key(p), []).append(p)
    return out


def measure(index: int, n: int) -> None:
    """Process *index* of *n*: the roots whose systolic part falls to it."""
    a = LtbfCgMemAdaptor()
    done = _done()
    by_root = roots(a)
    groups: dict[str, list[str]] = {}
    for key, pts in by_root.items():
        groups.setdefault(a.identities(pts[0])["sys"], []).append(key)
    mine = [k for i, (_s, keys) in enumerate(sorted(groups.items())) if i % n == index for k in keys]
    print(f"process {index} of {n}: {len(mine)} roots, "
          f"{sum(len(by_root[k]) for k in mine)} designs", flush=True)
    for key in mine:
        measure_root(a, str(index), by_root[key], done)


def shared() -> None:
    """The parts every root shares -- the vector unit's (by ``W`` and ``L``), the crossbar's (by
    ``banks``), the hub's -- synthesized once, before the processes split."""
    a = LtbfCgMemAdaptor()
    done = _done()
    seen: dict[tuple, dict] = {}
    for p in designs(a):
        ids = a.identities(p)
        for node in ("vec", "xbar", "hub"):
            seen.setdefault((node, ids[node]), p)
    for (node, ident), p in sorted(seen.items()):
        _call("shared", done, f"synth:{node}:{ident}", "synth", p, {"node": node, "identity": ident},
              lambda n=node, q=p: a.synth(n, q))


def status() -> dict:
    """What is measured: calls by function and status, and the RTL runs left."""
    rows = []
    for f in sorted(OUT.glob("g1_*.jsonl")):
        rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    out: dict = {}
    for r in rows:
        s = str(r["row"].get("status"))
        out.setdefault(r["fn"], {}).setdefault(s.split(":")[0], 0)
        out[r["fn"]][s.split(":")[0]] += 1
    a = LtbfCgMemAdaptor()
    n_runs = len(designs(a)) * len(workloads())
    ok = sum(1 for r in rows if r["fn"] == "rtlsim" and r["row"].get("status") in ("ok", "mismatch"))
    out["rtlsim_left"] = n_runs - ok
    out["hours_recorded"] = round(sum(float(r["wall_s"]) for r in rows) / 3600, 1)
    return out


TABLE = OUT / "table"


def _rows() -> list[dict]:
    rows = []
    for f in sorted(OUT.glob("g1_*.jsonl")):
        rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def _write(fn: str, entries: dict) -> None:
    TABLE.mkdir(parents=True, exist_ok=True)
    with (TABLE / f"{fn}.jsonl").open("w", encoding="utf-8") as fh:
        for e in entries.values():
            fh.write(json.dumps(e, default=str) + "\n")


def table() -> dict:
    """G1's measured rows by what the replay backend looks them up by (``service.ReplayBackend``):
    a part's synthesis and a kernel's csynth by identity, an RTL run by its simulation identity --
    the first ``ok`` row of each, at the tool time it took."""
    out: dict[str, dict] = {"synth": {}, "hls": {}, "rtlsim": {}}
    for r in _rows():
        if r["row"].get("status") not in ("ok", "mismatch"):
            continue
        if r["fn"] == "synth":
            out["synth"].setdefault(r["identity"], {"key": r["identity"], "row": r["row"],
                                                    "cost_s": float(r["row"].get("seconds") or 0.0)})
        elif r["fn"] == "hls_estimate":
            out["hls"].setdefault(r["identity"], {"key": r["identity"], "row": r["row"],
                                                  "cost_s": float(r["row"].get("seconds") or 0.0)})
        elif r["fn"] == "rtlsim":
            run = r["sim_identity"]["run"]
            out["rtlsim"].setdefault(run, {"key": run, "row": r["row"], "cost_s": _rtl_cost(r["row"])})
    for e in valid_retimes():       # a run G1 did not time at its condition, at the time it takes there
        old = out["rtlsim"].get(e["key"])
        if old is not None:
            out["rtlsim"][e["key"]] = {"key": e["key"], "row": e["row"], "cost_s": _rtl_cost(e["row"]),
                                       "retimed": {"why": e["why"], "g1_cost_s": old["cost_s"]}}
    for fn, entries in out.items():
        _write(fn, entries)
    return {fn: len(v) for fn, v in out.items()}


def finish() -> dict:
    """``pysim`` at every timing workload and ``golden`` at every accuracy workload, over the grid,
    into the table (keyed as the service caches them)."""
    from examples.dse_cg.service import eval_key

    a = LtbfCgMemAdaptor()
    out: dict[str, dict] = {"pysim": {}, "golden": {}}
    acc = [{"name": "accuracy", "t": t, "target": g} for t in GRID["t"][1] for g in GRID["target"][1]]
    for p in designs(a):
        q = a.fill(p)
        for fn, wls, run in (("pysim", workloads(), a.pysim), ("golden", acc, a.golden)):
            for w in wls:
                name, ww = a.workload(w)
                row = run(q, {"name": name, **ww})
                out[fn][eval_key(fn, q, name, ww)] = {"key": eval_key(fn, q, name, ww), "row": row,
                                                      "cost_s": float(row.get("seconds") or 0.0)}
    for fn, entries in out.items():
        _write(fn, entries)
    return {fn: len(v) for fn, v in out.items()}


# -- the runs G1 did not time at its condition (plan row of 2026-11-11) ------------------------------
#
# G1's condition: an RTL run built from scratch beside one other G1 call (two slots, ``K_WORKERS``) and
# nothing else.  Within a root, a run's simulation per cycle beside a third job was up to 1.8x the
# normal runs', and alone 0.6x; so a run that was not at the condition is re-timed at it
# (``retime``: two processes, each run rebuilt from scratch, a filler simulation keeping the machine
# at two runs until both are done), and the table takes the re-timed tool time when the run had a
# partner throughout and its result is G1's.

#: Other jobs that shared the machine with G1, local times (the plan's rows of 2026-11-07 and -08).
OTHER_JOBS = {
    "live gate": ("2026-11-07 12:29:00", "2026-11-07 14:41:00"),
    "pysim over the grid": ("2026-11-07 15:12:00", "2026-11-07 16:03:00"),
    "the retirement's checks": ("2026-11-07 22:38:00", "2026-11-08 00:58:00"),
}

#: A run had a partner when another process was busy for at least this share of it.
PARTNERED = 0.95

RETIME_DIR = BUILD_DIR / "retime"
_FMT = "%Y-%m-%d %H:%M:%S"


def _read(f: Path) -> list[dict]:
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rtl_cost(row: dict) -> float:
    """What replay charges for an RTL run: its build (every phase but ``simulate``) and simulation."""
    return float(row.get("build_s", 0)) + float(row.get("simulate_s", 0))


def _ts(s: str) -> float:
    """A row's ``at`` (the lab machine's local time) as a timestamp."""
    return datetime.strptime(s, _FMT).astimezone().timestamp()


def _span(r: dict) -> tuple[float, float]:
    """A G1 call's start and end (it is recorded when it ends)."""
    end = _ts(r["at"])
    return end - float(r["wall_s"]), end


def covered(span: tuple[float, float], others: list[tuple[float, float]]) -> float:
    """The share of *span* that the intervals *others* cover."""
    a, b = span
    cov, cur = 0.0, a
    for s, e in sorted(others):
        s, e = max(s, cur), min(e, b)
        if e > s:
            cov, cur = cov + e - s, e
    return cov / (b - a) if b > a else 1.0


def off_condition(files: dict[str, list[dict]] | None = None) -> dict[str, str]:
    """G1's RTL runs not timed at its condition, ``{run identity: why}``: a run that overlapped another
    job (:data:`OTHER_JOBS`), whose build an earlier step had left in place (``"build reused"``: the
    integration points, built at step 2c), or that had no partner (``"no partner"``: the other process
    busy for less than :data:`PARTNERED` of it).  *files*: ``{process: rows}``, by default G1's."""
    if files is None:
        files = {f.stem: _read(f) for f in sorted(OUT.glob("g1_*.jsonl")) if f.stem != "g1_shared"}
    jobs = {k: (_ts(a), _ts(b)) for k, (a, b) in OTHER_JOBS.items()}
    out: dict[str, str] = {}
    for proc, rows in files.items():
        others = [_span(r) for q, v in files.items() if q != proc for r in v]
        for r in rows:
            if r["fn"] != "rtlsim" or r["row"].get("status") not in ("ok", "mismatch"):
                continue
            span = _span(r)
            why = next((k for k, (a, b) in jobs.items() if span[0] < b and span[1] > a), None)
            if why is None and "elaborate" not in r["row"].get("phases_s", {}):
                why = "build reused"
            if why is None and covered(span, others) < PARTNERED:
                why = "no partner"
            if why:
                out.setdefault(r["sim_identity"]["run"], why)
    return out


def _retimed() -> list[dict]:
    rows: list[dict] = []
    for f in sorted(OUT.glob("retime_*.jsonl")):
        rows += _read(f)
    return rows


def valid_retimes(entries: list[dict] | None = None) -> list[dict]:
    """The re-timed runs the table takes: another process busy (a run or a filler) for at least
    :data:`PARTNERED` of the run, and the result G1's (status, cycles, bit-exactness)."""
    entries = _retimed() if entries is None else entries
    out = []
    for e in entries:
        if e.get("filler") or not e.get("same_result"):
            continue
        others = [(x["start"], x["end"]) for x in entries if x["proc"] != e["proc"]]
        if covered((e["start"], e["end"]), others) >= PARTNERED:
            out.append(e)
    return out


def _clear(a: LtbfCgMemAdaptor, p: dict, w: dict) -> Path:
    """Move a run's work folder and its crossbar IP aside (G1's, kept once under ``RETIME_DIR/g1``), so
    that the run is built from scratch; the run's work folder."""
    from examples.ltbf_cg_mem_dse.hw import build as hb

    work = Path(a.config(p).root_dir) / hb.WORK_DIR
    tag = a.system_tag(p, w)
    keep = RETIME_DIR / "g1" / a.hls_key(p)
    for src in [work / tag, *(work / "ip").glob(f"xbar_*{tag}_*")]:
        dst = keep / src.relative_to(work)
        if dst.exists():
            shutil.rmtree(src)          # an earlier attempt's: G1's is kept already
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
    return work / tag


def _filler(index: int, src: Path) -> dict:
    """One simulation of a finished run, copied aside: the partner a run gets while this process has
    nothing left (G1's condition is two runs at once)."""
    dst = RETIME_DIR / f"filler_{index}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)
    top = next(dst.glob("ltbf_top_*_tb.cpp")).name.removesuffix("_tb.cpp")
    t0 = time.time()
    rc = subprocess.run(["bash", "run.sh", top, f"{top}_tb", "run"], cwd=dst, check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    return {"filler": True, "proc": index, "source": str(src), "rc": rc, "start": t0, "end": time.time()}


def retime(index: int, n: int) -> None:
    """Process *index* of *n*: re-time its share of :func:`off_condition`'s runs, each built from
    scratch, then run fillers until every other process is done.  Resumable: a run re-timed already
    is skipped.  Every process must be started together (each waits for the others' state files)."""
    a = LtbfCgMemAdaptor()
    g1 = {r["sim_identity"]["run"]: r for r in _rows() if r["fn"] == "rtlsim"}
    off = off_condition()
    done = {e["key"] for e in _retimed() if not e.get("filler")}
    mine = [k for i, k in enumerate(sorted(off)) if i % n == index and k not in done]
    RETIME_DIR.mkdir(parents=True, exist_ok=True)
    state = [RETIME_DIR / f"state_{i}" for i in range(n)]
    state[index].write_text("busy")
    while not all(s.exists() for s in state):
        time.sleep(5)
    out = OUT / f"retime_{index}.jsonl"
    print(f"retime {index} of {n}: {len(mine)} of {len(off)} runs", flush=True)

    def put(e: dict) -> None:
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(e, default=str) + "\n")

    src = None
    for k in mine:
        r = g1[k]
        p, w = r["params"], r["workload"]
        src = _clear(a, p, w)
        t0 = time.time()
        row = a.rtlsim(p, w)
        t1 = time.time()
        same = all(row.get(f) == r["row"].get(f) for f in ("status", "cycles", "bit_exact"))
        put({"key": k, "why": off[k], "proc": index, "params": p, "workload": w, "row": row,
             "start": t0, "end": t1, "wall_s": t1 - t0, "same_result": same,
             "g1_cost_s": _rtl_cost(r["row"])})
        print(f"{time.strftime('%H:%M:%S')} {k} {off[k]}: {_rtl_cost(r['row']):.0f} -> {_rtl_cost(row):.0f} s"
              f"{'' if same else ' RESULT DIFFERS'}", flush=True)
    state[index].write_text("done")
    if src is None:                      # nothing of its own: a finished G1 run is the filler
        k = next(k for k in sorted(g1) if k not in off)
        src = Path(a.config(g1[k]["params"]).root_dir) / "xsi_work" / a.system_tag(g1[k]["params"],
                                                                                    g1[k]["workload"])
    while any(s.read_text() != "done" for i, s in enumerate(state) if i != index):
        e = _filler(index, src)
        put(e)
        print(f"{time.strftime('%H:%M:%S')} filler rc={e['rc']} {e['end'] - e['start']:.0f} s", flush=True)
        if e["rc"]:                     # a failed filler is no partner: do not spin (valid_retimes judges)
            time.sleep(60)


def retime_report() -> dict:
    """The re-timing: runs re-timed, valid, results G1's, and the tool time before and after."""
    ents = _retimed()
    runs = [e for e in ents if not e.get("filler")]
    ok = valid_retimes(ents)
    by: dict[str, dict] = {}
    for e in ok:
        b = by.setdefault(e["why"], {"runs": 0, "g1_h": 0.0, "retimed_h": 0.0})
        b["runs"] += 1
        b["g1_h"] += e["g1_cost_s"] / 3600
        b["retimed_h"] += _rtl_cost(e["row"]) / 3600
    return {"off_condition": len(off_condition()), "retimed": len(runs), "valid": len(ok),
            "same_result": sum(bool(e.get("same_result")) for e in runs),
            "fillers": sum(bool(e.get("filler")) for e in ents),
            "by_why": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in by.items()}}


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "status":
        print(json.dumps(status(), indent=1))
    elif args[0] == "shared":
        shared()
    elif args[0] == "measure":
        measure(int(args[1]), int(args[2]))
    elif args[0] == "table":
        print(table())
    elif args[0] == "finish":
        print(finish())
    elif args[0] == "off":
        for k, why in sorted(off_condition().items(), key=lambda kv: kv[1]):
            print(k, why)
    elif args[0] == "retime":
        retime(int(args[1]), int(args[2]))
    elif args[0] == "retimed":
        print(json.dumps(retime_report(), indent=1))
    else:
        raise SystemExit(__doc__)
