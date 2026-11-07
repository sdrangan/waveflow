"""build.py -- the system's devices in C++: generated headers, HLS tops, csynth (plan step 5.1).

``plans/ltbf_dse/ltbf_dse.md`` Phase 5.  The four HLS tops of the system (spec §5.1), each a
free-running composite whose leaves' bodies are hand-written (``src/ltbf_tasks.h``,
``src/ltbf_ingest_task.h``) or the framework's (the systolic engine, the vector unit, the in-band
memory streams):

* ``ltbf_sys``    -- :class:`~examples.ltbf_cg_mem_dse.hw.sys_wrap.SysWrap` (``SystolicEngineMem`` + tap + Ctrl),
  built with ``config_compile -pipeline_style flp`` (Vitis 2024.1 crashes on the engine's free-running
  pipeline, plan §14);
* ``ltbf_vec``    -- :class:`~examples.ltbf_cg_mem_dse.hw.vec_wrap.VecWrap` (the vector unit, one build for both
  programs, and its memory front end);
* ``ltbf_ingest`` -- :class:`~examples.ltbf_cg_mem_dse.hw.ingest.IngestDev`;
* ``ltbf_hub``    -- :class:`~examples.ltbf_cg_mem_dse.hw.resp_hub.RespHub`, three sources.

Their parameters are AC9's (``system.SysParams()``: N_rx 64, ``c`` 8, ``Mmax`` 16, ``J`` 9, the formats
profiled on drop 1).  ``src/`` is the only C++ source; ``include/`` and ``gen/`` (each top's ``.tcl``
beside its ``.cpp``) are build output, untracked (``plans/source_layout.md``).

    python -m examples.ltbf_cg_mem_dse.hw.build --through csynth     # codegen, then csynth of every top
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildDag, BuildStep
from waveflow.build.composite_gen import (
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_tcl,
    render_top,
    tcl_path,
)
from waveflow.build.streamutils import MemMgrStep, MemStreamStep
from waveflow.build.system_dag import CsynthStep, _SystemStep
from waveflow.hw.dataschema import DataSchemaStep
from waveflow.hw.mem_stream import MemRCmd, MemWCmd
from waveflow.linalg.build import collect_parts, linalg_headers_dag
from waveflow.simulation.simulation import Simulation
from waveflow.vecunit.build import PART, PERIOD_NS, VecUnitHeaderStep
from waveflow.vecunit.hls import CORE_HEADER, DEFINES, io_traits
from waveflow.vecunit.isa import SCHEMAS as VEC_SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
#: Vitis HLS 2024.1 and the systolic engine (spike 1.2 b, plan §14).
FLP = ("config_compile -pipeline_style flp",)
TOPS = ("ltbf_sys", "ltbf_vec", "ltbf_ingest", "ltbf_hub", "ltbf_hub1")
#: The device gates' systems (``devsys``) and their workspaces under ``xsi_work/``.
DEVICES = ("sys", "vec", "ingest")
WORK_DIR = "xsi_work"


def devices(sim: Simulation | None = None, p: Any = None) -> dict[str, Any]:
    """The four devices at AC9's parameters (or *p*'s, a ``SysParams``), named as their tops."""
    from examples.ltbf_cg_mem_dse.hw import system as sy
    from examples.ltbf_cg_mem_dse.hw.devsys import RespHub1
    from examples.ltbf_cg_mem_dse.hw.ingest import IngestDev
    from examples.ltbf_cg_mem_dse.hw.resp_hub import RespHub
    from examples.ltbf_cg_mem_dse.hw.sys_wrap import SysWrap
    from examples.ltbf_cg_mem_dse.hw.vec_wrap import VecWrap

    sim = sim or Simulation()
    inp = sy.inputs(p or sy.SysParams())
    S = sy.LtbfSystem(inp)
    p, d, cfg = S.p, S.d, S.cfg
    W = p.word_bits
    return {
        "ltbf_sys": SysWrap(name="ltbf_sys", sim=sim, engine_cfg=sy.engine_cfg(S)),
        "ltbf_vec": VecWrap(name="ltbf_vec", sim=sim, vu_cfg=S.vu, word_bits=W),
        "ltbf_ingest": IngestDev(name="ltbf_ingest", sim=sim, dma_cfg=sy.ingest_cfg(S)),
        "ltbf_hub": RespHub(name="ltbf_hub", sim=sim, n_src=3, word_bits=W),
        "ltbf_hub1": RespHub1(name="ltbf_hub1", sim=sim, word_bits=W),
        "_formats": (cfg.a, cfg.bc),
        "_vu": S.vu,
        "_word_bits": W,
        "_d": d,
    }


def own_schemas() -> tuple[type, ...]:
    from examples.ltbf_cg_mem_dse.hw import records as rec
    from examples.ltbf_cg_mem_dse.hw.vec_wrap import StorePlan, VecMemJob

    return (rec.Resp, rec.SysCfg, rec.SysStatus, rec.IngestCfg, rec.IngestStatus, VecMemJob,
            StorePlan)  # fmt: skip


def gen_headers(root: Path, devs: dict) -> None:
    """Every header the four tops include, into ``include/``."""
    W = int(devs["_word_bits"])
    words = sorted({32, 64, W})
    eng = collect_parts(devs["ltbf_sys"])
    vu = devs["_vu"]
    schemas = tuple(dict.fromkeys(s for s in (*eng.schemas, *VEC_SCHEMAS, *own_schemas())
                                  if s not in (MemRCmd, MemWCmd)))  # fmt: skip
    dag = linalg_headers_dag((*eng.traits, io_traits(vu)), (*eng.bodies, CORE_HEADER), INCLUDE_DIR,
                             words, schemas)  # fmt: skip
    dag.add(VecUnitHeaderStep([vu], INCLUDE_DIR))
    dag.add(MemMgrStep(output_dir=INCLUDE_DIR))
    dag.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in (MemRCmd, MemWCmd):
        dag.add(
            DataSchemaStep(
                cls, word_bw_supported=words, include_dir=INCLUDE_DIR, framed=True
            )
        )
    res = dag.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = {k: r.message for k, r in res.items() if not r.success}
    if bad:
        raise RuntimeError(f"header generation failed: {bad}")


def gen_top(root: Path, mod, W: int, **tcl) -> str:
    spec = composite_top_spec(mod, width=W)
    gen = root / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    (gen / f"{spec.top_name}.cpp").write_text(render_top(spec), encoding="utf-8")
    tcl_path(root, spec.top_name).write_text(
        render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS, **tcl),
        encoding="utf-8",
    )
    return spec.top_name


def generate(root: Path = ROOT, p: Any = None) -> list[str]:
    """Headers, the four tops and their ``.tcl`` (at *p*, else AC9's): Python only, seconds."""
    devs = devices(p=p)
    gen_headers(root, devs)
    W = int(devs["_word_bits"])
    return [
        gen_top(root, devs["ltbf_sys"], W, solution_config=FLP),
        gen_top(root, devs["ltbf_vec"], W, defines=DEFINES),
        gen_top(root, devs["ltbf_ingest"], W),
        gen_top(root, devs["ltbf_hub"], W),
        gen_top(root, devs["ltbf_hub1"], W),
    ]


@dataclass(kw_only=True)
class TurnaroundStep(_SystemStep):
    """``<prefix>mem_turnaround``: Shared Mem's turnaround fitted from a traced run of the systolic
    device alone on it (:func:`add_turnaround`) and filed in ``calib/`` (:func:`file_mem_turnaround`)."""

    description = "Fit Shared Mem's turnaround from the systolic device's back-to-back reads."
    step_name: ClassVar[str] = "mem_turnaround"
    stamped: ClassVar[bool] = False
    top: str = ""
    span: int = 0
    rate: int = 2

    @property
    def consumes(self) -> list:  # type: ignore[override]
        return [self.a("report")]

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.a("mem_turnaround"): self.work / "mem_turnaround.json"}

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        import json

        work = self.work_dir(config)
        fit = file_mem_turnaround(work / f"{self.top}_trace.vcd", f"{self.top}.u_ltbf_sys_sub.u_ltbf_sys",
                                  "m_axi_gmem0", self.rate, spans=(self.span,))  # fmt: skip
        out = work / "mem_turnaround.json"
        out.write_text(json.dumps({str(k): v for k, v in fit.items()}, indent=1) + "\n", encoding="utf-8")
        return {self.a("mem_turnaround"): out}


