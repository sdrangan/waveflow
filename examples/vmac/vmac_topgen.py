"""Generated-top VMAC cosim — the auto-extracted ``run_proc`` kernel (Phases 3–4).

Unlike :mod:`examples.vmac.vmac_build`'s hand-rolled ``render_top`` (which unpacks the command
into ``s_axilite`` scalar registers), this generates VMAC's synthesizable top **from the
extracted ``run_proc`` IR** via the framework codegen (``kernel_signature`` +
``kernel_body_to_cpp``): a free-running ``m_axi``-only kernel that dequeues ``VmacCmd``s from
the in-memory ring (the synthesizable :meth:`AXIMMQueue.get` → ``aximm_queue_impl::queue_get``
hook), runs the ``vmac_compute`` datapath, and stops on ``OpCode.end`` (decision D6 — the
command is read from memory, never crossing the ``s_axilite`` boundary, so the top's only ports
are the ``m_axi`` gmem + ``ap_ctrl``).

The cosim builds the ring image in memory (head/tail/capacity + one command slot + an ``END``
slot, via the producer-side :meth:`VmacCmd.serialize`), lays the A/B operands after the ring,
invokes the generated top, and checks the Y region **bit-exact** against
:meth:`VmacAccel.execute_mem` (the same golden the csim conformance uses — no new golden)."""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.hwgen import kernel_body_to_cpp, kernel_signature
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import (
    ArrayUtilsStep,
    _array_utils_filename,
    _array_utils_namespace,
)
from waveflow.hw.aximm_queue import AXIMMQueue, AXIMMQueueLayout
from waveflow.simulation.simulation import Simulation

try:
    from examples.vmac.vmac import VmacAccel
    from examples.vmac.vmac_build import (
        BUILD_HDRS,
        HOOK_FILES,
        INCLUDE_DIR,
        MEM_AWIDTH,
        MEM_WORD_BWS,
        _BUILD_DIR,
        _SOURCE_DIR,
        _cmd_schema_steps,
        _mem_words,
        _pair,
        StructCfg,
    )
    from examples.vmac.vmac_cmd import OpCode
except ModuleNotFoundError:  # direct execution from the example dir
    from vmac import VmacAccel  # type: ignore[no-redef]
    from vmac_build import (  # type: ignore[no-redef]
        BUILD_HDRS, HOOK_FILES, INCLUDE_DIR, MEM_AWIDTH, MEM_WORD_BWS,
        _BUILD_DIR, _SOURCE_DIR, _cmd_schema_steps, _mem_words, _pair, StructCfg,
    )
    from vmac_cmd import OpCode  # type: ignore[no-redef]

from waveflow.utils import complexutils as cx  # noqa: E402

# The auto-extracted top is generic over the ring geometry; the cosim pins one concrete config.
TOPGEN_CAP = 4          # ring capacity (power of two; usable depth 3 >= command + END)
TOPGEN_MAX_COLS = 64    # vmac_compute_core acc[] capacity (>= n_cols)
TOPGEN_N_ROWS = 4
TOPGEN_N_COLS = 4       # multiple of every PF (1/2/4) so rows are word-aligned



def _topgen_accel(cfg: StructCfg) -> VmacAccel:
    """A sim-mode VmacAccel at the config's concrete widths, with a ring ``cmd_queue`` attached
    (geometry baked into the generated ``queue_get<...>`` template args)."""
    accel = VmacAccel(
        name="vmac", sim=Simulation(),
        mem_dwidth=cfg.mem_dwidth, mem_awidth=MEM_AWIDTH, data_bw=cfg.data_bw,
        int_bits=cfg.int_bits, acc_bw=cfg.acc_bw, out_bw=cfg.out_bw,
        q_rnd=cfg.q_rnd, o_sat=cfg.o_sat,
    )
    ew = accel.Cmd.nwords_per_inst(cfg.mem_dwidth)
    accel.cmd_queue = AXIMMQueue(
        master=accel.m_mem,
        layout=AXIMMQueueLayout(
            base_addr=0, capacity=TOPGEN_CAP, elem_words=ew, mem_bw=cfg.mem_dwidth,
        ),
    )
    return accel


