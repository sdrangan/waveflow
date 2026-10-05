"""vitis_fft.py — the calibration fixture for ``VitisFft``, the Vitis L1 SSR FFT.

``VitisFft`` (:mod:`waveflow.vitis_l1.hw`) is framework infrastructure, so its timing is a property of
``(component, platform)``: fit once on the platform, reloaded by every design that composes it.  It has
**two** separately measured delays (:mod:`waveflow.vitis_l1.timing`), so this fixture fits two
:class:`~waveflow.vitis_l1.timing.VitisFftTimingModel` s per configuration:

* **proc** -- last input word in -> last output word out, on **isolated** frames: a 48-frame phase
  sweep whose gaps spread arrivals over the vendor core's free-running input commutator.  Aggregated
  by **mean**: the pysim emits one number, and the mean minimizes its squared error over phases.
* **ii** -- the frame interval, **back to back**, where the core stays in step and it is exact.

It also files each build's csynth **resources** onto the platform's module store
(``modules/<key>/resource/``), keyed by the module's identity -- ``L`` and the widths are ``HwParam`` s,
so each configuration is its own entry.  Both timing models are stored as a lookup per measured ``L`` (:mod:`waveflow.vitis_l1.timing` says why not a law).  The RTL side needs Vitis + Vivado
(:func:`waveflow.vitis_l1.rtl.measure`); what it measures is collected into the platform's ``rtl/`` trees
and committed, so a refit -- or a pysim on another machine -- needs no toolchain.

    python -m waveflow.calib.fixtures.vitis_fft --work <short dir> [--lengths 16 64 256 1024]
    python -m waveflow.calib.fixtures.vitis_fft --holdout 1024        # fit without it, report it

**Use a short ``--work`` path**: csynth fails silently from a long one (see :mod:`waveflow.vitis_l1.rtl`).
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from waveflow.calib.fixture import ComponentFixture, SweepPoint, register
from waveflow.vitis_l1 import timing as vt

#: The lengths the shipped calibration covers.  ``L`` must be a power of ``R = 4``.
LENGTHS = (16, 64, 256, 1024, 4096)
#: The shipped width configuration: the module's defaults -- 16-bit input, 18-bit twiddles.  Another
#: is a constructor argument (or ``--in-w`` / ``--tw-w`` ...), and lands under its own timing
#: components and resource keys beside this one.  ``NO_SCALING`` and natural order are fixed: they
#: are the only modes the bit-exact model covers past L=16.
CONFIG = dict(in_w=16, in_i=2, tw_w=18, tw_i=2)
TASK_FN = "vitis_fft_task"


def _ids(config: dict | None = None) -> tuple[str, str]:
    c = {**CONFIG, **(config or {})}
    return vt.component_ids(TASK_FN, c["in_w"], c["in_i"], c["tw_w"], c["tw_i"], 0, 0)


class VitisFftFixture(ComponentFixture):
    """Calibration fixture for ``VitisFft`` (both models)."""

    component = _ids()[0].removesuffix(".proc")
    basis = list(vt.FEATURES)

    def __init__(self, lengths=LENGTHS, n_b2b: int = 6, n_sweep: int = 48, seed: int = 0,
                 config: dict | None = None) -> None:
        self.config = {**CONFIG, **(config or {})}
        self.lengths = tuple(lengths)
        self.n_b2b, self.n_sweep, self.seed = int(n_b2b), int(n_sweep), int(seed)

    def sweep(self) -> list[SweepPoint]:
        return [SweepPoint(label=f"L{n}", features=vt.features(n)) for n in self.lengths]

    # The base contract's single-model hooks do not fit a two-model component; calibration is driven
    # by :meth:`calibrate_platform` instead.
    def run_pysim(self, point, *, comp_dir, platform_dir, clk):          # pragma: no cover
        raise NotImplementedError("VitisFft has two models: use calibrate_platform()")

    # -- the two models -------------------------------------------------------------------------
    @staticmethod
    def models(platform_dir, clk=None, config: dict | None = None):
        """``(proc, ii)`` models at *platform_dir* for *config*, keyed as ``VitisFft`` keys them."""
        from waveflow.calib.platform import PlatformCalib
        lib = PlatformCalib(platform_dir)
        proc_id, ii_id = _ids(config)
        return (vt.VitisFftTimingModel(component=proc_id, calib_dir=lib.component_dir(proc_id),
                                       clk=clk, aggregate="mean"),
                vt.VitisFftTimingModel(component=ii_id, calib_dir=lib.component_dir(ii_id),
                                       clk=clk))

    # -- RTL ------------------------------------------------------------------------------------
    def rtl_events(self, length: int, work_dir) -> tuple[dict, dict]:
        """Measure *length* at RTL and reduce it to the two models' firing tables (cycles)."""
        from waveflow.vitis_l1 import rtl

        m = rtl.measure(Path(work_dir) / self._label(length), length, self.n_b2b, self.n_sweep,
                        self.seed, config=self.config)
        feat = vt.features(length)
        proc_id, ii_id = _ids(self.config)
        # Frame 0 arrives straight out of reset, in step with the commutator by construction -- a
        # case a running system never sees -- so the proc sweep starts at frame 1.
        proc = [{"component": proc_id, "index": k, **feat, "blocked": 0,
                 "span": f["done"] - f["last_in"]}
                for k, f in enumerate(m["sweep"]) if k >= 1]
        # The interval settles after one frame of start-up transient.
        b = m["b2b"]
        ii = [{"component": ii_id, "index": k, **feat, "blocked": 0,
               "span": b[k]["done"] - b[k - 1]["done"]} for k in range(2, len(b))]
        return {"top": "vitis_fft", "firings": proc}, {"top": "vitis_fft", "firings": ii}

    # -- pysim ----------------------------------------------------------------------------------
    def pysim_records(self, length: int, platform_dir) -> tuple[list[dict], list[dict]]:
        """The pysim's own firings for the same two scenarios, timed by the platform's CURRENT
        models (the zero seed before the first fit: the residual framing makes any run usable)."""
        from waveflow.simulation.simulation import Simulation
        from waveflow.vitis_l1 import rtl
        from waveflow.vitis_l1.testbench import VitisFftTB, write_scenario

        def run(n, gaps):
            with tempfile.TemporaryDirectory() as d:
                write_scenario(d, n, length, **self.config)
                tb = VitisFftTB(name="tb", sim=Simulation(), length=length, n_frames=n,
                                burst_gaps=list(gaps), platform_dir=platform_dir,
                                require_calibrated=False, root=Path(d), **self.config)
                tb.sim.run_sim()
                return tb.dut
        sweep = run(self.n_sweep, rtl.sweep_gaps(length, self.n_sweep, self.seed))
        b2b = run(self.n_b2b, ())
        return sweep.proc_records[1:], b2b.firing_records[2:]

    def _label(self, length: int) -> str:
        """The build directory for *length*: ``L<n>`` at the shipped widths, qualified otherwise so
        two configurations never share (and silently reuse) one build."""
        if self.config == CONFIG:
            return f"L{length}"
        c = self.config
        return f"L{length}_w{c['in_w']}_{c['in_i']}_t{c['tw_w']}_{c['tw_i']}"

    # -- the whole calibration ------------------------------------------------------------------
    def calibrate_platform(self, platform_dir, *, work_dir=None, remeasure: bool = False,
                           fit_lengths=None, max_passes: int = 6,
                           tol_cycles: float = 0.5) -> dict:
        """Collect RTL (measuring any length not already on the platform, or all with *remeasure*)
        and pysim for every length, then fit both models on *fit_lengths* (default: all)."""
        from waveflow.hw.clock import Clock
        from waveflow.vitis_l1.testbench import CLK_HZ

        clk = Clock(freq=CLK_HZ)
        proc_m, ii_m = self.models(platform_dir, clk, self.config)
        for n in self.lengths:
            label = f"L{n}"
            have = all((m.calib_dir / "rtl" / label / "firings.csv").exists() for m in (proc_m, ii_m))
            if remeasure or not have:
                if work_dir is None:
                    raise ValueError(f"L={n} is not measured on {platform_dir}: pass work_dir "
                                     f"(a SHORT path) to measure it with Vitis + Vivado")
                proc_ev, ii_ev = self.rtl_events(n, work_dir)
                proc_m.collect_rtl(proc_ev, run_id=label)
                ii_m.collect_rtl(ii_ev, run_id=label)
                # The synthesis that build ran is the expensive part; its resource report is free.
                from waveflow.vitis_l1 import rtl
                rtl.record_resources(Path(work_dir) / self._label(n), n, platform_dir,
                                     config=self.config)
        # Pysim on the CURRENT models, then refit -- repeated to a fixed point.  The residual framing
        # (rtl - pysim + current) is exact for a delay the model adds directly (proc converges in
        # one pass), but the interval also depends on the in-flight slots the predictions size, so
        # it needs a few passes before the pysim's interval is the RTL's.
        keep = None if fit_lengths is None else {float(n) for n in fit_lengths}
        report: dict = {"passes": 0}
        for _ in range(max_passes):
            for n in self.lengths:
                proc_rec, ii_rec = self.pysim_records(n, platform_dir)
                proc_m.collect_pysim(proc_rec, run_id=f"L{n}")
                ii_m.collect_pysim(ii_rec, run_id=f"L{n}")
            worst = 0.0
            for m in (proc_m, ii_m):
                df = m.gen_data_frame()
                if keep is not None:
                    df = df[df["L"].isin(keep)]
                worst = max(worst, float((df["span_pysim"] - df["span_rtl"]).abs().max()))
                m.fit(df)
                report[m.component] = m._model.to_params()
            report["passes"] += 1
            report["max_span_error_cycles"] = round(worst, 3)
            if worst < tol_cycles:
                break
        return report

    def holdout(self, platform_dir, test_lengths) -> dict:
        """Fit each model WITHOUT *test_lengths* and report how it predicts them: the evidence that
        the ``[1, L, L log2 L]`` law extrapolates, rather than a claim that it does.  Leaves the
        platform's fitted params untouched."""
        from waveflow.calib.calib import LinCalibModel

        test = {float(n) for n in test_lengths}
        out = {}
        for m in self.models(platform_dir, config=self.config):
            df = m.gen_data_frame()
            lm = LinCalibModel(basis=list(vt.FEATURES), target="residual", fit_intercept=True,
                               coeff_names=list(vt.FEATURES))
            out[m.component] = lm.holdout_report(df[~df["L"].isin(test)], df[df["L"].isin(test)])
        return out


