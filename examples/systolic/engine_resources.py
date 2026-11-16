"""engine_resources.py -- the systolic engine's resource model on the platform, from the sweep's csynth.

    python -m examples.systolic.engine_resources fit   --root <sweep root>   # fit and file
    python -m examples.systolic.engine_resources score --root <sweep root>   # held-out, once
    python -m examples.systolic.engine_resources describe                    # record the kernels

``plans/calib_2025_1.md`` Stage 4 (*The systolic engine, resources*).  The points are
:mod:`examples.systolic.systolic_sweep`'s (``SystolicEngine`` at the centre and one parameter at a
time, ``SystolicEngineMem`` at the LTBF system's array shapes), built by its ``synth`` under one root
per point; their csynth projects are read here, nothing is rebuilt.  Each kernel's report is split
per task (the task rows, the ``_Pipeline_*`` sub-blocks folded into their task) and the remainder,
top less tasks, is the composites' own share (their buffers, FIFOs and ``m_axi`` adapters).

``fit`` fits every task's model (``waveflow.linalg.cost``: DSPs counted, no block RAM in a task,
LUT and FF on the terms it declares) and the shares' (block RAM counted with the 2025.1 binding,
LUT and FF fitted), on every point but :data:`systolic_sweep.HOLDOUT`, and files them on the
platform (``models/<task>/params.json``, ``models/systolic_engine_channels/params.json``).  ``score``
predicts the held-out kernels whole -- ``SystolicEngineMem.get_rm``, what ``estimate_system`` prices
-- and scores them once against the targets of ``examples/mimo_cg/hw/linalg_cal.py``'s ``validate``:
DSP and BRAM exact on at least 90% of the builds, LUT and FF within 10% mean.  Both write
:data:`RECORD`.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from examples.systolic import engine_harness as EH
from examples.systolic import engine_mem_harness as EMH
from examples.systolic import systolic_sweep as SW

HERE = Path(__file__).resolve().parent
#: The calibration's record: each point's measured kernel and tasks, the fit's errors, the score.
RECORD = HERE / "engine_resources.json"
COUNTERS = ("lut", "ff", "dsp", "bram")
#: linalg_cal.py's validate: DSP and BRAM exact on this share of the builds, LUT and FF mean error.
EXACT_SHARE, MEAN_TARGET = 0.90, 0.10


def kernel(name: str):
    """The point's kernel, elaborated as the sweep built it."""
    cfg = SW.params(name)
    built = {k: EH.reg(*v) if k in ("a", "b", "c") else v for k, v in cfg.items()}
    return EMH.engine_mem(**built) if name in SW.LTBF_POINTS else EH.engine(**built)


def kernel_spec(name: str) -> dict:
    """How the point's kernel is constructed, for a reader without the examples (the tests): its
    class, its integer parameters and its formats (``W``, integer bits, signed, the modes)."""
    k = kernel(name)
    eng = k.engine if name in SW.LTBF_POINTS else k
    params = {f: int(getattr(eng, f)) for f in ("word_bits", "Mmax", "Kmax", "Nmax", "Pmax", "L",
                                                "R", "C", "form", "sob_depth", "lane_bits")}
    fmts = {f: [int(x.W), int(x.int_bits), bool(x.signed), x.q_mode.value, x.o_mode.value]
            for f, x in (("a", eng.a), ("b", eng.b), ("c", eng.c))}
    return {"cls": type(k).__name__, "name": k.name, "params": params, "formats": fmts}


def describe() -> None:
    """Add each recorded point's :func:`kernel_spec` to :data:`RECORD` (no score is touched)."""
    rec = json.loads(RECORD.read_text(encoding="utf-8"))
    for n in rec["points"]:
        rec["points"][n]["kernel"] = kernel_spec(n)
    RECORD.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")


def attribute(root: Path, name: str) -> dict:
    """The point's csynth report split per task: ``top``, ``tasks`` (``[{cls, task, rtl, counters}]``)
    and ``share`` (top less tasks).  A task's row is the one starting with its RTL prefix, or -- when
    csynth kept a single instantiation's bare name (``systolic_engine_load_a_task``) -- its task
    name; a task with no row is an error."""
    from waveflow.calib.module_key import walk_modules
    from waveflow.calib.record_store import normalize_resources
    from waveflow.calib.synth_report import SUBBLOCK_MARKER, rtl_prefix
    from waveflow.utils.csynthparse import CsynthParser

    k = kernel(name)
    proj = root / name / f"{EMH.KERNEL if name in SW.LTBF_POINTS else EH.KERNEL}_proj"
    parser = CsynthParser(sol_path=str(proj / "solution1"))
    parser.get_total_resources()
    parser.get_module_resources()
    rows = {n: normalize_resources(r) for n, r in parser.module_info.items()}
    top = normalize_resources(parser.total_resources)
    tasks = []
    for path, comp, _ident in walk_modules(k):
        if getattr(comp, "sub_comps", None):
            continue
        kt = comp.kernel_task()
        pre = rtl_prefix(kt)
        cand = sorted((n for n in rows if n.startswith(pre) and SUBBLOCK_MARKER not in n), key=len)
        if not cand and kt.task_fn in rows:
            cand = [kt.task_fn]
        if not cand:
            raise RuntimeError(f"{name}: no csynth row for {path} ({pre}); rows: {sorted(rows)}")
        tasks.append({"path": path, "cls": type(comp).__name__, "task": kt.task_fn, "rtl": cand[0],
                      "counters": {c: int(rows[cand[0]].get(c, 0)) for c in COUNTERS}})
    top = {c: int(top.get(c, 0)) for c in COUNTERS}
    share = {c: top[c] - sum(t["counters"][c] for t in tasks) for c in COUNTERS}
    return {"top": top, "tasks": tasks, "share": share}


