"""rtl.py -- build a stream block in its own harness and run it under XSI: the block's RTL gate.

    python -m waveflow.streams.rtl <root> [--n-out 2] [--bitwidth 32] [--through compare]

Everything before csynth is Python: the stream helpers, the block's body (copied by its step) and the
``ap_ctrl_none`` top from ``composite_top_spec``.  The run is the system flow on the block's harness
(``harness(StreamBroadcast(...))``: a stream driver on ``s_in``, a sink on each output): ``csynth``,
``system_rtl``, ``scenario``, ``pysim``, ``system_xsi``, ``compare``.

**Build in a short directory**: csynth fails silently past the Windows path limit.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import GEN_DIR, INCLUDE_DIR, composite_top_spec, render_tcl, render_top, tcl_path
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.harness import harness
from waveflow.simulation.simulation import Simulation
from waveflow.streams.broadcast import StreamBroadcast, StreamBroadcastStep


def top_name(n_out: int, bitwidth: int) -> str:
    return f"bcast{n_out}_w{bitwidth}"


def block(n_out: int = 2, bitwidth: int = 32) -> StreamBroadcast:
    return StreamBroadcast(name=StreamBroadcast.cpp_kernel_name, sim=Simulation(), n_out=n_out,
                           bitwidth=bitwidth)


def generate(root: Path, n_out: int = 2, bitwidth: int = 32) -> str:
    """Everything before csynth at *root*: the headers, the body, the top and its ``.tcl``."""
    root = Path(root).resolve()
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    dag.add(StreamBroadcastStep(output_dir=INCLUDE_DIR))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")
    spec = composite_top_spec(block(n_out, bitwidth), width=bitwidth)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name), encoding="utf-8")
    return spec.top_name


@dataclass(kw_only=True)
class StreamBlockCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh; csynth decides by content."""

    n_out: int = 2
    bitwidth: int = 32
    description = "Generate the stream helpers, the block's body and its free-running top."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root, self.n_out, self.bitwidth)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag(work_dir, n_out: int = 2, bitwidth: int = 32) -> BuildDag:
    """``codegen``, then the system steps of the block's harness (top :func:`top_name`)."""
    dag = BuildDag()
    dag.add(StreamBlockCodegenStep(name="codegen", n_out=n_out, bitwidth=bitwidth))
    add_system_steps(dag, harness(block(n_out, bitwidth), workload="test"), work_dir=work_dir,
                     top=top_name(n_out, bitwidth))
    return dag


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--n-out", type=int, default=2)
    ap.add_argument("--bitwidth", type=int, default=32)
    ap.add_argument("--through", default="compare")
    a = ap.parse_args()
    root = a.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    res = build_dag(root / "xw", a.n_out, a.bitwidth).run(
        BuildConfig(root_dir=root, params={"synth": "build"}), through=a.through)
    bad = {n: r.message for n, r in res.items() if not r.success}
    for n, r in res.items():
        print(f"{n}: {'ok' if r.success else 'FAILED ' + str(r.message)[:400]}")
    raise SystemExit(1 if bad else 0)
