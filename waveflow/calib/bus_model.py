"""bus_model.py — platform-scoped m_axi bus-transfer calibration.

The two-level calibration split (``project-two-level-calibration``) as a *storage* decision: the bus
transfer cost is a property of the **platform** (the ``m_axi`` adapter + memory system), not of any
one accelerator, so it is calibrated **once per platform** and every accelerator on that platform
reuses it *without recalibrating*.

``BusCalib`` fits a per-direction ``span(num_trans, nwords)`` model from the memory ports (component
-independent — the datapoints come from AR/AW/R/W activity, not from a kernel's firing) and stores it
in a platform directory.  ``BusTiming`` (``waveflow/hw/memif.py``) already owns the *runtime* side —
a :class:`~waveflow.calib.calib.CalibModel`-like ``read``/``write`` predictor a
:class:`~waveflow.hw.memif.Region` slice consults through the interconnect.  This module is the
*calibration* side: fit those predictors, persist them, and hand back a configured ``BusTiming``.

Contrast with :mod:`waveflow.calib.timing_model`: that fits a *component's* control residual into the
component's own ``calib_dir``; this fits the *platform's* bus law into a shared platform dir.  A new
accelerator loads the platform bus model (no recalibration) and then fits only its own residual —
which is now small, because pysim's bus model already charges the transfer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from waveflow.calib.calib import LinCalibModel

#: Where a fit lands / loads within a platform directory.
BUS_MODEL_FILE = "mm_bus.json"


@dataclass
class BusCalib:
    """Fit + persist a platform's per-direction bus-transfer span model.

    Parameters
    ----------
    platform_dir : str | Path
        The platform's calibration directory — shared across accelerators on this board/BFM.  The
        fitted model lives at ``<platform_dir>/mm_bus.json``.
    clk_freq : float
        Clock the spans are expressed against (the model is fit in **cycles**; the produced
        :class:`~waveflow.hw.memif.BusTiming` converts to seconds with this freq).
    basis : tuple[str, ...]
        Regression features — the structural facts of a transfer.  ``(num_trans, nwords)`` = burst
        count and words moved, exactly the row a ``Region`` slice supplies.
    """

    platform_dir: str | Path
    clk_freq: float = 100e6
    basis: tuple = ("num_trans", "nwords")

    @property
    def path(self) -> Path:
        return Path(self.platform_dir) / BUS_MODEL_FILE

    def _model(self) -> LinCalibModel:
        # span = a*num_trans + b*nwords + c: through-a-fit intercept captures fixed per-burst setup,
        # the coeffs the per-burst and per-word rates.  cycles in, cycles out.
        return LinCalibModel(basis=list(self.basis), target="span", fit_intercept=True,
                             coeff_names=list(self.basis))

    # -- corpus (accumulate one measured transfer per sweep run) -----------
    @property
    def points_dir(self) -> Path:
        return Path(self.platform_dir) / "points"

    def add_run(self, run_id: str, *, read: dict | None = None,
                write: dict | None = None) -> Path:
        """Persist one run's measured ``{num_trans, nwords, span}`` datapoints (read and/or write)
        to ``points/<run_id>.json`` — the platform corpus a sweep accumulates.

        Per-run files (not a shared csv) so a re-run overwrites its point and the corpus is
        concurrency-safe, mirroring :class:`~waveflow.calib.timing_model.TimingModel`.  A bus law
        needs ≥2 *distinct* sizes to fit (the slope), so a sweep calls this per point."""
        self.points_dir.mkdir(parents=True, exist_ok=True)
        out = self.points_dir / f"{run_id}.json"
        out.write_text(json.dumps({"read": read, "write": write}), encoding="utf-8")
        return out

    def _corpus(self) -> tuple[list[dict], list[dict]]:
        """Collect the accumulated per-run points into ``(read_points, write_points)``."""
        read, write = [], []
        for p in sorted(self.points_dir.glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec.get("read"):
                read.append(rec["read"])
            if rec.get("write"):
                write.append(rec["write"])
        return read, write

    # -- fit / persist -----------------------------------------------------
    def fit(self, read_points: list[dict] | None = None,
            write_points: list[dict] | None = None) -> dict:
        """Fit read and/or write from ``{num_trans, nwords, span}`` datapoints (span in cycles),
        write ``mm_bus.json``, and return the stored ``{direction: state_dict}``.

        Points may be passed directly (the one-shot case) or, when both are ``None``, collected from
        the accumulated per-run corpus (:meth:`add_run`) — so a sweep is ``add_run`` per point then a
        single ``fit()``.  A direction with no points is simply absent (a write-only accelerator
        calibrates only the write channel)."""
        if read_points is None and write_points is None:
            read_points, write_points = self._corpus()
        stored: dict[str, dict] = {}
        for direction, pts in (("read", read_points), ("write", write_points)):
            if pts:
                m = self._model().fit(pd.DataFrame(pts))
                stored[direction] = m.to_params()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"clk_freq": self.clk_freq, "basis": list(self.basis),
                                         "models": stored}, indent=2) + "\n", encoding="utf-8")
        return stored

    # -- load / deploy -----------------------------------------------------
    def _load_direction(self, models: dict, direction: str):
        if direction not in models:
            return None
        return self._model().load_params(models[direction])

    def bus_timing(self):
        """A :class:`~waveflow.hw.memif.BusTiming` configured from the stored model, or an
        *unconfigured* one (both directions ``None`` → the word_bw fallback) when no fit exists.

        This is the ``load_or_default`` a fresh design calls: on a calibrated platform it inherits
        the bus law; on an uncalibrated one it degrades to the plain per-word span, never crashes."""
        from waveflow.hw.memif import BusTiming

        if not self.path.exists():
            return BusTiming(clk_freq=self.clk_freq)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        models = data.get("models", {})
        return BusTiming(read=self._load_direction(models, "read"),
                         write=self._load_direction(models, "write"),
                         clk_freq=data.get("clk_freq", self.clk_freq))


def measure_bus_span(bt, bundle: str, direction: str, idle_gap: int = 8) -> dict | None:
    """Measure a ``m_axi`` bundle's **per-transfer** bus span as a ``{num_trans, nwords, span}``
    datapoint — **component-independently**, from the bundle's own activity in *bt* (a
    :class:`~waveflow.utils.trace.BoundTrace`).

    The bus law is read off the memory port, not any kernel's firing.  Beats are grouped into
    transfers by idle gaps (a gap ``> idle_gap`` cycles ends a transfer — bigger than the ~2-cycle
    inter-burst gaps within one transfer, smaller than the idle between firings), and the **median**
    transfer is returned: ``num_trans`` = address handshakes in its window, ``nwords`` = its data
    beats, ``span`` = its cycle extent.  Measuring one transfer (not the whole run) keeps inter-firing
    idle out of the bus span.  The idealised BFM memory yields ``~nwords + gap·(num_trans-1)``; a real
    board's DRAM latency would widen it — the point of measuring rather than assuming.
    """
    import numpy as np

    from waveflow.utils.vcd import clock_sample_times, extract_clock_times, resample_signal

    p = bt.port(bundle)
    if p["kind"] != "maxi":
        return None
    sig_names = {k: bt.resolved[v] for k, v in p["signals"].items() if v in bt.resolved}
    grid = clock_sample_times(extract_clock_times(bt.parser.sig_info[bt.clock]))

    def hs(v_key, r_key):
        if v_key not in sig_names or r_key not in sig_names:
            return np.array([], dtype=int)
        for n in (sig_names[v_key], sig_names[r_key]):
            bt.parser.sig_info[n].get_values()
        v = np.asarray(resample_signal(bt.parser.sig_info[sig_names[v_key]], grid))
        r = np.asarray(resample_signal(bt.parser.sig_info[sig_names[r_key]], grid))
        return np.nonzero((v != 0) & (r != 0))[0]

    addr = hs("AWVALID", "AWREADY") if direction == "write" else hs("ARVALID", "ARREADY")
    beat = hs("WVALID", "WREADY") if direction == "write" else hs("RVALID", "RREADY")
    if not len(beat) or not len(addr):
        return None

    # Split the beats into per-transfer groups on idle gaps.
    groups = np.split(beat, np.nonzero(np.diff(beat) > idle_gap)[0] + 1)
    rows = []
    for g in groups:
        lo, hi = int(g[0]), int(g[-1])
        nt = int(((addr >= lo - idle_gap) & (addr <= hi)).sum())   # AW/AR precede/overlap the beats
        rows.append((nt, len(g), hi - lo + 1))
    if not rows:
        return None
    rows.sort(key=lambda r: r[2])
    nt, nw, span = rows[len(rows) // 2]                            # the median transfer
    return {"num_trans": int(nt), "nwords": int(nw), "span": int(span)}
