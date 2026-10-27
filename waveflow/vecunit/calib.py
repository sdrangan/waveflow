"""calib.py — the vector unit's calibration (``plans/vecunit_new.md``, Stage 7): its execute timing
fitted at RTL, and a resource model fitted over csynth runs.

**Pre-registered (rule 5).**  The sets below were committed before any calibration run; the held-out
sets are scored once.

**Timing.**  The execute task's time for a job is linear in counts its program gives before it runs
(:func:`waveflow.vecunit.unit.job_features`): instruction words, passes, runs, run elements, scalar
runs and instructions, divide runs, control instructions, and a constant a job.  pysim charges each
item it issues from the same coefficients (:class:`waveflow.vecunit.unit.VecTiming`), so a job's
outputs leave at the right times inside it, not only at its end.  At RTL a job's execute time is the
interval between its status and the previous job's (``m_status``), on jobs that keep the execute task
busy -- small blocks for the random jobs, so the loader is never what it waits for.  The fit is a
non-negative least squares; the held-out score is each held-out job's interval, pysim (with the fit)
against RTL, end to end -- the matched filter's included, whose loader binds.

**Resources.**  csynth's estimate of the unit (LUT, FF, DSP, BRAM) over the configurations below,
linear in terms of the configuration (:func:`config_features`, :data:`RESOURCE_BASIS`); the held-out
configurations are scored once.

Every set uses formats that keep every product and the union datapath moderate: the Stage 6 record
has a format mix whose fused body csynth cannot build.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path

import numpy as np

#: Execute-timing jobs on the CG configuration (``examples.vecunit_cg.cg_unit.CgVecUnit``):
#: ``random`` random jobs (seeded) over every instruction form with blocks of at most ``n_max``
#: columns and the pass counts ``npass`` cycled, then CG ``solves`` ``(k, n, nit, seed)``.
CG_JOBS = {
    "calib": {"random": 12, "seed": 21, "npass": (8, 16, 24, 32), "n_max": 8,
              "solves": [(8, 32, 1, 11), (8, 32, 2, 12), (4, 16, 3, 13), (8, 8, 5, 14),
                         (2, 32, 2, 15), (8, 32, 4, 16), (5, 13, 3, 17), (8, 24, 2, 18)]},
    "holdout": {"random": 6, "seed": 99, "npass": (12, 20, 28), "n_max": 8,
                "solves": [(8, 32, 6, 31), (3, 12, 4, 32)]},
}

#: The held-out timing scenarios: ``(unit, workload)`` -- the two above's ``holdout``, and the steady
#: workloads Stage 6 measured (four ``8 x 32`` CG solves; four matched-filter frames at ``VLMAX`` 64).
TIMING_HOLDOUT = (("cg", "holdout"), ("cg", "steady"), ("mf", "steady"))

#: Resource-model configurations: ``(L, NG, S, slot capabilities, formats)``, formats ``"cg"`` (the
#: CG unit's) or ``"mf"`` (the matched filter's, 16 bits); ``"all"`` capabilities, or ``"mul1"``: only
#: slot 0 multiplies.
RESOURCE_FIT = (
    (4, 32, 3, "all", "cg"), (2, 32, 3, "all", "cg"), (8, 32, 3, "all", "cg"),
    (4, 16, 3, "all", "cg"), (4, 64, 3, "all", "cg"), (4, 32, 2, "all", "cg"),
    (4, 32, 3, "mul1", "cg"), (4, 8, 3, "all", "mf"), (2, 8, 3, "all", "mf"),
    (4, 16, 2, "all", "mf"),
)
RESOURCE_HOLDOUT = ((8, 16, 2, "all", "cg"), (4, 8, 3, "mul1", "mf"), (2, 16, 3, "all", "cg"))

#: Held-out scores the plan asks to report (systolic plan's targets): mean and worst relative error.
TARGET_MEAN, TARGET_WORST = 0.03, 0.06

# -- Stage 9d: by phase and by configuration (pre-registered 2026-10-24, before any of its runs) ----
#: A fresh held-out set on the CG configuration (Stage 7's ``holdout`` has been scored).
CG_JOBS["holdout2"] = {"random": 6, "seed": 199, "npass": (10, 18, 26), "n_max": 8,
                       "solves": [(8, 32, 5, 41), (4, 20, 3, 42)]}
#: The matched filter's own: random jobs over every instruction form, their blocks at most
#: ``k_max x n_max`` (execute-bound), then frames ``(nsamp, nrx, seed)``.
MF_JOBS = {
    "calib": {"random": 10, "seed": 41, "npass": (8, 16, 24), "n_max": 4, "k_max": 64,
              "frames": [(128, 8, 51), (200, 5, 52), (64, 2, 53)]},
    "holdout2": {"random": 6, "seed": 299, "npass": (12, 20), "n_max": 4, "k_max": 64,
                 "frames": [(256, 6, 61), (100, 3, 62)]},
}
#: The builds Stage 9 measures at RTL: ``name -> (unit, its parameters)``; each is a configuration
#: identity whose execute-loop schedule csynth reports (``report.loop_schedule``) and pysim loads.
BUILDS = {
    "cg": ("cg", {}), "cg_w128": ("cg", {"word_bits": 128}), "cg_l2": ("cg", {"L": 2}),
    "cg_l8": ("cg", {"L": 8}),
    "mf": ("mf", {}), "mf_w128": ("mf", {"word_bits": 128}), "mf_v16": ("mf", {"VLMAX": 16}),
    "mf_v256": ("mf", {"VLMAX": 256}), "mf_l8": ("mf", {"L": 8}),
}
#: The fit: ``(build, workload)`` -- two configurations of each application, so no schedule is the
#: only one seen (lesson 9).  A job enters the fit only if the execute task set its interval, by
#: :data:`EXECUTE_MARGIN` (:func:`timing2_rows`): the model is the execute task's time.
TIMING2_FIT = (("cg", "calib"), ("cg", "steady"), ("cg_l2", "calib"),
               ("mf", "calib"), ("mf", "steady"), ("mf_v256", "calib"))
EXECUTE_MARGIN = 1.2
#: Held out, scored once (pysim's job intervals against the RTL's, every job but each run's first):
#: fresh jobs on fitted builds, the catalogue's other workloads, and builds never fitted.
TIMING2_HOLDOUT = (
    ("cg", "holdout2"), ("cg", "steady_k2"), ("cg", "steady_nit1"), ("cg", "steady_nit8"),
    ("cg", "steady_n4"), ("cg_l8", "steady"), ("cg_l8", "holdout2"), ("cg_w128", "steady"),
    ("mf", "holdout2"), ("mf", "steady_nrx4"), ("mf", "steady_nrx1"), ("mf_v16", "steady"),
    ("mf_l8", "steady"), ("mf_w128", "steady"),
)


#: The units the sets above name, as ``module:class`` (the library imports no example).
UNITS = {"cg": "examples.vecunit_cg.cg_unit:CgVecUnit", "mf": "examples.vecunit_mf.mf_unit:MfVecUnit"}
#: The fitted models' names on the packaged platform (``models/<name>/params.json``).
TIMING_MODEL, RESOURCE_MODEL = "vecunit_timing", "vecunit_resources"


def model_path(name: str) -> Path:
    """Where a fitted model is filed: the packaged platform the linear-algebra models share
    (:func:`waveflow.linalg.cost.platform_dir`)."""
    from waveflow.linalg.cost import platform_dir

    return platform_dir() / "models" / name / "params.json"


def make_unit(which: str, **kw):
    """A fresh unit of :data:`UNITS` (``"cg"``, ``"mf"``) on its own simulation."""
    from waveflow.simulation.simulation import Simulation

    mod, cls = UNITS[which].split(":")
    return getattr(importlib.import_module(mod), cls)(name="vecunit", sim=Simulation(), **kw)


def build_unit(build: str):
    """A fresh unit of the :data:`BUILDS` entry *build*."""
    which, kw = BUILDS[build]
    return make_unit(which, **kw)


# -- timing ---------------------------------------------------------------------------------------
def rtl_intervals(root) -> list[int]:
    """Each job's interval at RTL: the cycles between its status and the previous job's.  The first
    job's start is the run's, so it has none: the list starts at the second job."""
    from waveflow.vecunit.report import job_cycles

    return np.diff(job_cycles(Path(root))).astype(int).tolist()


def features(unit, kind: str) -> list[dict]:
    """The execute features of each job of *unit*'s workload *kind* but the first (as
    :func:`rtl_intervals`)."""
    from waveflow.vecunit.unit import job_features

    return [job_features(p.execute, unit.cfg) for _tx, p, _inputs in unit.jobs(kind)][1:]


@contextlib.contextmanager
def _timing(timing):
    """pysim's tasks charged from *timing* for the duration (it is a class attribute, read when the
    module is imported)."""
    from waveflow.vecunit.unit import _VecPart

    saved = _VecPart.timing
    _VecPart.timing = timing
    try:
        yield
    finally:
        _VecPart.timing = saved


def pysim_status(unit, kind: str, timing=None) -> list[float]:
    """The cycle each job's status leaves *unit* in pysim, in its harness on workload *kind*, charged
    from *timing* (default: the unit's)."""
    from waveflow.hw.harness import harness

    with _timing(timing if timing is not None else type(unit).timing):
        sysm = harness(unit, workload=kind, n_cycles=10**7)
        sink = sysm.stand_ins["m_status"]  # type: ignore[attr-defined]  # harness() attaches them
        times: list[float] = []
        rx = sink.stream_ep.rx_proc

        def timed(words):
            times.append(sink.env.now)
            yield from rx(words)

        sink.stream_ep.rx_proc = timed
        sysm.run()
    return (np.asarray(times) * unit.clk.freq).tolist()


def pysim_intervals(unit, kind: str, timing=None) -> list[float]:
    """:func:`rtl_intervals` in pysim: *unit* in its harness on workload *kind*, each status timed as
    it leaves, charged from *timing* (default: the unit's)."""
    return np.diff(pysim_status(unit, kind, timing)).tolist()


def fit_timing(feats: list[dict], cycles, weights=None) -> dict:
    """The coefficients of :data:`waveflow.vecunit.unit.FEATURES`: non-negative least squares on the
    relative error (each job weighted by ``1 / w**2``, ``w`` its *weights* -- its whole interval when
    *cycles* is what is left of it -- or *cycles*), the score's own measure."""
    from sklearn.linear_model import LinearRegression  # type: ignore[import-untyped]

    from waveflow.vecunit.unit import FEATURES

    X = np.array([[f[k] for k in FEATURES] for f in feats], dtype=float)
    y = np.asarray(cycles, dtype=float)
    w = np.asarray(cycles if weights is None else weights, dtype=float)
    reg = LinearRegression(fit_intercept=False, positive=True).fit(X, y, sample_weight=1.0 / w**2)
    return {k: float(c) for k, c in zip(FEATURES, reg.coef_)}


def errors(pred, meas) -> dict:
    """Relative errors of *pred* against *meas*: each, their mean and the worst."""
    p, m = np.asarray(pred, dtype=float), np.asarray(meas, dtype=float)
    if p.shape != m.shape:
        raise ValueError(f"{len(p)} predicted against {len(m)} measured")
    rel = np.abs(p - m) / m
    return {"rel": [round(float(r), 4) for r in rel], "mean": float(rel.mean()),
            "worst": float(rel.max())}


def _file(name: str, record: dict) -> Path:
    from waveflow.linalg.cost import PART, TOOL

    path = model_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": name, "part": PART, "tool": TOOL, **record}, indent=1)
                    + "\n", encoding="utf-8")
    return path


