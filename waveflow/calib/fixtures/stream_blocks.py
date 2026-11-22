"""stream_blocks.py — the calibration fixture for the stream blocks' decision cycles: ``StreamArbiter``
and ``StreamDispatch``.

``plans/stream_dispatch.md`` Stage 7; the models are :mod:`waveflow.streams.timing`.  Each block alone,
csynth'd for the platform's part and clock (``xczu48dr`` at 4 ns, Vitis HLS 2025.1) and run under XSI
(Vivado 2025.1) with a backlog; the same scenario in pysim on the platform's current model; the residual
fit per point and the pysim run again, until pysim's spans are the RTL's.  The RTL side is collected into
the platform's ``rtl/`` trees and committed, so a refit -- or a pysim anywhere -- needs no toolchain.

* **arbiter** -- every input pre-loaded with 12 frames of 8 words (``frames_of(k, 1, "long")``).  A span
  is the idle cycles between consecutive frames on ``m_out``, from the ``n``-th frame on.
* **dispatch, a job** -- 40 jobs of 8 words on ``s_in`` (``jobs_of(1, "long")``) and 40 credits a worker,
  so none is ever waited for.  A span is the idle cycles between consecutive jobs, whichever outputs
  they leave on, where no credit firing came between: the run's most common gap.
* **dispatch, a credit** -- the same run with 24 credits on ``s_free``, 8 arriving at cycle
  :data:`CREDITS`'s first and 16 at its second.  A credit is read between two jobs (credits come first),
  so the 24 lengthen a few gaps; what they add, divided by their number, is a credit firing's span --
  measured per block, and the two must agree.  Every credit names worker 0, which does not always hold
  a job by then (round-robin, or a slow decision); a credit naming a worker with none is read and
  ignored by the same firing (the body's update is a select, not a branch), and ``--spot`` measures a
  run whose every credit names no worker against it.

``--spot`` also builds the dispatcher at 128 bits with 2-burst jobs, and the arbiter likewise: the
models are measured at 32 bits and 1-burst frames and keyed by function (``config_specific=False``),
and the spot checks measure how far that holds.  At 4 ports (Vitis / xsim 2025.1): every frame or job
gap one cycle longer at 128 bits with 2-burst frames, the credit firing unchanged, and a credit naming
no worker costing what a valid one does.

    python -m waveflow.calib.fixtures.stream_blocks --work <short dir> [--blocks arbiter dispatch]
                                                    [--n 2 4 8 16] [--remeasure] [--jobs 3]
    python -m waveflow.calib.fixtures.stream_blocks --work <short dir> --spot
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from waveflow.calib.fixture import ComponentFixture, SweepPoint, register
from waveflow.streams import timing as st

#: The worker / input counts the shipped calibration covers.
NS = (2, 4, 8, 16)
POLICIES = {"arbiter": ("priority", "round_robin"), "dispatch": ("first_free", "round_robin")}
#: Points past the default width and frame (32 bits, 1-burst frames) that a design needs, as
#: ``(n, policy, bitwidth, frame_bursts)``: the arbiter's basis carries its width and frame, and a design
#: calibrates at its own (``plans/stream_dispatch.md`` open question 7).  ``examples/os_filterbank``'s
#: arbiter: 4 inputs, 128 bits, 2-burst frames (``plans/osfb_pysim_fidelity.md``, cause E).
EXTRA = {"arbiter": ((4, "priority", 128, 2), (4, "round_robin", 128, 2)), "dispatch": ()}
#: The dispatcher's scenario: jobs of 8 words, and credits a worker so none is waited for.
JOBS, WORDS = 40, 8
#: When the credits arrive (cycle, how many), all naming worker 0.
CREDITS = ((120, 8), (300, 16))
#: Gaps a run's start-up may shape: not measured.
SKIP_JOBS = 2
#: The RTL run's bound: the 16-input arbiter's 192 frames need more than the harness's default.
N_CYCLES = 20000


def _tag(policy: str) -> str:
    return {"round_robin": "rr", "priority": "p", "first_free": "ff"}[policy]


def _label(n: int, policy: str, kind: str | None = None, extra: str = "", *, w: int = 32, fb: int = 1) -> str:
    return (f"n{n}_{_tag(policy)}" + (f"_w{w}f{fb}" if (w, fb) != (32, 1) else "")
            + (f"_{kind}" if kind else "") + extra)


def _target(platform_dir) -> tuple[str, float]:
    """The platform's part and clock (Hz)."""
    pj = json.loads((Path(platform_dir) / "platform.json").read_text(encoding="utf-8"))
    return str(pj["part"]), float(pj["clk_freq_hz"])


