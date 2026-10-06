"""A csim/csynth harness for the SSR FFT task chain (F2 of ``plans/ssr_fft.md``).

Builds, for one :class:`~waveflow.dsp.ssr_fft.model.Geometry`, a free-running top that chains every
task by hand -- the transposer's commutators, each stage, the commutator after it -- and a
testbench that pushes frames back to back and records every output word.  The output is the last
stage's order (digit-reversed); the reorder joins in F3, as does the generated top.

Used by ``test_hls_chain.py``; runnable on its own for a quick look::

    python -m tests.dsp.ssr_fft.hls_chain 64 /tmp/ssr64
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from waveflow.dsp.ssr_fft import hls
from waveflow.dsp.ssr_fft.model import Geometry, pipeline, to_words

PART = "xczu48dr-ffvg1517-2-e"


def chain_tasks(geo: Geometry, natural: bool = True) -> list[tuple[str, str, str]]:
    """``(instance, wrapper, out edge)`` per task, in order; each reads the previous one's edge.

    The same generated wrappers the real top instantiates (:func:`hls.task_instances`), minus the
    two lane adaptors: this harness drives the first word stream and reads the last directly.  With
    ``natural``, the reorder follows: its commutator ``rc``, the SOB writer (into ``blk``) and the
    reader (edge ``out``)."""
    ns = hls.config_namespace(geo)
    out = []
    for t in hls.task_instances(geo, natural=natural)[1:-1]:
        edge = {"reorder_write": "blk", "reorder_read": "out"}.get(t.kind, t.inst)
        out.append((f"t_{t.inst}", f"{ns}_{t.inst}", edge))
    return out


def last_edge(natural: bool, geo: Geometry) -> str:
    return "out" if natural else f"st{geo.S - 1}"


def render_top(geo: Geometry, name: str = "chain_top", natural: bool = True) -> str:
    ns = hls.config_namespace(geo)
    last = last_edge(natural, geo)
    tasks = chain_tasks(geo, natural)
    lines = [
        '#include "hls_task.h"',
        '#include "hls_streamofblocks.h"',
        f'#include "{hls.wrappers_header(geo)}"',
        "",
        f"void {name}(hls::stream<ap_uint<{ns}::e_in::W> >& s_in,",
        f"           hls::stream<ap_uint<{ns}::e_{last}::W> >& s_out) {{",
        "#pragma HLS INTERFACE axis port = s_in",
        "#pragma HLS INTERFACE axis port = s_out",
        "#pragma HLS INTERFACE ap_ctrl_none port = return",
    ]
    for _, _, edge in tasks[:-1]:
        if edge == "blk":
            lines.append(f"    hls_thread_local hls::stream_of_blocks<ap_uint<{ns}::e_rc::W>"
                         f"[{geo.n_words}], 2> blk;")
        else:
            lines.append(f"    hls_thread_local hls::stream<ap_uint<{ns}::e_{edge}::W> > {edge};")
            if edge == "rc":
                # CSIM ONLY.  With the default depth-2 FIFO here, csim corrupts the frames when the
                # commutator back-pressures into the SOB writer -- though the commutator alone under
                # back-pressure, and the SOB pair alone behind other tasks, are both exact in csim.
                # csim of hls::task + stream_of_blocks is not authoritative (XSI is); the RTL rung
                # (F3) runs the reorder at the default depth.
                lines.append(f"#pragma HLS STREAM variable = rc depth = {geo.n_words}")
    src = "s_in"
    for inst, fn, edge in tasks:
        dst = "s_out" if edge == last else edge
        lines.append(f"    hls_thread_local hls::task {inst}({fn}, {src}, {dst});")
        src = edge
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_tb(geo: Geometry, n_frames: int, name: str = "chain_top", natural: bool = True) -> str:
    ns = hls.config_namespace(geo)
    last = f"e_{last_edge(natural, geo)}"
    return f"""\
#include <cstdio>
#include "hls_task.h"
#include "{hls.config_header(geo)}"
#include "streamutils_hls.h"

void {name}(hls::stream<ap_uint<{ns}::e_in::W> >& s_in,
           hls::stream<ap_uint<{ns}::{last}::W> >& s_out);