def calibrate_timing(calib_root) -> dict:
    """Fit the execute timing on the RTL run of the CG unit's ``"calib"`` workload at *calib_root*
    and file it; returns the record (with the fit's in-sample errors: the model's, and pysim's
    charged from it)."""
    from waveflow.vecunit.unit import FEATURES, VecTiming

    unit = make_unit("cg")
    feats, meas = features(unit, "calib"), rtl_intervals(calib_root)
    coefs = fit_timing(feats, meas)
    t = VecTiming.from_coefs(coefs)
    record = {"features": list(FEATURES), "coefs": coefs,
              "fit": {"set": "cg calib", "jobs": len(meas), "rtl": meas,
                      "model": errors([t.predict(f) for f in feats], meas),
                      "pysim": errors(pysim_intervals(unit, "calib", t), meas)},
              "plan": "plans/vecunit_new.md Stage 7"}
    _file(TIMING_MODEL, record)
    return record


def score_timing(roots: dict) -> dict:
    """The held-out score, once (rule 5): each :data:`TIMING_HOLDOUT` scenario's job intervals, pysim
    charged from the filed fit against the RTL run at ``roots[(unit, workload)]``; filed beside the
    fit."""
    from waveflow.vecunit.unit import VecTiming

    path = model_path(TIMING_MODEL)
    record = json.loads(path.read_text(encoding="utf-8"))
    if "holdout" in record:
        raise RuntimeError(f"{path}: the held-out set is scored once, and it has been")
    t = VecTiming.from_coefs(record["coefs"])
    out, pred, meas = {}, [], []
    for which, kind in TIMING_HOLDOUT:
        m = rtl_intervals(roots[(which, kind)])
        p = pysim_intervals(make_unit(which), kind, t)
        out[f"{which} {kind}"] = {"rtl": m, "pysim": [round(x, 1) for x in p], **errors(p, m)}
        pred += p
        meas += m
    out["all"] = {k: v for k, v in errors(pred, meas).items() if k != "rel"}
    out["targets"] = {"mean": TARGET_MEAN, "worst": TARGET_WORST}
    record["holdout"] = out
    _file(TIMING_MODEL, {k: v for k, v in record.items() if k not in ("model", "part", "tool")})
    return out