def _errors(pred: dict, meas: dict) -> dict:
    return {c: (pred[c] - meas[c]) / meas[c] if meas[c] else float(pred[c] != meas[c])
            for c in COUNTERS}


def _summary(errs: dict) -> dict:
    """Per counter over builds: DSP/BRAM the share exact, LUT/FF the mean and worst |error|."""
    out = {}
    for c in COUNTERS:
        e = [abs(v[c]) for v in errs.values()]
        if c in ("dsp", "bram"):
            out[c] = {"exact_share": sum(x == 0 for x in e) / len(e)}
        else:
            out[c] = {"mean": sum(e) / len(e), "worst": max(e)}
    return out


def _reset_caches() -> None:
    from waveflow.linalg import cost

    cost.engine_channel_model.cache_clear()


def predict_kernel(name: str) -> dict:
    """The point's kernel predicted whole, as ``estimate_system`` prices it (its class's model)."""
    from waveflow.linalg import cost

    k = kernel(name)
    return dict(type(k).get_rm(cost.platform()).predict(k))


def fit(root: Path) -> dict:
    """Fit every task's model and the shares' on every point but the held-out ones; file them."""
    from waveflow.linalg import cost

    names = [n for n in sorted(json.loads(SW.RESOURCES.read_text(encoding="utf-8"))["points"])
             if n not in SW.HOLDOUT]
    measured = {n: attribute(root, n) for n in names}
    pdir = cost.platform_dir() / "models"
    prov = {"plan": "plans/calib_2025_1.md Stage 4 (the systolic engine, resources)",
            "tool": cost.TOOL, "part": cost.PART, "fit_points": names,
            "held_out": list(SW.HOLDOUT), "source": "csynth of examples/systolic/systolic_sweep.py",
            "date": datetime.now().astimezone().date().isoformat()}
    # the tasks
    from waveflow.calib.module_key import walk_modules

    samples: dict = {}
    for n in names:
        comps = {path: comp for path, comp, _i in walk_modules(kernel(n))}
        for t in measured[n]["tasks"]:
            if t["cls"] == "MemWStream":
                continue
            samples.setdefault(t["task"], []).append((comps[t["path"]], t["counters"]))
    for task in cost.ENGINE_TASKS:
        smp = samples[task]
        m = cost.new_model(task, type(smp[0][0])).fit(samples=smp)
        path = pdir / task / "params.json"
        m.save_model(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["provenance"] = {**prov, "builds": len(smp)}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # the shares and the memory writer
    chan = cost.fit_engine_channels(
        [{"kernel": kernel(n), **measured[n]["share"]} for n in names])
    writer: dict = {}
    for n in names:
        for t in measured[n]["tasks"]:
            if t["cls"] == "MemWStream":
                w = str(int(kernel(n).wstream.mem_dwidth))
                writer.setdefault(w, []).append(t["counters"])
    chan["mem_writer"] = {w: {c: sorted(r[c] for r in rs)[len(rs) // 2] for c in ("lut", "ff")}
                          for w, rs in writer.items()}
    chan["model"], chan["provenance"] = cost.ENGINE_CHANNELS, prov
    (pdir / cost.ENGINE_CHANNELS).mkdir(parents=True, exist_ok=True)
    (pdir / cost.ENGINE_CHANNELS / "params.json").write_text(
        json.dumps(chan, indent=2) + "\n", encoding="utf-8")
    _reset_caches()
    # the fit's errors, kernel by kernel, as estimate_system prices them
    pred = {n: predict_kernel(n) for n in names}
    errs = {n: _errors(pred[n], measured[n]["top"]) for n in names}
    rec = {"plan": prov["plan"], "tool": cost.TOOL, "part": cost.PART, "targets": {
               "dsp_bram_exact_share": EXACT_SHARE, "lut_ff_mean": MEAN_TARGET},
           "points": {n: {**measured[n], "role": "fit", "predicted": pred[n],
                          "kernel": kernel_spec(n)} for n in names},
           "fit": {"summary": _summary(errs), "errors": errs}, "holdout": None}
    RECORD.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    return rec["fit"]["summary"]


def score(root: Path) -> dict:
    """The held-out kernels predicted whole and scored, once: refused if a score is recorded."""
    rec = json.loads(RECORD.read_text(encoding="utf-8"))
    if rec.get("holdout") is not None:
        raise SystemExit("the held-out points are scored already (rule 2: once)")
    _reset_caches()
    measured = {n: attribute(root, n) for n in SW.HOLDOUT}
    pred = {n: predict_kernel(n) for n in SW.HOLDOUT}
    errs = {n: _errors(pred[n], measured[n]["top"]) for n in SW.HOLDOUT}
    s = _summary(errs)
    s["met"] = {"dsp_bram": all(s[c]["exact_share"] >= EXACT_SHARE for c in ("dsp", "bram")),
                "lut_ff": all(s[c]["mean"] <= MEAN_TARGET for c in ("lut", "ff"))}
    for n in SW.HOLDOUT:
        rec["points"][n] = {**measured[n], "role": "holdout", "predicted": pred[n],
                            "kernel": kernel_spec(n)}
    rec["holdout"] = {"set": list(SW.HOLDOUT), "summary": s, "errors": errs,
                      "date": datetime.now().astimezone().date().isoformat()}
    RECORD.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("fit", "score", "describe"))
    ap.add_argument("--root", type=Path, help="the sweep's build root (fit, score)")
    a = ap.parse_args()
    if a.cmd == "describe":
        describe()
        return
    if a.root is None:
        ap.error("fit and score need --root")
    out = fit(a.root.resolve()) if a.cmd == "fit" else score(a.root.resolve())
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
