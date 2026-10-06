"""What the C++ needs from Python: the generated configuration of one SSR FFT.

The task bodies (``src/ssr_fft_tasks.h``) are hand-written and generic; everything that depends on
``L`` and the formats is generated here, from the same :class:`~.model.Geometry` the model uses:

* the ``<elem>_array_utils.h`` of every edge's lane type (the only packing code there is);
* ``ssr_fft_<key>.h``: per edge, an adaptor struct ``e_<edge>`` (``value_type``, ``W``,
  ``read``/``write`` delegating to those utils); per stage, a struct ``st<s>`` with the stage's
  formats, its sub-transform length and its twiddle ROM.

So a format can never be written twice: Python derives it once and both backends read it.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from waveflow.build.build import BuildConfig
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import _array_utils_filename, _array_utils_namespace, gen_array_utils
from waveflow.utils.fixputils import Format
from waveflow.vitis_l1.cxquant import fixed_from_format
from waveflow.vitis_l1.fft import _exp_table, _stage_formats, exp_table_format

from .model import Geometry
from .types import EdgeType, edge_types

SRC_DIR = Path(__file__).resolve().parent / "src"
TASKS_H = "ssr_fft_tasks.h"


def config_key(geo: Geometry) -> str:
    """Identifies one configuration: ``L64_16_2_18_2`` (L, input format, twiddle format)."""
    return f"L{geo.L}_{geo.in_w}_{geo.in_i}_{geo.tw_w}_{geo.tw_i}"


def config_namespace(geo: Geometry) -> str:
    return f"ssr_fft_{config_key(geo)}"


def config_header(geo: Geometry) -> str:
    return f"{config_namespace(geo)}.h"


def _cpp(fmt: Format) -> str:
    return fixed_from_format(fmt).cpp_type


def _real(stored: int, fmt: Format) -> str:
    """A stored integer as an exact C++ literal of its value (``stored / 2^F`` is exact in double)."""
    return repr(float(stored) / float(1 << fmt.frac_bits))


def stage_in_edge(geo: Geometry, s: int) -> str:
    """The edge stage ``s`` reads: the last transposer edge, or the previous commutator's."""
    return f"tp{geo.S - 2}" if s == 0 else f"cm{s - 1}"


def twiddle_rom(geo: Geometry, s: int) -> tuple[list[int], list[int]]:
    """Stage ``s``'s ROM: entry ``j`` is ``W_L^(j R^s)``; the stage reads index ``m*q``.

    ``m < L/R^(s+1)`` and ``q < R``, so ``m*q`` never reaches ``L/R^s``: no modulo in hardware.
    """
    tw_r, tw_i = geo.twiddles
    n = 3 * (geo.m_count(s) - 1) + 1
    step = geo.R ** s
    return ([int(tw_r[step * j]) for j in range(n)], [int(tw_i[step * j]) for j in range(n)])


def _edge_struct(e: EdgeType) -> str:
    ns = _array_utils_namespace(e.elem)
    return "\n".join([
        f"struct e_{e.name} {{",
        f"    typedef {ns}::value_type value_type;",
        f"    static const int W = {e.bitwidth};",
        f"    static void read(hls::stream<ap_uint<W> >& s, value_type x[{e.R}]) {{",
        "#pragma HLS INLINE",
        f"        {ns}::read_stream_lane<W>(s, x);",
        "    }",
        f"    static void write(hls::stream<ap_uint<W> >& s, const value_type y[{e.R}]) {{",
        "#pragma HLS INLINE",
        f"        {ns}::write_stream_lane<W>(y, s);",
        "    }",
        "};",
    ])


def _rom_fn(name: str, t: str, values: list[int], fmt: Format) -> str:
    body = ", ".join(_real(v, fmt) for v in values)
    return "\n".join([
        f"    static {t} {name}(int i) {{",
        "#pragma HLS INLINE",
        f"        static const {t} t[{len(values)}] = {{{body}}};",
        "        return t[i];",
        "    }",
    ])


def _stage_struct(geo: Geometry, s: int) -> str:
    f_in, _ = geo.stage_fmts[s]
    fprod, facc1, facc2 = _stage_formats(f_in, s == 0, geo.mode)
    ftw = exp_table_format(geo.tw_w, geo.tw_i)
    _, ex_r, ex_i = _exp_table(geo.tw_w, geo.tw_i)
    rotate = s < geo.S - 1
    lines = [
        f"struct st{s} {{",
        f"    typedef e_{stage_in_edge(geo, s)} in_e;",
        f"    typedef e_st{s} out_e;",
        f"    typedef {_cpp(fprod)} prod_t;",
        f"    typedef {_cpp(facc1)} acc1_t;",
        f"    typedef {_cpp(facc2)} acc2_t;",
        f"    typedef {_cpp(geo.stage_out_fmt(s))} out_t;",
        f"    typedef {_cpp(ftw)} tw_t;",
        f"    static const bool ROTATE = {'true' if rotate else 'false'};",
        f"    static const int MC = {geo.m_count(s)};",
        _rom_fn("ex_re", "tw_t", [int(v) for v in ex_r[:geo.R]], ftw),
        _rom_fn("ex_im", "tw_t", [int(v) for v in ex_i[:geo.R]], ftw),
    ]
    if rotate:
        rr, ri = twiddle_rom(geo, s)
        lines += [_rom_fn("tw_re", "tw_t", rr, ftw), _rom_fn("tw_im", "tw_t", ri, ftw)]
    else:
        lines += ["    static tw_t tw_re(int) { return tw_t(0); }",
                  "    static tw_t tw_im(int) { return tw_t(0); }"]
    lines.append("};")
    return "\n".join(lines)


