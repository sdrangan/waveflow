"""grid.py -- plan step 4: measure the grid G1 on the live backend (``plans/dse_cg.md``).

    python -m examples.dse_cg.grid shared               # the parts every root shares, first
    python -m examples.dse_cg.grid measure 0 2          # process 0 of 2 (the lab machine's two slots)
    python -m examples.dse_cg.grid status               # what is measured
    python -m examples.dse_cg.grid table                # G1's table, for the replay backend
    python -m examples.dse_cg.grid finish               # pysim and golden over the grid, into it

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
import sys
import time

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
            out["rtlsim"].setdefault(run, {"key": run, "row": r["row"], "cost_s":
                                           float(r["row"].get("build_s", 0)) + float(r["row"].get("simulate_s", 0))})
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
    else:
        raise SystemExit(__doc__)
