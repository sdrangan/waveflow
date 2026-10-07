"""measure.py — one build, measured: csynth resources, the RTL check, and cycles.

Step 5.2 of ``plans/mimo_cg/mimo_cg_paper_sims.md`` (gate 5.0 decision record, §14).

:func:`measure` takes one build of the split (:mod:`examples.mimo_cg.hw.space`) through

1. **generate → csynth** for ``xczu48dr-ffvg1517-2-e`` at 4 ns (:mod:`examples.mimo_cg.hw.build`);
2. **attribution** of the report to the design's modules (the framework's
   :func:`~waveflow.calib.synth_report.report_from_solution`), plus the top's channel tables;
3. a **traced XSI run** of a short scenario, whose every output word is checked against the golden
   (``check_xsi_outputs``), so each build is also an RTL check of its knobs;
4. **extraction** of the two cycle ground truths of gate 5.0.

Cycle ground truth
------------------
*Job intervals* (full designs).  Jobs are issued back to back, and the done sink logs the cycle of
every completion.  The interval between consecutive completions is fitted as ``T0 + nit·T_iter``.

*Block spans* (blocks).  A unit's job interval can be bound by memory traffic instead of by its block,
so a block is timed from its own stream-of-blocks **lock handshakes**, which Vitis lifts into the top
scope where a level-1 trace sees them (the nets are named as in
:meth:`waveflow.utils.vcd.VcdParser.add_sob_signals`).  A task pulses ``<chan>_read`` when it releases
an input block and ``<chan>_write`` when it releases an output block.  One *span* runs from the later
of "the input block was released by its producer" and "the task's own previous lock event", to the
task's release of the output block:

* ``wait`` regime — the task was idle when its input arrived (the detector's loop, and a
  memory-bound unit);
* ``b2b`` regime — the input was already waiting (a block-bound unit).

In the detector the vector unit and the matmul wait for each other, so their ``wait`` spans tile the
loop: one iteration takes exactly ``mm.iter + vec.iter`` cycles.  A task writes its output block
freely and only the hand-over (the ``_write`` pulse) waits for the channel to be free, so a span whose
output channel became free just before the hand-over is marked stalled and left out.

Since gate 9.0 (plan §14) the detector's blocks are Waveflow's ``SystolicCore`` and
``CgVectorCore``, and a unit build is the component's standalone unit in the ``tests/linalg/`` bench
its calibration used: its workload is the bench's requests and its RTL check the bench's (every
reply against the model).  A lock net is named by the task's *port* and a free net by the
*channel*; in the detector the systolic core's ``b_blk`` and ``c_blk`` ports sit on the ``p_blk``
and ``s_blk`` channels, so :func:`block_channels` maps one to the other from the generated top.

``python -m examples.mimo_cg.hw.measure --build <label>`` measures one build of the split.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import zlib
from pathlib import Path

import numpy as np

from examples.mimo_cg.hw import build as B
from examples.mimo_cg.hw.common import DEFAULT_N, CgDesc, hw_format
from examples.mimo_cg.hw.space import NITS, HwConfig, read_split

HERE = Path(__file__).resolve().parent
#: Per-build measurement records (gitignored working files; the merged tables go to paper_data/).
POINTS_DIR = HERE.parent / "results" / "hw_points"
#: The work tier of the calibration platform (untracked; promoted with ``waveflow_calib publish``).
WORK_ROOT = HERE.parent / "calib" / "work"
PLATFORM = "xczu48dr_250mhz"
CLK_FREQ = 250e6

TOP_NAME = {"vec": B.CG_UNIT_TOP, "mm": B.SYSTOLIC_UNIT_TOP, "det": B.DET_TOP}
#: The lock events that open and close one span of a block: ``kind -> (task, input port, output
#: port)``.  The kinds keep Phase 5's names: ``mm.iter`` is one matrix of the systolic core, the
#: ``vec.*`` spans the CG core's start, iteration and last iteration.
SPAN_KINDS = {
    "mm.iter": ("systolic_core_task", "b_blk", "c_blk"),
    "vec.init": ("cg_vector_task", "b_blk", "p_blk"),
    "vec.iter": ("cg_vector_task", "s_blk", "p_blk"),
    "vec.last": ("cg_vector_task", "s_blk", "x_blk"),
}
#: A writer whose output channel became free at most this many cycles before it handed its block
#: over was waiting for the channel: that span is stalled.
_STALL_WINDOW = 3


# --- the build and its workload --------------------------------------------------------------


def comp_class(top: str):
    """The top's component class: a unit build is Waveflow's standalone unit in its bench (gate
    9.0)."""
    if top == "vec":
        from tests.linalg._cg_unit_bench import CgUnitBench

        return CgUnitBench
    if top == "mm":
        from tests.linalg._unit_bench import UnitBench

        return UnitBench
    from examples.mimo_cg.hw.detector import CgDetector

    return CgDetector


def gen_kwargs(top: str, c: HwConfig) -> dict:
    """The keyword arguments of the top's ``generate_*`` function for this configuration."""
    kw = {"K": c.K, "L": c.L, "fmt": c.fmt, "mem_dw": c.mem_dw}
    if top == "det":
        kw["cmd_depth"] = c.cmd_depth  # the units' commands are not queued
    kw["sob_depth"] = c.sob_depth
    if top != "vec":
        kw |= {"R": c.R, "C": c.C, "cmul": c.cmul}
    return kw