def lane_edges(geo: Geometry) -> list[EdgeType]:
    """The boundary lanes: one complex sample per word, in (``lane_in``) and out (``lane_out``)."""
    return [EdgeType("lane_in", geo.in_fmt, 1), EdgeType("lane_out", geo.out_fmt, 1)]


def _lane_struct(e: EdgeType) -> str:
    ns = _array_utils_namespace(e.elem)
    return "\n".join([
        f"struct e_{e.name} {{",
        f"    typedef {ns}::value_type value_type;",
        f"    static const int W = {e.bitwidth};",
        "    static void read(hls::stream<ap_uint<W> >& s, value_type* x) {",
        "#pragma HLS INLINE",
        f"        {ns}::read_stream_lane<W>(s, x);",
        "    }",
        "    static void write(hls::stream<ap_uint<W> >& s, const value_type* y) {",
        "#pragma HLS INLINE",
        f"        {ns}::write_stream_lane<W>(y, s);",
        "    }",
        "};",
    ])


def render_config(geo: Geometry) -> str:
    """The whole configuration header for ``geo``."""
    edges = edge_types(geo)
    ns = config_namespace(geo)
    guard = f"WAVEFLOW_{ns.upper()}_H"
    incs = sorted({_array_utils_filename(e.elem) for e in edges + lane_edges(geo)})
    out = [
        f"#ifndef {guard}",
        f"#define {guard}",
        f"// Generated by waveflow.dsp.ssr_fft.hls for {config_key(geo)} -- do not edit.",
        "// Edge adaptors over the generated lane routines, and each stage's formats and ROMs.",
        "#include <ap_fixed.h>",
        "#include <ap_int.h>",
        "#include <hls_stream.h>",
        *[f'#include "{h}"' for h in incs],
        "",
        f"namespace {ns} {{",
        "",
        f"static const int L = {geo.L};",
        f"static const int S = {geo.S};",
        "",
    ]
    out += [_edge_struct(e) + "\n" for e in edges]
    out += [_lane_struct(e) + "\n" for e in lane_edges(geo)]
    out += [_stage_struct(geo, s) + "\n" for s in range(geo.S)]
    out += [f"}}  // namespace {ns}", "", f"#endif  // {guard}", ""]
    return "\n".join(out)


@dataclass(frozen=True)
class TaskInstance:
    """One ``hls::task`` of the pipeline, in order.

    ``inst`` names it (the wrapper is ``<namespace>_<inst>``, so the RTL instance is
    ``<namespace>_<inst>_U0`` -- predictable, unlike a template instantiation's); ``kind`` is which
    body; ``call`` the templated body it wraps; ``params`` its C++ parameters, in order, as
    ``(name, type)``; ``d``/``s`` the commutator block size / stage index where they apply.
    """
    inst: str
    kind: str
    call: str
    params: tuple[tuple[str, str], ...]
    d: int = 0
    s: int = -1


REORDERS = ("sob", "pingpong")


