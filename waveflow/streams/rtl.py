"""rtl.py -- build a stream block in its own harness and run it under XSI: the blocks' RTL gates.

    python -m waveflow.streams.rtl <root> [--block broadcast|demux|demux_hdr|pair|framer] [--n 2]
                                          [--bitwidth 32] [--frame-bursts 1] [--policy priority]
                                          [--through compare]

Everything before csynth is Python: the stream helpers, the blocks' bodies (copied by their steps) and the
``ap_ctrl_none`` top from ``composite_top_spec``.  The run is the system flow on the module's harness (a
stream driver on each input, a sink on each output): ``csynth``, ``system_rtl``, ``scenario``, ``pysim``,
``system_xsi``, ``compare``.

``StreamArbiter`` has no gate alone: which input wins depends on when the stand-ins deliver, so its RTL
interleave need not be pysim's.  Its gate is :class:`ArbiterDemuxPair` -- the arbiter's ``m_out`` and
``m_sel`` into a demux -- whose every output carries one input's frames in order, whatever the
interleave: the frames whole, none lost, every index right.

**Build in a short directory**: csynth fails silently past the Windows path limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (
    DEFAULT_PART,
    DEFAULT_PERIOD_NS,
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_tcl,
    render_top,
    tcl_path,
)
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIF
from waveflow.simulation.simulation import Simulation
from waveflow.streams.arbiter import StreamArbiter, StreamArbiterStep, frames_of
from waveflow.streams.broadcast import StreamBroadcast, StreamBroadcastStep
from waveflow.streams.demux import StreamDemux, StreamDemuxStep
from waveflow.streams.framer import StreamFramer, StreamFramerStep

#: Every block's body, copied into every build (each a header; unused ones cost nothing).
BODY_STEPS = (StreamBroadcastStep, StreamArbiterStep, StreamDemuxStep, StreamFramerStep)


@dataclass
class ArbiterDemuxPair(FreeRunMod):
    """``StreamArbiter(n) -> StreamDemux(n)``, the arbiter's ``m_sel`` the demux's ``s_sel``: each input
    ``s_in_k`` comes back out on ``m_out_k``.  The arbiter's gate (its interleave is timing's, the pair's
    outputs are not), and the shape of a shared link: K would sit between the two."""

    cpp_kernel_name: ClassVar[str | None] = "stream_arb_pair"
    build_fields: ClassVar[tuple[str, ...]] = ("policy",)

    n: HwParam[int] = 2
    bitwidth: HwParam[int] = 32
    frame_bursts: HwParam[int] = 1
    policy: str = "priority"
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    #: Link depths, in words: the data link holds a frame's burst, the index link a few.
    DEPTH: ClassVar[dict] = {"data": 16, "sel": 4}

    def __post_init__(self) -> None:
        super().__post_init__()
        n, w, nb = int(self.n), int(self.bitwidth), int(self.frame_bursts)
        self.arb = StreamArbiter(name=f"{self.name}_arb", sim=self.sim, n_in=n, bitwidth=w, frame_bursts=nb,
                                 policy=self.policy, clk=self.clk)
        self.dmx = StreamDemux(name=f"{self.name}_dmx", sim=self.sim, n_out=n, bitwidth=w, frame_bursts=nb,
                               clk=self.clk)
        self.add_comp(self.arb)
        self.add_comp(self.dmx)
        for name, master, slave in (("data", self.arb.m_out, self.dmx.s_in),
                                    ("sel", self.arb.m_sel, self.dmx.s_sel)):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                             framed=True, depth=self.DEPTH[name])
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)
        for k in range(n):
            setattr(self, f"s_in_{k}", self.arb.s_in[k])
        for k in range(n):
            setattr(self, f"m_out_{k}", self.dmx.m_out[k])
        self.boundary = [*(f"s_in_{k}" for k in range(n)), *(f"m_out_{k}" for k in range(n))]

    def workload(self, kind: str = "test") -> dict:
        return {f"s_in_{k}": frames_of(k, int(self.frame_bursts), kind) for k in range(int(self.n))}

    def golden(self, kind: str = "test") -> dict:
        """Every input's bursts back on its own output, in order."""
        return {f"m_out_{k}": [np.asarray(b.words, dtype=np.uint64) for b in v]
                for k, v in ((int(p.rsplit("_", 1)[1]), v) for p, v in self.workload(kind).items())}


def top_name(n_out: int, bitwidth: int) -> str:
    """The broadcast's top."""
    return f"bcast{n_out}_w{bitwidth}"


def block(n_out: int = 2, bitwidth: int = 32) -> StreamBroadcast:
    """The broadcast, named for its body."""
    return StreamBroadcast(name=StreamBroadcast.cpp_kernel_name, sim=Simulation(), n_out=n_out,
                           bitwidth=bitwidth)