def elab_params(top: str, c: HwConfig) -> dict:
    """The elaboration parameters of the top (the names the component classes use)."""
    kw = gen_kwargs(top, c)
    if top != "det":
        return {"unit": tuple(B.unit_fields(top, **kw).items())}
    kw["mem_dwidth"] = kw.pop("mem_dw")
    return kw | {"N": DEFAULT_N}


#: In a ``steady`` run, jobs of up to this many iterations are issued twice in a row.
STEADY_PAIR_MAX_NIT = 4


def job_nits(K: int, steady: bool = False) -> list[int]:
    """The scenario's jobs.

    By default (the calibration and held-out campaigns): a short ramp, the longest job, then the
    ramp's start again, so the interval fit has four distinct iteration counts and one repeat.

    ``steady`` (the brute force, plan step 6.3): every iteration count of the accuracy table, in
    rising order, the short ones twice in a row.  A short job can finish before the next job's
    matrices are loaded, and that delays the next job; the second of two equal jobs is past it, so
    its interval is the job time of a stream of such jobs (:func:`job_intervals`).
    """
    if not steady:
        return [1, 2, 3, K, 1, 2]
    return [
        nit for nit in NITS[K] for _ in range(2 if nit <= STEADY_PAIR_MAX_NIT else 1)
    ]


def workload(top: str, c: HwConfig, build: str, steady: bool = False):
    """``(problems, jobs)`` of the build's scenario, seeded by the build's name.

    A detector's problems are ``(A, B, scale)`` per job, the last the zero-residual one.  A unit
    build's are the bench's requests (gate 9.0): a vector unit runs the job list as CG jobs (a
    ``START`` and ``nit`` ``STEP`` requests each, ``S`` from the model; the last job's ``B`` has a
    zero column), and a matmul unit one ``MUL`` request per iteration of the job list.
    """
    jobs = job_nits(c.K, steady)
    seed = zlib.crc32(build.encode()) & 0xFFFF
    if top == "det":
        from examples.mimo_cg.hw.detector import detector_problems

        problems = detector_problems(
            64 if c.K > 4 else 32, c.K, DEFAULT_N, len(jobs), seed
        )
    elif top == "vec":
        from tests.linalg._cg_unit_bench import random_job

        rng = np.random.default_rng([seed, c.K])
        f = hw_format(c.fmt)
        problems = [
            random_job(rng, f, nit, c.K, DEFAULT_N, zero_column=i == len(jobs) - 1)
            for i, nit in enumerate(jobs)
        ]
    else:
        from tests.linalg._unit_bench import random_job
        from waveflow.linalg.systolic import MatmulOp

        rng = np.random.default_rng([seed, c.K])
        unit = B.unit_fields("mm", **gen_kwargs("mm", c))
        problems = [
            random_job(rng, unit, MatmulOp.MUL, c.K, c.K, DEFAULT_N, edge=i == 0)
            for i in range(sum(jobs))
        ]
    return problems, jobs


