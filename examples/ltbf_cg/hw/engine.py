"""engine.py -- the LTBF long-term CG engine as a Waveflow free-running kernel.

``plans/mimo_cg/ltbf_cg_dse.md`` steps 4.1-4.3 (§9 c, AC5).  :class:`LtbfEngine` is one leaf whose
hand-written body is ``hw/cpp/ltbf_engine_task.h``: one firing solves one sector (or, with ``H``
kept, one UE of it) -- the factored ``S = D + H_alpha (H_alpha^H D)`` or, explicit, ``S = Q D`` with
``Q`` formed on chip -- then the CG vector step, ``nit`` times, on stored integers.  Its pysim twin
(:meth:`LtbfEngine.run_iter`) is :func:`examples.ltbf_cg.ltbf_fixed.solve_stored`, the model the
C-sim conformance (step 2.3) proved equal to ``ap_fixed``.

Knobs (``HwParam``): the maximum sizes ``nmax`` (N_rx), ``cmax`` (8 x connected UEs), ``mmax``
(right-hand sides: 8 x own UEs joint, 8 per UE), the tile ``R`` x ``C`` (R C complex MACs per
cycle; ``C`` is also the vector unit's lane count), the form, and the word width ``W`` (formats
from :data:`CANON_RANGES`).

The stream protocol (64-bit words, no TLAST): an :class:`LtbfCmd`, then (``load_h``) ``n x cols``
words of ``H``, then ``n x m`` words of ``B``, each one complex value as a ``wf_lanes`` group of one
lane (``waveflow.linalg.lanes``); out come ``n x m`` words of ``X`` after ``nit`` iterations.  A
:class:`LtbfSystem` drives it in pysim and, through the system flow, at RTL (XSI).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import ClassVar

import numpy as np

from examples.ltbf_cg import ltbf_fixed as lf
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import SEQUENTIAL_XSI_TB
from waveflow.hw.dataschema import DataList, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.hw_system import GraphSystem
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.linalg import cg as lcg
from waveflow.linalg import lanes
from waveflow.linalg.matmul import acc_format
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

HERE = Path(__file__).resolve().parent
CPP_DIR = HERE / "cpp"
WORD_BW = 64
#: The engine's clock: the study's 250 MHz (4 ns).
FREQ = 250e6

Word32 = IntField.specialize(bitwidth=32, signed=False)


class LtbfCmd(DataList):
    """One job on ``s_in``: the sizes, the iteration count and whether ``H`` follows."""

    include_filename: ClassVar[str | None] = "ltbf_cmd.h"
    elements: ClassVar[dict] = {
        "n_rx": {"schema": Word32, "description": "antennas N_rx (rows of H, B, X)"},
        "cols": {
            "schema": Word32,
            "description": "columns of H_alpha (8 x connected UEs)",
        },
        "m": {"schema": Word32, "description": "right-hand sides (columns of B, X)"},
        "nit": {"schema": Word32, "description": "CG iterations t"},
        "load_h": {
            "schema": Word32,
            "description": "1: H follows (and Q is formed); 0: keep",
        },
    }


SCHEMA_CLASSES = [LtbfCmd]

#: The register ranges the hardware formats are built for: drop 1 at 32 x 32, 100 MHz, nominal
#: SRS, both regimes (step 2.3's profile, rounded up) -- generous for every array.
CANON_RANGES: dict[str, float] = {
    "H": 0.4, "Q": 4.1, "B": 1.2, "P": 6.0, "R": 1.2, "S": 33.0, "X": 0.9, "T": 51.0,
    "ps": 4300.0, "rz": 150.0, "alpha": 0.37, "beta": 4.4,
}  # fmt: skip


@cache
def hw_formats(W: int, guard: int = 1) -> lf.LtbfFormats:
    """The formats a ``W``-bit engine is built with."""
    return lf.formats_for(int(W), CANON_RANGES, int(guard))


def type_map(f: lf.LtbfFormats, nmax: int, cmax: int) -> dict:
    """Every register and exact accumulator the body names, at the maximum sizes."""
    c = f.cg
    acc = lcg.accumulator_formats(c, nmax)
    return {
        "h_t": f.H, "q_t": f.Q, "t_t": f.T, "b_t": c.B, "p_t": c.P, "r_t": c.R, "s_t": c.S,
        "x_t": c.X, "ps_t": c.ps, "rz_t": c.rz, "alpha_t": c.alpha, "beta_t": c.beta,
        "rzw_t": acc["rzw"],
        "acc_hh_t": acc_format(f.H, f.H, cmax), "acc_qp_t": acc_format(f.Q, c.P, nmax),
        "acc_hp_t": acc_format(f.H, c.P, nmax), "acc_ht_t": acc_format(f.H, f.T, cmax),
        "dot_ps_t": acc["dot_ps"], "dot_rz_t": acc["dot_rz"],
    }  # fmt: skip


@dataclass
class LtbfEngine(FreeRunMod):
    """The long-term CG engine: one sector (or UE) solve per firing."""

    cpp_kernel_name: ClassVar[str | None] = "ltbf_engine"

    nmax: HwParam[int] = 64
    cmax: HwParam[int] = 64
    mmax: HwParam[int] = 16
    R: HwParam[int] = 2
    C: HwParam[int] = 2
    explicit: HwParam[bool] = False
    W: HwParam[int] = 16
    clk: Clock = field(default_factory=lambda: Clock(freq=FREQ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.check_knobs()
        self.formats = hw_formats(int(self.W))
        lf.check_widths(self.formats, int(self.nmax), int(self.cmax))
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=WORD_BW, has_tlast=False
        )
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=WORD_BW, has_tlast=False
        )
        self.add_endpoint(self.s_in)
        self.add_endpoint(self.m_out)
        self._h: tuple[np.ndarray, np.ndarray] | None = None
        self.jobs = 0

    def check_knobs(self) -> None:
        n, c, m, r, cc = (
            int(v) for v in (self.nmax, self.cmax, self.mmax, self.R, self.C)
        )
        for name, v in (("R", r), ("C", cc)):
            if v < 1 or v & (v - 1):
                raise ValueError(f"{name} must be a power of two, got {v}")
        if n % r or c % r or m % cc or n % cc:
            raise ValueError(
                f"need R | nmax, R | cmax, C | mmax, C | nmax: {n, c, m, r, cc}"
            )
        if self.explicit and r != cc:
            raise ValueError("the explicit engine needs R == C")

    def kernel_task(self) -> KernelTask:
        """The hand-written body (``hw/cpp/ltbf_engine_task.h``); sizes and types come from the
        generated ``ltbf_config.h``, so the call takes no template arguments."""
        return KernelTask("ltbf_engine_task", "ltbf_engine_task.h", ("s_in", "m_out"))

    def run_iter(self) -> ProcessGen[None]:
        f = self.formats
        cmd = yield from self.s_in.get_schema(LtbfCmd)
        n, cols, m, nit = int(cmd.n_rx), int(cmd.cols), int(cmd.m), int(cmd.nit)
        if int(cmd.load_h):
            words = yield from self.s_in.get(nwords_max=n * cols)
            hr, hi = unpack_words(words, f.H.W)
            self._h = (hr.reshape(n, cols), hi.reshape(n, cols))
        words = yield from self.s_in.get(nwords_max=n * m)
        br, bi = unpack_words(words, f.cg.B.W)
        assert self._h is not None, "a job without H before any job with it"
        state = lf.solve_stored(
            *self._h, br.reshape(n, m), bi.reshape(n, m), f, nit,
            "explicit" if self.explicit else "factored",
        )  # fmt: skip
        yield from self.m_out.write(pack_words(state.xr, state.xi, f.cg.X.W))
        self.jobs += 1


def pack_words(re: np.ndarray, im: np.ndarray, W: int) -> np.ndarray:
    """One complex value per 64-bit word, row-major: a one-lane ``wf_lanes`` group."""
    re, im = np.ravel(re), np.ravel(im)
    return np.array(
        [lanes.pack_group([int(a)], [int(b)], W) for a, b in zip(re, im, strict=True)],
        dtype=np.uint64,
    )


def unpack_words(words, W: int) -> tuple[np.ndarray, np.ndarray]:
    re, im = [], []
    for w in np.asarray(words, dtype=np.uint64):
        r, i = lanes.unpack_group(int(w), W, 1)
        re.append(r[0])
        im.append(i[0])
    return np.array(re, dtype=np.int64), np.array(im, dtype=np.int64)


# --- the scenario ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One firing's inputs, as floats (quantized by the engine's formats)."""

    h: np.ndarray | None  # N x cols, or None to keep the previous job's H
    b: np.ndarray  # N x m
    nit: int


