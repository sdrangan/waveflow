"""calib.py -- the stream blocks' resource records on a platform: the sweep that files them.

    python -m waveflow.streams.calib arbiter [--dry-run] [--resume] [--n-in 2 4 ...]   # csynth, file
    python -m waveflow.streams.calib demux   [--dry-run] [--resume] [--n-out 2 4 ...]
    waveflow_calib publish work/stream_calib/calib/work/xczu48dr_250mhz_vitis2025_1 \\
                           waveflow/calib/platforms/xczu48dr_250mhz_vitis2025_1 --apply

``plans/shared_kernel_rfsoc_timing.md``: what sharing a kernel costs in arbiter and demux, against
replicating it.  Each point is the block alone as its own ``ap_ctrl_none`` top -- every port an AXIS port
of the top -- synthesized at the **platform's** part and clock (``tcl_target``; the packaged
``xczu48dr_250mhz_vitis2025_1`` is xczu48dr at 4 ns) and attributed by ``InspectSynthStep``, which files
one resource record per point under the block's module key.  The key is the block's structure, so
``policy`` and ``route``, which pick the body, are separate keys though not ``HwParam``\\ s.

The sweep writes the untracked work tier (``Platform.resolve``: the packaged platform is a read-only
fallback, so a build files under ``<platforms_root>/<name>``); ``waveflow_calib publish`` promotes it into
the library.  Everything about running a grid -- resume, failure isolation, the summary -- is
:mod:`waveflow.build.sweep`'s.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    RFSOC4X2_PART,
    tcl_path,
    tcl_target,
)
from waveflow.build.resource_steps import InspectSynthStep
from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli
from waveflow.streams.arbiter import StreamArbiter
from waveflow.streams.demux import StreamDemux

REPO = Path(__file__).resolve().parents[2]
#: The sweep's build directories and its work tier (``/work/`` is not tracked).
WORK = REPO / "work" / "stream_calib"
PLATFORMS_ROOT = WORK / "calib" / "work"
#: The packaged library the records are published into, and its target.
PLATFORM = "xczu48dr_250mhz_vitis2025_1"
PART, CLK_FREQ = RFSOC4X2_PART, 250e6

#: The header route's field, as the RTL gate builds it (bits 16-23).
SEL_LSB, SEL_BITS = 16, 8


@dataclass(frozen=True)
class Block:
    """One block's sweep: its class, the parameters a point elaborates it with, and its grid."""

    cls: type
    params: tuple[str, ...]
    grid: ParamGrid
    fixed: tuple[tuple[str, Any], ...] = ()

    @property
    def top(self) -> str:
        return str(self.cls.cpp_kernel_name)

    def module(self, point: dict):
        from waveflow.simulation.simulation import Simulation
        kw = {**dict(self.fixed), **{k: point[k] for k in self.params if k in point}}
        return self.cls(name=self.top, sim=Simulation(), **kw)


_WIDTHS, _BURSTS = (32, 64, 128), (1, 2)
#: ``n`` outer: a partial run then covers whole sizes.  48 points each.
BLOCKS: dict[str, Block] = {
    "arbiter": Block(StreamArbiter, ("n_in", "bitwidth", "frame_bursts", "policy"),
                     ParamGrid(n_in=(2, 4, 8, 16), bitwidth=_WIDTHS, frame_bursts=_BURSTS,
                               policy=("priority", "round_robin"))),
    "demux": Block(StreamDemux, ("n_out", "bitwidth", "frame_bursts", "route", "sel_lsb", "sel_bits"),
                   ParamGrid(n_out=(2, 4, 8, 16), bitwidth=_WIDTHS, frame_bursts=_BURSTS,
                             route=("sel", "header")),
                   fixed=(("sel_lsb", SEL_LSB), ("sel_bits", SEL_BITS))),
}


@dataclass(kw_only=True)
class _BlockCodegenStep(BuildStep):
    """``codegen``: the stream helpers, the block's body and its top, for the build's platform.  Never
    fresh: a sweep point is a different block in the same directory."""

    block: str
    description = "Generate the stream helpers, the block's body and its top for the platform's target."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        from waveflow.streams.rtl import generate_for

        root = Path(config.root_dir)
        part, period = tcl_target(config)
        generate_for(root, BLOCKS[self.block].module(dict(config.params)), part=part, period_ns=period)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


@dataclass(kw_only=True)
class _BlockCsynthStep(BuildStep):
    """``csynth``: the generated ``.tcl``; publishes the solution and the seconds it took."""

    top: str
    description = "Run Vitis HLS C-synthesis of the block's top."
    consumes: ClassVar[list] = ["gen"]
    params: ClassVar[dict] = {}

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {"report_dir": Path(f"{self.top}_proj/solution1"), "synth_seconds": None}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        from waveflow.toolchain.toolchain import run_vitis_hls

        root = Path(config.root_dir)
        t0 = time.perf_counter()
        r = run_vitis_hls(tcl_path(root, self.top), work_dir=root, capture_output=True)
        out = (r.stdout or "") + (r.stderr or "")
        (root / "csynth.log").write_text(out, encoding="utf-8")
        if "WAVEFLOW_CSYNTH_OK" not in out:
            raise RuntimeError(f"csynth of {self.top} did not report WAVEFLOW_CSYNTH_OK (see csynth.log)")
        return {"report_dir": root / f"{self.top}_proj" / "solution1",
                "synth_seconds": time.perf_counter() - t0}


def build_dag(block: str) -> BuildDag:
    """``codegen`` -> ``csynth`` -> ``resources`` (attribute and file, every loop II = 1 required)."""
    b = BLOCKS[block]
    dag = BuildDag()
    dag.add(_BlockCodegenStep(name="codegen", block=block))
    dag.add(_BlockCsynthStep(name="csynth", top=b.top))
    dag.add(InspectSynthStep(name="resources", comp_class=b.cls, top_name=b.top,
                             elaborate_params=b.params + tuple(k for k, _ in b.fixed),
                             params={**{k: None for k in b.params}, **dict(b.fixed)},
                             require_ii1=True))
    return dag


def runner(block: str) -> SweepRunner:
    """The block's sweep: its own build directory, the shared work tier, the RFSoC target."""
    b = BLOCKS[block]
    return SweepRunner(dag_factory=lambda: build_dag(block), root_dir=WORK / block,
                       platform=PLATFORM, platforms_root=PLATFORMS_ROOT, part=PART, clk_freq=CLK_FREQ,
                       extra_params=dict(b.fixed))


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in BLOCKS:
        raise SystemExit(f"usage: python -m waveflow.streams.calib {{{'|'.join(BLOCKS)}}} [sweep options]")
    block = argv.pop(0)
    (WORK / block).mkdir(parents=True, exist_ok=True)
    return sweep_cli(runner(block), BLOCKS[block].grid,
                     description=f"Sweep {BLOCKS[block].cls.__name__} resources at {PART}, 4 ns",
                     stages=[Stage("resources")], dry_run_stages=[Stage("codegen", use_platform=False)],
                     argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