// in.txt: {n_frames} frames x {geo.L} lines "re im" (stored integers, natural order).
// out.txt: one line "re im" per output lane, word by word.
int main(int argc, char** argv) {{
    typedef {ns}::e_in EI;
    typedef {ns}::{last} EO;
    typedef EI::value_type::value_type in_t;
    const char* dir = argc > 1 ? argv[1] : ".";
    char path[1024];
    snprintf(path, sizeof path, "%s/in.txt", dir);
    FILE* fi = fopen(path, "r");
    if (!fi) {{ printf("cannot open %s\\n", path); return 1; }}
    hls::stream<ap_uint<EI::W> > s_in;
    hls::stream<ap_uint<EO::W> > s_out;
    for (int w = 0; w < {n_frames * geo.n_words}; w++) {{
        EI::value_type x[4];
        for (int r = 0; r < 4; r++) {{
            long re, im;
            if (fscanf(fi, "%ld %ld", &re, &im) != 2) {{ printf("short input\\n"); return 1; }}
            x[r] = EI::value_type(streamutils::bits_to_fixed<in_t>((ap_uint<in_t::width>)re),
                                  streamutils::bits_to_fixed<in_t>((ap_uint<in_t::width>)im));
        }}
        EI::write(s_in, x);
    }}
    fclose(fi);
    {name}(s_in, s_out);
    snprintf(path, sizeof path, "%s/out.txt", dir);
    FILE* fo = fopen(path, "w");
    typedef EO::value_type::value_type out_t;
    for (int w = 0; w < {n_frames * geo.n_words}; w++) {{
        EO::value_type y[4];
        EO::read(s_out, y);
        for (int r = 0; r < 4; r++) {{
            // The stored integer: the bit pattern reinterpreted as a signed out_t::width-bit int.
            ap_int<out_t::width> re = streamutils::fixed_to_bits<out_t>(y[r].real());
            ap_int<out_t::width> im = streamutils::fixed_to_bits<out_t>(y[r].imag());
            fprintf(fo, "%lld %lld\\n", (long long)re.to_int64(), (long long)im.to_int64());
        }}
    }}
    fclose(fo);
    printf("SSR_CHAIN_DONE %d words\\n", {n_frames * geo.n_words});
    return 0;
}}
"""


def render_tcl(geo: Geometry, *, csynth: bool, period_ns: float = 4.0, name: str = "chain_top") -> str:
    synth = ("if {[catch {csynth_design} res]} { puts \"GATE_ERROR csynth: $res\"; exit 1 }\n"
             "puts \"GATE_CSYNTH_DONE\"\n") if csynth else ""
    return (
        f"open_project -reset proj\n"
        f"set_top {name}\n"
        f'add_files top.cpp -cflags "-I. -std=c++14"\n'
        f'add_files -tb tb.cpp -cflags "-I. -std=c++14"\n'
        "open_solution -reset sol -flow_target vivado\n"
        f"set_part {{{PART}}}\n"
        f"create_clock -period {period_ns}\n"
        "config_rtl -reset state\n"
        "set here [file dirname [file normalize [info script]]]\n"
        'if {[catch {csim_design -argv "$here"} res]} { puts "GATE_ERROR csim: $res"; exit 1 }\n'
        'puts "GATE_CSIM_DONE"\n'
        f"{synth}exit 0\n"
    )


def frames(geo: Geometry, n_frames: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """``(n_frames, L)`` stored-integer inputs; frame 0 is the most-negative corner."""
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (geo.in_w - 1)), 1 << (geo.in_w - 1)
    re = rng.integers(lo, hi, (n_frames, geo.L))
    im = rng.integers(lo, hi, (n_frames, geo.L))
    re[0], im[0] = lo, hi - 1
    return re, im


def write(geo: Geometry, root: Path, n_frames: int, *, csynth: bool, natural: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    hls.write_sources(geo, root, natural=natural)
    (root / "top.cpp").write_text(render_top(geo, natural=natural), encoding="utf-8")
    (root / "tb.cpp").write_text(render_tb(geo, n_frames, natural=natural), encoding="utf-8")
    (root / "run.tcl").write_text(render_tcl(geo, csynth=csynth), encoding="utf-8")
    re, im = frames(geo, n_frames)
    mask = (1 << geo.in_w) - 1
    lines = [f"{int(r) & mask} {int(i) & mask}" for r, i in zip(re.reshape(-1), im.reshape(-1))]
    (root / "in.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def expected(geo: Geometry, n_frames: int, natural: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """The model's output in the same order: ``(n_frames * L/R, R)``."""
    re, im = frames(geo, n_frames)
    outs = [pipeline(geo, to_words(re[f]), to_words(im[f]), natural=natural) for f in range(n_frames)]
    return (np.concatenate([o[0] for o in outs]), np.concatenate([o[1] for o in outs]))


def read_out(geo: Geometry, root: Path, n_frames: int) -> tuple[np.ndarray, np.ndarray]:
    vals = np.loadtxt(root / "out.txt", dtype=np.int64).reshape(-1, 2)
    return vals[:, 0].reshape(-1, geo.R), vals[:, 1].reshape(-1, geo.R)


if __name__ == "__main__":
    n = int(sys.argv[1])
    write(Geometry(n), Path(sys.argv[2]), 4, csynth=len(sys.argv) > 3)