def credit_bursts(word: int = 0):
    """``s_free``'s stimulus: :data:`CREDITS`, one-word bursts timed by ``gap_before``."""
    from waveflow.utils.burst_io import StreamBurst

    out, t = [], 0
    for at, m in CREDITS:
        for j in range(m):
            out.append(StreamBurst(np.asarray([word], dtype=np.uint64), True,
                                   gap_before=(at - t) if j == 0 else 0))
            t = at + j + 1 if j == 0 else t + 1
    return out


# --- what a run's spans are ------------------------------------------------------------------------


def frame_gaps(spans: list[tuple[int, int]], skip: int) -> list[int]:
    """Idle cycles between consecutive bursts ``(first word, last word)``, sorted by start, from burst
    *skip* on."""
    spans = sorted(spans)
    return [int(spans[i + 1][0] - spans[i][1] - 1) for i in range(skip, len(spans) - 1)]


def job_and_credit(spans: list[tuple[int, int]]) -> dict:
    """The dispatcher's two spans from one run: the job gap (the most common, no credit between) and a
    credit firing's (what each block of :data:`CREDITS` adds to the gaps after it, divided by its size).
    Raises unless both blocks give the same whole number of cycles."""
    spans = sorted(spans)
    gaps = [(spans[i][1], int(spans[i + 1][0] - spans[i][1] - 1)) for i in range(SKIP_JOBS, len(spans) - 1)]
    g = Counter(x for _, x in gaps).most_common(1)[0][0]
    bounds = [at for at, _ in CREDITS] + [float("inf")]
    per = []
    for (at, m), hi in zip(CREDITS, bounds[1:]):
        extra = sum(x - g for t, x in gaps if at <= t < hi)
        per.append(extra / m)
    if len({round(k, 9) for k in per}) != 1 or abs(per[0] - round(per[0])) > 1e-9:
        raise RuntimeError(f"credit firings do not cost one whole number of cycles: {per} (job gap {g}; "
                           f"gaps {[x for _, x in gaps]})")
    return {"job": g, "credit": round(per[0]), "job_gaps": [x for _, x in gaps if x == g],
            "gaps": [x for _, x in gaps]}


def _burst_spans(trace_dir: Path) -> list[tuple[int, int, int]]:
    """A recording's bursts as ``(first word's cycle, last word's cycle, first word's low 64 bits)``;
    none if empty.  A word wider than 64 bits is recorded as several 64-bit chunks, low first."""
    if not (trace_dir / "cycles.bin").is_file():
        return []
    cyc = np.fromfile(trace_dir / "cycles.bin", dtype="<u8")
    bounds = np.fromfile(trace_dir / "bounds.bin", dtype="<u8")
    words = np.fromfile(trace_dir / "words.bin", dtype="<u8")
    k = len(words) // len(cyc) if len(cyc) else 1
    out = []
    for s, e in zip([0, *bounds[:-1]], bounds):
        s, e = int(s), int(e)
        if e > s:
            out.append((int(cyc[s]), int(cyc[e - 1]), int(words[s * k])))
    return out


