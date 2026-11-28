"""bram_window_contention.py — several readers on one BRAM window over the crossbar
(``plans/bram_fullrate_port.md``, Stage 4: the contention gate, the turnaround, the crossbar's settings).

The emitter example's ring is read by several units at once, each through a ``MemRStream`` (16-beat
bursts) over the crossbar.  This is that, alone: *n* ``MemRStream`` kernels (csynth'd for ``xczu48dr``
at 4 ns, one top instanced *n* times) reading a 1,024-word block each of one window over AMD's
``axi_crossbar``, a ``MemWStream`` on one more crossbar port filling the memory first, and each reader's
command arriving after the fill (``StreamDriver.start_gap_cycles``).  At RTL the window is its memory's
slot -- :func:`~waveflow.hw.mm_bram.window_memory`, the front, the leaf and the BRAM: ``system_top`` does
not wire a window's port B yet (``plans/bram_window_port_b.md``), and port A does not see it.  In pysim
the same system runs with the memory, and again with a :class:`~waveflow.hw.mm_bram.MemSlaveBramWindow`
in its place loading the same record -- the window's own model against the same RTL.

What it measures (all xsim 2025.1, 250 MHz):

* **each block's read time** -- from the readers' common start to the last word of a reader's block
  at its sink: RTL (the sink's per-word cycles) against pysim (a :class:`TimedSink`'s);
* **the turnaround** -- the cycles the front stays busy between one burst and the next already waiting,
  from a traced run of one reader (:func:`~waveflow.calib.mem_timing.back_to_back_rows`,
  :func:`~waveflow.calib.mem_timing.fit_turnaround`), filed beside the memory's timing;
* **the crossbar's latency** -- a request's cycles from a reader's AR to the front's, and a read
  beat's from the front's R to the reader's, from the same trace.

    python -m waveflow.calib.fixtures.bram_window_contention [--work <dir>] [--platform-dir <dir>]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from waveflow.hw.mm_bram import WINDOW_MEMORY_FREQ, WINDOW_MEMORY_WORDS, window_memory
from waveflow.simulation.stream_tb import StreamSink

PART = "xczu48dr-ffvg1517-2-e"
PERIOD_NS = 4.0
#: Words a reader reads: one channel's block of the emitter example's ring (128 bits a word).
BLOCK = 1024
#: The readers' command arrives this many cycles in: after the writer has filled the whole memory.
START = 3000
COUNTS = (2, 3, 4)
#: The pysim crossbar's settings at 250 MHz on xczu48dr, for this crossbar (AMD axi_crossbar, SAMD, as
#: ``AxiXbarConfig.from_crossbar`` generates it), measured from the one-reader trace
#: (:func:`crossbar_latency`, xsim 2025.1): a request reaches the front 2 cycles after the reader
#: presents it, all of it before the request contends for the slave (travel), and a read beat reaches
#: the reader 1 cycle after the front sends it.  No record type holds these yet (see
#: ``docs/guide/interface/axi_mm/crossbar.md``).
XBAR_SETTINGS = {"xbar_latency": 2.0, "xbar_travel": 2.0, "xbar_return": 1.0}


@dataclass
class TimedSink(StreamSink):
    """A :class:`~waveflow.simulation.stream_tb.StreamSink` that also notes, in pysim, when each burst
    arrived and how long it was (:attr:`arrivals`: ``[(seconds, words)]``).  Its C++ twin is the
    sink's (``AxisSlave``), which records every word's cycle in its ``out_bundle``."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.arrivals: list[tuple[float, int]] = []

    def rx_proc(self, words):
        self.arrivals.append((self.env.now, len(words)))
        yield from super().rx_proc(words)