@dataclass
class LtbfCodegenStep(BuildStep):
    """``codegen``: :func:`generate`.  Never fresh: its inputs are Python the DAG cannot see, and
    csynth after it decides by content."""

    description = "Generate the headers, the four device tops and their .tcl."
    params: ClassVar[dict] = {}
    produces: ClassVar[dict] = {"include": Path(INCLUDE_DIR), "gen": Path(GEN_DIR)}
    #: The system's parameters (``SysParams``; AC9's when ``None``).
    sysp: Any = None

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool:
        return False

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        root = Path(config.root_dir)
        generate(root, self.sysp)
        return {"include": root / INCLUDE_DIR, "gen": root / GEN_DIR}


def build_dag() -> BuildDag:
    """``codegen``, then a csynth a top."""
    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen"))
    for top in TOPS:
        dag.add(CsynthStep(top=top))
    return dag


def device_dag(
    names=DEVICES, work_dir=WORK_DIR, trace: bool = False, trace_bus: bool = False,
    p=None, shared: bool = False, tag: str = "",
) -> BuildDag:
    """``codegen``, then the framework's system flow for each device's gate system
    (``devsys.<name>_system``): csynth of its tops, the system RTL, the scenario, pysim, XSI with the
    host's C++ twin, and ``compare`` (RTL against pysim), each prefixed ``<name>_``.

    *p* (``SysParams``): the devices at *p*'s build (``codegen`` at its HLS configuration).
    *shared*: each device on Shared Mem as the system builds it (``sys`` and ``vec``), its timing
    measured first (:func:`add_memcal`) -- to time a device as the system runs it.  *tag*: added to
    each device's prefix, top and workspace, so several variants share a DAG and a root."""
    from examples.ltbf_cg_mem_dse.hw import devsys
    from waveflow.build.system_dag import add_system_steps

    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen", sysp=p))
    cals: dict = {}
    for name in names:
        build = getattr(devsys, f"{name}_system")
        sysm = build(p=p, shared=True) if shared else (build(p=p) if p is not None else build())
        add_system_steps(dag, sysm, work_dir=work_dir, top=f"ltbf_{name}{tag}_top",
                         prefix=f"{name}{tag}_", workspace=f"ltbf_{name}{tag}", trace=trace,
                         trace_bus=trace_bus)  # fmt: skip
        if shared:
            from examples.ltbf_cg_mem_dse.hw import system as sy

            S = sy.LtbfSystem(sy.inputs(p or sy.SysParams()))
            key = (S.bank_span, S.p.mem_rate)
            if key not in cals:
                cals[key] = add_memcal(dag, S.bank_span,
                                       f"umemcal{S.bank_span.bit_length() - 1}r{S.p.mem_rate}_",
                                       work_dir, rate=S.p.mem_rate)  # fmt: skip
            dag.require(f"{name}{tag}_pysim", [cals[key]])
    return dag