def widen(bursts, bitwidth: int):
    """*bursts* for ports wider than 64 bits: each word as its 64-bit chunks, low first, the rest 0."""
    from waveflow.utils.burst_io import StreamBurst

    k = -(-int(bitwidth) // 64)
    if k == 1:
        return list(bursts)
    out = []
    for b in bursts:
        w = np.zeros((len(b.words), k), dtype=np.uint64)
        w[:, 0] = np.asarray(b.words, dtype=np.uint64).reshape(-1)
        out.append(StreamBurst(w, b.tlast, gap_before=b.gap_before))
    return out


# --- RTL ---------------------------------------------------------------------------------------------


def rtl_spans(block: str, n: int, policy: str, root: Path, platform_dir, *, bitwidth: int = 32,
              frame_bursts: int = 1, credit_word: int = 0) -> list[tuple[int, int, int]]:
    """Build *block* alone at the platform's target, run it under XSI, and return its output bursts."""
    from waveflow.build.build import BuildConfig
    from waveflow.streams import rtl
    from waveflow.streams.arbiter import frames_of
    from waveflow.streams.dispatch import jobs_of

    part, hz = _target(platform_dir)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if block == "dispatch":
        workload = {"s_in": widen(jobs_of(frame_bursts, "long"), bitwidth),
                    "s_free": widen(credit_bursts(credit_word), bitwidth)}
        dag, top = rtl.build_block_dag(root / "xw", "dispatch", n, bitwidth, frame_bursts, policy,
                                       workload=workload, credits=JOBS, part=part, period_ns=1e9 / hz,
                                       n_cycles=N_CYCLES)
        outs = [f"m_out_{k}" for k in range(n)]
    else:
        workload = {f"s_in_{k}": widen(frames_of(k, frame_bursts, "long"), bitwidth) for k in range(n)}
        dag, top = rtl.build_block_dag(root / "xw", "arbiter", n, bitwidth, frame_bursts, policy,
                                       workload=workload, part=part, period_ns=1e9 / hz, n_cycles=N_CYCLES)
        outs = ["m_out"]
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "build"}), through="system_xsi")
    bad = {k: r.message for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"{block} n={n} {policy}: {bad}")
    spans = [s for p in outs for s in _burst_spans(root / "xw" / top / "traces" / p)]
    if frame_bursts > 1:                                  # a frame's bursts are one span
        spans = _merge_frames(spans, frame_bursts)
    return spans


def _merge_frames(spans, frame_bursts: int):
    """Bursts on one output to frames: consecutive groups of *frame_bursts*, per output (by first word's
    job / input field, bits 12 and up)."""
    by_owner: dict = {}
    for s in sorted(spans):
        by_owner.setdefault(s[2] >> 12, []).append(s)
    out = []
    for group in by_owner.values():
        for i in range(0, len(group), frame_bursts):
            f = group[i:i + frame_bursts]
            out.append((f[0][0], f[-1][1], f[0][2]))
    return out


# --- pysim ---------------------------------------------------------------------------------------------