def generate_readers(root, dw: int) -> None:
    """The ``MemRStream`` top at *dw* bits, for the system flow's csynth under *root*: the headers
    (the fixed task body, the command schema at *dw*), ``gen/mem_r_stream.cpp`` and its ``.tcl``
    (``xczu48dr`` at 4 ns).  The schema is generated at *dw* here rather than widening
    ``mem_stream.WORD_BW_SUPPORTED``, which would rewrite every example's generated headers."""
    from waveflow.build.build import BuildConfig, BuildDag
    from waveflow.build.composite_gen import (
        GEN_DIR,
        INCLUDE_DIR,
        composite_top_spec,
        render_tcl,
        render_top,
        tcl_path,
    )
    from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep
    from waveflow.hw.dataschema import DataSchemaStep
    from waveflow.hw.mem_stream import SCHEMA_CLASSES, MemRStream, MemWStream
    from waveflow.simulation.simulation import Simulation

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=[int(dw)], include_dir=INCLUDE_DIR))
    res = inner.run(BuildConfig(root_dir=root, params={}), force=True)
    bad = [n for n, r in res.items() if not r.success]
    if bad:
        raise RuntimeError(f"generate_readers: {bad}")
    (root / GEN_DIR).mkdir(exist_ok=True)
    for cls in (MemRStream, MemWStream):
        leaf = cls(name=cls.cpp_kernel_name, sim=Simulation(), mem_dwidth=int(dw))
        leaf.cmd_headers = (leaf.Cmd.resolved_include_filename(),)
        spec = composite_top_spec(leaf, width=int(dw))
        for path, text in ((root / GEN_DIR / f"{spec.top_name}.cpp", render_top(spec)),
                           (tcl_path(root, spec.top_name),
                            render_tcl(spec.top_name, part=PART, period_ns=PERIOD_NS))):
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")


def fill_data(dw: int) -> np.ndarray:
    """What the writer fills the memory with: :data:`~waveflow.hw.mm_bram.WINDOW_MEMORY_WORDS`
    distinct words of *dw* bits (``(n, k)`` chunks above 64)."""
    from waveflow.calib.fixtures.bram_window import _data

    return _data(WINDOW_MEMORY_WORDS, dw, 300)