def system_dag(
    work_dir=WORK_DIR, trace: bool = False, trace_bus: bool = False
) -> BuildDag:
    """``codegen``, then the framework's system flow for AC9's system (step 5.2): ``system.build`` at
    :data:`~examples.ltbf_cg_mem_dse.hw.system.AC9` for the RTL flow -- csynth of its tops, the system RTL,
    the scenario (the C++ host's, from a reference pysim run), pysim, and XSI with ``hw/ltbf_host.h``,
    each prefixed ``ltbf_``; before pysim, Shared Mem measured in its own harness (``umemcal_``) and
    its timing filed in ``calib/``.  The Resp FIFO's order is timing's, so the gate (the test) compares what
    must agree word for word rather than the flow's ``ltbf_compare`` (plan §9 e)."""
    from examples.ltbf_cg_mem_dse.hw import system as sy
    from waveflow.build.system_dag import add_system_steps

    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen"))
    hw, S, _host = sy.build(sy.inputs(sy.AC9), rtl=True)
    add_system_steps(dag, hw, work_dir=work_dir, top="ltbf_top", prefix="ltbf_", workspace="ltbf",
                     trace=trace, trace_bus=trace_bus)  # fmt: skip
    # Shared Mem alone first (plans/unit_harness.md Stage 4c): its RTL reads cost a cycle more a word
    # than pysim's plain memory, so the system's pysim loads the timing measured here.
    dag.require("ltbf_pysim", [add_memcal(dag, S.mem_span, "umemcal_", work_dir)])
    return dag