# Registered on package import.  Under `python -m` this module runs a SECOND time as __main__ (the
# package import already registered it), and the registry rightly refuses a duplicate.
FIXTURE = register(VitisFftFixture()) if __name__ != "__main__" else VitisFftFixture()


def main(argv=None) -> None:
    from waveflow.calib.platform import packaged_platforms_dir

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform-dir", type=Path,
                    default=(packaged_platforms_dir() or Path(".")) / vt.PLATFORM)
    ap.add_argument("--work", type=Path, default=None, help="a SHORT build directory")
    ap.add_argument("--lengths", type=int, nargs="*", default=list(LENGTHS))
    ap.add_argument("--remeasure", action="store_true")
    ap.add_argument("--holdout", type=int, nargs="*", default=None,
                    help="fit without these lengths and report how they are predicted")
    for k, v in CONFIG.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=int, default=v,
                        help=f"width configuration ({k}, default {v})")
    a = ap.parse_args(argv)
    fx = VitisFftFixture(lengths=a.lengths, config={k: getattr(a, k) for k in CONFIG})
    if a.holdout:
        import json
        print(json.dumps(fx.holdout(a.platform_dir, a.holdout), indent=1))
        return
    print(fx.calibrate_platform(a.platform_dir, work_dir=a.work, remeasure=a.remeasure))


if __name__ == "__main__":
    main()