def pysim_spans(block: str, n: int, policy: str, tm, clk, *, bitwidth: int = 32,
                frame_bursts: int = 1) -> list[tuple[int, int, int]]:
    """The same scenario in pysim, the block timed by *tm* (the platform's current model: zero before
    the first fit), each output read as it comes; bursts timed at their last word (one word a cycle).
    The arbiter at *bitwidth* and *frame_bursts* (a frame's bursts merged into one span, as at RTL)."""
    from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
    from waveflow.simulation.simulation import Simulation
    from waveflow.streams.arbiter import StreamArbiter, frames_of
    from waveflow.streams.dispatch import StreamDispatch, jobs_of

    sim = Simulation()
    per = clk.period

    bw = int(bitwidth) if block == "arbiter" else 32

    def link(name, master, slave):
        lk = StreamIF(name=name, sim=sim, clk=clk, bitwidth=bw, framed=True, depth=4096)
        lk.bind("master", master)
        lk.bind("slave", slave)

    if block == "dispatch":
        mod = StreamDispatch(name="dsp", sim=sim, n_out=n, bitwidth=32, credits=JOBS, policy=policy, clk=clk)
        feeds = {"s_in": (mod.s_in, [(0, b.words) for b in jobs_of(1, "long")])}
        t, cr = 0, []
        for b in credit_bursts():
            t += int(b.gap_before)
            cr.append((t, b.words))
            t += 1
        feeds["s_free"] = (mod.s_free, cr)
        outs = mod.m_out
    else:
        mod = StreamArbiter(name="arb", sim=sim, n_in=n, bitwidth=bw, frame_bursts=int(frame_bursts),
                            policy=policy, clk=clk)
        feeds = {f"s_in_{k}": (mod.s_in[k], [(0, b.words) for b in widen(frames_of(k, int(frame_bursts), "long"),
                                                                          bw)])
                 for k in range(n)}
        sel = StreamIFSlave(name="r_sel", sim=sim, bitwidth=bw)
        link("o_sel", mod.m_sel, sel)
        outs = [mod.m_out]
    mod.add_timing_model(tm)
    got: list[tuple[int, int, int]] = []

    def drive(tx, items):
        for at, words in items:
            wait = at * per - sim.env.now
            if wait > 0:
                yield sim.env.timeout(wait)
            yield from tx.write(np.asarray(words, dtype=np.uint64 if bw > 32 else np.uint32))

    def read(rx):
        while True:
            words = yield from rx.get()
            end = round(sim.env.now / per)
            first = np.asarray(words, dtype=np.uint64).reshape(len(words), -1)[0, 0]   # a wide word's low chunk
            got.append((end - len(words) + 1, end, int(first)))

    for port, (ep, items) in feeds.items():
        tx = StreamIFMaster(name=f"t_{port}", sim=sim, bitwidth=bw)
        link(f"l_{port}", tx, ep)
        sim.env.process(drive(tx, items))
    for k, ep in enumerate(outs):
        rx = StreamIFSlave(name=f"r{k}", sim=sim, bitwidth=bw)
        link(f"o{k}", ep, rx)
        sim.env.process(read(rx))
    sim.run_sim()
    if block == "arbiter" and int(frame_bursts) > 1:                 # a frame's bursts are one span
        got = _merge_frames(got, int(frame_bursts))
    return got


# --- the fixture ---------------------------------------------------------------------------------------