#: Shared Mem's spans in the DSE's systems (a power of two each): N_rx 64, 256 and 1024 at AC9's
#: other parameters.  A memory's timing record is filed under its identity, its span included.
MEM_SPANS = (1 << 17, 1 << 19, 1 << 21)


def add_memcal(dag: BuildDag, span: int, prefix: str, work_dir=WORK_DIR, rate: int = 2) -> str:
    """Shared Mem of *span* words in its own harness, through XSI, and its timing fitted and filed
    in ``calib/`` (``MemCalibStep``); returns the record's artifact.  *rate*: the memory's cycles a
    read word at RTL (``system.shared_mem``; 1, the pipelined leaf)."""
    from examples.ltbf_cg_mem_dse.hw import system as sy
    from waveflow.build.phase_calib import MemCalibStep
    from waveflow.build.system_dag import add_system_steps
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.clock import Clock
    from waveflow.hw.harness import memory_harness

    clk = Clock(freq=sy.F_HOST)
    uncal = memory_harness(lambda sim: sy.shared_mem(sim, span, clk, calibrated=False, rate=rate),
                           sy.mem_script("calib", span), n_cycles=40_000)  # fmt: skip
    xsi = add_system_steps(
        dag, uncal, work_dir=work_dir, top="ltbf_mem_unit", prefix=prefix
    )
    mem_cal = MemCalibStep(
        sysm=uncal, work=xsi.work, prefix=prefix, store=ModuleStore(sy.CALIB_DIR)
    )
    dag.add(mem_cal)
    return mem_cal.a("mem_timing")


def add_turnaround(dag: BuildDag, p, mem_timing: str, work_dir=WORK_DIR) -> str | None:
    """Shared Mem's turnaround at *p*'s bank span and rate (:func:`file_mem_turnaround`), measured
    once a machine: when the memory already has a record in ``calib/``, nothing is added (``None``);
    else the systolic device alone on Shared Mem (``devsys.sys_system(shared=True)``), traced, after
    the memory's timing (*mem_timing*, :func:`add_memcal`'s artifact), and a step filing the fit --
    the artifact to require before a pysim."""
    from examples.ltbf_cg_mem_dse.hw import devsys
    from examples.ltbf_cg_mem_dse.hw import system as sy
    from waveflow.build.system_dag import add_system_steps
    from waveflow.calib.mem_timing import stored_mem_turnaround
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.clock import Clock
    from waveflow.simulation.simulation import Simulation

    S = sy.LtbfSystem(sy.inputs(p))
    span, rate = S.bank_span, S.p.mem_rate
    mem = sy.shared_mem(Simulation(), span, Clock(freq=sy.F_HOST), rate=rate)
    if stored_mem_turnaround(ModuleStore(sy.CALIB_DIR), mem) is not None:
        return None
    tag = f"turn{span.bit_length() - 1}r{rate}"
    sysm = devsys.sys_system(p=p, shared=True)
    xsi = add_system_steps(dag, sysm, work_dir=work_dir, top=f"ltbf_{tag}_top", prefix=f"{tag}_",
                           workspace=f"ltbf_{tag}", trace=True)  # fmt: skip
    dag.require(f"{tag}_scenario", [mem_timing])
    step = TurnaroundStep(sysm=sysm, work=xsi.work, prefix=f"{tag}_", top=f"ltbf_{tag}_top",
                          span=span, rate=rate)  # fmt: skip
    dag.add(step)
    return step.a("mem_turnaround")


