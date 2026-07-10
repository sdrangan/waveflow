"""mem_stream_gen.py — template codegen for the ``MemRStream`` / ``MemWStream`` kernels.

Their kernel body is **FIXED** (= the validated sandbox ``a2s`` / ``s2a`` in
``interleaver_task_sob3.cpp``), parameterized only by ``MEM_DW`` and the generated command struct
— so codegen is a **template**, not a ``run_proc`` extraction (the framework ``HlsCodegenStep``
emits an ``ap_ctrl_hs`` top with no ``hls::task``; we need a free-running ``ap_ctrl_none`` single-
``hls::task`` kernel, which we emit directly).  Simplest possible codegen: reuse the standard
header steps (``DataSchemaStep`` for the command struct — the single source shared with the pysim
``.get()`` — plus ``MemMgrStep`` / ``StreamUtilsStep``) and stamp out the fixed ``.cpp`` + a csynth
``.tcl``.

Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/interleaver/mem_stream_gen.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, StreamUtilsStep  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402

from examples.interleaver.mem_stream import (  # noqa: E402
    MRCmd,
    MWCmd,
    MemRStream,
    MemWStream,
    SCHEMA_CLASSES,
    WORD_BW_SUPPORTED,
)

INCLUDE_DIR = "include"
GEN_DIR = "gen"
DEFAULT_MEM_DW = 64


# ---------------------------------------------------------------------------
# The fixed kernel template
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KernelSpec:
    """The bits that vary between the two fixed-body memory endpoints."""
    kernel_name: str        # top function / set_top name
    cmd_struct: str         # generated command struct (C++ class name)
    cmd_header: str         # its generated header filename
    mem_dir: str            # 'R' (read owner, const ptr) or 'W' (write owner)
    data_stream: str        # the word stream name ('m_out' for R, 's_in' for W)


def spec_for(comp_class) -> KernelSpec:
    """Derive the :class:`KernelSpec` from a component class (the two are the only cases)."""
    if comp_class is MemRStream:
        return KernelSpec("mem_r_stream", MRCmd.cpp_class_name(),
                          MRCmd.resolved_include_filename(), "R", "m_out")
    if comp_class is MemWStream:
        return KernelSpec("mem_w_stream", MWCmd.cpp_class_name(),
                          MWCmd.resolved_include_filename(), "W", "s_in")
    raise ValueError(f"no mem-stream kernel template for {comp_class!r}")


def render_kernel(spec: KernelSpec) -> str:
    """Emit the standalone free-running (``ap_ctrl_none``) single-``hls::task`` kernel .cpp.

    The ``@port_read`` capability of ``m_mem`` drives the ``const`` pointer for the read owner
    (a stray write is then a compile error); the write owner gets a plain pointer.  All streams
    are word-granular ``ap_uint<MEM_DW>`` (the sob3 word-rate lesson); the command rides its own
    stream, deserialized by the generated ``<Cmd>::read_stream<MEM_DW>`` (single source with the
    pysim ``.get()``)."""
    const = "const " if spec.mem_dir == "R" else ""
    role = "AXI->stream (a2s)" if spec.mem_dir == "R" else "stream->AXI (s2a)"
    loop_label = "A2S" if spec.mem_dir == "R" else "S2A"

    # task arg list + body: read owner bursts mem -> m_out; write owner drains s_in -> mem.
    if spec.mem_dir == "R":
        task_args = (f"hls::stream<word_t>& s_cmd,\n"
                     f"                              {const}ap_uint<MEM_DW>* m_mem,\n"
                     f"                              hls::stream<word_t>& m_out")
        body = "        m_out.write(m_mem[w0 + w]);"
        top_args = (f"hls::stream<word_t>& s_cmd,\n"
                    f"                  {const}ap_uint<MEM_DW>* m_mem,\n"
                    f"                  hls::stream<word_t>& m_out")
        task_call = "s_cmd, m_mem, m_out"
        data_pragma = "#pragma HLS INTERFACE axis port=m_out"
        stable = "#pragma HLS stable variable=m_mem\n"
    else:
        task_args = (f"hls::stream<word_t>& s_cmd,\n"
                     f"                              hls::stream<word_t>& s_in,\n"
                     f"                              {const}ap_uint<MEM_DW>* m_mem")
        body = "        m_mem[w0 + w] = s_in.read();"
        top_args = (f"hls::stream<word_t>& s_cmd,\n"
                    f"                  hls::stream<word_t>& s_in,\n"
                    f"                  {const}ap_uint<MEM_DW>* m_mem")
        task_call = "s_cmd, s_in, m_mem"
        data_pragma = "#pragma HLS INTERFACE axis port=s_in"
        stable = ""

    return f"""\
