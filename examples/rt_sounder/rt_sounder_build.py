"""rt_sounder_build.py — build the ray-traced channel sounder (``plans/sionna_rt/sionna_rt.md``, M5).

The rungs, in the order a failure is cheapest to diagnose:

    trace_channel   -> the street canyon traced with Sionna RT into data/street_canyon.{npz,json}
                       (skipped while the committed file still describes the scene: no Sionna
                       needed after the first trace)

Run it with::

    python -m examples.rt_sounder.rt_sounder_build --list-steps
    python -m examples.rt_sounder.rt_sounder_build                  # every rung
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