def variant_dag(variants: dict, work_dir=WORK_DIR, trace: bool = False,
                timeout: int = 4 * 3600) -> BuildDag:
    """``codegen`` at the variants' one HLS configuration, then the system flow for each variant
    (``{tag: SysParams}``), prefixed ``<tag>_`` with its own top ``ltbf_top_<tag>`` and workspace
    ``<tag>``, each pysim after its Shared Mem's timing is measured (:func:`add_memcal` at its bank
    span and rate).  The variants must differ only in what generates no HLS (``mem_banks``,
    ``mem_rate``, the workload), so their tops' csynth is shared; run it in a root of its own
    (``plans/dse_cg.md`` step 1a), not the example's, whose sources are AC9's.  *timeout*: the XSI
    run's, in seconds (the system's run with a slot is close to an hour on a quiet machine)."""
    from examples.ltbf_cg_mem_dse.hw import system as sy
    from waveflow.build.system_dag import add_system_steps

    first = next(iter(variants.values()))
    dag = BuildDag()
    dag.add(LtbfCodegenStep(name="codegen", sysp=first))
    cals: dict = {}
    turns: dict = {}
    for tag, p in variants.items():
        hw, S, _host = sy.build(sy.inputs(p), rtl=True)
        add_system_steps(dag, hw, work_dir=work_dir, top=f"ltbf_top_{tag}", prefix=f"{tag}_",
                         workspace=tag, trace=trace, timeout=timeout)  # fmt: skip
        key = (S.bank_span, p.mem_rate)
        if key not in cals:
            cals[key] = add_memcal(dag, S.bank_span, f"umemcal{S.bank_span.bit_length() - 1}r{p.mem_rate}_",
                                   work_dir, rate=p.mem_rate)  # fmt: skip
            turns[key] = add_turnaround(dag, p, cals[key], work_dir)
        dag.require(f"{tag}_pysim", [cals[key]] + ([turns[key]] if turns[key] else []))
    return dag


def rtl_against_pysim(work: Path, tolerance: float = 0.05) -> dict:
    """A system run's RTL against its pysim, in workspace *work* (after ``<prefix>system_xsi``), as
    ``test_system_at_rtl`` checks it (plan §9 e: the Resp FIFO's order is timing's): each engine's
    Job FIFO writes, the Ingest DMA's Ctrl writes and the O-RU's symbols word for word, the Resp
    records per source in order, every burst of data the host read or wrote as a multiset (its
    one-word credit writes batch as timing has it), and pysim's end within *tolerance* of RTL's.
    Returns ``{"match", "endpoints": {name: bool}, "pysim_cycles", "rtl_cycles"}``."""
    import json
    import re

    from examples.ltbf_cg_mem_dse.hw.records import Resp
    from waveflow.utils.burst_io import read_burst_bundle

    def bursts(d: Path) -> list[tuple]:
        # An endpoint the run never wrote has no bundle (no slot: no symbols).
        if not (d / "words.bin").exists():
            return []
        return [tuple(int(w) for w in b) for b in read_burst_bundle(d)]

    def by_source(d: Path) -> dict:
        out: dict = {}
        for burst in bursts(d):
            r = Resp().deserialize(list(burst), word_bw=64)
            out.setdefault(int(r.src), []).append((int(r.status), int(r.tx_id), int(r.seq)))
        return out

    py, rtl = Path(work) / "pysim_traces", Path(work) / "traces"
    eps = {ep: bursts(py / ep) == bursts(rtl / ep) for ep in ("qjob_sys", "qjob_vec", "ing_ctrl", "qsym")}
    eps["qresp"] = by_source(py / "qresp") == by_source(rtl / "qresp")
    eps["bus"] = sorted(x for x in bursts(py / "bus") if len(x) > 1) == sorted(
        x for x in bursts(rtl / "bus") if len(x) > 1)
    t_py = int(json.loads((Path(work) / "pysim.json").read_text())["cycles"])
    out = json.loads((Path(work) / "report.json").read_text())["output"]
    t_rtl = int(re.search(r"DONE done=1 cycles=(\d+)", out).group(1))
    eps["end"] = abs(t_py / t_rtl - 1) <= tolerance
    return {"match": all(eps.values()), "endpoints": eps, "pysim_cycles": t_py, "rtl_cycles": t_rtl}