class _StreamBlockFixture:
    """One block's decision-cost calibration (:func:`calibrate_platform`): the code both blocks share.
    A plain mixin, so the registry's check (a concrete ``ComponentFixture`` names its component) applies
    only to the two fixtures below."""

    block: str = ""

    def __init__(self, ns=NS) -> None:
        self.ns = tuple(ns)

    def points(self) -> list[tuple[int, str, int, int]]:
        """Every point calibrated, ``(n, policy, bitwidth, frame_bursts)``: each count under each policy at
        32 bits and 1-burst frames, and the block's :data:`EXTRA` points whose count is swept."""
        return ([(n, p, 32, 1) for n in self.ns for p in POLICIES[self.block]]
                + [x for x in EXTRA[self.block] if x[0] in self.ns])

    def feat(self, n: int, policy: str, code=None, w: int = 32, fb: int = 1) -> dict[str, float]:
        """A point's features: the arbiter's carry its width and frame (``timing.BASIS``)."""
        wf = {"w": w, "fb": fb} if "w" in st.BASIS[self.block] else {}
        return st.features(n, policy == "round_robin", code, **wf)

    def sweep(self) -> list[SweepPoint]:
        return [SweepPoint(label=_label(n, p, w=w, fb=fb), features=self.feat(n, p, None, w, fb))
                for n, p, w, fb in self.points()]

    # -- one point, both sides --------------------------------------------------------------------
    def rtl_rows(self, n: int, policy: str, work_dir, w: int = 32, fb: int = 1) -> dict[str, list[dict]]:
        """Measure ``(n, policy, w, fb)`` at RTL: ``{run label: firing rows}`` (spans in cycles)."""
        spans = rtl_spans(self.block, n, policy, Path(work_dir) / f"{self.block}_{_label(n, policy, w=w, fb=fb)}",
                          self._platform_dir, bitwidth=w, frame_bursts=fb)
        return self._rows(n, policy, spans, rtl=True, w=w, fb=fb)

    def pysim_rows(self, n: int, policy: str, tm, clk, w: int = 32, fb: int = 1) -> dict[str, list[dict]]:
        spans = pysim_spans(self.block, n, policy, tm, clk, bitwidth=w, frame_bursts=fb)
        return self._rows(n, policy, spans, rtl=False, tm=tm, clk=clk, w=w, fb=fb)

    def _rows(self, n, policy, spans, *, rtl: bool, tm=None, clk=None, w: int = 32,
              fb: int = 1) -> dict[str, list[dict]]:
        cid = st.COMPONENTS[self.block]
        out = {}
        if self.block == "dispatch":
            jc = job_and_credit([(s, e) for s, e, _ in spans])
            parts = {"job": (st.JOB, jc["job_gaps"]), "credit": (st.CREDIT, [jc["credit"]])}
        else:
            parts = {None: (None, frame_gaps([(s, e) for s, e, _ in spans], n))}
        for kind, (code, values) in parts.items():
            feat = self.feat(n, policy, code, w, fb)
            if not values:
                raise RuntimeError(f"{self.block} n={n} {policy} {kind}: no span measured")
            if rtl:
                rows = [{"component": cid, "index": i, **feat, "blocked": 0, "span": int(v)}
                        for i, v in enumerate(values)]
            else:
                cur = tm.predict_feat(feat)[0]
                rows = [{**feat, "span": float(v) * clk.period, "current_dly": cur} for v in values]
            out[_label(n, policy, kind, w=w, fb=fb)] = rows
        return out

    # -- the whole calibration -----------------------------------------------------------------------
    def calibrate_platform(self, platform_dir, *, work_dir=None, remeasure: bool = False, jobs: int = 3,
                           max_passes: int = 5, tol_cycles: float = 0.5) -> dict:
        """Collect RTL (measuring any point not already on the platform, or all with *remeasure*) and
        pysim for every point, then fit; repeat the pysim and the fit until pysim's spans are the RTL's."""
        from waveflow.hw.clock import Clock

        self._platform_dir = Path(platform_dir)
        _part, hz = _target(platform_dir)
        clk = Clock(freq=hz)
        tm = st.model(self.block, platform_dir, clk)
        points = self.points()
        kinds = ("job", "credit") if self.block == "dispatch" else (None,)
        todo = [(n, p, w, fb) for n, p, w, fb in points
                if remeasure or not all((tm.calib_dir / "rtl" / _label(n, p, k, w=w, fb=fb) / "firings.csv").is_file()
                                        for k in kinds)]
        if todo and work_dir is None:
            raise ValueError(f"{todo} not measured on {platform_dir}: pass work_dir (a SHORT path) to measure "
                             f"them with Vitis + Vivado")
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for rows in pool.map(lambda pt: self.rtl_rows(pt[0], pt[1], work_dir, pt[2], pt[3]), todo):
                for label, rs in rows.items():
                    tm.collect_rtl({"firings": rs}, run_id=label)
        report: dict = {"component": tm.component,
                        "measured_now": [_label(n, p, w=w, fb=fb) for n, p, w, fb in todo], "passes": 0}
        for _ in range(max_passes):
            for n, p, w, fb in points:
                for label, rs in self.pysim_rows(n, p, tm, clk, w, fb).items():
                    tm.collect_pysim(rs, run_id=label)
            df = tm.gen_data_frame()
            worst = float((df["span_pysim"] - df["span_rtl"]).abs().max())
            tm.fit(df)
            report["passes"] += 1
            report["max_span_error_cycles"] = round(worst, 3)
            if worst < tol_cycles:
                break
        report["table"] = df.to_dict("records")
        return report

    # -- spot checks ----------------------------------------------------------------------------------
    def spot(self, platform_dir, work_dir, n: int = 4) -> dict:
        """Measure at 128 bits with 2-burst frames (and, for the dispatcher, credits naming no worker)
        against the calibrated 32-bit spans: the evidence for keying the model by function."""
        self._platform_dir = Path(platform_dir)
        out = {}
        for p in POLICIES[self.block]:
            base = Path(work_dir) / f"{self.block}_spot_{_label(n, p)}"
            wide = rtl_spans(self.block, n, p, base.with_name(base.name + "_w128f2"), platform_dir,
                             bitwidth=128, frame_bursts=2)
            row = {"w128_f2": self._summary(n, wide)}
            if self.block == "dispatch":
                stray = rtl_spans(self.block, n, p, base.with_name(base.name + "_stray"), platform_dir,
                                  credit_word=n + 1)
                row["credits_naming_no_worker"] = self._summary(n, stray)
            out[_label(n, p)] = row
        return out

    def _summary(self, n, spans) -> dict:
        if self.block == "dispatch":
            jc = job_and_credit([(s, e) for s, e, _ in spans])
            return {"job": jc["job"], "credit": jc["credit"]}
        gaps = frame_gaps([(s, e) for s, e, _ in spans], n)
        return {"gaps_seen": sorted(set(gaps))}