def scenario_jobs(
    n: int, cols: int, m_joint: int, nit: int, seed: int = 1
) -> list[Job]:
    """AC5's program: a joint solve of a sector (``m_joint`` columns), then the same sector one UE
    (8 columns) at a time with ``H`` kept, then a second sector."""
    from examples.ltbf_cg import ltbf_conformance as cf

    rng = np.random.default_rng(seed)
    spec = cf.CaseSetSpec("hw", 1, "factored", n, cols // 8, m_joint // 8, 16, 1, seed)
    jobs = []
    for sector in range(2):
        ha, b = cf._random_problem(spec, rng)
        jobs.append(Job(ha, b, nit))
        if sector == 0:
            for k in range(m_joint // 8):
                jobs.append(Job(None, b[:, 8 * k : 8 * (k + 1)], nit))
    return jobs


def job_words(job: Job, f: lf.LtbfFormats) -> list[np.ndarray]:
    """The bursts one job puts on ``s_in``: the command, H (if any), B."""
    n, m = job.b.shape
    cols = 0 if job.h is None else job.h.shape[1]
    cmd = LtbfCmd(n_rx=n, cols=cols, m=m, nit=job.nit, load_h=int(job.h is not None))
    out = [np.asarray(cmd.serialize(word_bw=WORD_BW), dtype=np.uint64)]
    if job.h is not None:
        out.append(pack_words(*lf.to_stored(job.h, f.H), f.H.W))
    out.append(pack_words(*lf.to_stored(job.b, f.cg.B), f.cg.B.W))
    return out


def golden(jobs: list[Job], f: lf.LtbfFormats, explicit: bool) -> list[np.ndarray]:
    """The X words each job must produce (the model, H kept across jobs)."""
    out, h = [], None
    for job in jobs:
        if job.h is not None:
            h = lf.to_stored(job.h, f.H)
        br, bi = lf.to_stored(job.b, f.cg.B)
        st = lf.solve_stored(
            *h, br, bi, f, job.nit, "explicit" if explicit else "factored"
        )
        out.append(pack_words(st.xr, st.xi, f.cg.X.W))
    return out


# --- the testbench and the system ---------------------------------------------------------------


@dataclass
class LtbfTB(FreeRunMod):
    """driver -> engine -> sink, walkable so the XSI harness generates from it."""

    potential_targets: ClassVar[frozenset[str]] = frozenset({SEQUENTIAL_XSI_TB})

    nmax: HwParam[int] = 64
    cmax: HwParam[int] = 64
    mmax: HwParam[int] = 16
    R: HwParam[int] = 2
    C: HwParam[int] = 2
    explicit: HwParam[bool] = False
    W: HwParam[int] = 16
    n_cycles: int = 2_000_000
    clk: Clock = field(default_factory=lambda: Clock(freq=FREQ))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dut = LtbfEngine(
            name=f"{self.name}_dut", sim=self.sim, nmax=self.nmax, cmax=self.cmax,
            mmax=self.mmax, R=self.R, C=self.C, explicit=self.explicit, W=self.W, clk=self.clk,
        )  # fmt: skip
        self.driver = StreamDriver(
            sim=self.sim, bitwidth=WORD_BW, in_bundle="vectors/s_in"
        )
        cap = int(self.nmax) * int(self.mmax) + 16
        self.sink = StreamSink(
            sim=self.sim, bitwidth=WORD_BW, out_bundle="vectors/m_out", queue_size=cap
        )
        for c in (self.dut, self.driver, self.sink):
            self.add_comp(c)
        in_if = StreamIF(
            name=f"{self.name}_in_if", sim=self.sim, clk=self.clk, bitwidth=WORD_BW
        )
        in_if.bind(ep_name="master", endpoint=self.driver.stream_ep)
        in_if.bind(ep_name="slave", endpoint=self.dut.s_in)
        self.add_if(in_if)
        out_if = StreamIF(
            name=f"{self.name}_out_if",
            sim=self.sim,
            clk=self.clk,
            bitwidth=WORD_BW,
            depth=cap,
        )
        out_if.bind(ep_name="master", endpoint=self.dut.m_out)
        out_if.bind(ep_name="slave", endpoint=self.sink.stream_ep)
        self.add_if(out_if)
        self.boundary = []


@dataclass
class LtbfSystem(GraphSystem):
    """The engine as a system: the :class:`LtbfTB` graph, the cut the engine alone."""

    nmax: int = 64
    cmax: int = 64
    mmax: int = 16
    R: int = 2
    C: int = 2
    explicit: bool = False
    W: int = 16
    nit: int = 4
    n_cycles: int = 2_000_000
    #: The scenario's sizes (0: the build's maxima): N_rx, columns of H, the joint solve's columns.
    n_run: int = 0
    cols_run: int = 0
    m_run: int = 0

    def knobs(self) -> dict:
        return {
            "nmax": self.nmax, "cmax": self.cmax, "mmax": self.mmax, "R": self.R, "C": self.C,
            "explicit": self.explicit, "W": self.W,
        }  # fmt: skip

    def jobs(self) -> list[Job]:
        return scenario_jobs(
            int(self.n_run or self.nmax), int(self.cols_run or self.cmax),
            int(self.m_run or self.mmax), int(self.nit),
        )  # fmt: skip

    def graph(self) -> LtbfTB:
        self.tb = LtbfTB(
            name="tb", sim=Simulation(), n_cycles=int(self.n_cycles), **self.knobs()
        )
        return self.tb

    def dut_modules(self, g) -> list:
        return [g.dut]

    def write_inputs(self, root) -> None:
        f = hw_formats(int(self.W))
        bursts = [w for job in self.jobs() for w in job_words(job, f)]
        write_burst_bundle(bursts, Path(root) / "vectors" / "s_in")

    def expected(self) -> list[np.ndarray]:
        return golden(self.jobs(), hw_formats(int(self.W)), bool(self.explicit))

    def check(self) -> list[np.ndarray]:
        """The sink's words against the model, job by job."""
        got = np.concatenate(
            [np.asarray(b).ravel() for b in self.tb.sink.words]
        ).astype(np.uint64)
        want = np.concatenate(self.expected())
        np.testing.assert_array_equal(got, want)
        return self.tb.sink.words


# --- generation -----------------------------------------------------------------------------------


def render_config(knobs: dict) -> str:
    """``ltbf_config.h``: the sizes, the tile and every type, from Python."""
    from examples.mimo_cg.hw.common import ap_type

    n, c, m, r, cc = (int(knobs[k]) for k in ("nmax", "cmax", "mmax", "R", "C"))
    f = hw_formats(int(knobs["W"]))
    types = "\n".join(
        f"    typedef {ap_type(v)} {k};" for k, v in type_map(f, n, c).items()
    )
    return f"""// GENERATED by examples/ltbf_cg/hw/engine.py -- do not edit.
#ifndef LTBF_CONFIG_H
#define LTBF_CONFIG_H
#include <ap_fixed.h>
#define LTBF_NMAX {n}
#define LTBF_CMAX {c}
#define LTBF_MMAX {m}
#define LTBF_R {r}
#define LTBF_C {cc}
#define LTBF_EXPLICIT {int(bool(knobs["explicit"]))}
#define LTBF_NB {n // r}
#define LTBF_CB {c // r}
#define LTBF_NC {n // cc}
#define LTBF_MB {m // cc}
struct ltbf_types {{
{types}
}};
#endif
"""


def generate(out_dir: Path, knobs: dict, part: str = "xczu48dr-ffvg1517-2-e") -> Path:
    """Headers, the ``ap_ctrl_none`` top and its csynth TCL for one engine configuration."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.composite_gen import (
        GEN_DIR,
        composite_top_spec,
        render_tcl,
        render_top,
    )
    from waveflow.build.elaborate import elaborate
    from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep

    out_dir = Path(out_dir)
    config = BuildConfig(root_dir=out_dir, params={})
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir="include"))
    dag.add(MemMgrStep(output_dir="include"))
    for cls in SCHEMA_CLASSES:
        dag.add(DataSchemaStep(cls, word_bw_supported=[WORD_BW], include_dir="include"))
    res = dag.run(config)
    bad = [k for k, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")
    inc = out_dir / "include"
    (inc / "ltbf_config.h").write_text(render_config(knobs), encoding="utf-8")
    shutil.copyfile(CPP_DIR / "ltbf_engine_task.h", inc / "ltbf_engine_task.h")
    shutil.copyfile(
        Path(__file__).resolve().parents[3] / "waveflow" / "build" / "wf_lanes.h",
        inc / "wf_lanes.h",
    )
    comp = elaborate(LtbfEngine, dict(knobs), name="ltbf_engine")
    spec = composite_top_spec(comp, width=WORD_BW)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl = out_dir / f"{spec.top_name}.tcl"
    tcl.write_text(render_tcl(spec.top_name, part=part, period_ns=4), encoding="utf-8")
    return tcl