def make(kind: str, n: int = 2, bitwidth: int = 32, frame_bursts: int = 1, policy: str = "priority"):
    """A gated module and its top's name: ``broadcast``, ``demux`` (by ``s_sel``), ``demux_hdr`` (by
    bits 16-23 of a frame's first word), ``pair`` (:class:`ArbiterDemuxPair`) or ``framer`` (its frame
    length passed as *frame_bursts*).  Also ``arbiter``, the arbiter alone: it has no gate (its
    interleave is timing's), but its csynth is the block's own resources and its RTL run the block's
    own timing (:mod:`waveflow.streams.calib`)."""
    sim = Simulation()
    if kind == "broadcast":
        return block(n, bitwidth), top_name(n, bitwidth)
    if kind == "arbiter":
        mod = StreamArbiter(name=StreamArbiter.cpp_kernel_name, sim=sim, n_in=n, bitwidth=bitwidth,
                            frame_bursts=frame_bursts, policy=policy)
        return mod, f"sarb{'rr' if policy == 'round_robin' else 'p'}{n}_w{bitwidth}_f{frame_bursts}"
    if kind in ("demux", "demux_hdr"):
        hdr = kind == "demux_hdr"
        mod = StreamDemux(name=StreamDemux.cpp_kernel_name, sim=sim, n_out=n, bitwidth=bitwidth,
                          frame_bursts=frame_bursts, route="header" if hdr else "sel", sel_lsb=16, sel_bits=8)
        return mod, f"{'dmxh' if hdr else 'dmx'}{n}_w{bitwidth}_f{frame_bursts}"
    if kind == "framer":
        mod = StreamFramer(name=StreamFramer.cpp_kernel_name, sim=sim, frame_words=frame_bursts,
                           bitwidth=bitwidth)
        return mod, f"frm{frame_bursts}_w{bitwidth}"
    if kind == "pair":
        mod = ArbiterDemuxPair(name=ArbiterDemuxPair.cpp_kernel_name, sim=sim, n=n, bitwidth=bitwidth,
                               frame_bursts=frame_bursts, policy=policy)
        return mod, f"arb{'rr' if policy == 'round_robin' else 'p'}{n}_w{bitwidth}_f{frame_bursts}"
    raise ValueError(f"unknown stream block {kind!r}")


def generate_for(root: Path, mod, *, part: str = DEFAULT_PART,
                 period_ns: float = DEFAULT_PERIOD_NS) -> str:
    """Everything before csynth at *root* for *mod*: the headers, the bodies, the top and its ``.tcl``.

    *part* / *period_ns* are the csynth target; the defaults are the gates' (``DEFAULT_PART`` at
    100 MHz), and only a probe passes others (:mod:`waveflow.streams.rfsoc_probe`)."""
    root = Path(root).resolve()
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    for step in BODY_STEPS:
        dag.add(step(output_dir=INCLUDE_DIR))
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")
    spec = composite_top_spec(mod, width=int(mod.bitwidth))
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(render_tcl(spec.top_name, part=part, period_ns=period_ns),
                                             encoding="utf-8")
    return spec.top_name


def generate(root: Path, n_out: int = 2, bitwidth: int = 32) -> str:
    """The broadcast's :func:`generate_for`."""
    return generate_for(root, block(n_out, bitwidth))


@dataclass(kw_only=True)
class StreamBlockCodegenStep(BuildStep):
    """``codegen``: :func:`generate_for` the module :func:`make` builds.  Never fresh; csynth decides by
    content."""

    kind: str = "broadcast"
    n_out: int = 2
    bitwidth: int = 32
    frame_bursts: int = 1
    policy: str = "priority"
    description = "Generate the stream helpers, the blocks' bodies and the free-running top."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        mod, _top = make(self.kind, self.n_out, self.bitwidth, self.frame_bursts, self.policy)
        generate_for(root, mod)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_block_dag(work_dir, kind: str, n: int = 2, bitwidth: int = 32, frame_bursts: int = 1,
                    policy: str = "priority", workload: str = "test") -> tuple[BuildDag, str]:
    """``codegen``, then the system steps of the module's harness; returns the DAG and the top's name."""
    mod, top = make(kind, n, bitwidth, frame_bursts, policy)
    dag = BuildDag()
    dag.add(StreamBlockCodegenStep(name="codegen", kind=kind, n_out=n, bitwidth=bitwidth,
                                   frame_bursts=frame_bursts, policy=policy))
    add_system_steps(dag, harness(mod, workload=workload), work_dir=work_dir, top=top)
    return dag, top


def build_dag(work_dir, n_out: int = 2, bitwidth: int = 32) -> BuildDag:
    """The broadcast's DAG: ``codegen``, then the system steps of its harness (top :func:`top_name`)."""
    return build_block_dag(work_dir, "broadcast", n_out, bitwidth)[0]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--block", default="broadcast",
                    choices=("broadcast", "demux", "demux_hdr", "pair", "arbiter", "framer"))
    ap.add_argument("--n", "--n-out", dest="n", type=int, default=2)
    ap.add_argument("--bitwidth", type=int, default=32)
    ap.add_argument("--frame-bursts", type=int, default=1)
    ap.add_argument("--policy", default="priority", choices=("priority", "round_robin"))
    ap.add_argument("--through", default="compare")
    a = ap.parse_args()
    root = a.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    dag, _top = build_block_dag(root / "xw", a.block, a.n, a.bitwidth, a.frame_bursts, a.policy)
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "build"}), through=a.through)
    bad = {n: r.message for n, r in res.items() if not r.success}
    for n, r in res.items():
        print(f"{n}: {'ok' if r.success else 'FAILED ' + str(r.message)[:400]}")
    raise SystemExit(1 if bad else 0)