# -- timing by phase and configuration (Stage 9d) --------------------------------------------------
def measured_runs(paths) -> dict:
    """The RTL runs of the Stage 9c catalogues (``measured.json``, :mod:`waveflow.vecunit.measure`)
    at *paths*, merged: ``{"<build>_<workload>": run}``."""
    runs: dict = {}
    for p in paths:
        runs.update(json.loads(Path(p).read_text(encoding="utf-8"))["runs"])
    return runs


def _run(runs: dict, build: str, kind: str) -> dict:
    run = runs.get(f"{build}_{kind}")
    if run is None or not run.get("exact"):
        raise RuntimeError(f"no exact RTL run of {build} {kind} in the catalogues")
    return run


def timing2_rows(runs: dict, pairs, *, execute_only: bool) -> list[dict]:
    """Per job but each run's first (its interval is the run's start-up): its features, its build's
    schedule offset and its RTL interval.  *execute_only*: only jobs whose interval is the execute
    task's -- longer by :data:`EXECUTE_MARGIN` than the I/O that could have set it: the job's load,
    its store, and the previous job's store, which frees the output blocks the job writes (the fit's
    rule; the first fit read only the job's own bounds against the then-filed execute estimate, and
    admitted two jobs whose interval the previous job's store set: the plan's Stage 9 record)."""
    from waveflow.vecunit.report import job_bounds
    from waveflow.vecunit.unit import Schedule, job_features

    rows = []
    for build, kind in pairs:
        run = _run(runs, build, kind)
        unit = build_unit(build)
        sched = Schedule.from_dict(run["schedule"])
        meas = np.diff(run["status_cycles"]).astype(int).tolist()
        jobs = unit.jobs(kind)
        bounds = [job_bounds(unit.cfg, progs) for _tx, progs, _inp in jobs]
        for j, ((_tx, progs, _inp), m) in enumerate(zip(jobs[1:], meas), start=1):
            io = max(bounds[j]["load"], bounds[j]["store"], bounds[j - 1]["store"])
            if execute_only and m < EXECUTE_MARGIN * io:
                continue
            f = job_features(progs.execute, unit.cfg)
            rows.append({"build": build, "kind": kind, "feats": f, "offset": sched.offset(f),
                         "rtl": m})
    return rows


