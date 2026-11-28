"""bram_window.py — the calibration fixture for a BRAM window's bus side: the memory timing a
:class:`~waveflow.hw.mm_bram.MemSlaveBramWindow` loads (``plans/bram_fullrate_port.md``, Stage 4).

A window's port A is, at RTL, ``axi_slave_front`` and a BRAM leaf on a ``bram_t2p``: the slot an
on-chip memory on the bus renders as.  So the window's timing is measured on
:func:`~waveflow.hw.mm_bram.window_memory` ``(leaf, width)`` -- a ``MemoryMod`` (``mm_bram_port``) or a
``FullRateMemoryMod`` (``mm_bram_port_pipe``) -- in its own harness
(:func:`~waveflow.hw.harness.memory_harness`: the memory's slot behind a direct link, a scripted master
playing reads and writes), and filed under that memory's identity, where the window looks it up.  Per
leaf and width (128 and 512 bits):

* ``<tag>cal_*`` -- the harness on a memory that loads no timing, playing :func:`mem_script`
  ``("calib")``: every length 1-32 and 38, 64, 128, 256 alone, and a read and a write started together
  at several lengths; ``MemCalibStep`` fits ``init + per_word * n`` per direction and ``serialize``
  (:func:`~waveflow.calib.mem_timing.fit_mem_timing`) and files it in the store;
* ``<tag>_*`` -- the check: the harness on a memory that loads the record, playing
  :func:`mem_script` ``("test")``, pysim and RTL each op's ``[t_start, t_end]`` side by side.

Vivado's xsim only (no csynth: a memory is hand-written RTL).  The turnaround -- the cycles the front
stays busy between back-to-back transactions -- needs a master with its next burst already waiting,
which this harness's master is not; it is fitted from the contention gate's traced runs
(``tests/build/test_bram_window_contention_xsi.py``).

    python -m waveflow.calib.fixtures.bram_window [--work <dir>] [--platform-dir <dir>]
                                                  [--leaves ...] [--widths 128 512]

files into the packaged platform ``xczu48dr_250mhz_vitis2025_1`` by default (a repeat that measures
the same files nothing).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from waveflow.hw.mm_bram import BRAM_WINDOW_LEAVES, WINDOW_MEMORY_WORDS, window_memory

PLATFORM = "xczu48dr_250mhz_vitis2025_1"
WIDTHS = (128, 512)
#: Short tags for a leaf, in a step prefix and a top's name.
LEAF_TAG = {"mm_bram_port": "p2", "mm_bram_port_pipe": "pp"}


def tag(leaf: str, dw: int) -> str:
    return f"bw{LEAF_TAG[leaf]}{dw}"


def _data(n: int, dw: int, seed: int) -> np.ndarray:
    """*n* distinct words of *dw* bits: a 1-D array up to 64 bits, else ``(n, k)`` chunks."""
    k = 1 if dw <= 64 else dw // 64
    base = np.arange(n * k, dtype=np.uint64) * np.uint64(0x9E3779B1) + np.uint64(seed * 0x10001 + 1)
    return base if k == 1 else base.reshape(n, k)


def mem_script(kind: str, dw: int) -> list:
    """What the window memory's harness plays: each length a write then a read back, spaced so no two
    meet (``"test"``: 1, 2, 8, 16, 64, 256); ``"calib"``: every length 1-32 and 38, 64, 128, 256, and a
    read and a write started together at 4, 16, 64 and 256 (the two channels meeting at the front)."""
    from waveflow.simulation.mm_tb import READ, WRITE, script_op

    bpw = dw // 8
    lens = (1, 2, 8, 16, 64, 256) if kind == "test" else (*range(1, 33), 38, 64, 128, 256)
    ops, t, gap = [], 10, 40
    if kind != "test":
        # Fill the memory first: a read of words never written is X at RTL and 0 in pysim, and the
        # collisions below read what they did not write themselves.
        for j in range(WINDOW_MEMORY_WORDS // 256):
            ops.append(script_op(WRITE, j * 256 * bpw, 256, t, _data(256, dw, 200 + j)))
            t += 256 + gap
    for i, n in enumerate(lens):
        w0 = (i * 37) % (WINDOW_MEMORY_WORDS - n)
        ops.append(script_op(WRITE, w0 * bpw, n, t, _data(n, dw, i)))
        t += n + gap
        ops.append(script_op(READ, w0 * bpw, n, t))
        t += 2 * n + gap
    if kind != "test":
        for i, n in enumerate((4, 16, 64, 256)):
            ops.append(script_op(READ, 0, n, t))
            ops.append(script_op(WRITE, (WINDOW_MEMORY_WORDS - n) * bpw, n, t, _data(n, dw, 100 + i)))
            t += 3 * n + 2 * gap
    return ops


def script_cycles(ops) -> int:
    """A run long enough for *ops*: the last op's start, three cycles a word of it, and a margin."""
    return int(max(int(o[3]) + 3 * int(o[2]) for o in ops)) + 2000


