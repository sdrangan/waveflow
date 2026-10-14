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
                    "stalled": bool(a[4] or b[4]), "pysim_stalled": bool(r.stalled)})
    return out


def fit_phase(phase, rows: list[dict], holdout=None, save: bool = True) -> dict:
    """Fit each span of *phase* on the clean *rows* (not stalled) whose features are not held out,
    then report every row's residual.  *holdout*: a predicate on a row's features -- those rows test
    the fit and are not used by it.  With *save*, the parameters go to the phase's ``calib_dir``
    (``<span>.json``), where the phase loads them from.  Returns ``{span: {"params", "train",
    "holdout"}}``, each row as ``(features, rtl, predicted)``."""
    import pandas as pd

    held = holdout or (lambda feats: False)
    report = {}
    for span, model in phase.tm.items():
        mine = [r for r in rows if r["span"] == span and not r["stalled"]]
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


__all__ = ["fit_phase", "phase_corpus", "rtl_xfers", "timed_ports"]
