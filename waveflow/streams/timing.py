"""timing.py -- the stream blocks' decision cycles in pysim: a lookup per measured point on a platform.

``plans/stream_dispatch.md`` Stage 7.  Between consecutive frames (``StreamArbiter``) or jobs
(``StreamDispatch``) the HLS bodies spend idle cycles deciding: the ``empty()`` tests, the search, the
count or pointer update, entering the copy loop.  The pysim models decide at once.  A block given
``platform_dir`` -- a platform its decision cost was calibrated on -- adds the measured cycles to each
firing through :meth:`~waveflow.hw.hw_freerun.FreeRunMod.timed_delay`; a block given none stays untimed,
as before, so nothing that names no platform changes.

* **One model a block**, keyed by its task body on the platform (``components/<id>/``):
  ``stream_arbiter_task.decision`` on the basis ``(n, rr)``, and ``stream_dispatch_task.decision`` on
  ``(n, rr, kind)`` -- ``kind`` 0 a job sent, 1 a credit taken back, for a credit firing is a firing of
  its own.  ``n`` is the block's ports a side, ``rr`` 1 for round-robin.
* **A lookup, not a law.**  The cost steps where Vitis reschedules the search (round-robin's gap at
  100 MHz goes 4, 5, 10, 20 from 2 to 16 workers), so between two measured points the truth can be
  anywhere: an unmeasured ``(n, rr)`` is ``UNCALIBRATED`` and :func:`attach` refuses it.
* **The residual framing** of :class:`~waveflow.calib.timing_model.TimingModel`: a span is the idle
  cycles between consecutive frames or jobs (or what one credit firing adds to them), and the model
  stores ``rtl - pysim + current``, so ``run_iter`` adds only what the channels do not already charge
  (the arbiter's frames are a cycle apart in pysim before any model; the dispatcher's, none).
* **Keyed by function, measured at one width and frame.**  The calibration builds every block at 32
  bits and 1-burst frames.  The spot check (``--spot`` of :mod:`waveflow.calib.fixtures.stream_blocks`,
  4 ports) at 128 bits and 2-burst frames found every frame or job gap one cycle longer, both blocks
  and both policies, the credit firing unchanged -- not yet separated into width and bursts.  So a
  block at another width or frame is predicted to within about a cycle a frame, and the models say so
  (``config_specific=False``) rather than imply a match.
* **Measured with every port at the top.**  Each block is calibrated alone, every port a top-level
  AXI-Stream port.  Inside a composite its ports are FIFOs, and a firing can cost less: in the
  dispatcher pool at 250 MHz a credit firing adds 2 cycles to a job gap at 2 workers (4 alone) and 4 at
  4 workers round-robin (5 alone), so the timed pysim of that pool runs 7 to 17% long, where the
  untimed one ran 28 to 45% short (``plans/stream_dispatch.md``, Stage 7).

The cycles are the platform's clock's; :func:`attach` refuses a block on another clock, as ``VitisFft``
does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from waveflow.calib.timing_model import RESIDUAL, TimingModel

#: The platform the blocks' decision cost is calibrated on: xczu48dr at 250 MHz, Vitis / Vivado 2025.1.
PLATFORM = "xczu48dr_250mhz_vitis2025_1"

#: Each block's component on the platform, and its basis.
COMPONENTS = {"arbiter": "stream_arbiter_task.decision", "dispatch": "stream_dispatch_task.decision"}
BASIS = {"arbiter": ["n", "rr"], "dispatch": ["n", "rr", "kind"]}

#: The dispatcher's two firings.
JOB, CREDIT = 0, 1


@dataclass
class StreamBlockTimingModel(TimingModel):
    """A :class:`TimingModel` whose residual is a lookup per measured point (``LookupCalibModel``)."""

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.calib.calib import LookupCalibModel

        # Seeded with an EMPTY table: a point never calibrated predicts no extra delay and reports
        # UNCALIBRATED -- what the fixture's first pysim pass needs, and what attach() refuses.
        self._model = LookupCalibModel(basis=list(self.features), target=RESIDUAL, name=self.component,
                                       path=self.calib_dir / "params.json",
                                       seed={"basis": list(self.features), "targets": [RESIDUAL],
                                             "table": []})


def model(block: str, platform_dir, clk=None) -> StreamBlockTimingModel:
    """The *block*'s (``"arbiter"`` or ``"dispatch"``) decision model on *platform_dir*."""
    from waveflow.calib.platform import PlatformCalib

    cid = COMPONENTS[block]
    return StreamBlockTimingModel(component=cid, calib_dir=PlatformCalib(platform_dir).component_dir(cid),
                                  features=list(BASIS[block]), clk=clk, config_specific=False)


def features(n: int, round_robin: bool, kind: int | None = None) -> dict[str, float]:
    """A firing's point on the basis: ``kind`` only for the dispatcher."""
    out = {"n": float(n), "rr": float(bool(round_robin))}
    if kind is not None:
        out["kind"] = float(kind)
    return out


def require_points(mod, block: str, points: list[dict]) -> None:
    """Refuse any of *points* -- the firings *mod* will make -- that ``mod.platform_dir`` has not measured:
    a lookup does not extrapolate.  At construction, so a design learns it at once."""
    from waveflow.calib.confidence import ConfidenceLevel

    tm = model(block, mod.platform_dir)
    for p in points:
        if tm.confidence_feat(p).level == ConfidenceLevel.UNCALIBRATED:
            raise ValueError(
                f"{mod.name}: {tm.component} has no measurement at {p} on {mod.platform_dir} -- the "
                f"decision cost is a lookup per measured point, not an extrapolation.  Calibrate it: "
                f"python -m waveflow.calib.fixtures.stream_blocks --work <short dir>")


def attach(mod, block: str, points: list[dict]) -> StreamBlockTimingModel:
    """Attach the *block*'s model on ``mod.platform_dir`` to *mod* -- from its ``pre_sim``, so a structural
    copy that never simulates (a codegen rebuild at the class's default clock) needs no clock to agree.
    Refuses a block whose clock is not the platform's (the model is in its cycles) and an unmeasured
    point (:func:`require_points`)."""
    pdir = Path(mod.platform_dir)
    pj = pdir / "platform.json"
    want = float(json.loads(pj.read_text(encoding="utf-8")).get("clk_freq_hz", 0) or 0) if pj.is_file() else 0.0
    if want and abs(want - float(mod.clk.freq)) > 1e-6 * want:
        raise ValueError(f"{mod.name}: platform {pdir.name} is calibrated at {want / 1e6:g} MHz, but this "
                         f"block's clock is {mod.clk.freq / 1e6:g} MHz")
    require_points(mod, block, points)
    tm = model(block, pdir, mod.clk)
    mod.add_timing_model(tm)
    return tm


def decide(mod, feats: dict):
    """The firing's decision cycles, as a ``timeout`` -- nothing at all (not even a zero timeout, which
    would reorder same-time events) on an untimed block."""
    if mod.timing_model is not None:
        dly = mod.timed_delay(feats)
        if dly > 0:
            yield mod.timeout(dly)