def _reload_timing() -> None:
    """Make pysim charge from the filed timing and schedules (read when the module was imported)."""
    from waveflow.vecunit import unit as U

    U._VecPart.timing = U._calibrated()


def calibrate_timing2(paths) -> dict:
    """Fit the execute timing by phase and configuration (Stage 9d, pre-registered): each build's
    schedule from csynth accounts for its loops, and a non-negative least squares on the relative
    error fits the rest over :data:`TIMING2_FIT`'s execute-bound jobs; file it with every measured
    build's schedule (Stage 7's record kept under ``"v1"``).  Returns the record."""
    from waveflow.vecunit.hls import header_name
    from waveflow.vecunit.unit import FEATURES, VecTiming

    runs = measured_runs(paths)
    rows = timing2_rows(runs, TIMING2_FIT, execute_only=True)
    coefs = fit_timing([r["feats"] for r in rows], [r["rtl"] - r["offset"] for r in rows],
                       weights=[r["rtl"] for r in rows])
    path = model_path(TIMING_MODEL)
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    schedules = {r["header"]: r["schedule"] for r in runs.values() if r.get("schedule")}
    default = header_name(build_unit("cg").cfg)
    record: dict = {"version": 2, "features": list(FEATURES), "coefs": coefs, "schedules": schedules,
              "default_schedule": default,
              "v1": old.get("v1") or {k: v for k, v in old.items()
                                      if k not in ("model", "part", "tool")},
              "plan": "plans/vecunit_new.md Stage 9d"}
    t = VecTiming.from_coefs(coefs)
    pred = [t.predict(r["feats"]) + r["offset"] for r in rows]  # t has no schedule: add it
    record["fit"] = {"set": [list(x) for x in TIMING2_FIT], "jobs": len(rows),
                     "model": errors(pred, [r["rtl"] for r in rows])}
    _file(TIMING_MODEL, record)
    _reload_timing()
    t = type(make_unit("cg")).timing
    p_all, m_all = [], []
    for build, kind in TIMING2_FIT:
        run = _run(runs, build, kind)
        p_all += pysim_intervals(build_unit(build), kind, t)
        m_all += np.diff(run["status_cycles"]).astype(int).tolist()
    record["fit"]["pysim"] = errors(p_all, m_all)
    _file(TIMING_MODEL, {k: v for k, v in record.items() if k not in ("model", "part", "tool")})
    return record


