"""dispatch_timing.py -- ``StreamDispatch``'s cost per job: the idle cycles between consecutive jobs, and
the credit round trip in a pool.

    python -m waveflow.streams.dispatch_timing [--n 2 4 8 16] [--policy first_free round_robin]
                                               [--round-trip-n 2 4] [--no-rtl]

``plans/stream_dispatch.md`` Stage 4; the model is :mod:`waveflow.streams.arbiter_timing`.  Both measures
run on the default part at 100 MHz under XSI, as the gates do, and on the pysim model on the same
scenario: ``jobs_of(1, "long")``, 40 jobs of 8 words, a backlog on ``s_in``.

* **The gap**: the dispatcher alone (``rtl.make("dispatch", ...)``), credits ample -- :data:`JOBS` a
  worker, so no job waits for one, and nothing on ``s_free`` -- every output read as it comes.  The
  cycles between one job's last word and the next job's first, whichever outputs they leave on, are what
  a firing's decision costs (the two ``empty()`` tests, the search, the count update, entering the copy
  loop).  RTL: each ``m_out_k``'s trace holds the cycle each word left.  pysim: the model alone, ``s_in``
  pre-loaded into a deep link, timed at each burst it delivers.  First-free with ample credits sends every
  job to worker 0; round-robin to each worker in turn.
* **The round trip**: the pool (:class:`~waveflow.streams.rtl.DispatchPool`) with one credit a worker.
  For every job sent on a returned credit, the cycles from the arbiter writing worker ``k``'s index on
  ``m_sel`` (the credit leaves) to the dispatcher putting the next job's first word on worker ``k``'s
  link: the time a one-credit worker that finished as its result was taken would wait for its next job
  (under round-robin that includes waiting its turn behind the other workers).  At RTL it is split into
  the credit's wait on its link (written to read: mostly the dispatcher finishing the job it is copying)
  and the dispatcher's reaction (read to the job's first word), from a traced run whose links are read
  off the waveform by name (:func:`waveflow.utils.trace.load_trace`); in pysim, from the stamps the
  model's links keep.  Also the jobs each worker took and the idle cycles between consecutive jobs on any
  link (a job firing and a credit firing between them).  The arbiter writes ``m_sel`` as it starts
  copying a frame, so inside the pool the round trip overlaps that copy; a worker between the two would
  expose it.

Neither is modelled in pysim (the arbiter's decision cost is not either): the run reports the gap, and
whether to model it is the plan's open question.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from waveflow.streams.calib import WORK

#: The scenario's jobs and words a job (``jobs_of(1, "long")``), and the credits a worker the
#: dispatcher alone is built with: one per job, so no job ever waits for a credit.
JOBS, WORDS = 40, 8
OUT = WORK / "dispatch_timing"


def _tag(policy: str) -> str:
    return "rr" if policy == "round_robin" else "ff"


def _gaps(starts, ends, skip: int = 0) -> list[int]:
    """Idle cycles between consecutive jobs, from job *skip* on: next start - this end - 1."""
    return [int(starts[i + 1] - ends[i] - 1) for i in range(skip, len(starts) - 1)]


def _run_dag(root: Path, kind: str, n: int, policy: str, credits: int, trace: bool = False):
    from waveflow.build.build import BuildConfig
    from waveflow.streams import rtl

    root.mkdir(parents=True, exist_ok=True)
    dag, top = rtl.build_block_dag(root / "xw", kind, n, 32, 1, policy, workload="long", credits=credits,
                                   trace=trace)
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "build"}), through="system_xsi")
    bad = {k: r.message for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"{kind} n={n} {policy} C={credits}: {bad}")
    return root / "xw" / top, top


# --- the gap: the dispatcher alone ----------------------------------------------------------------


def rtl_gaps(n: int, policy: str, root: Path | None = None) -> dict:
    """The dispatcher alone at RTL (default part, XSI): the idle cycles between consecutive jobs across
    every output, the idle cycles inside a job, and the worker each job went to."""
    root = Path(root or OUT / f"gap_{_tag(policy)}{n}").resolve()
    work, _top = _run_dag(root, "dispatch", n, policy, JOBS)
    jobs = []                                            # (first word's cycle, last's, worker, job)
    intra: set[int] = set()
    for k in range(n):
        tr = work / "traces" / f"m_out_{k}"
        if not (tr / "cycles.bin").is_file():
            continue
        cyc = np.fromfile(tr / "cycles.bin", dtype="<u8")
        bounds = np.fromfile(tr / "bounds.bin", dtype="<u8")
        words = np.fromfile(tr / "words.bin", dtype="<u8")
        for s, e in zip([0, *bounds[:-1]], bounds):
            s, e = int(s), int(e)
            if e == s:                                   # an output that took nothing records one
                continue                                 # empty burst
            jobs.append((int(cyc[s]), int(cyc[e - 1]), k, int(words[s]) >> 12))
            intra |= {int(d) - 1 for d in np.diff(cyc[s:e])}
    jobs.sort()
    return {"jobs": len(jobs), "gaps": _gaps([j[0] for j in jobs], [j[1] for j in jobs]),
            "intra_job_idle": sorted(intra), "workers": [j[2] for j in jobs], "order": [j[3] for j in jobs],
            "first_word": jobs[0][0] if jobs else None, "last_word": jobs[-1][1] if jobs else None}


def pysim_gaps(n: int, policy: str) -> dict:
    """The pysim model alone, ``s_in`` pre-loaded, credits ample: the idle cycles between jobs."""
    from waveflow.hw.clock import Clock
    from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
    from waveflow.simulation.simulation import Simulation
    from waveflow.streams.dispatch import StreamDispatch, jobs_of

    sim = Simulation()
    clk = Clock(freq=100e6)
    per = 1 / clk.freq
    d = StreamDispatch(name="dsp", sim=sim, n_out=n, bitwidth=32, frame_bursts=1, credits=JOBS,
                       policy=policy, clk=clk)
    tx = {}
    for port, ep in (("s_in", d.s_in), ("s_free", d.s_free)):
        tx[port] = StreamIFMaster(name=f"t_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"l_{port}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", tx[port])
        link.bind("slave", ep)
    rx = []
    for k in range(n):
        r = StreamIFSlave(name=f"r{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", d.m_out[k])
        link.bind("slave", r)
        rx.append(r)
    got: list[tuple] = []

    def drive():
        for b in jobs_of(1, "long"):
            yield from tx["s_in"].write(np.asarray(b.words, dtype=np.uint32))

    def read(k):
        while True:
            words = yield from rx[k].get()
            got.append((sim.env.now / per, len(words), k, int(words[0]) >> 12))

    sim.env.process(drive())
    for k in range(n):
        sim.env.process(read(k))
    sim.run_sim()
    # A burst is timed when the reader has it: its last word's cycle; its first word's is that less its
    # length less one, at one word a cycle (arbiter_timing's convention).
    got.sort()
    ends = [round(t) for t, _, _, _ in got]
    starts = [round(t) - nw + 1 for t, nw, _, _ in got]
    return {"jobs": len(got), "gaps": _gaps(starts, ends), "workers": [k for _, _, k, _ in got]}


# --- the round trip: the pool at one credit a worker -----------------------------------------------


def _bound_trace(work: Path, mod):
    """The traced run's waveform bound to the pool's manifest, at the scope its links are found under."""
    from vcdvcd import VCDVCD  # type: ignore[import-untyped]

    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.utils.trace import load_trace

    (vcd,) = sorted(work.glob("*_trace.vcd"))
    manifest = composite_top_spec(mod, width=int(mod.bitwidth)).trace_manifest()
    net = manifest["channels"][0]["write"]["full_n"]
    names = [s for s in VCDVCD(str(vcd), only_sigs=True).signals if s.endswith("." + net)]
    if len(names) != 1:
        raise LookupError(f"{vcd}: {len(names)} nets named ...{net}; cannot place the pool's scope")
    return load_trace(manifest, vcd, scope=names[0].rsplit(".", 1)[0])


def _beats_by_burst(bt, channel: str, side: str) -> list[tuple[list[int], list[int]]]:
    """One end of a framed link, a burst at a time: ``(the cycles its words moved, its words)``."""
    cycles = [int(c) for c in bt.fifo_beats(channel, side)]
    bursts, _per = bt.channel_bursts(channel, side)
    out, i = [], 0
    for b in bursts:
        n_w = len(b["data"])
        out.append((cycles[i:i + n_w], [int(x) for x in b["data"]]))
        i += n_w
    if i != len(cycles):
        raise RuntimeError(f"{channel} {side}: {len(cycles)} beats but {i} words in its bursts")
    return out


def _pair(credits: list[tuple[float, int]], jobs: dict[int, list[float]]) -> list[tuple[float, float, int]]:
    """Each credit ``(when it left, worker)`` with the first job to that worker after it: ``(credit,
    job, worker)``.  A credit no job followed (the backlog's end) is left out."""
    out, used = [], {k: 0 for k in jobs}
    for t, k in sorted(credits):
        js = jobs.get(k, [])
        while used[k] < len(js) and js[used[k]] <= t:
            used[k] += 1
        if used[k] < len(js):
            out.append((t, js[used[k]], k))
            used[k] += 1
    return out


def _stats(xs) -> dict:
    xs = [int(x) for x in xs]
    if not xs:
        return {"n": 0, "min": None, "max": None, "mean": None, "seen": []}
    return {"n": len(xs), "min": min(xs), "max": max(xs), "mean": round(float(np.mean(xs)), 3),
            "seen": sorted(set(xs))}


def rtl_round_trip(n: int, policy: str, credits: int = 1, root: Path | None = None) -> dict:
    """The pool at RTL, traced: for each job sent on a returned credit, the cycles from the credit's
    write (the arbiter's ``m_sel``) to the job's first word on its worker's link, split into the
    credit's wait on its link and the dispatcher's reaction."""
    from waveflow.streams import rtl

    root = Path(root or OUT / f"rt_{_tag(policy)}{n}_c{credits}").resolve()
    work, _top = _run_dag(root, "pool", n, policy, credits, trace=True)
    mod, _ = rtl.make("pool", n, 32, 1, policy, credits)
    bt = _bound_trace(work, mod)
    cw = _beats_by_burst(bt, "credit", "write")
    cr = _beats_by_burst(bt, "credit", "read")
    if len(cw) != len(cr):
        raise RuntimeError(f"credit link: {len(cw)} written, {len(cr)} read")
    out_on = {k: _beats_by_burst(bt, f"w{k}", "write") for k in range(n)}
    first_on = {k: [c[0] for c, _w in v] for k, v in out_on.items()}
    spans = sorted((c[0], c[-1]) for v in out_on.values() for c, _w in v)
    read_at = {c[0]: r[0] for (c, _), (r, _) in zip(cw, cr)}        # a FIFO: the i-th in, the i-th out
    pairs = _pair([(c[0], w[0]) for c, w in cw], first_on)
    return {"credits_returned": len(cw), "jobs_on_credit": len(pairs),
            "jobs": sum(len(v) for v in first_on.values()),
            "per_worker": [len(first_on[k]) for k in range(n)],
            "job_gaps": _stats(_gaps([s for s, _ in spans], [e for _, e in spans])),
            "round_trip": _stats(j - t for t, j, _ in pairs),
            "credit_wait": _stats(read_at[t] - t for t, _j, _ in pairs),
            "react": _stats(j - read_at[t] for t, j, _ in pairs)}


def pysim_round_trip(n: int, policy: str, credits: int = 1) -> dict:
    """The pool in pysim, the same backlog: the cycles from a credit's write to the next job's first
    word on its worker's link, from the stamps the links keep (each burst's first word's arrival)."""
    from waveflow.hw.harness import harness
    from waveflow.streams import rtl

    mod, _ = rtl.make("pool", n, 32, 1, policy, credits)
    sysm = harness(mod, workload="long")
    pool = sysm.dut()
    per = pool.clk.period
    log: dict = {"credit": [], **{k: [] for k in range(n)}}

    def stamped(ep, key):
        orig = ep.get

        def get(*a, **kw):
            words = yield from orig(*a, **kw)
            log[key].append((ep._last_stamp[0] / per, [int(x) for x in np.asarray(words).reshape(-1)],
                             ep._last_stamp[1] / per))
            return words
        ep.get = get

    def copied(ep, key):
        """The arbiter reads a burst in two halves (``copy_burst``): its stamp is whole at ``end_read``."""
        orig = ep.end_read

        def end_read(x, *a, **kw):
            yield from orig(x, *a, **kw)
            log[key].append((x.stamp.first / per, [int(v) for v in np.asarray(x.data).reshape(-1)],
                             x.stamp.nominal / per))
        ep.end_read = end_read

    stamped(pool.dsp.s_free, "credit")
    for k in range(n):
        copied(pool.arb.s_in[k], k)
    sysm.run()
    pairs = _pair([(t, w[0]) for t, w, _ in log["credit"]], {k: [t for t, _, _ in log[k]] for k in range(n)})
    spans = sorted((round(t), round(e)) for k in range(n) for t, _, e in log[k])
    return {"credits_returned": len(log["credit"]), "jobs_on_credit": len(pairs),
            "jobs": sum(len(log[k]) for k in range(n)), "per_worker": list(pool.dsp.sent),
            "job_gaps": _stats(_gaps([s for s, _ in spans], [e for _, e in spans])),
            "round_trip": _stats(round(j - t) for t, j, _ in pairs)}


# --- the timed pysim against the RTL: the pool on a calibrated platform -----------------------------


def pool_fidelity(n: int, policy: str, credits: int = 1, platform_dir=None, root: Path | None = None) -> dict:
    """The pool at a calibrated platform's target (xczu48dr at 4 ns by default), at RTL (traced) and in
    pysim timed by the platform's models (``plans/stream_dispatch.md`` Stage 7): the jobs each worker
    took on each side, and whether ``m_out`` carried the jobs in the same order -- which worker takes a
    job, and so the order out, is timing's, so it is the timing model that has to make them agree."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.system_dag import add_system_steps
    from waveflow.calib.platform import packaged_platforms_dir
    from waveflow.hw.harness import harness
    from waveflow.streams import rtl
    from waveflow.streams.timing import PLATFORM

    pdir = Path(platform_dir or packaged_platforms_dir() / PLATFORM)
    pj = json.loads((pdir / "platform.json").read_text(encoding="utf-8"))
    part, hz = str(pj["part"]), float(pj["clk_freq_hz"])
    root = Path(root or OUT / f"fid_{_tag(policy)}{n}_c{credits}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    mod, top = rtl.make("pool", n, 32, 1, policy, credits, clk_hz=hz, platform_dir=pdir)
    sysm = harness(mod, workload="long")
    dag = BuildDag()
    dag.add(rtl.StreamBlockCodegenStep(name="codegen", kind="pool", n_out=n, bitwidth=32, frame_bursts=1,
                                       policy=policy, credits=credits, part=part, period_ns=1e9 / hz))
    add_system_steps(dag, sysm, work_dir=root / "xw", top=top, trace=True)
    cfg = BuildConfig(root_dir=root, params={"synth": "build"})
    res = dag.run(cfg, through="system_xsi")
    res.update(dag.run(cfg, through="pysim"))
    bad = {k: r.message for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"pool n={n} {policy} C={credits}: {bad}")
    work = root / "xw" / top
    bt = _bound_trace(work, mod)
    out_on = {k: _beats_by_burst(bt, f"w{k}", "write") for k in range(n)}
    words = {side: np.fromfile(work / d / "m_out" / "words.bin", dtype="<u8")
             for side, d in (("rtl", "traces"), ("pysim", "pysim_traces"))}
    cyc = np.fromfile(work / "traces" / "m_out" / "cycles.bin", dtype="<u8")
    return {"n": n, "policy": policy, "credits": credits,
            "rtl_per_worker": [len(out_on[k]) for k in range(n)],
            "pysim_per_worker": list(sysm.dut().dsp.sent),
            "same_order": bool(np.array_equal(words["rtl"], words["pysim"])),
            "rtl_first_last_word": [int(cyc[0]), int(cyc[-1])],
            "pysim_cycles": json.loads((work / "pysim.json").read_text(encoding="utf-8"))["cycles"]}


# --- the run ------------------------------------------------------------------------------------------


def summarize(g: dict) -> dict:
    """The gaps' range and mean -- ``None`` each when the run gave too few jobs to measure."""
    gaps = g["gaps"]
    if not gaps:
        return {"jobs": g["jobs"], "gap_min": None, "gap_max": None, "gap_mean": None, "gaps_seen": []}
    return {"jobs": g["jobs"], "gap_min": min(gaps), "gap_max": max(gaps),
            "gap_mean": round(float(np.mean(gaps)), 3), "gaps_seen": sorted(set(gaps))}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, nargs="*", default=[2, 4, 8, 16], help="the gap's worker counts")
    ap.add_argument("--policy", nargs="+", default=["first_free", "round_robin"])
    ap.add_argument("--round-trip-n", type=int, nargs="*", default=[2, 4], help="the round trip's")
    ap.add_argument("--no-rtl", action="store_true", help="pysim only")
    ap.add_argument("--pool-fidelity", action="store_true",
                    help="only the pool on the calibrated platform, RTL against the timed pysim (--round-trip-n)")
    a = ap.parse_args(argv)
    if a.pool_fidelity:
        rows = [pool_fidelity(n, p) for p in a.policy for n in a.round_trip_n]
        for r in rows:
            print(json.dumps(r))
        out = OUT / "pool_fidelity.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
        print(f"-> {out}")
        return 0
    gaps, trips = [], []
    for policy in a.policy:
        for n in a.n:
            row = {"n_out": n, "policy": policy, "pysim": summarize(pysim_gaps(n, policy))}
            if not a.no_rtl:
                r = rtl_gaps(n, policy)
                row["rtl"] = {**summarize(r), "intra_job_idle": r["intra_job_idle"],
                              "workers_head": r["workers"][: 2 * n], "first_word": r["first_word"],
                              "last_word": r["last_word"]}
            gaps.append(row)
            print(json.dumps(row))
        for n in a.round_trip_n:
            row = {"n": n, "policy": policy, "credits": 1, "pysim": pysim_round_trip(n, policy)}
            if not a.no_rtl:
                row["rtl"] = rtl_round_trip(n, policy)
            trips.append(row)
            print(json.dumps(row))
    out = OUT / "dispatch_timing.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"gaps": gaps, "round_trip": trips}, indent=1) + "\n", encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