def add_window_calibration(dag, leaf: str, dw: int, *, work_dir, store_dir) -> dict[str, str]:
    """Add one leaf and width to *dag*: the calibration (filed in ``<store_dir>/modules``) and the
    check that loads it.  Returns ``{"mem_timing": artifact, "check": the check's prefix}``."""
    from waveflow.build.phase_calib import MemCalibStep
    from waveflow.build.system_dag import add_system_steps
    from waveflow.calib.record_store import ModuleStore
    from waveflow.hw.harness import memory_harness

    t = tag(leaf, dw)
    cal_ops, test_ops = mem_script("calib", dw), mem_script("test", dw)
    uncal = memory_harness(lambda sim: window_memory(leaf, dw, sim), cal_ops,
                           n_cycles=script_cycles(cal_ops), name=f"{t}_mem_unit")
    xsi = add_system_steps(dag, uncal, work_dir=work_dir, top=f"{t}_mem_unit", prefix=f"{t}cal_")
    cal = MemCalibStep(sysm=uncal, work=xsi.work, prefix=f"{t}cal_", store=ModuleStore(store_dir))
    dag.add(cal)
    check = memory_harness(lambda sim: window_memory(leaf, dw, sim, calib_dir=store_dir), test_ops,
                           n_cycles=script_cycles(test_ops), name=f"{t}_mem_unit")
    add_system_steps(dag, check, work_dir=work_dir, top=f"{t}_mem_unit", prefix=f"{t}_")
    dag.require(f"{t}_pysim", [cal.a("mem_timing")])         # the check's pysim loads the fresh fit
    return {"mem_timing": cal.a("mem_timing"), "check": f"{t}_"}