def score_timing2(paths) -> dict:
    """The held-out score, once (rule 5): each :data:`TIMING2_HOLDOUT` pair's job intervals but the
    first, pysim charged from the filed fit against the RTL's; and, reported beside it, each run's
    first status (its start-up), pysim against RTL.  Filed beside the fit."""
    path = model_path(TIMING_MODEL)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("version") != 2:
        raise RuntimeError(f"{path}: fit Stage 9d's timing first (calibrate_timing2)")
    if "holdout" in record:
        raise RuntimeError(f"{path}: the held-out set is scored once, and it has been")
    _reload_timing()
    runs = measured_runs(paths)
    out, pred, meas, start = {}, [], [], {}
    for build, kind in TIMING2_HOLDOUT:
        run = _run(runs, build, kind)
        ps = pysim_status(build_unit(build), kind)
        p, m = np.diff(ps).tolist(), np.diff(run["status_cycles"]).astype(int).tolist()
        out[f"{build} {kind}"] = {"rtl": m, "pysim": [round(x, 1) for x in p], **errors(p, m)}
        start[f"{build} {kind}"] = {"rtl": int(run["status_cycles"][0]), "pysim": round(ps[0], 1)}
        pred += p
        meas += m
    out["all"] = {k: v for k, v in errors(pred, meas).items() if k != "rel"}
    out["targets"] = {"mean": TARGET_MEAN, "worst": TARGET_WORST}
    out["start"] = start
    record["holdout"] = out
    _file(TIMING_MODEL, {k: v for k, v in record.items() if k not in ("model", "part", "tool")})
    return out