def file_mem_turnaround(vcd, scope: str, bundle: str, rate: int, spans=(1 << 16, 1 << 17)) -> dict:
    """Shared Mem's turnaround (``plans/dse_cg.md`` step 1f), from a traced run of a device on it: the
    back-to-back read bursts of the ``m_axi`` *bundle* of the kernel at *scope* in *vcd* (the
    systolic feeder's, which requests a tile's panels ahead), fitted against each memory's stored
    timing and filed beside it in ``calib/`` -- for Shared Mem at *rate* and each bank span in
    *spans* (the leaf is the same RTL whatever its span).  Returns ``{span: fit}``.  The writers never
    meet back to back here (they wait on their data), so writes keep none."""
    from vcdvcd import VCDVCD

    from examples.ltbf_cg_mem_dse.hw import system as sy
    from waveflow.calib.mem_timing import (
        back_to_back_rows,
        fit_turnaround,
        mem_turnaround_record,
        stored_mem_timing,
    )
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.clock import Clock
    from waveflow.simulation.simulation import Simulation
    from waveflow.utils.vcd import VcdParser

    parser = VcdParser(VCDVCD(str(vcd)))
    clock = f"{scope}.ap_clk"
    parser.add_signal(clock)
    sigs, _bits = parser.add_aximm_signals(prefix=f"{scope}.{bundle}_", dir="read")
    _w, reads, period = parser.extract_aximm_bursts(clock, sigs)
    rows = back_to_back_rows([], reads, period)
    store, out = ModuleStore(sy.CALIB_DIR), {}
    for span in spans:
        mem = sy.shared_mem(Simulation(), span, Clock(freq=sy.F_HOST), rate=rate)
        timing = stored_mem_timing(store, mem)
        if timing is None:
            raise RuntimeError(f"no stored timing for Shared Mem of {span} words at rate {rate}: "
                               "measure it first (add_memcal)")
        fit = fit_turnaround(rows, timing.payload)
        rec, ident = mem_turnaround_record(mem, fit, workload=f"{Path(vcd).name}:{bundle}",
                                           period_ns=1e9 / sy.F_HOST, tool="vitis 2025.1")
        store.append(rec, identity=ident)
        out[span] = fit
    return out


def memcal_dag(spans=MEM_SPANS, work_dir=WORK_DIR) -> BuildDag:
    """Shared Mem's timing at every span the DSE's systems use (:func:`add_memcal`); the memory is
    framework RTL, so no codegen."""
    dag = BuildDag()
    for span in spans:
        add_memcal(dag, span, f"umemcal{span.bit_length() - 1}_", work_dir)
    return dag


if __name__ == "__main__":
    from waveflow.build.cli import run_dag_cli

    run_dag_cli(lambda a: build_dag(), description=__doc__.splitlines()[0], default_through=None,
                root_dir=ROOT,
                extra_args=[(("--synth",), {"choices": ("build", "check"), "default": "build"})],
                params_from_args=lambda a: {"synth": a.synth})  # fmt: skip
