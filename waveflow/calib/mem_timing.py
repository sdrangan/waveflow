"""mem_timing.py — an on-chip memory's timing, calibrated in its own harness (``plans/unit_harness.md``
Stage 4c).

A memory's slave port costs what its RTL makes it cost: behind ``axi_slave_front.v`` and
``mm_bram_port.v``, a read word every ``LAT + 1`` cycles, one transaction at a time.  Measured, never
assumed: :func:`~waveflow.hw.harness.memory_harness` plays a script of reads and writes in pysim and at
RTL, each op's start and end recorded by both (:class:`~waveflow.simulation.mm_tb.MmScript`), and
:func:`fit_mem_timing` fits what the memory adds over the rest of the harness:

* per direction, ``init + per_word * n`` cycles -- the RTL duration less pysim's for the same op with
  no stored timing, fitted on the ops that met no other (each alone at the memory);
* ``serialize`` -- whether two ops that met were served one after the other: true when the later one
  ended past where it would have alone.

:func:`mem_timing_record` files the fit under the memory's identity (D7), where
:class:`~waveflow.hw.memory.MemoryMod` loads it (``calib_dir``).
"""
from __future__ import annotations

import numpy as np

#: The payload kind of a memory-timing record in a ``ModuleStore`` (``target="timing"``).
MEM_TIMING_KIND = "mem_timing"


def _alone(times) -> list[bool]:
    """Per op, whether its ``[t_start, t_end]`` overlaps no other op's."""
    out = []
    for i, (a0, a1) in enumerate(times):
        out.append(all(b1 < a0 or b0 > a1 for j, (b0, b1) in enumerate(times) if j != i))
    return out


def fit_mem_timing(ops, base, rtl) -> dict:
    """The memory's timing from one script: *ops* (``[kind, addr, n, not_before, ...]`` each), *base*
    and *rtl* the ``[t_start, t_end]`` of each op in pysim (no stored timing) and at RTL.  Returns the
    record payload: ``{"read": {"init", "per_word"}, "write": {...}, "serialize": bool,
    "max_abs_residual": {kind: cycles}, "n_fit": {kind: ops}}``."""
    from waveflow.simulation.mm_tb import READ, WRITE

    ops = [np.asarray(o) for o in ops]
    rtl_t = [(int(t[0]), int(t[1])) for t in rtl]
    base_t = [(int(t[0]), int(t[1])) for t in base]
    alone = _alone(rtl_t)
    out: dict = {"max_abs_residual": {}, "n_fit": {}}
    for kind, name in ((READ, "read"), (WRITE, "write")):
        rows = [(int(o[2]), (r[1] - r[0]) - (b[1] - b[0]))
                for o, r, b, ok in zip(ops, rtl_t, base_t, alone) if ok and int(o[0]) == kind]
        if len(rows) < 2:
            raise RuntimeError(f"mem timing: {len(rows)} lone {name}s -- the script needs at least two "
                               f"lengths of each, apart from every other op")
        n, extra = np.array([r[0] for r in rows], float), np.array([r[1] for r in rows], float)
        per_word, init = np.polyfit(n, extra, 1)
        resid = extra - (init + per_word * n)
        out[name] = {"init": float(init), "per_word": float(per_word)}
        out["max_abs_residual"][name] = float(np.max(np.abs(resid)))
        out["n_fit"][name] = len(rows)

    def alone_dur(o) -> float:
        k = out["read" if int(o[0]) == READ else "write"]
        return int(o[2]) + k["init"] + k["per_word"] * int(o[2])

    # Serialized: an op that met another ended later than it would have alone, by more than a cycle.
    late = [r[1] - r[0] - alone_dur(o) for o, r, ok in zip(ops, rtl_t, alone) if not ok]
    out["serialize"] = bool(late) and max(late) > 1.0
    return out


def mem_timing_record(mem, payload: dict, *, workload: str, part: str = "", period_ns: float = 0.0,
                      tool: str = "", source: str = "xsi"):
    """The timing :class:`~waveflow.calib.record_store.Record` of *mem* (``kind: mem_timing``), and the
    identity it is filed under."""
    from waveflow.calib.phases import module_identity
    from waveflow.calib.record_store import Provenance, Record

    ident = module_identity(mem)
    rec = Record(key=ident.key, target="timing", source=source,
                 payload={"kind": MEM_TIMING_KIND, "workload": workload, **payload},
                 provenance=Provenance(signature=ident.signature, part=part,
                                       period_ns=float(period_ns), tool=tool))
    return rec, ident


def stored_mem_timing(store, mem, *, workload: str | None = None):
    """The newest memory-timing record for *mem* in *store*, verified against its identity, or
    ``None``."""
    from waveflow.calib.phases import module_identity

    ident = module_identity(mem)
    recs = [r for r in store.read(ident.key, "timing", identity=ident)
            if r.payload.get("kind") == MEM_TIMING_KIND
            and (workload is None or r.payload.get("workload") == workload)]
    return recs[-1] if recs else None


__all__ = ["MEM_TIMING_KIND", "fit_mem_timing", "mem_timing_record", "stored_mem_timing"]
