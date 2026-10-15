"""phases.py — calibrating declared timed phases from RTL (``plans/pipelined_ops.md`` Stage 5).

Everything here is **generated from a module's declarations** (its :class:`~waveflow.hw.timed_phase.TimedPhase`
s): which ports to observe, how to read their transfers off a waveform, how to pair them, what to fit.
The one hand-written piece of a calibration is the scenario that produces the features.

* :func:`timed_ports` -- every endpoint a declared phase names, with its RTL port name: the attribute
  the module holds it under, which is the port's name in the generated kernel.
* :func:`rtl_xfers` -- each such port's transfers from a VCD of the kernel's ports, as the rows pysim
  logs: ``(index, first, last, nwords, stalled)`` in cycles.  Framed by TLAST; a transfer's
  ``stalled`` is the other side holding it (an input not valid, an output not ready) between its
  first and last beats.
* :func:`phase_corpus` -- the join: **pysim supplies the pairing and the features** (its phase records
  name each span's start and end as ``(endpoint, transfer index, edge)``), **the RTL supplies the
  times**.  It is legal because a phase's features are known before its first event, and it needs
  both sides to carry the same transfers on every port: functional equivalence is a precondition of
  timing calibration.
* :func:`fit_phase` -- fit each span's model on the clean rows (neither side stalled), hold out the
  rows the caller names, and file the parameters where the phase loads them from.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def timed_ports(module) -> dict[str, tuple]:
    """``{pysim endpoint name: (endpoint, RTL port name)}`` for every endpoint a declared phase of
    *module* names.  The RTL port is the attribute the module holds the endpoint under."""
    by_id = {id(v): k for k, v in vars(module).items() if hasattr(v, "xfer_log")}
    # A composite endpoint's halves are ports of their own: ``m_u`` -> ``m_u_fwd``, ``m_u_crd``.
    for k, v in list(vars(module).items()):
        if hasattr(v, "physical_endpoints") and not hasattr(v, "xfer_log"):
            for p in v.physical_endpoints():
                if p is not v and p.name.startswith(v.name):
                    by_id.setdefault(id(p), k + p.name[len(v.name):])
    out: dict[str, tuple] = {}
    for phase in getattr(module, "timing_phases", {}).values():
        for ev in phase.events():
            attr = by_id.get(id(ev.ep))
            if attr is None:
                raise ValueError(f"'{module.name}': endpoint '{ev.ep.name}' is not an attribute of the "
                                 f"module, so its RTL port name is unknown")
            out[ev.ep.name] = (ev.ep, attr)
    return out


def _is_output(ep) -> bool:
    from waveflow.hw.interface import QueuedTransferIFMaster

    return isinstance(ep, QueuedTransferIFMaster)


def rtl_xfers(vcd_path, module, scope: str) -> dict[str, list[tuple]]:
    """Each timed port's transfers in the VCD at *vcd_path*, the kernel instance at *scope* (a Vitis
    cosim trace: ``AESL_inst_<top>``): ``{pysim endpoint name: [(index, first, last, nwords,
    stalled), ...]}``, times in cycles from the trace's first clock edge."""
    from vcdvcd import VCDVCD

    from waveflow.utils.vcd import VcdParser

    vp = VcdParser(VCDVCD(str(vcd_path), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    out: dict[str, list[tuple]] = {}
    for ep_name, (ep, port) in timed_ports(module).items():
        sigs, _ = vp.add_axiss_signals(name=f"{scope}.{port}_", short_name_prefix=port,
                                       ignore_multiple=True)
        if "tlast" not in sigs or sigs.get("tlast") is None:
            raise KeyError(f"{vcd_path}: port '{port}' has no TLAST in the trace -- its transfers "
                           f"cannot be framed from the wire")
        bursts, _period = vp.extract_axis_bursts(clk, sigs)
        hold = 2 if _is_output(ep) else 1          # stall: not ready (output); idle: not valid (input)
        rows = []
        for k, b in enumerate(bursts):
            if not b.get("complete", True):
                continue
            first = int(b["start_idx"])
            last = first + len(b["beat_type"]) - 1
            rows.append((k, float(first), float(last), int(np.asarray(b["data"]).shape[0]),
                         hold in b["beat_type"]))
        out[ep_name] = rows
    return out


def phase_corpus(phase, rtl: dict[str, list[tuple]]) -> list[dict]:
    """One row per span occurrence of *phase* (its pysim :attr:`~TimedPhase.records`): the features,
    the RTL duration (from *rtl*, :func:`rtl_xfers`), pysim's duration and prediction, and whether
    either side was stalled.  A record whose transfers the RTL does not have is skipped -- reported
    by the caller as missing, never silently fitted around."""
    index = {name: {r[0]: r for r in rows} for name, rows in rtl.items()}
    out = []
    for r in phase.records:
        if r.end is None:
            continue
        (sp, si, se, _st), (ep_, ei, ee, _et) = r.start, r.end
        a, b = index.get(sp, {}).get(si), index.get(ep_, {}).get(ei)
        if a is None or b is None:
            continue
        t0 = a[1] if se == "first" else a[2]
        t1 = b[1] if ee == "first" else b[2]
        # Whether a row is training data is the RTL's question: the target is the RTL duration, and
        # pysim's own environment (its testbench's queues) is not the one measured.  pysim's flag is
        # kept beside it, for the report.
        out.append({**r.features, "span": r.span, "occurrence": r.occurrence,
                    "rtl": t1 - t0, "pysim": r.actual, "predicted": r.predicted,
                    "stalled": bool(a[4] or b[4]), "pysim_stalled": bool(r.stalled),
                    "short": bool(r.short)})
    return out


def fit_phase(phase, rows: list[dict], holdout=None, save: bool = True) -> dict:
    """Fit each span of *phase* on the clean *rows* (not stalled, not short) whose features are not
    held out,
    then report every row's residual.  *holdout*: a predicate on a row's features -- those rows test
    the fit and are not used by it.  With *save*, the parameters go to the phase's ``calib_dir``
    (``<span>.json``), where the phase loads them from.  Returns ``{span: {"params", "train",
    "holdout"}}``, each row as ``(features, rtl, predicted)``."""
    import pandas as pd

    held = holdout or (lambda feats: False)
    report = {}
    for span, model in phase.tm.items():
        # Training rows: the RTL did not stall them, and their load delivered what the features say.
        mine = [r for r in rows if r["span"] == span and not r["stalled"] and not r.get("short")]
        train = [r for r in mine if not held({f: r[f] for f in phase.features})]
        test = [r for r in mine if held({f: r[f] for f in phase.features})]
        if not train:
            raise RuntimeError(f"phase {phase.name!r}, span {span!r}: no clean training row")
        if phase.features:
            df = pd.DataFrame([{**{f: r[f] for f in phase.features}, span: r["rtl"]} for r in train])
            model.fit(df)
        else:
            # No feature: the duration is a constant, and its least-squares fit is the mean.
            model.load_params({"intercept": float(np.mean([r["rtl"] for r in train]))})
        if save and phase.calib_dir is not None:
            model.save_model(Path(phase.calib_dir) / f"{span}.json")

        def rows_of(rs):
            return [({f: r[f] for f in phase.features}, r["rtl"],
                     float(model.predict_feat({f: r[f] for f in phase.features}))) for r in rs]

        report[span] = {"params": model.to_params(), "train": rows_of(train), "holdout": rows_of(test)}
    return report


# ---------------------------------------------------------------------------
# Records under identity (plans/unit_harness.md D7, Stage 3)
# ---------------------------------------------------------------------------

#: The payload kind of a timed-phase calibration record in a :class:`~waveflow.calib.record_store.
#: ModuleStore` (``target="timing"``).
PHASE_RECORD_KIND = "timed_phases"


def module_identity(module):
    """*module*'s content-addressed identity **free of its context**: the key its phases are filed under
    -- the same wherever the module sits (its harness, behind AXI-MM), since what its ports are bound
    to is context, not structure.

    ``identify_instance`` alone is not that: it exempts the boundary from the bound check but still
    hashes each boundary port's ``queue_size`` -- the depth of whatever channel it is bound to, the
    harness's 2 or a queue view's 256 -- so one kernel got two keys (measured on stream_kernel,
    2026-10-15).  Here the module's own ports' depths -- and their halves', for a composite port such
    as a credit stream -- are blanked for the hash: a module's own ports are its boundary wherever it
    sits (markov's generator: its harness's 2 against the system's 16); likewise a slave port's
    place in the address map.  Fixing that in the identity
    itself would move every existing key with a boundary (``plans/unit_harness.md``, Stage 3 record);
    timed-phase records are new, so they start context-free.

    A class may name further attributes that are run-time state, not structure
    (``__wf_identity_exclude__``): a memory's backing store holds its contents and allocations (the
    harness allocates one region, the system several), so it is blanked too (Stage 4c)."""
    from waveflow.calib.module_key import boundary_endpoint_ids, identify_instance

    exempt = boundary_endpoint_ids(module)
    own = list((getattr(module, "endpoints", {}) or {}).values())
    own += [p for e in list(own) for p in e.physical_endpoints() if p is not e]
    eps = [ep for ep in own if hasattr(ep, "queue_size")]
    saved = [ep.queue_size for ep in eps]
    # Where a slave port sits in the address map is the system's choice too.
    placed = [ep for ep in own if getattr(ep, "addr_range", None) is not None]
    ranges = [ep.addr_range for ep in placed]
    state = [a for a in getattr(type(module), "__wf_identity_exclude__", ()) if hasattr(module, a)]
    held = {a: getattr(module, a) for a in state}
    try:
        for ep in eps:
            ep.queue_size = None
        for a in state:
            setattr(module, a, None)
        for ep in placed:
            ep.addr_range = None
        return identify_instance(module, exempt=exempt)
    finally:
        for ep, q in zip(eps, saved):
            ep.queue_size = q
        for ep, r in zip(placed, ranges):
            ep.addr_range = r
        for a, v in held.items():
            setattr(module, a, v)


def phase_record(module, fits: dict, *, workload: str, part: str = "", period_ns: float = 0.0,
                 tool: str = "", cost_seconds: float = 0.0, source: str = "xsi"):
    """One timing :class:`~waveflow.calib.record_store.Record` for *module*'s fitted phases: *fits* is
    ``{phase: fit_phase report}``; the payload keeps each span's parameters, its residuals and the
    workload's digest (what a repeat compares against)."""
    from waveflow.calib.record_store import Provenance, Record

    ident = module_identity(module)
    phases, resid = {}, {}
    for pname, rep in fits.items():
        phases[pname] = {span: r["params"] for span, r in rep.items()}
        resid[pname] = {span: max([abs(rtl - pred) for _f, rtl, pred in r["train"] + r["holdout"]]
                                  or [0.0]) for span, r in rep.items()}
    rec = Record(key=ident.key, target="timing", source=source, cost_seconds=float(cost_seconds),
                 payload={"kind": PHASE_RECORD_KIND, "workload": workload, "phases": phases,
                          "max_abs_residual": resid},
                 provenance=Provenance(signature=ident.signature, part=part,
                                       period_ns=float(period_ns), tool=tool))
    return rec, ident


def stored_phases(store, module, *, workload: str | None = None):
    """The newest timed-phase record for *module* in *store* -- verified against its identity, so a
    record of different hardware raises rather than loads -- or ``None``.  With *workload*, only a
    record measured on that workload counts."""
    ident = module_identity(module)
    recs = [r for r in store.read(ident.key, "timing", identity=ident)
            if r.payload.get("kind") == PHASE_RECORD_KIND
            and (workload is None or r.payload.get("workload") == workload)]
    return recs[-1] if recs else None


def scenario_digest(sysm) -> str:
    """A digest of the scenario *sysm* writes -- every byte its testbench plays -- written to a
    scratch directory and hashed, so it is known without simulating.  Written by a fresh copy: writing
    a scenario anchors the participants to where it was written, and the scratch is deleted."""
    import hashlib
    import tempfile

    sysm = sysm.fresh()
    h = hashlib.sha256()
    with tempfile.TemporaryDirectory() as tmp:
        sysm.write_scenario(tmp)
        for f in sorted(Path(tmp).rglob("*")):
            if f.is_file():
                h.update(f.relative_to(tmp).as_posix().encode())
                h.update(f.read_bytes())
    return h.hexdigest()[:16]


__all__ = ["PHASE_RECORD_KIND", "fit_phase", "module_identity", "phase_corpus", "phase_record",
           "rtl_xfers", "scenario_digest", "stored_phases", "timed_ports"]