def cycles_bound(top: str, c: HwConfig, jobs: list[int]) -> int:
    """A generous upper bound on the cycles the scenario needs (the harness runs exactly this many).

    Not a model: only a budget, checked afterwards by the done count and doubled by :func:`measure`
    when it was too small.
    """
    N, K = DEFAULT_N, c.K
    words = K * N * 32 // c.mem_dw  # one K×N matrix in memory
    vec = (N // c.L) * (3 * K + 4 * (c.W + c.g_s) + 80) + 2 * K * N // c.L
    mm = (K // c.R) * (N // c.C) * (K + c.R + c.C + 12) + 2 * K * N // c.L
    per_iter = {"vec": vec, "mm": mm, "det": vec + mm}[top]
    per_iter = max(per_iter, 3 * words) + 100
    per_job = 6 * words + 400
    return int(1.5 * (sum(jobs) * per_iter + len(jobs) * (per_iter + per_job))) + 5000


#: A model-based budget is this much above the predicted run, plus :data:`BUDGET_SLACK` cycles.
BUDGET_MARGIN = 1.25
BUDGET_SLACK = 4000


def cycles_budget(c: HwConfig, jobs: list[int]) -> int:
    """A tight budget for a detector run, from the calibrated job time (falls back to
    :func:`cycles_bound` when there is no model file).

    A thousand-build campaign cannot afford :func:`cycles_bound`, which is three to seven times
    what a run needs.  Here each job is budgeted at its predicted time, or at the time its matrices
    take to cross the memory port when that is longer, with :data:`BUDGET_MARGIN` on top.  It is a
    budget and nothing else: :func:`measure` checks the done count afterwards and runs again with
    twice the cycles on a miss, so a budget cannot change a measured interval.
    """
    from examples.mimo_cg.hw import models as MD

    models = MD.calibrated()
    if models is None:
        return cycles_bound("det", c, jobs)
    words = (c.K * c.K + 2 * c.K * DEFAULT_N) * 32 // c.mem_dw  # A and B in, X out
    need = sum(max(models.job_cycles(c, nit), words) for nit in jobs)
    return int(BUDGET_MARGIN * need) + 3 * words + BUDGET_SLACK


def _bench(top: str):
    """The ``tests/linalg/`` bench module of a unit build."""
    if top == "vec":
        from tests.linalg import _cg_unit_bench as bench
    else:
        from tests.linalg import _unit_bench as bench
    return bench


def _sim(top: str, c: HwConfig, problems, jobs, n_cycles: int):
    if top == "det":
        from examples.mimo_cg.hw.detector import CgDetectorSim

        kw = elab_params(top, c) | {"n_cycles": n_cycles}
        kw.pop("N")
        return CgDetectorSim(problems, jobs, **kw)
    unit = B.unit_fields(top, **gen_kwargs(top, c))
    bench = _bench(top)
    sim_class = bench.CgUnitBenchSim if top == "vec" else bench.UnitBenchSim
    return sim_class(unit, problems, n_cycles=n_cycles)


def block_channels(top: str, c: HwConfig) -> dict:
    """``{(task, port): channel}`` for every stream-of-blocks port of the build's top, from the
    generated top's task calls (a task's arguments are its ports' channels, in its port order).
    """
    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.build.elaborate import elaborate

    comp = elaborate(comp_class(top), elab_params(top, c), name=TOP_NAME[top])
    calls = {
        t.task_fn: t.args for t in composite_top_spec(comp, width=int(c.mem_dw)).tasks
    }
    out: dict = {}
    stack = [comp]
    while stack:
        m = stack.pop()
        subs = list(getattr(m, "sub_comps", {}).values())
        stack.extend(subs)
        if subs or not hasattr(m, "kernel_task"):
            continue
        kt = m.kernel_task()
        for port, chan in zip(kt.signature, calls.get(kt.task_fn, ()), strict=False):
            if port.endswith("_blk"):
                out[(kt.task_fn, port)] = chan
    return out


# --- resources: the report, attributed -------------------------------------------------------


def _table(rpt: str, section: str) -> list[dict]:
    """The rows of one ``* <section>:`` table of the top's utilization detail, as dicts."""
    m = re.search(
        rf"^\s*\* {re.escape(section)}:\s*\n(.*?)(?=^\s*\* \w|\Z)",
        rpt,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return []
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    cells = [[x.strip() for x in ln.strip("|").split("|")] for ln in lines]
    header, rows = cells[0], cells[1:]
    return [dict(zip(header, r, strict=True)) for r in rows if r[0] and r[0] != "Total"]


def _int(text: str) -> int:
    return int(text) if text.strip().lstrip("-").isdigit() else 0


def channel_rows(report_dir: Path, top_name: str, module_rtl: set[str]) -> list[dict]:
    """What the top holds besides the modules: its memories (the stream-of-blocks), its FIFOs and
    the instances that are not modules (the bus adapters and the entry process)."""
    rpt = (Path(report_dir) / f"{top_name}_csynth.rpt").read_text(encoding="utf-8")
    util = rpt[
        rpt.index("== Utilization Estimates") :
    ]  # the latency section has tables too
    detail = util[util.index("+ Detail:") :]
    out = []
    for r in _table(detail, "Memory"):
        out.append(
            {
                "name": r["Memory"],
                "kind": "memory",
                "bram": _int(r["BRAM_18K"]),
                "ff": _int(r["FF"]),
                "lut": _int(r["LUT"]),
                "words": _int(r["Words"]),
                "bits": _int(r["Bits"]),
                "banks": _int(r["Banks"]),
            }
        )
    for r in _table(detail, "FIFO"):
        out.append(
            {
                "name": r["Name"],
                "kind": "fifo",
                "bram": _int(r["BRAM_18K"]),
                "ff": _int(r["FF"]),
                "lut": _int(r["LUT"]),
                "words": _int(r["Depth"]),
                "bits": _int(r["Bits"]),
                "banks": 1,
            }
        )
    for r in _table(detail, "Instance"):
        if r["Module"] not in module_rtl:
            out.append(
                {
                    "name": r["Instance"],
                    "kind": "instance",
                    "bram": _int(r["BRAM_18K"]),
                    "ff": _int(r["FF"]),
                    "lut": _int(r["LUT"]),
                    "words": 0,
                    "bits": 0,
                    "banks": 0,
                }
            )
    return out


def _loop_label(rtl_name: str) -> str:
    """The loop a sub-block row belongs to: ``cg_mm_task_64_4_…_Pipeline_SWEEP`` → ``SWEEP``."""
    return rtl_name.split("_Pipeline_", 1)[-1]


def attribute(top: str, c: HwConfig, out_dir: Path) -> dict:
    """The build's csynth report, attributed: module rows (each with the rows of its pipelined
    loops), the integration remainder, the total, the top's channel rows and the estimated clock.
    """
    from waveflow.build.elaborate import elaborate
    from waveflow.calib.synth_report import report_from_solution
    from waveflow.utils.csynthparse import synth_target

    name = TOP_NAME[top]
    comp = elaborate(comp_class(top), elab_params(top, c), name=name)
    sol = Path(out_dir) / f"{name}_proj" / "solution1"
    report = report_from_solution(comp, sol, top_name=name)
    modules = [
        {
            "cls": m.cls_name,
            "key": m.key,
            "rtl_module": m.rtl_module,
            **m.resources,
            "subblocks": {_loop_label(k): dict(v) for k, v in m.subblocks.items()},
        }
        for m in report.modules
    ]
    target = synth_target(sol / "syn" / "report")
    channels = channel_rows(
        sol / "syn" / "report", name, {m["rtl_module"] for m in modules}
    )
    # The report does not itemize everything it counts (its FIFO table lists no LUTs, its summary
    # does), so whatever the rows leave of the integration remainder is kept as one more row.
    rest = {
        k: report.integration[k] - sum(ch.get(k, 0) for ch in channels)
        for k in ("lut", "ff", "dsp", "bram", "uram")
    }
    if any(rest.values()):
        channels.append(
            {
                "name": "unitemized",
                "kind": "unitemized",
                **rest,
                "words": 0,
                "bits": 0,
                "banks": 0,
            }
        )
    return {
        "modules": modules,
        "integration": dict(report.integration),
        "total": dict(report.top),
        "channels": channels,
        "est_ns": target["estimated_period_ns"],
        "target_ns": target["target_period_ns"],
        "part": target["part"],
    }


#: The block a unit build exists to measure (gate 5.0 decision 3; the components' cores since
#: gate 9.0).
BLOCK_CLASS = {"vec": "CgVectorCore", "mm": "SystolicCore"}


def files_record(top: str, cls_name: str) -> bool:
    """Whether a build of ``top`` files the record of a module of class ``cls_name``.

    A block's models are fitted only on rows from that block's own unit builds, and the glue only on
    detector builds (gate 5.0 decision 3).  So a unit build files its block and nothing else, and a
    detector build files everything but the two blocks.
    """
    if top in BLOCK_CLASS:
        return cls_name == BLOCK_CLASS[top]
    return cls_name not in BLOCK_CLASS.values()


def file_records(
    top: str,
    c: HwConfig,
    out_dir: Path,
    *,
    tool: str,
    cost_seconds: float,
    work_root: Path | None = None,
) -> int:
    """File the build's records into the work platform's store; returns how many were filed.

    Which module records a build files is :func:`files_record`.  A detector build also files its
    integration record (the top minus its modules); a unit build's remainder belongs to a top that
    is not part of the detector and is not filed.

    Called serially, and only for ``fit`` builds: a held-out build must never reach the store the
    models are fitted from.
    """
    from waveflow.build.elaborate import elaborate
    from waveflow.calib.module_key import walk_modules
    from waveflow.calib.platform import Platform
    from waveflow.calib.record_store import ModuleStore
    from waveflow.calib.resource_model import InterfaceResourceModel
    from waveflow.calib.synth_report import report_from_solution, store_report

    name = TOP_NAME[top]
    comp = elaborate(comp_class(top), elab_params(top, c), name=name)
    report = report_from_solution(
        comp, Path(out_dir) / f"{name}_proj" / "solution1", top_name=name
    )
    platform = Platform.resolve(
        work_root or WORK_ROOT, PLATFORM, part=B.PART, clk_freq=CLK_FREQ
    )
    walked = list(walk_modules(comp))
    identities = {i.key: i for _, m, i in walked if files_record(top, type(m).__name__)}
    filed = store_report(
        report,
        ModuleStore(platform.dir),
        identities,
        source="hls_estimate",
        part=B.PART,
        period_ns=B.PERIOD_NS,
        tool=tool,
        cost_seconds=cost_seconds,
        top_identity=(
            next((i for _p, m, i in walked if m is comp), None)
            if top == "det"
            else None
        ),
        boundary=InterfaceResourceModel().get_params(comp) if top == "det" else None,
    )
    return len(filed)


# --- cycles: job intervals -------------------------------------------------------------------


def job_intervals(
    done_cycles: list[int], jobs: list[int], steady: bool = False
) -> dict:
    """Fit the intervals between consecutive job completions as ``T0 + nit·T_iter``.

    ``done_cycles`` holds one cycle per done word; a job's completion is its last word.

    With ``steady`` (a :func:`job_nits` ``steady`` list) the result also has ``steady``, the job
    time per iteration count: the interval of the *last* job of each count, which for a short job
    is the second of its pair.  The fit then uses those intervals only.
    """
    per = len(done_cycles) // len(jobs)
    if per < 1 or per * len(jobs) != len(done_cycles):
        raise ValueError(f"{len(done_cycles)} done words for {len(jobs)} jobs")
    done = np.asarray(done_cycles[per - 1 :: per], dtype=float)
    gaps = np.diff(done)
    nit = np.asarray(jobs[1:], dtype=float)
    out = {
        "first_done": int(done[0]),
        "nit": [int(n) for n in nit],
        "interval": [int(g) for g in gaps],
    }
    if steady:
        last = sorted({int(n): i for i, n in enumerate(nit)}.values())
        out["steady"] = {str(int(nit[i])): int(gaps[i]) for i in last}
        nit, gaps = nit[last], gaps[last]
    t_iter, t0 = np.polyfit(nit, gaps, 1)
    return out | {
        "t0": float(t0),
        "t_iter": float(t_iter),
        "max_resid": float(np.abs(gaps - (t0 + t_iter * nit)).max()),
    }


# --- cycles: block spans from the trace ------------------------------------------------------

_LOCK = re.compile(
    r"\.(?P<task>[a-z_]+?_task)_[0-9_]+_U0_(?P<port>[a-z]_blk)_(?P<kind>read|write)$"
)
_FULL = re.compile(r"\.(?P<chan>\w+?)_i_full_n$")
_CLK = re.compile(r"\.ap_clk$")


def lock_events(vcd_path: Path) -> dict:
    """The stream-of-blocks lock events of a level-1 trace, in clock cycles.

    Returns ``{"pulses": {(task, port, kind): [cycle, ...]}, "free": {chan: [cycle, ...]}}``:
    the rising edges of every ``<task>_<port>_read`` / ``_write`` net, and the cycles at which each
    channel's ``i_full_n`` rose after the start (the channel became free for its writer again).
    """
    from vcdvcd import VCDVCD

    names = VCDVCD(str(vcd_path), only_sigs=True).signals
    core = {s: s.split("[")[0] for s in names}
    clk = next(s for s in names if _CLK.search(core[s]))
    want = [s for s in names if _LOCK.search(core[s]) or _FULL.search(core[s])]
    vcd = VCDVCD(str(vcd_path), signals=[clk, *want], store_tvs=True)
    edges = [t for t, v in vcd[clk].tv if v == "1"]
    t0, period = edges[0], edges[1] - edges[0]

    def rises(sig: str) -> list[int]:
        """Cycles of the 0 → 1 transitions (the value a net starts with is not a transition)."""
        prev, out = None, []
        for t, v in vcd[sig].tv:
            if v == "1" and prev == "0":
                out.append(int((t - t0) // period))
            prev = v
        return out

    pulses: dict = {}
    free: dict = {}
    for s in want:
        m = _LOCK.search(core[s])
        if m:
            pulses[(m["task"], m["port"], m["kind"])] = rises(s)
        else:
            free[_FULL.search(core[s])["chan"]] = rises(s)
    return {"pulses": pulses, "free": free}


def block_spans(events: dict, channels: dict | None = None) -> dict:
    """Every span of :data:`SPAN_KINDS` found in the trace.

    ``channels`` (:func:`block_channels`) gives the channel of each ``(task, port)``; a port it does
    not name is its own channel.  Returns ``{kind: [{"span", "regime", "stalled", "start", "end"},
    ...]}`` in time order.
    """
    pulses, free = events["pulses"], events["free"]
    channels = channels or {}

    def chan_of(task: str, port: str) -> str:
        return channels.get((task, port), port)

    producer = {chan_of(t, p): v for (t, p, k), v in pulses.items() if k == "write"}
    tasks = {t for t, _, _ in pulses}
    out: dict = {kind: [] for kind, (task, _, _) in SPAN_KINDS.items() if task in tasks}
    for task in tasks:
        own = sorted(
            (cyc, kind, port)
            for (t, port, kind), cycles in pulses.items()
            if t == task
            for cyc in cycles
        )
        seen: dict = {}
        for i, (cyc, kind, port) in enumerate(own):
            if kind != "read":
                continue
            chan = chan_of(task, port)
            j = seen.get(chan, 0)
            seen[chan] = j + 1
            nxt = next(((t, p) for t, k, p in own[i + 1 :] if k == "write"), None)
            if nxt is None:
                continue
            end, out_port = nxt
            out_chan = chan_of(task, out_port)
            span_kind = next(
                (
                    name
                    for name, (tk, pin, pout) in SPAN_KINDS.items()
                    if (tk, pin, pout) == (task, port, out_port)
                ),
                None,
            )
            if span_kind is None or j >= len(producer.get(chan, ())):
                continue
            avail = producer[chan][j]
            ready = own[i - 1][0] if i > 0 else -1
            start = max(avail, ready)
            freed = max((f for f in free.get(out_chan, ()) if f <= end), default=None)
            out[span_kind].append(
                {
                    "span": end - start,
                    "regime": "wait" if avail > ready else "b2b",
                    "stalled": freed is not None and end - freed <= _STALL_WINDOW,
                    "start": start,
                    "end": end,
                }
            )
    return out


def summarize_spans(spans: dict) -> dict:
    """Per span kind: the block's span, and per regime how many clean samples back it.

    ``span`` is the smallest clean sample: the time from "input there and task free" to the
    output's hand-over.  A back-to-back sample is longer by the task's own loop-back states, which
    a waiting task has already spent: 1 cycle in the vector unit, and 1, 3 or 7 in the matmul
    (K = 4, 8, 16).  ``None`` when every sample was stalled.
    """
    out: dict = {}
    for kind, samples in spans.items():
        clean = [s["span"] for s in samples if not s["stalled"]]
        row = {
            "span": min(clean) if clean else None,
            "n": len(samples),
            "stalled": sum(s["stalled"] for s in samples),
        }
        for regime in ("wait", "b2b"):
            vals = [
                s["span"] for s in samples if s["regime"] == regime and not s["stalled"]
            ]
            row[regime] = {
                "n": len(vals),
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
            }
        out[kind] = row
    return out


# --- one build, end to end -------------------------------------------------------------------


def _tool_version() -> str:
    from waveflow.toolchain import toolchain

    exe = toolchain.find_vitis_path()
    return (
        f"vitis_hls {toolchain.tool_version(exe, '--version')}" if exe else "vitis_hls"
    )


def _write_dumper(out_dir: Path, top: str) -> None:
    from waveflow.build.build import BuildConfig
    from waveflow.build.trace_steps import AddVcdTopStep

    step = AddVcdTopStep(
        comp_class=comp_class(top), source_artifact="source", top=TOP_NAME[top]
    )
    step.run(BuildConfig(root_dir=out_dir, params={}))


def prune_build(out_dir: Path, top_name: str) -> None:
    """Delete what the tools left in ``out_dir`` beyond the reports, the logs, the generated
    sources and the test vectors: the HLS database, the generated RTL and the compiled simulation.
    The csynth reports stay, so the build can be re-attributed; anything else needs a rebuild.
    """
    sol = Path(out_dir) / f"{top_name}_proj" / "solution1"
    for sub in (sol / ".autopilot", sol / "impl", Path(out_dir) / "xsi" / "xsim.dir"):
        shutil.rmtree(sub, ignore_errors=True)
    for sub in (sol / "syn").glob("*"):
        if sub.is_dir() and sub.name != "report":
            shutil.rmtree(sub, ignore_errors=True)
    for path in (Path(out_dir) / "xsi").glob("*.wdb"):
        path.unlink()


def measure(
    build: str,
    top: str,
    c: HwConfig,
    out_dir: Path | None = None,
    *,
    role: str = "",
    keep_trace: bool = False,
    steady: bool = False,
    trace: bool = True,
    prune: bool = False,
    points_dir: Path | None = None,
    workload_label: str | None = None,
) -> dict:
    """Take one build through csynth, attribution, the traced RTL run and the extraction.

    Returns the build's record and writes it to ``<points_dir>/<build>.json`` (default
    :data:`POINTS_DIR`).  A step that fails is recorded in ``error`` with whatever was measured
    before it; nothing is raised for a failed build, so a campaign keeps its other points.
    ``workload_label`` seeds the scenario instead of ``build`` (a re-measured build keeps its old
    build's problems).

    ``steady`` runs the steady-state job list (:func:`job_nits`).  ``trace=False`` runs the RTL
    without a waveform, for when only the job intervals are wanted: there are then no block spans.
    ``prune`` deletes the tool's working files afterwards (:func:`prune_build`), which a campaign
    of a thousand builds needs: a build directory is 100–400 MB with them and a few MB without.
    """
    out_dir = Path(out_dir) if out_dir else B.BUILD_ROOT / build
    name = TOP_NAME[top]
    rec: dict = {
        "build": build,
        "top": top,
        "role": role,
        "config": {k: getattr(c, k) for k in HwConfig.__dataclass_fields__},
        "tool": _tool_version(),
    }
    try:
        started = time.perf_counter()
        {
            "vec": B.generate_cg_unit,
            "mm": B.generate_systolic_unit,
            "det": B.generate_detector,
        }[top](out_dir, **gen_kwargs(top, c))
        ok, _report, log = B.csynth(out_dir, name)
        rec["csynth_seconds"] = round(time.perf_counter() - started, 1)
        if not ok:
            raise RuntimeError("csynth failed: " + log[-600:])
        rec["resources"] = attribute(top, c, out_dir)

        problems, jobs = workload(top, c, workload_label or build, steady)
        tight = steady and top == "det"
        n_cycles = cycles_budget(c, jobs) if tight else cycles_bound(top, c, jobs)
        if trace:
            _write_dumper(out_dir, top)
        started = time.perf_counter()
        for _attempt in range(3):
            sim = _sim(top, c, problems, jobs, n_cycles)
            if top == "det":
                scenario = B.generate_tb(out_dir, name, sim.tb, sim)
                want = int(scenario["done_words"])
            else:  # the bench's own testbench: one reply header per request on s_done
                scenario = _bench(top).generate_tb(out_dir, sim)
                want = sum(len(d) for d in scenario["done"])
            proc = B.run_xsi(out_dir, name, trace=trace)
            if proc.returncode != 0:
                raise RuntimeError(
                    "XSI run failed: " + (proc.stdout + proc.stderr)[-600:]
                )
            done = np.fromfile(
                out_dir / "xsi" / "vectors" / "s_done" / "cycles.bin", dtype="<u8"
            )
            if len(done) >= want:
                break
            n_cycles *= 2  # the budget was too small: not every job finished
        # raises unless bit-exact and complete
        if top == "det":
            cycles = B.check_xsi_outputs(out_dir, scenario)
        else:
            cycles = _bench(top).check_xsi(out_dir, scenario, int(c.mem_dw))
        rec["xsi_seconds"] = round(time.perf_counter() - started, 1)
        rec["rtl"] = {"bit_exact": True, "n_cycles": n_cycles, "jobs": jobs}
        if top == "det":
            rec["rtl"] |= {
                "done_words_per_job": CgDesc.nwords_per_inst(c.mem_dw),
                "done_cycles": cycles,
            }
            rec["intervals"] = job_intervals(cycles, jobs, steady)
        else:  # a unit's job time is its wrapper's, and is not compared (gate 9.0)
            rec["rtl"]["reply_cycles"] = cycles
        if trace:
            vcd = out_dir / "xsi" / f"{name}_trace.vcd"
            spans = block_spans(lock_events(vcd), block_channels(top, c))
            rec["spans"] = summarize_spans(spans)
            rec["span_samples"] = {
                k: [[s["span"], s["regime"], int(s["stalled"])] for s in v]
                for k, v in spans.items()
            }
            if not keep_trace:
                vcd.unlink()
    except Exception as exc:  # noqa: BLE001 - a failed build is a datapoint
        rec["error"] = f"{type(exc).__name__}: {exc}"
    if (
        prune and "error" not in rec
    ):  # a failed build keeps everything, for the post-mortem
        prune_build(out_dir, name)
    points = Path(points_dir) if points_dir else POINTS_DIR
    points.mkdir(parents=True, exist_ok=True)
    (points / f"{build}.json").write_text(
        json.dumps(rec, indent=1) + "\n", encoding="utf-8"
    )
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--build", required=True, help="a build label of paper_data/holdout_split.csv"
    )
    ap.add_argument("--keep-trace", action="store_true")
    args = ap.parse_args(argv)
    split = {b: (t, r, c) for b, t, r, c in read_split()}
    top, role, c = split[args.build]
    rec = measure(args.build, top, c, role=role, keep_trace=args.keep_trace)
    shown = {k: v for k, v in rec.items() if k not in ("span_samples", "resources")}
    print(json.dumps(shown, indent=1))
    return 1 if "error" in rec else 0


if __name__ == "__main__":
    raise SystemExit(main())