def _ring_geometry(cfg: StructCfg) -> dict:
    """Word/element offsets of the ring + the A|B|Y data region (one inner_prod case)."""
    accel = _topgen_accel(cfg)
    mbw = cfg.mem_dwidth
    pf = cfg.pf
    ew = accel.Cmd.nwords_per_inst(mbw)
    ring_words = AXIMMQueueLayout.NUM_CONTROL_WORDS + TOPGEN_CAP * ew  # control + CAP slots
    n, m = TOPGEN_N_ROWS, TOPGEN_N_COLS
    nm = n * m
    # data region starts at the first word past the ring; addresses are ABSOLUTE element indices
    # (word-aligned: ring_words*pf), since the kernel maps element->word as elem/PF over gmem[0].
    a_addr = ring_words * pf
    b_addr = a_addr + nm
    y_addr = b_addr + nm
    data_words = (3 * nm) // pf
    return {
        "accel": accel, "mbw": mbw, "pf": pf, "ew": ew, "ring_words": ring_words,
        "n": n, "m": m, "nm": nm, "a_addr": a_addr, "b_addr": b_addr, "y_addr": y_addr,
        "data_words": data_words, "total_words": ring_words + data_words,
        "y_word_start": y_addr // pf, "y_word_end": (y_addr + nm) // pf,
    }


def _topgen_vectors(cfg: StructCfg) -> dict:
    """Build the ring image (mem_in) + the golden expected image (mem_exp), bit-exact via
    :meth:`VmacAccel.execute_mem`.  inner_prod (no reduce), distinct B."""
    g = _ring_geometry(cfg)
    accel = g["accel"]
    in_fmt = accel._in_fmt()
    n, m, nm, ew = g["n"], g["m"], g["nm"], g["ew"]
    a_addr, b_addr, y_addr = g["a_addr"], g["b_addr"], g["y_addr"]

    rng = np.random.default_rng(7)
    A = _pair(rng.integers(-30, 31, (n, m)), rng.integers(-30, 31, (n, m)))
    B = _pair(rng.integers(-30, 31, (n, m)), rng.integers(-30, 31, (n, m)))

    # structured complex image covering [0, y_addr+nm) elements; the ring words occupy the first
    # ring_words*pf "element slots" (untouched by the golden — addresses point past the ring).
    extent = y_addr + nm
    mem_struct = cx.make_complex(np.zeros(extent), np.zeros(extent), in_fmt)
    mem_struct[a_addr:a_addr + nm] = cx.make_complex(A[0].ravel(), A[1].ravel(), in_fmt)
    mem_struct[b_addr:b_addr + nm] = cx.make_complex(B[0].ravel(), B[1].ravel(), in_fmt)

    cmd = accel.Cmd()
    cmd.op, cmd.reduce, cmd.n_rows, cmd.n_cols = OpCode.inner_prod, 0, n, m
    cmd.a = {"addr": a_addr, "row_stride": m}
    cmd.b = {"addr": b_addr, "row_stride": m}
    cmd.y = {"addr": y_addr, "row_stride": m}
    cmd.alpha = {"direct": 1, "imm": (16, 0), "addr": 0, "stride": 0}

    end_cmd = accel.Cmd()
    end_cmd.op, end_cmd.reduce, end_cmd.n_rows, end_cmd.n_cols = OpCode.end, 0, n, m
    for nm_field in ("a", "b", "y"):
        setattr(end_cmd, nm_field, {"addr": 0, "row_stride": m})
    end_cmd.alpha = {"direct": 1, "imm": (0, 0), "addr": 0, "stride": 0}

    mem_exp_struct = mem_struct.copy()
    accel.execute_mem(cmd, mem_exp_struct)  # the golden writes Y into mem_exp_struct

    # serialize the data region (A|B|Y elements) to mem words, packed pf/word
    data_in = _mem_words(mem_struct[a_addr:y_addr + nm], cfg.in_elem(), g["mbw"])
    data_exp = _mem_words(mem_exp_struct[a_addr:y_addr + nm], cfg.in_elem(), g["mbw"])

    # ring control words + command slots (producer-side serialize == the kernel's read_array)
    slot0 = [int(w) for w in np.asarray(cmd.serialize(word_bw=g["mbw"])).ravel()]
    slot1 = [int(w) for w in np.asarray(end_cmd.serialize(word_bw=g["mbw"])).ravel()]
    assert len(slot0) == ew and len(slot1) == ew, (len(slot0), len(slot1), ew)

    gmem = [0] * g["total_words"]
    gmem[0], gmem[1], gmem[2], gmem[3] = 0, 2, TOPGEN_CAP, 0   # head, tail, capacity, reserved
    base = AXIMMQueueLayout.NUM_CONTROL_WORDS
    gmem[base:base + ew] = slot0
    gmem[base + ew:base + 2 * ew] = slot1
    gmem[g["ring_words"]:g["ring_words"] + len(data_in)] = data_in

    gmem_exp = list(gmem)
    gmem_exp[g["ring_words"]:g["ring_words"] + len(data_exp)] = data_exp

    g.update({"mem_in": gmem, "mem_exp": gmem_exp})
    return g


