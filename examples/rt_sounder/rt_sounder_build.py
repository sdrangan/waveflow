"""rt_sounder_build.py — build the ray-traced channel sounder (``plans/sionna_rt/sionna_rt.md``, M5).

The rungs, in the order a failure is cheapest to diagnose:

    trace_channel   -> the street canyon traced with Sionna RT into data/street_canyon.{npz,json}
                       (skipped while the committed file still describes the scene and the
                       installed tracer; another sionna-rt, Mitsuba or Dr.Jit version retraces it
                       and rewrites the committed file, a change to review)

Run it with::

    python -m examples.rt_sounder.rt_sounder_build --list-steps
    python -m examples.rt_sounder.rt_sounder_build                  # every rung

The two modes after it need no rung of their own: channel mode is ``python -m
examples.rt_sounder.rt_sounder`` (prints AC7's tap table).  Source mode at RTL replays through
``examples/rf_loopback``'s ``rf_pass_through``, so its RTL comes from that example::

    python -m examples.rf_loopback.rf_dut_build --through csynth   # the RTL (Vitis HLS)
    python -m examples.rt_sounder.rt_sounder_xsi                   # this workspace: xsi/
    pytest -m xsi -rs tests/examples/test_rt_sounder_xsi.py        # the replay gate (Vivado xsim)
"""

from __future__ import annotations

from pathlib import Path

from examples.rt_sounder.rt_sounder import CHANNEL, SPEC
from waveflow.build.build import BuildDag
from waveflow.rfenv.cache import TraceChannelStep

HERE = Path(__file__).resolve().parent


def build_rt_sounder_dag() -> BuildDag:
    dag = BuildDag()
    dag.add(
        TraceChannelStep(
            name="trace_channel", artifact="street_canyon", spec=SPEC, stem=CHANNEL
        )
    )
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(
        build_rt_sounder_dag,
        description="Build the ray-traced channel sounder (examples/rt_sounder).",
        default_through="trace_channel",
        root_dir=HERE,
    )