def _no_run_pysim(self, point, *, comp_dir, platform_dir, clk):     # pragma: no cover
    raise NotImplementedError("the stream blocks are calibrated by calibrate_platform()")


class ArbiterFixture(_StreamBlockFixture, ComponentFixture):
    component = st.COMPONENTS["arbiter"]
    basis = list(st.BASIS["arbiter"])  # noqa: RUF012  (ComponentFixture's contract: a class attribute)
    block = "arbiter"
    run_pysim = _no_run_pysim


class DispatchFixture(_StreamBlockFixture, ComponentFixture):
    component = st.COMPONENTS["dispatch"]
    basis = list(st.BASIS["dispatch"])  # noqa: RUF012  (ComponentFixture's contract: a class attribute)
    block = "dispatch"
    run_pysim = _no_run_pysim


# Registered on package import.  Under `python -m` this module runs a SECOND time as __main__ (the
# package import already registered it), and the registry rightly refuses a duplicate.
FIXTURES = ({"arbiter": register(ArbiterFixture()), "dispatch": register(DispatchFixture())}
            if __name__ != "__main__" else {"arbiter": ArbiterFixture(), "dispatch": DispatchFixture()})


def main(argv=None) -> None:
    from waveflow.calib.platform import packaged_platforms_dir

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform-dir", type=Path, default=(packaged_platforms_dir() or Path(".")) / st.PLATFORM)
    ap.add_argument("--work", type=Path, default=None, help="a SHORT build directory")
    ap.add_argument("--blocks", nargs="+", choices=tuple(POLICIES), default=list(POLICIES))
    ap.add_argument("--n", type=int, nargs="+", default=list(NS))
    ap.add_argument("--remeasure", action="store_true")
    ap.add_argument("--jobs", type=int, default=3, help="RTL runs at once")
    ap.add_argument("--spot", action="store_true", help="the width / burst / stray-credit spot checks")
    a = ap.parse_args(argv)
    for b in a.blocks:
        fx = (ArbiterFixture if b == "arbiter" else DispatchFixture)(ns=a.n)
        if a.spot:
            print(json.dumps({b: fx.spot(a.platform_dir, a.work)}, indent=1))
        else:
            rep = fx.calibrate_platform(a.platform_dir, work_dir=a.work, remeasure=a.remeasure, jobs=a.jobs)
            print(json.dumps(rep, indent=1, default=float))


if __name__ == "__main__":
    main()
