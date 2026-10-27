"""workload.py — scenarios for the vector unit's harness: jobs as stream bursts, and their golden.

A **job** is ``(tx_id, Programs, inputs)``: its three programs and, per input port, the ``(re, im)``
stored-integer matrices that arrive there, in the order the load program takes them.
:func:`job_bursts` turns jobs into the harness's stimulus (a burst per program and per matrix),
:func:`golden_bursts` into what the outputs must carry -- the interpreter's results.
:func:`random_jobs` makes jobs for any configuration that use every instruction form; an application
supplies its own (``examples/vecunit_cg``, ``examples/vecunit_mf``).
"""

from __future__ import annotations

import numpy as np

from waveflow.linalg.lanes import to_words
from waveflow.linalg.message import Status
from waveflow.utils.burst_io import StreamBurst
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs, assemble, check
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import pass_status, run_job

CTRL_BITS = 64


def _matrix(rng, fmt, k: int, n: int):
    lo, hi = -(1 << (fmt.W - 1)), 1 << (fmt.W - 1)
    return rng.integers(lo, hi, (k, n)), rng.integers(lo, hi, (k, n))


def _random_rs(rng, cfg: VecUnitConfig) -> int | None:
    """A scaled instruction's own result shift, half the time (else ``wf.setrs``'s)."""
    return int(rng.integers(cfg.rs_max + 1)) if rng.integers(2) else None


def _random_vector(rng, cfg: VecUnitConfig, G: int, extended: bool = False) -> I.Instr:
    g = lambda: int(rng.integers(cfg.NG))
    s = lambda: int(rng.integers(cfg.NS))
    if extended and rng.integers(3) == 0:  # the scalar extensions and the result shift (F2, F4)
        kind = int(rng.integers(6))
        if kind == 0:
            return I.vmul_vs(g(), s(), g(), rs=_random_rs(rng, cfg))
        if kind in (1, 2):
            return (I.vadd_vs if kind == 1 else I.vsub_vs)(g(), s(), g())
        if kind == 3:
            return I.vmv_s_v(s(), g())
        ctor = (I.vmul_vv, I.vmacc_vs)[kind - 4]
        if kind == 4:
            return ctor(g(), g(), g(), conj=bool(rng.integers(2)), rs=_random_rs(rng, cfg))
        return ctor(g(), s(), g(), rs=_random_rs(rng, cfg))
    kind = int(rng.integers(16))
    conj, bcast = bool(rng.integers(2)), bool(rng.integers(2))
    if kind == 0:
        return I.vmv_v_i(g(), int(rng.integers(-2048, 2048)))
    if kind == 1:
        return I.vmv_v_v(g(), g())
    if kind in (2, 3):
        return (I.vadd_vv if kind == 2 else I.vsub_vv)(g(), g(), g())
    if kind <= 7:
        ctor = (I.vmul_vv, I.vmacc_vv, I.vnmsac_vv, I.vmadd_vv)[kind - 4]
        return ctor(g(), g(), g(), conj=conj, bcast=bcast)
    if kind <= 10:
        return (I.vmacc_vs, I.vnmsac_vs, I.vmadd_vs)[kind - 8](g(), s(), g())
    if kind == 11:
        return I.vdotr(s(), g(), g())
    if kind in (12, 13) and cfg.NQI:
        return I.vlse(g(), int(rng.integers(cfg.NQI)), int(rng.integers(G)), G)
    return I.vsse(int(rng.integers(cfg.NQO)), g(), int(rng.integers(G)), G)


def random_job(cfg: VecUnitConfig, rng, tx_id: int, npass: int = 12, n_max: int | None = None,
               k_max: int | None = None, extended: bool = False):
    """A job using every instruction form: a block per input queue, every group and scalar written,
    ``npass`` random passes (and divides, moves and ``VL`` changes between them), every output
    queue's block written in full, the queues balanced.  ``n_max`` and ``k_max`` bound the blocks'
    columns and rows (small blocks keep a job execute-bound: a calibration measures the execute
    task).  ``extended`` also uses the scalar extensions, the result shift and ``wf.setrs``
    (``plans/vecunit_future.md`` F2, F4); off, the jobs are the ones the calibration registered."""
    k = int(rng.integers(1, min(cfg.Kmax, cfg.VLMAX, k_max or cfg.Kmax) + 1))
    n = int(rng.integers(1, min(cfg.Nmax, n_max or cfg.Nmax) + 1))
    G = cfg.groups_per_row(n)
    p = Programs()
    inputs: dict[int, list] = {}
    for q in range(cfg.NQI):
        port = int(rng.integers(cfg.n_in_ports))
        p.load.append(I.ld(q, port, k, n))
        inputs.setdefault(port, []).append(_matrix(rng, cfg.in_queue_formats[q], k, n))
    ex = [I.setvl(k)]
    for g in range(cfg.NG):
        if cfg.NQI and rng.integers(2):
            ex.append(I.vlse(g, int(rng.integers(cfg.NQI)), int(rng.integers(G)), G))
        else:
            ex.append(I.vmv_v_i(g, int(rng.integers(-2048, 2048))))
    for j in range(cfg.NS):  # every scalar: a squared norm (one group, so no bank conflict)
        g = int(rng.integers(cfg.NG))
        ex.append(I.vdotr(j, g, g))
    while npass:
        r = float(rng.random())
        if r < 0.12:
            s = lambda: int(rng.integers(cfg.NS))
            if extended and rng.integers(3) == 0:
                j = s()
                W = cfg.scalar_formats[j].W
                ex.append(I.sli(j, int(rng.integers(-(1 << (W - 1)), 1 << (W - 1)))))
                continue
            ex.append(I.sdivz(s(), s(), s()) if rng.integers(2) else I.smv(s(), s()))
            continue
        if r < 0.2:
            if extended and rng.integers(2):
                ex.append(I.setrs(int(rng.integers(cfg.rs_max + 1))))
                continue
            ex.append(I.setvl(int(rng.integers(1, k + 1))))
            continue
        chain = [_random_vector(rng, cfg, G, extended)
                 for _ in range(int(rng.integers(1, cfg.S + 1)))]
        if pass_status(chain, cfg) != Status.OK:
            continue
        ex += [x.c for x in chain[:-1]] + [chain[-1]]
        npass -= 1
    ex.append(I.setvl(k))
    for q in range(cfg.NQO):
        ex += [I.vsse(q, int(rng.integers(cfg.NG)), g, G) for g in range(G)]
    ex += [I.qpop(q) for q in range(cfg.NQI)] + [I.qpush(q) for q in range(cfg.NQO)]
    p.execute = ex
    p.store = [I.st(int(rng.integers(cfg.n_out_ports)), q, q, k, n) for q in range(cfg.NQO)]
    problems = check(cfg, p)
    if problems:
        raise AssertionError(f"random_job made a job the assembler refuses: {problems}")
    return tx_id, p, inputs