def settle_timing2(paths) -> dict:
    """After :func:`score_timing2`: the incumbent timing (Stage 7's, kept under ``"v1"``) scored on the
    same held-out set -- no fit, a comparison against the model pysim already used -- and the one with
    the lower held-out mean filed for pysim, the other's record kept beside it (``"v1"`` or ``"v2"``),
    with both scores under ``"comparison"``."""
    from waveflow.vecunit import unit as U

    path = model_path(TIMING_MODEL)
    v2 = json.loads(path.read_text(encoding="utf-8"))
    if v2.get("version") != 2 or "holdout" not in v2:
        raise RuntimeError(f"{path}: fit and score Stage 9d's timing first")
    v1 = v2.pop("v1")
    t1 = U.VecTiming.from_coefs(v1["coefs"])
    saved = dict(U.SCHEDULES), list(U.DEFAULT_SCHEDULE)
    U.SCHEDULES.clear()  # Stage 7's timing carries no schedule
    U.DEFAULT_SCHEDULE.clear()
    runs = measured_runs(paths)
    pred, meas, pairs = [], [], {}
    try:
        for build, kind in TIMING2_HOLDOUT:
            run = _run(runs, build, kind)
            ps = pysim_status(build_unit(build), kind, t1)
            p, m = np.diff(ps).tolist(), np.diff(run["status_cycles"]).astype(int).tolist()
            pairs[f"{build} {kind}"] = {**errors(p, m), "start": round(ps[0] - run["status_cycles"][0], 1)}
            pred += p
            meas += m
    finally:
        U.SCHEDULES.update(saved[0])
        U.DEFAULT_SCHEDULE[:] = saved[1]
    a = errors(pred, meas)
    comparison = {"set": [list(x) for x in TIMING2_HOLDOUT],
                  "v1": {"all": {"mean": a["mean"], "worst": a["worst"]}, "pairs": pairs},
                  "v2": {"all": v2["holdout"]["all"]}}
    keep_v1 = a["mean"] < v2["holdout"]["all"]["mean"]
    rest = {k: v for k, v in v2.items() if k not in ("model", "part", "tool")}
    if keep_v1:
        record = {**{k: v for k, v in v1.items() if k not in ("model", "part", "tool")},
                  "v2": rest, "comparison": comparison, "kept": "v1"}
    else:
        record = {**rest, "v1": v1, "comparison": comparison, "kept": "v2"}
    _file(TIMING_MODEL, record)
    _reload_timing()
    return {"kept": record["kept"], **comparison}