def contention_system(n: int, leaf: str = "mm_bram_port_pipe", dw: int = 128, *, store_dir=None,
                      window: bool = False, xbar_latency: float = 4.0, xbar_travel: float = 2.0,
                      xbar_return: float = 0.0):
    """*n* readers on one window of leaf *leaf* at *dw* bits, over the crossbar (see the module
    docstring), as ``HwSystem(sim, dut)``.  *store_dir*: where the memory's (or the window's) timing
    record is loaded from.  *window*: a :class:`~waveflow.hw.mm_bram.MemSlaveBramWindow` in the
    memory's place -- pysim only.  *xbar_latency* / *xbar_travel* / *xbar_return*: the pysim crossbar's
    ``latency_init`` / ``latency_travel`` / ``latency_read_return``."""
    from waveflow.hw.clock import Clock
    from waveflow.hw.hw_system import DutTop, HwSystem
    from waveflow.hw.interface import StreamIF
    from waveflow.hw.mem_stream import MemRStream, MemWStream, MRCmd, MWCmd
    from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
    from waveflow.hw.mm_bram import MemSlaveBramWindow
    from waveflow.simulation.simulation import Simulation
    from waveflow.simulation.stream_tb import StreamDriver
    from waveflow.utils.burst_io import write_burst_bundle

    sim = Simulation()
    clk = Clock(freq=WINDOW_MEMORY_FREQ)
    span = WINDOW_MEMORY_WORDS * dw // 8
    if window:
        ring = MemSlaveBramWindow(name="ring", sim=sim, mem_dwidth=dw, nelem=WINDOW_MEMORY_WORDS,
                                  window=span, bram_leaf=leaf, clk=clk, calib_dir=store_dir)
        slave = ring.s_mem
    else:
        ring = window_memory(leaf, dw, sim, name="ring", calib_dir=store_dir)
        ring.alloc(int(ring.nwords_tot))
        slave = ring.s_mm
    k_chunks = 1 if dw <= 64 else dw // 64

    def stream(name, master, slave) -> None:
        s = StreamIF(name=name, sim=sim, clk=clk, bitwidth=dw)
        s.bind(ep_name="master", endpoint=master)
        s.bind(ep_name="slave", endpoint=slave)

    # The fill: a MemWStream kernel inside the cut, its command and data from two drivers (a scripted
    # host would make the run a host's, whose scenario the flow gives the host alone).
    wr = MemWStream(name="wr", sim=sim, mem_dwidth=dw, clk=clk)
    wcmd = StreamDriver(name="wcmd", sim=sim, bitwidth=dw, in_bundle="vectors/wcmd")
    wdat = StreamDriver(name="wdat", sim=sim, bitwidth=dw, in_bundle="vectors/wdat")
    stream("wcmd_if", wcmd.stream_ep, wr.s_cmd)
    stream("wdat_if", wdat.stream_ep, wr.s_in)
    xbar = AXIMMCrossBarIF(name="xbar", sim=sim, clk=clk, nports_master=n + 1, nports_slave=1,
                           bitwidth=dw, latency_init=xbar_latency, latency_travel=xbar_travel,
                           latency_read_return=xbar_return)
    xbar.bind("master_0", wr.m_mem)
    readers, sinks = [], []
    for k in range(n):
        r = MemRStream(name=f"rd{k}", sim=sim, mem_dwidth=dw, clk=clk)
        d = StreamDriver(name=f"cmd{k}", sim=sim, bitwidth=dw, in_bundle=f"vectors/cmd{k}",
                         start_gap_cycles=START)
        s = TimedSink(name=f"out{k}", sim=sim, bitwidth=dw, out_bundle=f"vectors/out{k}",
                      queue_size=BLOCK)
        stream(f"cmd{k}_if", d.stream_ep, r.s_cmd)
        stream(f"out{k}_if", r.m_out, s.stream_ep)
        xbar.bind(f"master_{k + 1}", r.m_mem)
        readers.append(r)
        sinks.append(s)
    xbar.bind("slave_0", slave)
    assign_address_ranges([slave], [(0, span)])
    dut = DutTop.of(f"bwc{n}", sim, comps=[wr, *readers, ring], ifs=[xbar])
    rcmd = [np.asarray(MRCmd(addr=0, len=BLOCK).serialize(word_bw=dw), dtype=np.uint64)]
    wcmd_w = [np.asarray(MWCmd(addr=0, len=WINDOW_MEMORY_WORDS).serialize(word_bw=dw),
                         dtype=np.uint64)]
    data = fill_data(dw)
    # One burst for the whole fill: the pysim MemWStream takes one burst a command (four 256-word
    # bursts wrote 256 words in pysim, 1,024 at RTL), the RTL counts words across bursts.
    bursts = [data]

    def inputs(root) -> None:
        v = Path(root) / "vectors"
        write_burst_bundle(wcmd_w, v / "wcmd", word_chunks=k_chunks)
        write_burst_bundle(bursts, v / "wdat", word_chunks=k_chunks)
        for k in range(n):
            write_burst_bundle(rcmd, v / f"cmd{k}", word_chunks=k_chunks)

    n_cycles = START + n * (BLOCK // 16) * 40 + 4000
    sysm = HwSystem(sim, dut, n_cycles=n_cycles, inputs=inputs,
                    fresh=lambda: contention_system(n, leaf, dw, store_dir=store_dir, window=window,
                                                    xbar_latency=xbar_latency,
                                                    xbar_travel=xbar_travel,
                                                    xbar_return=xbar_return))
    sysm.xbar, sysm.ring, sysm.writer, sysm.readers, sysm.sinks = xbar, ring, wr, readers, sinks
    return sysm


def pysim_read_times(sysm) -> list[float]:
    """Run *sysm* in pysim; per reader, the cycles from :data:`START` to its block's last word."""
    sysm.run()
    per = 1.0 / WINDOW_MEMORY_FREQ
    out = []
    for s in sysm.sinks:
        words = sum(w for _t, w in s.arrivals)
        if words != BLOCK:
            raise RuntimeError(f"{s.name}: {words} of {BLOCK} words in pysim")
        out.append(s.arrivals[-1][0] / per - START)
    return out


def rtl_read_times(work, n: int) -> list[int]:
    """Per reader, the cycles from :data:`START` to its block's last word at RTL: its sink's per-word
    cycles (``vectors/out<k>/cycles.bin`` of the system run)."""
    run = Path(work) / "xsi_run" / "vectors"
    out = []
    for k in range(n):
        cyc = np.fromfile(run / f"out{k}" / "cycles.bin", dtype=np.uint64)
        if len(cyc) != BLOCK:
            raise RuntimeError(f"out{k}: {len(cyc)} of {BLOCK} words at RTL")
        out.append(int(cyc[-1]) - START)
    return out


def _read_bursts(work, n: int, group: str, sub: str):
    """The read bursts on the AXI4 group *group* (``si<k>_axi`` / ``mi0_axi``) of sub-top *sub*
    (``u_rd0_sub``, ``u_ring_sub``: a traced run dumps each sub-top's wiring) in the traced run of *n*
    readers in *work*, and the clock period: :meth:`~waveflow.utils.vcd.VcdParser.extract_aximm_bursts`."""
    from vcdvcd import VCDVCD

    from waveflow.utils.vcd import VcdParser

    work = Path(work)
    top = work.name.split("_", 1)[1] if "_" in work.name else work.name
    parser = VcdParser(VCDVCD(str(work / f"{top}_trace.vcd")))
    scope = f"{top}.{sub}"
    clock = f"{scope}.ap_clk"
    parser.add_signal(clock)
    sigs, _bits = parser.add_aximm_signals(prefix=f"{scope}.{group}_", dir="read")
    _w, reads, period = parser.extract_aximm_bursts(clock, sigs)
    return reads, period


def fit_window_turnaround(work, n: int, leaf: str, dw: int, store_dir, *, file: bool = False) -> dict:
    """The window memory's turnaround (:func:`~waveflow.calib.mem_timing.fit_turnaround`) from the
    traced run of *n* readers in *work*: the back-to-back read bursts of reader 0 (crossbar SI 1; an
    HLS ``m_axi`` master requests ahead), against the memory's stored timing in *store_dir*.  With
    *file*, filed there beside it (``mem_turnaround_record``)."""
    from waveflow.calib.mem_timing import (
        back_to_back_rows,
        fit_turnaround,
        mem_turnaround_record,
        stored_mem_timing,
        stored_mem_turnaround,
    )
    from waveflow.calib.record_store import ModuleStore

    reads, period = _read_bursts(work, n, "si1_axi", "u_rd0_sub")
    rows = back_to_back_rows([], reads, period)
    store = ModuleStore(store_dir)
    mem = window_memory(leaf, dw)
    timing = stored_mem_timing(store, mem)
    if timing is None:
        raise RuntimeError(f"no stored timing for window_memory({leaf!r}, {dw}): calibrate it first")
    fit = fit_turnaround(rows, timing.payload)
    if file:
        rec, ident = mem_turnaround_record(mem, fit, workload=f"bwc{n}:si1_axi", part=PART,
                                           period_ns=PERIOD_NS, tool="vitis 2025.1")
        newest = stored_mem_turnaround(store, mem)
        if newest is None or newest.payload != rec.payload:
            store.append(rec, identity=ident)
    return fit


def crossbar_latency(work, n: int = 1) -> dict:
    """The crossbar's cycles, from the traced run of one reader (*n* = 1, so every read at the front
    is that reader's), read off the handshake lines themselves:

    * ``request`` -- from the reader's first ARVALID (crossbar SI 1) to the crossbar's first ARVALID at
      the front (MI 0), uncontended: the arbitration and forwarding, all before the request reaches
      the slave (the pysim crossbar's ``latency_travel``, and with nothing more added while the slave
      is held, its ``latency_init``);
    * ``return`` -- from each burst's RLAST at the front to the same burst's RLAST at the reader, the
      median over the bursts (``latency_read_return``)."""
    from vcdvcd import VCDVCD

    if n != 1:
        raise ValueError("the crossbar's latency is read from a one-reader run")
    work = Path(work)
    top = work.name.split("_", 1)[1] if "_" in work.name else work.name
    v = VCDVCD(str(work / f"{top}_trace.vcd"))
    si, mi = f"{top}.u_rd0_sub.si1_axi_", f"{top}.u_ring_sub.mi0_axi_"
    clk = [t for t, x in v[f"{top}.u_rd0_sub.ap_clk"].tv if x == "1"]
    t0, period = clk[0], clk[1] - clk[0]

    def rises(sig: str) -> list[int]:
        tv = v[sig].tv
        return [round((t - t0) / period) for (t, x), (_tp, xp) in zip(tv[1:], tv[:-1])
                if x == "1" and xp != "1"]

    request = rises(f"{mi}ARVALID")[0] - rises(f"{si}ARVALID")[0]
    last_si, last_mi = rises(f"{si}RLAST"), rises(f"{mi}RLAST")
    if len(last_si) != len(last_mi):
        raise RuntimeError(f"{len(last_si)} bursts end at the reader, {len(last_mi)} at the front")
    ret = [a - b for a, b in zip(last_si, last_mi)]
    return {"request": float(request), "return": float(np.median(ret)),
            "return_spread": [min(ret), max(ret)], "bursts": len(ret)}


def tag(n: int, leaf: str, dw: int) -> str:
    from waveflow.calib.fixtures.bram_window import LEAF_TAG

    return f"bwc{n}{LEAF_TAG[leaf]}{dw}"


def add_contention(dag, n: int, leaf: str, dw: int, *, work_dir, store_dir, trace: bool = False,
                   **xbar) -> str:
    """Add one system run (its prefix is returned): the readers' top csynth'd for *dw* (one csynth a
    width: the system's prefix's csynth when this is the first system at it)."""
    from waveflow.build.system_dag import add_system_steps

    t = tag(n, leaf, dw)
    sysm = contention_system(n, leaf, dw, store_dir=store_dir, **xbar)
    add_system_steps(dag, sysm, work_dir=work_dir, top=f"{t}_top", prefix=f"{t}_", trace=trace,
                     trace_bus=trace)
    return f"{t}_"


#: The runs the fixture makes: ``(readers, leaf, bits)`` -- one reader, traced, for each leaf and
#: width (the turnaround of each window memory, and from the 128-bit pipelined one the crossbar's
#: latency), and the contention counts on the full-rate leaf at 128 bits.
TURNAROUND_RUNS = [(1, leaf, dw) for dw in (128, 512) for leaf in ("mm_bram_port", "mm_bram_port_pipe")]
CONTENTION_RUNS = [(n, "mm_bram_port_pipe", 128) for n in COUNTS]


def build_dag(work, store_dir, runs, xbar=None):
    """One root a width under *work* (``w<bits>``: the readers' and the writer's tops at that width)
    and a DAG a root, with every run of *runs* in it.  Returns ``{(n, leaf, dw): (dag, root, prefix,
    run dir)}``."""
    from waveflow.build.build import BuildDag

    xbar = XBAR_SETTINGS if xbar is None else xbar
    dags, out = {}, {}
    for n, leaf, dw in runs:
        root = Path(work) / f"w{dw}"
        if dw not in dags:
            generate_readers(root, dw)
            dags[dw] = BuildDag()
        p = add_contention(dags[dw], n, leaf, dw, work_dir=root / "xsi_work", store_dir=store_dir,
                           trace=n == 1, **xbar)
        out[(n, leaf, dw)] = (dags[dw], root, p, root / "xsi_work" / f"{p}{tag(n, leaf, dw)}_top")
    return out


def main(argv=None) -> None:     # pragma: no cover - driven by the gate and by hand
    from waveflow.build.build import BuildConfig
    from waveflow.calib.platform import packaged_platforms_dir

    from waveflow.calib.fixtures.bram_window import PLATFORM

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", type=Path, default=Path("build_bram_window_contention"))
    ap.add_argument("--platform-dir", type=Path,
                    default=(packaged_platforms_dir() or Path(".")) / PLATFORM)
    ap.add_argument("--file", action="store_true",
                    help="file each one-reader run's turnaround fit on the platform")
    a = ap.parse_args(argv)
    store = a.platform_dir.resolve()
    runs = build_dag(a.work.resolve(), store, TURNAROUND_RUNS + CONTENTION_RUNS)
    for (n, leaf, dw), (dag, root, p, work) in runs.items():
        res = dag.run(BuildConfig(root_dir=root, params={}), through=f"{p}compare")
        bad = {k: r.message for k, r in res.items() if not r.success}
        if bad:
            raise SystemExit(f"{p}: {bad}")
        line = f"{p} ok"
        if n == 1:
            line += f" turnaround {fit_window_turnaround(work, 1, leaf, dw, store, file=a.file)}"
            line += f" crossbar {crossbar_latency(work, 1)}"
        else:
            line += f" rtl {rtl_read_times(work, n)}"
        print(line)


if __name__ == "__main__":     # pragma: no cover
    main()