def render_window_slot(name: str, leaf: str, dw: int, baw: int, law: int, origin: str) -> str:
    """An adaptor with one window as its own module *name*: the AXI slave port ``s_axi``, the
    memory's port B ``kb_*`` (in use: the window a kernel shares), and ``render_view_slot``'s front,
    *leaf* and a ``bram_t2p`` of ``2**baw`` words between them -- what the timing-closure runs
    implement (``tests/build/test_mm_bram_timing_vitis.py``) and what :func:`implement_window` files
    the resources of.  *origin* names the generator in the file's first line."""
    from waveflow.build.axi_xbar import axi_port_decls, axi_signals
    from waveflow.build.mm_adaptor_gen import BRAM_VIEWS, render_view_slot

    view = BRAM_VIEWS[leaf]("win", kport="kb", baw=baw, law=law)
    ports = ["input  wire ap_clk", "input  wire ap_rst_n"]
    ports += axi_port_decls("s_axi", axi_signals(dw, 32, 1), facing="slave")
    ports += ["input  wire [31:0] kb_addr", "input  wire kb_en", "input  wire [1:0] kb_we",
              f"input  wire [{dw - 1}:0] kb_din", f"output wire [{dw - 1}:0] kb_dout"]
    return (f"// {name}.v -- GENERATED by {origin}.\n"
            f"`timescale 1ns/1ps\nmodule {name} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + render_view_slot(view, "s_axi", dw, 32, 1) + "endmodule\n")


def implement_window(leaf: str, dw: int, out_dir, *, force: bool = False):
    """Synthesize, place and route the slot of :func:`~waveflow.hw.mm_bram.window_memory` ``(leaf,
    dw)`` -- 1,024 words -- inside a ring of registers (``rtl_ooc.impl_registered_ooc``; Vivado,
    ``xczu48dr`` at 4 ns), port B in use; cached by content."""
    from waveflow.build.mm_adaptor_gen import BRAM_VIEWS, leaf_module_sources
    from waveflow.build.rtl_ooc import impl_registered_ooc

    baw = WINDOW_MEMORY_WORDS.bit_length() - 1
    law = max(12, (WINDOW_MEMORY_WORDS * dw // 8).bit_length() - 1)
    name = f"wm_{leaf}_{dw}"
    src = Path(out_dir) / "src"
    src.mkdir(parents=True, exist_ok=True)
    dut = src / f"{name}.v"
    text = render_window_slot(name, leaf, dw, baw, law, "waveflow.calib.fixtures.bram_window")
    if not dut.exists() or dut.read_text(encoding="utf-8") != text:
        dut.write_text(text, encoding="utf-8")
    view = BRAM_VIEWS[leaf]
    sources = [dut, *leaf_module_sources("axi_slave_front"), *leaf_module_sources(view.module),
               *leaf_module_sources(view.mem_module)]
    return impl_registered_ooc(name, sources, part=IMPL_PART, period_ns=IMPL_PERIOD_NS,
                               out_dir=out_dir, force=force)


IMPL_PART = "xczu48dr-ffvg1517-2-e"
IMPL_PERIOD_NS = 4.0


def file_window_resources(store_dir, leaf: str, dw: int, res) -> None:
    """File *res* (an ``OocImpl`` of :func:`implement_window`) as the routed ``resource`` record of
    ``window_memory(leaf, dw)`` in *store_dir*: the adaptor's own LUT, FF and BRAM (the ring excluded),
    with its closure verdict.  A repeat of the same figures files nothing."""
    from waveflow.calib.phases import module_identity
    from waveflow.calib.record_store import ModuleStore, resource_record

    ident = module_identity(window_memory(leaf, dw))
    extra = {"context": "ooc_ring", "rtl_module": leaf, "words": WINDOW_MEMORY_WORDS,
             "port_b": "in use (the ring's flops)", "wns_ns": res.wns_ns, "whs_ns": res.whs_ns,
             "met": res.met, "children": res.children}
    rec = resource_record(ident, res.impl, source="vivado_impl", part=res.part,
                          period_ns=res.period_ns, tool=res.tool, cost_seconds=res.seconds,
                          extra=extra)
    store = ModuleStore(store_dir)
    same = [r for r in store.read(ident.key, "resource", identity=ident) if r.payload == rec.payload]
    if not same:
        store.append(rec, identity=ident)


def build_dag(work_dir, store_dir, leaves=BRAM_WINDOW_LEAVES, widths=WIDTHS):
    from waveflow.build.build import BuildDag

    dag = BuildDag()
    for leaf in leaves:
        for dw in widths:
            add_window_calibration(dag, leaf, dw, work_dir=work_dir, store_dir=store_dir)
    return dag


def main(argv=None) -> None:
    from waveflow.build.build import BuildConfig
    from waveflow.calib.platform import packaged_platforms_dir

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", type=Path, default=Path("build_bram_window"))
    ap.add_argument("--platform-dir", type=Path,
                    default=(packaged_platforms_dir() or Path(".")) / PLATFORM)
    ap.add_argument("--leaves", nargs="*", default=list(BRAM_WINDOW_LEAVES))
    ap.add_argument("--widths", nargs="*", type=int, default=list(WIDTHS))
    ap.add_argument("--force", action="store_true",
                    help="fit and file again even when the fitting step is fresh: its freshness "
                         "does not include the store it files into")
    ap.add_argument("--resources", action="store_true",
                    help="instead: implement each window memory's slot (Vivado, a few minutes "
                         "each, two at a time) and file its routed resources")
    a = ap.parse_args(argv)
    if a.resources:
        from concurrent.futures import ProcessPoolExecutor

        cfgs = [(leaf, dw) for leaf in a.leaves for dw in a.widths]
        with ProcessPoolExecutor(max_workers=2) as pool:
            futs = {c: pool.submit(implement_window, *c, a.work.resolve() / "impl") for c in cfgs}
            done = {c: f.result() for c, f in futs.items()}
        for (leaf, dw), r in done.items():
            file_window_resources(a.platform_dir.resolve(), leaf, dw, r)
            print(leaf, dw, "WNS", r.wns_ns, "WHS", r.whs_ns, r.impl, r.clocks)
        return
    from waveflow.toolchain.toolchain import find_vivado_path, tool_version

    # The release the records' provenance names (MemCalibStep files it as its tool).
    vivado = find_vivado_path()
    release = tool_version(vivado, "-version") if vivado else None
    dag = build_dag(a.work.resolve(), a.platform_dir.resolve(), a.leaves, a.widths)
    for leaf in a.leaves:
        for dw in a.widths:
            tg = f"{tag(leaf, dw)}_compare"
            force = [f"{tag(leaf, dw)}cal_mem_timing"] if a.force else False
            res = dag.run(BuildConfig(root_dir=a.work.resolve(), params={}, vitis_version=release),
                          through=tg, force=force)
            bad = {n: r.message for n, r in res.items() if not r.success}
            if bad:
                raise SystemExit(f"{tg}: {bad}")
            print(f"{tg}: ok")


if __name__ == "__main__":
    main()