# -- resources ------------------------------------------------------------------------------------
def resource_config(spec):
    """The configuration a :data:`RESOURCE_FIT` / :data:`RESOURCE_HOLDOUT` row names.  ``"cg"``: the
    CG unit's formats and layout (four groups a column group, so ``Nmax = L * NG / 4``; blocks of 8
    rows); ``"mf"``: the matched filter's (its four group formats repeated to ``NG``; blocks of 64
    rows and 8 columns).  ``"mul1"``: only slot 0 has a multiplier."""
    import dataclasses

    from waveflow.vecunit.config import Cap

    L, NG, S, caps, fmts = spec
    if fmts == "cg":
        cfg = make_unit("cg", L=L, Nmax=L * NG // 4).cfg
    else:
        cfg = make_unit("mf", L=L).cfg
        g = cfg.group_formats
        cfg = dataclasses.replace(cfg, group_formats=tuple(g[i % len(g)] for i in range(NG)))
    rest = Cap.MOVE | Cap.LOAD | Cap.STORE
    slot_caps = () if caps == "all" else (Cap.ALL,) + (rest,) * (S - 1)
    cfg = dataclasses.replace(cfg, S=S, slot_caps=slot_caps)
    if cfg.NG != NG:
        raise ValueError(f"{spec}: the configuration has {cfg.NG} groups")
    return cfg


def config_features(cfg) -> dict:
    """What a configuration's resources can depend on: its sizes and the datapath's widths
    (:func:`waveflow.vecunit.hls.widths`)."""
    from waveflow.vecunit.config import Cap
    from waveflow.vecunit.hls import widths

    w = widths(cfg)
    caps = [int(c) for c in cfg.slot_caps]
    return {"L": cfg.L, "NG": cfg.NG, "S": cfg.S, "NB": cfg.NB, "VLMAX": cfg.VLMAX,
            "Kmax": cfg.Kmax, "NGQ": cfg.NGQ, "NQI": cfg.NQI, "NQO": cfg.NQO, "NS": cfg.NS,
            "n_mul": sum(bool(c & int(Cap.MUL | Cap.DOT)) for c in caps),
            "n_dot": sum(bool(c & int(Cap.DOT)) for c in caps),
            "n_vfmt": len(cfg.vector_formats), "n_sfmt": len(cfg.scalar_format_set),
            "max_run": cfg.max_run, "max_srun": cfg.max_srun,
            **{k: getattr(w, k) for k in ("WV", "WSR", "AW", "RW", "DW", "NW", "QW")},
            "bram18": _bram18(cfg, w)}


def _bram18(cfg, w) -> int:
    """The unit's BRAM18 as its memories bind (Vitis HLS 2024.1): the register file, a simple
    dual-port memory a bank and lane (up to 36 bits wide, 512 deep); the scalar file, a true
    dual-port memory a lane (18-bit ports); each queue's two blocks, one memory of lane groups."""
    def sdp(width: int, depth: int) -> int:
        return -(-width // 36) * -(-depth // 512)

    rf = cfg.NB * cfg.L * sdp(2 * w.WV, cfg.NG // cfg.NB * cfg.VLMAX)
    sf = cfg.L * -(-w.WSR // 18) * -(-cfg.NS // 1024)
    q = (cfg.NQI + cfg.NQO) * sdp(2 * w.WV * cfg.L, 2 * cfg.Kmax * cfg.NGQ)
    return rf + sf + q


def measure_resources(spec, root) -> dict:
    """csynth (alone) of the generic unit with the configuration *spec* names, at *root*: its
    features and csynth's estimate for the unit and each task."""
    from waveflow.simulation.simulation import Simulation
    from waveflow.vecunit.build import build_config, build_dag
    from waveflow.vecunit.report import resources
    from waveflow.vecunit.unit import VecUnit

    cfg = resource_config(spec)
    unit = VecUnit(name="vecunit", sim=Simulation(), cfg=cfg)
    res = build_dag(unit).run(build_config(root), through="csynth")
    bad = [f"{n}: {r.message[:500]}" for n, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"{spec}: " + "; ".join(bad))
    return {"spec": list(spec), "features": config_features(cfg), "csynth": resources(Path(root))}



#: The resource model: per counter, the terms it is linear in (with an intercept), each a product of
#: :func:`config_features`, chosen from csynth's breakdown of the fit set alone.  LUT and FF: the
#: scalar dividers, one a lane (``L`` and ``L*NW*QW``: a lane's cost affine in its divider's width);
#: the fused loop, whose operand and forwarding muxes and pipeline grow with the slots chained
#: before each slot (``S*S*L``), for FF with a curvature in ``L`` (``S*S*L*L``: csynth gives the
#: CG build's run loop 8.5k, 27.5k and 47.0k FF at ``L`` 2, 4 and 8); the multipliers (``n_mul*L``).  Each basis is the one of the few tried with the
#: lowest leave-one-out error on the fit set.  DSP: four a complex multiplier.  BRAM: the memories
#: as they bind (``bram18``).
RESOURCE_BASIS: dict[str, tuple[str, ...]] = {
    "lut": ("L", "L*NW*QW", "S*S*L", "n_mul*L"),
    "ff": ("L", "L*NW*QW", "S*S*L", "S*S*L*L", "n_mul*L"),
    "dsp": ("n_mul*L",),
    "bram": ("bram18",),
}


def resource_terms(feats: dict, counter: str) -> dict[str, float]:
    """The values of *counter*'s terms (:data:`RESOURCE_BASIS`) for a configuration's features."""
    out = {}
    for term in RESOURCE_BASIS[counter]:
        v = 1.0
        for name in term.split("*"):
            v *= float(feats[name])
        out[term] = v
    return out


def fit_resources(rows: list[dict]) -> dict:
    """Each counter of the unit's csynth estimate, linear in its terms with an intercept, fitted on
    the relative error (each row weighted by ``1 / value**2``).  The signs are free: the fit set has
    two format sets, so a width term and its lane term together fix a lane's cost at two widths."""
    from sklearn.linear_model import LinearRegression  # type: ignore[import-untyped]

    model = {}
    for counter, basis in RESOURCE_BASIS.items():
        X = np.array([[resource_terms(r["features"], counter)[t] for t in basis] for r in rows])
        y = np.array([float(r["csynth"]["unit"][counter]) for r in rows])
        reg = LinearRegression().fit(X, y, sample_weight=1.0 / np.maximum(y, 1) ** 2)
        model[counter] = {"basis": list(basis), "coefs": [float(c) for c in reg.coef_],
                          "intercept": float(reg.intercept_)}
    return model


def predict_resources(model: dict, feats: dict) -> dict[str, float]:
    """The unit's LUT, FF, DSP and BRAM for a configuration's :func:`config_features`."""
    out = {}
    for counter, m in model.items():
        t = resource_terms(feats, counter)
        out[counter] = m["intercept"] + sum(c * t[b] for b, c in zip(m["basis"], m["coefs"]))
    return out


def _rows(directory, which: str) -> list[dict]:
    """The sweep's rows of one set (``<directory>/<which>_<i>.json``, as ``csynth`` prints them), in
    the set's order; a missing row is an error."""
    specs = {"fit": RESOURCE_FIT, "holdout": RESOURCE_HOLDOUT}[which]
    rows = []
    for i, spec in enumerate(specs):
        r = json.loads((Path(directory) / f"{which}_{i}.json").read_text(encoding="utf-8"))
        if tuple(r["spec"]) != tuple(spec):
            raise ValueError(f"{which}_{i}: measured {r['spec']}, pre-registered {spec}")
        r["features"] = config_features(resource_config(spec))  # a function of the configuration
        rows.append(r)
    return rows


def _resource_errors(model: dict, rows: list[dict]) -> dict:
    out = {}
    for counter in model:
        pred = [predict_resources(model, r["features"])[counter] for r in rows]
        meas = [r["csynth"]["unit"][counter] for r in rows]
        out[counter] = {"measured": meas, "predicted": [round(p, 1) for p in pred],
                        **errors(pred, meas)}
    return out


def calibrate_resources(directory) -> dict:
    """Fit the resource model on the sweep's fit rows at *directory* and file it."""
    rows = _rows(directory, "fit")
    model = fit_resources(rows)
    record = {"counters": model, "fit": {"configurations": [r["spec"] for r in rows],
                                         **_resource_errors(model, rows)},
              "plan": "plans/vecunit_new.md Stage 7"}
    _file(RESOURCE_MODEL, record)
    return record


def score_resources(directory) -> dict:
    """The held-out score, once (rule 5): the filed model against the sweep's held-out rows."""
    path = model_path(RESOURCE_MODEL)
    record = json.loads(path.read_text(encoding="utf-8"))
    if "holdout" in record:
        raise RuntimeError(f"{path}: the held-out set is scored once, and it has been")
    rows = _rows(directory, "holdout")
    out = {"configurations": [r["spec"] for r in rows],
           **_resource_errors(record["counters"], rows)}
    record["holdout"] = out
    _file(RESOURCE_MODEL, {k: v for k, v in record.items() if k not in ("model", "part", "tool")})
    return out


def main(argv=None) -> int:
    """``fit-timing <calib root>``; ``score-timing <cg holdout> <cg steady> <mf steady>`` (roots of
    the RTL runs); ``fit-timing2 <measured.json>...``, ``score-timing2 <measured.json>...``,
    ``settle-timing2 <measured.json>...`` (Stage 9d, on the Stage 9c catalogues); ``csynth <fit|holdout> <index> <root>`` (one resource row, its JSON printed);
    ``fit-resources <dir>``, ``score-resources <dir>`` (the rows ``csynth`` printed, saved as
    ``<dir>/<fit|holdout>_<index>.json``)."""
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["fit-timing"] and len(a) == 2:
        r = calibrate_timing(a[1])
        print(json.dumps({k: r[k] for k in ("coefs", "fit")}, indent=1))
        return 0
    if a[:1] == ["score-timing"] and len(a) == 4:
        print(json.dumps(score_timing(dict(zip(TIMING_HOLDOUT, a[1:]))), indent=1))
        return 0
    if a[:1] == ["fit-timing2"] and len(a) >= 2:
        r = calibrate_timing2(a[1:])
        print(json.dumps({k: r[k] for k in ("coefs", "fit")}, indent=1))
        return 0
    if a[:1] == ["score-timing2"] and len(a) >= 2:
        print(json.dumps(score_timing2(a[1:]), indent=1))
        return 0
    if a[:1] == ["settle-timing2"] and len(a) >= 2:
        r = settle_timing2(a[1:])
        print(json.dumps({"kept": r["kept"], "v1": r["v1"]["all"], "v2": r["v2"]["all"]}, indent=1))
        return 0
    if a[:1] == ["csynth"] and len(a) == 4:
        spec = {"fit": RESOURCE_FIT, "holdout": RESOURCE_HOLDOUT}[a[1]][int(a[2])]
        print(json.dumps(measure_resources(spec, a[3])))
        return 0
    if a[:1] in (["fit-resources"], ["score-resources"]) and len(a) == 2:
        fn = calibrate_resources if a[0] == "fit-resources" else score_resources
        print(json.dumps(fn(a[1]), indent=1))
        return 0
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