# --- generated C++ ------------------------------------------------------------
def _gen_vmac_hpp(cfg: StructCfg, total_words: int) -> str:
    """The generated kernel header: schema/array-utils/hook includes, the ``vmac_impl`` typedefs,
    and the concrete ``vmac_compute(cmd, mem)`` the auto-generated kernel calls (a non-templated
    overload forwarding to the templated struct-wrapper with this config's structural widths)."""
    in_ns = _array_utils_namespace(cfg.in_elem())
    in_hdr = _array_utils_filename(cfg.in_elem())
    mbw = cfg.mem_dwidth
    return "\n".join([
        "#ifndef VMAC_HPP",
        "#define VMAC_HPP",
        "#include <ap_int.h>",
        "#include <ap_fixed.h>",
        "#include <complex>",
        f'#include "{INCLUDE_DIR}/op_code.h"',
        f'#include "{INCLUDE_DIR}/vmac_cmd_data_bw{cfg.data_bw}_mem_awidth32.h"',
        f'#include "{in_hdr}"',
        "namespace vmac_impl {",
        f"typedef VmacCmd_data_bw{cfg.data_bw}_mem_awidth32 VmacCmd;",
        f"namespace vmac_in_au  = ::{in_ns};",
        f"namespace vmac_out_au = ::{in_ns};",
        "}",
        '#include "vmac_compute_impl.tpp"',
        '#include "aximm_queue_impl.tpp"',
        "namespace vmac_impl {",
        "// Concrete (non-templated) overload the auto-extracted kernel calls as",
        "// `vmac_impl::vmac_compute(cmd, gmem)` — forwards to the templated struct-wrapper with",
        "// this variant's structural widths (the templated overload is non-viable for the bare",
        "// call: only MEM_BW is deducible, so overload resolution selects this one).",
        f"inline void vmac_compute(VmacCmd cmd, ap_uint<{mbw}>* mem) {{",
        f"    vmac_compute<{mbw}, {MEM_AWIDTH}, {cfg.data_bw}, {cfg.int_bits}, {cfg.acc_bw}, "
        f"{cfg.out_bw}, {cfg.q_rnd}, {cfg.o_sat}, {TOPGEN_MAX_COLS}>(cmd, mem);",
        "}",
        "}",
        f"static const int m_mem_depth = {total_words};",
        "#endif",
        "",
    ])


def _gen_vmac_cpp(accel: VmacAccel) -> str:
    """The generated kernel ``vmac.cpp``: framework ``kernel_signature`` + the body emitted from
    the extracted ``run_proc`` IR (the ring-dequeue loop)."""
    sig = kernel_signature(accel)
    body = kernel_body_to_cpp(accel)
    body_inner = body.removeprefix("{\n").removesuffix("\n}")
    return '#include "vmac.hpp"\n\n' + sig + "\n" + body_inner + "\n}\n"