def task_instances(geo: Geometry, *, natural: bool = True, reorder: str = "sob"
                   ) -> list[TaskInstance]:
    """Every task of one SSR FFT, in pipeline order, from the boundary lanes in to the lanes out.

    ``lanes_in`` -> the transposer's ``S - 1`` commutators -> per stage: the stage, then (all but
    the last) its commutator -> with ``natural``, the reorder: its commutator ``rc``, then either
    the SOB writer ``rw`` and reader ``rr`` (``reorder="sob"``) or one ping-pong task ``rp``
    (``"pingpong"``) -> ``lanes_out``.
    """
    if reorder not in REORDERS:
        raise ValueError(f"reorder={reorder!r}: one of {REORDERS}")
    from .model import reorder_d

    ns = config_namespace(geo)

    def stream(edge: str) -> str:
        return f"hls::stream<ap_uint<{ns}::e_{edge}::W> >&"

    lane_in = [(f"s_in_{j}", stream("lane_in")) for j in range(geo.R)]
    lane_out = [(f"m_out_{j}", stream("lane_out")) for j in range(geo.R)]
    sob = f"hls::stream_of_blocks<ap_uint<{ns}::e_rc::W>[{geo.n_words}]>&"
    out = [TaskInstance("lanes_in", "lanes_in",
                        f"ssr_fft_hls::ssr_fft_lanes_in_task<{ns}::e_lane_in, {ns}::e_in>",
                        (*lane_in, ("m_out", stream("in"))))]
    prev = "in"
    for k, d in enumerate(geo.transposer_ds):
        out.append(TaskInstance(f"tp{k}", "commutator",
                                f"ssr_fft_hls::ssr_fft_commutator_task<{ns}::e_tp{k}, {d}>",
                                (("s_in", stream(prev)), ("m_out", stream(f"tp{k}"))), d=d))
        prev = f"tp{k}"
    for s in range(geo.S):
        out.append(TaskInstance(f"st{s}", "stage", f"ssr_fft_hls::ssr_fft_stage_task<{ns}::st{s}>",
                                (("s_in", stream(prev)), ("m_out", stream(f"st{s}"))), s=s))
        prev = f"st{s}"
        if s < geo.S - 1:
            d = geo.stage_d(s)
            out.append(TaskInstance(f"cm{s}", "commutator",
                                    f"ssr_fft_hls::ssr_fft_commutator_task<{ns}::e_cm{s}, {d}>",
                                    (("s_in", stream(prev)), ("m_out", stream(f"cm{s}"))), d=d))
            prev = f"cm{s}"
    if natural:
        d = reorder_d(geo)
        out.append(TaskInstance("rc", "commutator",
                                f"ssr_fft_hls::ssr_fft_commutator_task<{ns}::e_rc, {d}>",
                                (("s_in", stream(prev)), ("m_out", stream("rc"))), d=d))
        if reorder == "pingpong":
            out.append(TaskInstance(
                "rp", "reorder_pingpong",
                f"ssr_fft_hls::ssr_fft_reorder_pingpong_task<{ns}::e_rc, {geo.n_words}, {geo.S - 1}>",
                (("s_in", stream("rc")), ("m_out", stream("out"))), d=geo.n_words))
        else:
            out.append(TaskInstance(
                "rw", "reorder_write",
                f"ssr_fft_hls::ssr_fft_reorder_write_task<{ns}::e_rc, {geo.n_words}, {geo.S - 1}>",
                (("s_in", stream("rc")), ("m_blk", sob))))
            out.append(TaskInstance(
                "rr", "reorder_read",
                f"ssr_fft_hls::ssr_fft_reorder_read_task<{ns}::e_out, {geo.n_words}>",
                (("s_blk", sob), ("m_out", stream("out")))))
        prev = "out"
    out.append(TaskInstance("lanes_out", "lanes_out",
                            f"ssr_fft_hls::ssr_fft_lanes_out_task<{ns}::e_{prev}, {ns}::e_lane_out>",
                            (("s_in", stream(prev)), *lane_out)))
    return out


def wrappers_header(geo: Geometry) -> str:
    return f"{config_namespace(geo)}_tasks.h"


def render_wrappers(geo: Geometry, *, natural: bool = True, reorder: str = "sob") -> str:
    """One plain function per task: what the generated top instantiates as an ``hls::task``."""
    ns = config_namespace(geo)
    guard = f"WAVEFLOW_{ns.upper()}_TASKS_H"
    out = [f"#ifndef {guard}", f"#define {guard}",
           f"// Generated by waveflow.dsp.ssr_fft.hls for {config_key(geo)} -- do not edit.",
           "// One wrapper per task, so each RTL instance is <wrapper>_U0.",
           '#include "hls_stream.h"', '#include "hls_streamofblocks.h"',
           f'#include "{config_header(geo)}"', f'#include "{TASKS_H}"', ""]
    for t in task_instances(geo, natural=natural, reorder=reorder):
        args = ", ".join(f"{ty} {nm}" for nm, ty in t.params)
        names = ", ".join(nm for nm, _ in t.params)
        out += [f"static void {ns}_{t.inst}({args}) {{", f"    {t.call}({names});", "}", ""]
    out += [f"#endif  // {guard}", ""]
    return "\n".join(out)


def write_sources(geo: Geometry, root: Path | str, include_dir: str = ".", *,
                  natural: bool = True, reorder: str = "sob") -> Path:
    """Write everything the task bodies include into ``root/include_dir``: streamutils, every
    edge's array utils, the configuration header, the task wrappers, and the task bodies."""
    root = Path(root)
    inc = root / include_dir
    inc.mkdir(parents=True, exist_ok=True)
    StreamUtilsStep(output_dir=include_dir).run(BuildConfig(root_dir=root))
    widths: dict = {}
    for e in edge_types(geo) + lane_edges(geo):
        widths.setdefault(e.elem, set()).add(e.bitwidth)
    for elem, ws in widths.items():
        gen_array_utils(elem, sorted(ws), cfg=BuildConfig(root_dir=inc))
    (inc / config_header(geo)).write_text(render_config(geo), encoding="utf-8")
    (inc / wrappers_header(geo)).write_text(render_wrappers(geo, natural=natural, reorder=reorder), encoding="utf-8")
    shutil.copy(SRC_DIR / TASKS_H, inc / TASKS_H)
    return inc
