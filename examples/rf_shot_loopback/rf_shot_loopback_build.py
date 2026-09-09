"""rf_shot_loopback_build.py — build the loopback demonstration: pysim -> figure.

``plans/rf_shot_absolute.md`` S3.  The rungs, in the order a failure is cheapest to diagnose:

    pysim               -> the loopback in SimPy: the address difference, the aliasing, the epoch
    address_delay_figure-> the two memories on one address axis
    sync_docs_figures   -> promote the SVG into the committed docs assets

**There is no codegen rung, and that is a decision rather than an omission** — see
``plans/rf_shot_absolute.md`` S3, *What S3 built*.  The two designs in this graph are each
synthesized and RTL-gated by their **own** examples, at the very ``absolute_index = 1`` this one
runs; what a loopback adds is a claim about the *pair*, and that claim is an address correspondence
in the loosely-timed model rather than a property of either kernel's RTL.  Closing the loop at RTL
would need a second locked memory inside one kernel and a C++ twin for the path's delay, and would
re-derive a number two green gate sets already stand behind.

Run it with::

    python rf_shot_loopback_build.py                     # through the figure
    python rf_shot_loopback_build.py --through pysim     # the measurement alone
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag, BuildStep, SourceStep  # noqa: E402

from examples.rf_shot_loopback.rf_shot_loopback_figures import (  # noqa: E402
    AddressDelayFigureStep,
    SyncDocsFiguresStep,
)


@dataclass(kw_only=True)
class PySimStep(BuildStep):
    """Run the loopback in SimPy and file **what it measured**, not merely that it passed.

    Three runs, because the three claims are three runs: the demonstration itself, the aliasing pair,
    and the epoch pair.  The numbers land in ``results/`` so a reader without a toolchain — which is
    everyone, for this example — has the measurement rather than a green tick.
    """

    description = "Run the RfShotLoopbackTB pysim golden (the delay, the aliasing, the epoch)."
    consumes = ["rf_shot_loopback_source"]
    produces: ClassVar[dict] = {"pysim_results": Path("results/rf_shot_loopback_pysim.json")}
    params: ClassVar[dict] = {}

    def run(self, config: BuildConfig, **_) -> dict:
        from examples.rf_shot_loopback.rf_shot_loopback import (
            ALIAS_DELAY_SAMP,
            BLKSIZE,
            DELAY_SAMP,
            DEPTH,
            NSAMP,
            N_SPACER,
            REGION_SAMPLES,
            channel_delay,
            check_an_epoch_offset_moves_the_reading_too,
            check_both_waveforms_reached_the_air,
            check_every_window_is_whole_and_announced_clean,
            check_the_epochs_are_tied,
            check_the_reading_aliases,
            check_the_run_was_clean,
            check_the_two_ends_agree_on_phase,
            leading_silent_windows,
            measured_delay,
            run_pysim,
            scenario_frames,
            window_frames,
            windows_as_codes,
        )

        frames = scenario_frames()
        tb = run_pysim(frames=frames)
        wf = window_frames(tb)
        check_the_epochs_are_tied(tb, where="pysim: ")
        check_the_run_was_clean(tb, frames, where="pysim: ")
        check_every_window_is_whole_and_announced_clean(wf, where="pysim: ")
        n_a = check_both_waveforms_reached_the_air(wf, where="pysim: ")
        phase, n_samp = check_the_two_ends_agree_on_phase(wf, where="pysim: ")
        near, far = check_the_reading_aliases(where="pysim: ")
        tied, late = check_an_epoch_offset_moves_the_reading_too(where="pysim: ")

        out = {
            "geometry": {"depth": DEPTH, "nsamp": NSAMP, "region_samples": REGION_SAMPLES,
                         "blksize": BLKSIZE, "n_spacer": N_SPACER},
            "configured_delay_samp": DELAY_SAMP,
            "loop_latency_samp": int(tb.loop_latency_samp),
            "raw_address_difference": int(measured_delay(wf)),
            "measured_channel_delay": int(channel_delay(tb, wf)),
            "samples_in_agreement": int(n_samp),
            "agreed_difference": int(phase),
            "windows": len(windows_as_codes(wf)),
            "windows_of_first_waveform": int(n_a),
            "leading_silent_windows": int(leading_silent_windows(wf)),
            "aliasing": {"near_delay": DELAY_SAMP, "far_delay": ALIAS_DELAY_SAMP,
                         "near_reading": int(near), "far_reading": int(far)},
            "epoch": {"raw_tied": int(tied), "raw_tx_one_block_late": int(late)},
            "counters": {"rx_dropped": int(tb.rx.n_dropped),
                         "dac_underrun": int(tb.dac_if.underrun),
                         "adc_underrun": int(tb.adc_if.underrun),
                         "path_blocks_in": int(tb.chan.n_in),
                         "path_blocks_out": int(tb.chan.n_out)},
        }
        p = config.root_dir / "results" / "rf_shot_loopback_pysim.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
        return {"pysim_results": p}


def build_rf_shot_loopback_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(SourceStep(artifact="rf_shot_loopback_source", path=HERE / "rf_shot_loopback.py"))
    dag.add(PySimStep(name="pysim"))
    dag.add(AddressDelayFigureStep(name="address_delay_figure"))
    dag.add(SyncDocsFiguresStep(name="sync_docs_figures"))
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(build_rf_shot_loopback_dag,
                description="Build the rf_shot_loopback demonstration: pysim -> figure.",
                default_through="address_delay_figure",
                root_dir=HERE)