def _gen_topgen_tb(cfg: StructCfg, g: dict) -> str:
    mbw = cfg.mem_dwidth
    total = g["total_words"]
    return "\n".join([
        "// Generated-top cosim TB: load the ring image, run the auto-extracted vmac() top",
        "// (it drains the ring until OpCode::end and returns), and check the Y region bit-exact",
        "// against execute_mem's golden.",
        '#include "vmac.hpp"',
        "#include <fstream>",
        "#include <iostream>",
        "#include <string>",
        "#include <vector>",
        "",
        f"void vmac(ap_uint<{mbw}>* m_mem);",
        "",
        "static std::vector<unsigned long long> rw(const std::string& p) {",
        "    std::ifstream f(p.c_str()); std::vector<unsigned long long> v; unsigned long long x;",
        "    while (f >> x) v.push_back(x); return v;",
        "}",
        "",
        f"static ap_uint<{mbw}> mem[{total}];",
        "",
        "int main(int argc, char** argv) {",
        "    std::string d = argv[1];",
        '    std::vector<unsigned long long> mw = rw(d + "/mem_in.txt");',
        '    std::vector<unsigned long long> ew = rw(d + "/mem_exp.txt");',
        f"    if ((int)mw.size() < {total} || (int)ew.size() < {total}) {{",
        '        std::cerr << "VMAC_TB_ERROR: short vector files" << char(10); return 2; }',
        f"    for (int i = 0; i < {total}; ++i) mem[i] = (ap_uint<{mbw}>)mw[i];",
        "    vmac(mem);",
        "    int bad = 0;",
        f"    for (int i = {g['y_word_start']}; i < {g['y_word_end']}; ++i)",
        "        if ((unsigned long long)mem[i] != ew[i]) ++bad;",
        '    if (bad) { std::cerr << "VMAC_COSIM_MISMATCH " << bad << char(10); return 1; }',
        '    std::cout << "VMAC_COSIM_OK" << char(10);',
        "    return 0;",
        "}",
        "",
    ])


_TOPGEN_TCL = """# Vitis HLS csim -> csynth -> cosim for the auto-extracted VMAC top.
set d [file dirname [file normalize [info script]]]
set data_dir [file join $d data]
open_project -reset vmac_topgen_proj
set_top vmac
add_files vmac.cpp -cflags "-I. -Iinclude"
add_files -tb vmac_tb.cpp -cflags "-I. -Iinclude"
set su [file join $d include streamutils.cpp]
if {[file exists $su]} { add_files -tb $su -cflags "-I. -Iinclude" }
open_solution -reset "solution1"
set_part {xc7z020clg484-1}
create_clock -period 10
foreach {stage cmd} {csim csim_design csynth csynth_design cosim cosim_design} {
    if {$stage eq "csim"}  { set rc [catch {csim_design -argv $data_dir} res] }
    if {$stage eq "csynth"} { set rc [catch {csynth_design} res] }
    if {$stage eq "cosim"} { set rc [catch {cosim_design -argv $data_dir -trace_level none} res] }
    if {$rc} { puts "WAVEFLOW_ERROR: vmac $stage failed."; puts $res; exit 1 }
}
puts "WAVEFLOW_SUCCESS: vmac topgen csim/csynth/cosim passed."
exit 0
"""


def gen_topgen_config(cfg: StructCfg, tdir: Path) -> dict:
    """Generate the auto-extracted top + cosim TB + ring vectors into *tdir*."""
    tdir = Path(tdir).resolve()
    tdir.mkdir(parents=True, exist_ok=True)
    bc = BuildConfig(root_dir=tdir)
    dag = BuildDag()
    dag.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    for step in _cmd_schema_steps(cfg):
        dag.add(step)
    dag.add(ArrayUtilsStep(cfg.in_elem(), MEM_WORD_BWS))
    dag.run(bc)
    for fname in HOOK_FILES:  # vmac_compute_impl.tpp (the datapath hook), in the example dir
        shutil.copy(_SOURCE_DIR / fname, tdir / fname)
    # the ring-dequeue hook + complex toolkit live in the framework build dir
    for fname in BUILD_HDRS + ("aximm_queue_impl.tpp",):
        shutil.copy(_BUILD_DIR / fname, tdir / fname)

    g = _topgen_vectors(cfg)
    (tdir / "vmac.hpp").write_text(_gen_vmac_hpp(cfg, g["total_words"]), encoding="utf-8")
    (tdir / "vmac.cpp").write_text(_gen_vmac_cpp(g["accel"]), encoding="utf-8")
    (tdir / "vmac_tb.cpp").write_text(_gen_topgen_tb(cfg, g), encoding="utf-8")
    (tdir / "run.tcl").write_text(_TOPGEN_TCL, encoding="utf-8")
    data = tdir / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "mem_in.txt").write_text(
        "\n".join(str(w) for w in g["mem_in"]) + "\n", encoding="utf-8")
    (data / "mem_exp.txt").write_text(
        "\n".join(str(w) for w in g["mem_exp"]) + "\n", encoding="utf-8")
    return {
        "mem_dwidth": cfg.mem_dwidth, "pf": cfg.pf, "total_words": g["total_words"],
        "y_words": (g["y_word_start"], g["y_word_end"]),
    }
