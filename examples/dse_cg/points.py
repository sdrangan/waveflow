"""points.py -- plan step 2c: six design points through every method, timed (``plans/dse_cg.md``).

    python -m examples.dse_cg.points [name ...]        # measure, report to BUILD_DIR/points/<name>.json
    python -m examples.dse_cg.points --table           # the reported table over what is measured

Each point runs ``golden`` (accuracy), ``pysim`` (the timing workload in pysim, the resource
estimate), ``hls_estimate`` (each built HLS top), ``synth`` (each built part) and ``rtlsim`` (the timing
workload at RTL, against pysim), every call timed on this machine.  The gates are the step's: every
call ``ok``, ``rtlsim`` bit-exact, ``golden`` and ``pysim`` agreeing on accuracy, the workload-only and
simulation-only points sharing the defaults' part identities.  The table -- pysim against ``rtlsim``
cycles, pysim and ``hls_estimate`` against ``synth`` resources, each call's tool time -- is reported,
not gated, and is what the plan's resizing rule is applied to.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from examples.dse_cg.ltbf_cg_mem import BUILD_DIR, BUILT, NODE_TOPS, LtbfCgMemAdaptor

#: The six points of plan step 2c: ``(design parameters, timing workload, why)``.
POINTS: dict[str, tuple[dict, dict, str]] = {
    "defaults": ({}, {"name": "long_term"}, "the reference, from Phase 1"),
    "smallest": ({"R": 2, "C": 4, "W": 12, "L": 2, "form": "factored", "banks": 1},
                 {"name": "long_term"}, "an edge of the grid builds"),
    "largest": ({"R": 8, "C": 8, "W": 16, "L": 4, "form": "explicit", "banks": 2, "mem_rate": 1,
                 "cg_window": 4}, {"name": "long_term"}, "the other edge"),
    "R_changed": ({"R": 8, "C": 8}, {"name": "long_term"},
                  "a hardware change: a new identity for sys alone"),
    "sim_only": ({"mem_rate": 1, "cg_window": 4}, {"name": "long_term"},
                 "a simulation-only change: no new part, a new RTL run"),
    "workload_only": ({}, {"name": "long_term", "t": 2},
                      "a workload change: same identities, no new synthesis"),
}
OUT = BUILD_DIR / "points"


def _timed(fn, *a, **kw) -> tuple:
    t0 = time.perf_counter()
    try:
        out = fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 -- a failure is a row, as the service will make it
        out = {"status": f"error: {type(e).__name__}: {str(e)[-1500:]}"}
    return out, time.perf_counter() - t0


def measure(name: str, adaptor: LtbfCgMemAdaptor | None = None) -> dict:
    """Every method at point *name*, each call timed; written to ``OUT/<name>.json``."""
    a = adaptor or LtbfCgMemAdaptor()
    params, workload, why = POINTS[name]
    rec: dict = {"name": name, "params": a.fill(params), "workload": workload, "why": why,
                 "identities": a.identities(params), "hls_identities": a.hls_identities(params),
                 "sim_identity": a.sim_identity(params, workload), "calls": {}}
    calls = rec["calls"]
    _n, w = a.workload(workload)
    acc = {"name": "accuracy", "t": w["t"], "target": w["target"]}
    calls["golden"], _ = _timed(a.golden, params, acc)
    calls["pysim"], calls["pysim_wall_s"] = _timed(a.pysim, params, workload)
    for node in sorted(NODE_TOPS):
        if node in BUILT:
            for top in NODE_TOPS[node]:
                calls[f"hls_estimate.{top}"], calls[f"hls_estimate.{top}_wall_s"] = _timed(a.hls_estimate, top, params)
    for node in sorted(BUILT):
        calls[f"synth.{node}"], calls[f"synth.{node}_wall_s"] = _timed(a.synth, node, params)
    calls["rtlsim"], calls["rtlsim_wall_s"] = _timed(a.rtlsim, params, workload)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(rec, indent=1, default=str))
    return rec


def table(names=None) -> str:
    """The reported table over the points measured so far."""
    rows = ["| point | pysim cycles | RTL cycles | pysim / RTL | LUT pysim / csynth / synth | DSP pysim / csynth / synth | rtlsim min | synth min |",
            "|---|---|---|---|---|---|---|---|"]
    for name in names or POINTS:
        f = OUT / f"{name}.json"
        if not f.is_file():
            continue
        c = json.loads(f.read_text())["calls"]
        py, rtl = c.get("pysim", {}), c.get("rtlsim", {})
        syn = {k[6:]: v for k, v in c.items() if k.startswith("synth.") and isinstance(v, dict)}
        hls = {k[13:]: v for k, v in c.items() if k.startswith("hls_estimate.") and isinstance(v, dict)}

        def tot(d: dict, key: str) -> int:
            return int(sum(v.get(key, 0) or 0 for v in d.values() if isinstance(v, dict)))

        p_res = py.get("resources", {})
        ratio = (py.get("cycles", 0) / rtl["cycles"]) if rtl.get("cycles") else float("nan")
        rows.append(f"| {name} | {py.get('cycles', '')} | {rtl.get('cycles', '')} | {ratio:.4f} | "
                    f"{p_res.get('lut', '')} / {tot(hls, 'lut')} / {tot(syn, 'lut')} | "
                    f"{p_res.get('dsp', '')} / {tot(hls, 'dsp')} / {tot(syn, 'dsp')} | "
                    f"{c.get('rtlsim_wall_s', 0) / 60:.1f} | "
                    f"{sum(v for k, v in c.items() if k.startswith('synth.') and k.endswith('_wall_s')) / 60:.1f} |")
    return "\n".join(rows)


def write_costs() -> dict:
    """``costs.py`` from the measured points: the median tool time of each evaluation."""
    import statistics

    from examples.dse_cg import costs

    got: dict[str, list[float]] = {}
    for f in sorted(OUT.glob("*.json")):
        c = json.loads(f.read_text())["calls"]
        for fn in ("golden", "pysim"):
            if (c.get(fn) or {}).get("seconds"):
                got.setdefault(fn, []).append(float(c[fn]["seconds"]))
        for k, v in c.items():
            if isinstance(v, dict) and v.get("seconds") and not v.get("cached") and \
                    k.startswith(("hls_estimate.", "synth.")):
                got.setdefault(k.split(".")[0], []).append(float(v["seconds"]))
        r = c.get("rtlsim") or {}
        if r.get("build_s"):
            got.setdefault("rtlsim_build", []).append(float(r["build_s"]) + float(r.get("simulate_s", 0)))
        if r.get("simulate_s"):
            got.setdefault("rtlsim_run", []).append(float(r["simulate_s"]))
    med = {k: round(statistics.median(v), 1) for k, v in got.items()}
    path = Path(costs.__file__)
    text = path.read_text(encoding="utf-8")
    body = "COSTS: dict[str, float | None] = {\n" + "".join(
        f'    "{k}": {med.get(k)},\n' for k in costs.COSTS) + "}\n"
    start = text.index("COSTS: dict")
    end = text.index("}\n", start) + 2
    text = text[:start] + body + text[end:]
    stamp = f'MEASURED = "{time.strftime("%Y-%m-%d")}, Vitis HLS / Vivado 2025.1, the lab machine"'
    text = text[:text.index("MEASURED = ")] + stamp + "\n"
    path.write_text(text, encoding="utf-8")
    return med


def write_table(out_dir: Path) -> dict:
    """The six points as a ground-truth table in G1's format (``grid.py table`` + ``finish``): their
    parts, kernels and RTL runs as measured, pysim and ``golden`` computed -- for the replay backend
    and ``score.py`` before G1 is measured."""
    from examples.dse_cg.service import eval_key

    a = LtbfCgMemAdaptor()
    t: dict[str, dict] = {fn: {} for fn in ("synth", "hls", "rtlsim", "golden", "pysim")}
    for name in POINTS:
        f = OUT / f"{name}.json"
        if not f.is_file():
            continue
        rec = json.loads(f.read_text())
        calls, params, w = rec["calls"], rec["params"], rec["workload"]
        for node, ident in rec["identities"].items():
            row = calls.get(f"synth.{node}")
            if isinstance(row, dict) and row.get("status") == "ok":
                t["synth"].setdefault(ident, {"key": ident, "row": row, "cost_s": float(row["seconds"])})
        for kernel, ident in rec["hls_identities"].items():
            row = calls.get(f"hls_estimate.{kernel}")
            if isinstance(row, dict) and row.get("status") == "ok":
                t["hls"].setdefault(ident, {"key": ident, "row": row, "cost_s": float(row["seconds"])})
        row = calls["rtlsim"]
        run = rec["sim_identity"]["run"]
        t["rtlsim"][run] = {"key": run, "row": row,
                            "cost_s": float(row.get("build_s", 0)) + float(row.get("simulate_s", 0))}
        nm, ww = a.workload(w)
        prow = a.pysim(params, {"name": nm, **ww})
        t["pysim"][eval_key("pysim", params, nm, ww)] = {"key": eval_key("pysim", params, nm, ww),
                                                         "row": prow, "cost_s": float(prow["seconds"])}
        for target in ("-6_14", "-6_3"):
            nm2, w2 = a.workload({"name": "accuracy", "t": ww["t"], "target": target})
            grow = a.golden(params, {"name": nm2, **w2})
            t["golden"][eval_key("golden", params, nm2, w2)] = {"key": eval_key("golden", params, nm2, w2),
                                                                "row": grow, "cost_s": float(grow.get("seconds") or 0)}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for fn, rows in t.items():
        (out_dir / f"{fn}.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in rows.values()))
    return {fn: len(v) for fn, v in t.items()}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--costs" in args:
        print(write_costs())
    elif "--table" in args:
        print(table())
    else:
        for n in args or list(POINTS):
            r = measure(n)
            print(n, {k: (v.get("status") if isinstance(v, dict) else round(v, 1)) for k, v in r["calls"].items()},
                  flush=True)