// {spec.kernel_name}.cpp — GENERATED (template codegen, examples/interleaver/mem_stream_gen.py).
// DO NOT EDIT: the fixed body is the validated sandbox {role} (interleaver_task_sob3.cpp).
//
// Free-running (ap_ctrl_none) single-hls::task memory endpoint, word-granular (ap_uint<MEM_DW>).
// The sole m_axi {"read" if spec.mem_dir == "R" else "write"} owner touches ONLY streams (the DTLP /
// hls::task+m_axi de-risk: an m_axi owner never also locks a stream_of_blocks). Verify via XSI —
// ap_ctrl_none Vitis cosim is unreliable.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "{spec.cmd_header}"

#ifndef MEM_DW
#define MEM_DW {DEFAULT_MEM_DW}
#endif
typedef ap_uint<MEM_DW> word_t;

// {role}: dequeue one {spec.cmd_struct}, byte_addr -> word index, burst n_words words. Word rate.
static void {spec.kernel_name}_task({task_args}) {{
    {spec.cmd_struct} c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.byte_addr);
    const int nw = (int)c.n_words;
{loop_label}: for (int w = 0; w < nw; ++w) {{
#pragma HLS PIPELINE II=1
{body}
    }}
}}

void {spec.kernel_name}({top_args}) {{
#pragma HLS INTERFACE axis port=s_cmd
{data_pragma}
#pragma HLS INTERFACE m_axi port=m_mem offset=slave bundle=gmem0 depth=8192
#pragma HLS INTERFACE ap_ctrl_none port=return
{stable}    hls_thread_local hls::task t({spec.kernel_name}_task, {task_call});
}}
"""


def render_tcl(spec: KernelSpec) -> str:
    """Emit a csynth ``.tcl`` for ``vitis-run --mode hls --tcl`` (sob3-shaped; MEM_DW overridable)."""
    return f"""\
set part {{xc7z020clg484-1}}
set dw {DEFAULT_MEM_DW}; if {{[info exists ::env(WAVEFLOW_IL_DW)]}} {{ set dw $::env(WAVEFLOW_IL_DW) }}
set cf "-I{INCLUDE_DIR} -DMEM_DW=$dw"
puts "WAVEFLOW_INFO: {spec.kernel_name} MEM_DW=$dw"
open_project -reset {spec.kernel_name}_proj
set_top {spec.kernel_name}
add_files {GEN_DIR}/{spec.kernel_name}.cpp -cflags $cf
open_solution -reset "solution1"
set_part $part
create_clock -period 10
if {{[catch {{csynth_design}} res]}} {{ puts "WAVEFLOW_ERROR: csynth"; puts $res; exit 1 }}
puts "WAVEFLOW_CSYNTH_OK"
exit 0
"""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def gen_headers(config: BuildConfig) -> None:
    """Generate the command-struct headers + memmgr.hpp + streamutils_hls.h into ``include/``."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED,
                                 include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")


def generate(out_dir: Path = HERE) -> dict[str, Path]:
    """Generate headers + the two kernel .cpp + their csynth .tcl into *out_dir*."""
    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for comp_class in (MemRStream, MemWStream):
        spec = spec_for(comp_class)
        cpp = gen / f"{spec.kernel_name}.cpp"
        cpp.write_text(render_kernel(spec), encoding="utf-8")
        tcl = out_dir / f"{spec.kernel_name}.tcl"
        tcl.write_text(render_tcl(spec), encoding="utf-8")
        written[spec.kernel_name] = cpp
        print(f"generated {cpp.relative_to(out_dir)} + {tcl.name}")
    return written


if __name__ == "__main__":
    generate()
