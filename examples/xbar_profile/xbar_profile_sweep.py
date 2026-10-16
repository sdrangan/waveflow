"""xbar_profile_sweep.py — every crossbar configuration in ``xbar_profile``: 5 shapes x 2 modes x 2 widths.

Each point is one ``create_ip`` (~30 s), one out-of-context synthesis + place and route (minutes), and
one XSI run (~1 min to build, seconds to simulate), all cached by configuration::

    python -m examples.xbar_profile.xbar_profile_sweep --dry-run      # IP generation only
    python -m examples.xbar_profile.xbar_profile_sweep
    python -m examples.xbar_profile.xbar_profile_sweep --shape 4x4 --resume
    python -m examples.xbar_profile.xbar_profile_table               # the tables, from the store

The records land in the **work-tier** platform ``calib/work/zu7ev_xbar_250mhz`` (untracked), which
is where an exploratory sweep belongs; promoting them into a tracked platform library is a deliberate
step, ``waveflow_calib publish`` (dry-run by default).
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli  # noqa: E402

from examples.xbar_profile.xbar_profile import (  # noqa: E402
    CLK_FREQ,
    DATA_WIDTHS,
    MODES,
    PART,
    SHAPES,
)
from examples.xbar_profile.xbar_profile_build import (  # noqa: E402
    PLATFORM,
    PLATFORMS_ROOT,
    build_xbar_profile_dag,
)

#: Width outermost, so a partial run covers every shape and mode at one width first.
GRID = ParamGrid(dw=DATA_WIDTHS, shape=SHAPES, mode=MODES)

RUNNER = SweepRunner(dag_factory=build_xbar_profile_dag, root_dir=HERE,
                     platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                     part=PART, clk_freq=CLK_FREQ)

#: One stage: IP, synthesis + place and route, XSI latency, records.  ``--dry-run`` stops after the
#: IP (no license needed, no platform touched).
STAGES = [Stage("records")]
DRY_RUN_STAGES = [Stage("ip", use_platform=False)]


def main(argv: "list[str] | None" = None) -> int:
    return sweep_cli(RUNNER, GRID, description="Sweep axi_crossbar area, timing closure and latency",
                     stages=STAGES, dry_run_stages=DRY_RUN_STAGES, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