def random_jobs(cfg: VecUnitConfig, count: int, *, seed: int = 0, npass=12,
                n_max: int | None = None, k_max: int | None = None,
                extended: bool = False) -> list:
    """``count`` random jobs; ``npass`` an int, or a sequence cycled over the jobs."""
    rng = np.random.default_rng(seed)
    seq = [npass] if isinstance(npass, int) else list(npass)
    return [random_job(cfg, rng, 100 + j, seq[j % len(seq)], n_max, k_max, extended)
            for j in range(count)]


def job_bursts(cfg: VecUnitConfig, jobs: list) -> dict[str, list[StreamBurst]]:
    """The harness's stimulus for ``jobs``: a burst per program on ``s_lprog``, ``s_xprog``,
    ``s_sprog``, and per input matrix on its port, as memory elements of its queue's format."""
    out: dict[str, list] = {"s_lprog": [], "s_xprog": [], "s_sprog": []}
    for p in range(cfg.n_in_ports):
        out[f"s_in{p}"] = []
    programs: dict = {}  # each program the jobs share, checked and encoded once
    for tx_id, progs, inputs in jobs:
        words = assemble(cfg, progs, tx_id, programs)
        for port, name in (("s_lprog", "load"), ("s_xprog", "execute"), ("s_sprog", "store")):
            out[port].append(StreamBurst(np.asarray(words[name], np.uint64), True))
        nxt = {port: iter(mats) for port, mats in inputs.items()}
        for x in progs.load:
            re, im = next(nxt[x.rs1])
            w = to_words(re, im, cfg.in_queue_formats[x.rd], cfg.lane_bits, cfg.word_bits)
            out[f"s_in{x.rs1}"].append(StreamBurst(np.asarray(w, np.uint64), True))
    return out


def golden_bursts(cfg: VecUnitConfig, jobs: list, results: list | None = None) -> dict:
    """What every output port and ``m_status`` carries for ``jobs``, from the interpreter (or from
    ``results``, the jobs' :class:`~waveflow.vecunit.model.JobResult` when the caller ran them, a
    ``None`` for one it did not): per ``wf.st`` a ``VecResp`` burst and a payload burst, per job a
    ``VecStatus`` burst; nothing after a job that failed (an error is fatal).  The interpreter runs
    its passes instruction-major, the same bits as element-major, faster (Stage 10)."""
    out: dict[str, list] = {f"s_out{p}": [] for p in range(cfg.n_out_ports)}
    out["m_status"] = []
    checks: dict = {}
    for j, (tx_id, progs, inputs) in enumerate(jobs):
        r = results[j] if results is not None else None
        if r is None:
            r = run_job(cfg, progs.load, progs.execute, progs.store, inputs, tx_id=tx_id,
                        mode="serial", checks=checks)
        fmts: dict[int, list] = {}
        for x in progs.store:
            fmts.setdefault(int(x.rd), []).append(cfg.out_queue_formats[x.rs1])
        for port, items in r.outputs.items():
            for (resp, re, im), fmt in zip(items, fmts.get(port, [])):
                h = I.VecResp()
                h.tx_id, h.tag, h.k, h.n = resp["tx_id"], resp["tag"], resp["k"], resp["n"]
                out[f"s_out{port}"].append(np.asarray(h.serialize(word_bw=cfg.word_bits), np.uint64))
                out[f"s_out{port}"].append(np.asarray(
                    to_words(re, im, fmt, cfg.lane_bits, cfg.word_bits), np.uint64))
        s = I.VecStatus()
        s.tx_id, s.status = int(tx_id), int(r.status)
        out["m_status"].append(np.asarray(s.serialize(word_bw=CTRL_BITS), np.uint64))
        if r.status != Status.OK:
            break
    return out
