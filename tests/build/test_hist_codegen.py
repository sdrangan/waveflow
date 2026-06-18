"""Phase 2: multi-buffer, multi-type m_axi IR + lowering for the histogram.

The histogram kernel issues three array ops against one m_axi bundle at three
different MemAddr fields, with two element types (Float32 reads, Uint32 write)
and per-buffer compile-time bounds. This asserts the extractor produces the
three stmts with the right (elem_type, count, addr, max) and that each lowers to
the correct ``<elem>_array_utils::{read,write}_array_slice`` call.
"""
from __future__ import annotations

from examples.shared_mem.hist import HistAccel
from waveflow.build.hwcodegen import extract_kernel
from waveflow.build.hwgen import (
    CodegenCtx,
    _emit_mm_array_read,
    _emit_mm_array_write,
)
from waveflow.hw.hwstmt import MMArrayReadStmt, MMArrayWriteStmt
from waveflow.simulation.simulation import Simulation


def _collect_mm_stmts(tree):
    out = []

    def walk(n):
        if isinstance(n, (MMArrayReadStmt, MMArrayWriteStmt)):
            out.append(n)
        if hasattr(n, "stmts"):
            for s in n.stmts:
                walk(s)
        for attr in ("if_true", "if_false", "body"):
            sub = getattr(n, attr, None)
            if sub is not None:
                walk(sub)

    walk(tree)
    return out


def _extract():
    sim = Simulation()
    comp = HistAccel(name="hist_accel", sim=sim)
    return comp, _collect_mm_stmts(extract_kernel(comp))


def test_three_array_stmts_with_distinct_addrs_types_bounds():
    """Extractor yields three array stmts: two Float32 reads + one Uint32 write,
    at three distinct addresses, each with its own compile-time max bound."""
    _comp, mm = _extract()
    assert len(mm) == 3

    data, edges, counts = mm
    assert isinstance(data, MMArrayReadStmt)
    assert isinstance(edges, MMArrayReadStmt)
    assert isinstance(counts, MMArrayWriteStmt)

    # Two element types over the one bundle.
    assert data.elem_type.cpp_class_name() == "float"
    assert edges.elem_type.cpp_class_name() == "float"
    assert counts.elem_type.cpp_class_name() == "ap_uint<32>"

    # Per-buffer compile-time bounds from the explicit max_count= args.
    assert data.max_expr.param_name == "max_ndata"
    assert edges.max_expr.param_name == "max_nbins"
    assert counts.max_expr.param_name == "max_nbins"


def test_data_read_lowers_to_float32_array_utils():
    comp, mm = _extract()
    ctx = CodegenCtx(comp=comp)
    cpp = _emit_mm_array_read(mm[0], ctx)
    assert "static float data[max_ndata];" in cpp
    assert ("float32_array_utils::read_array_slice<32>("
            "m_mem + memmgr::byte_addr_to_word_index<32>(cmd.data_addr), "
            "0, cmd.ndata, data);") in cpp


def test_edges_read_lowers_with_binop_count():
    """The edge count is the BinOp ``nbins - 1`` — it must lower verbatim."""
    comp, mm = _extract()
    ctx = CodegenCtx(comp=comp)
    cpp = _emit_mm_array_read(mm[1], ctx)
    assert "static float edges[max_nbins];" in cpp
    assert "byte_addr_to_word_index<32>(cmd.bin_edges_addr)" in cpp
    assert "0, cmd.nbins - 1, edges);" in cpp


def test_counts_write_lowers_to_uint32_array_utils():
    comp, mm = _extract()
    ctx = CodegenCtx(comp=comp)
    cpp = _emit_mm_array_write(mm[2], ctx)
    assert ("uint32_array_utils::write_array_slice<32>("
            "counts, m_mem + memmgr::byte_addr_to_word_index<32>(cmd.cnt_addr), "
            "0, cmd.nbins);") in cpp


def test_kernel_signature_and_pragmas():
    """Full kernel signature: stream + m_axi ports, ap_ctrl_hs, depth constant."""
    from waveflow.build.hwgen import kernel_to_cpp
    cpp = kernel_to_cpp(HistAccel)
    assert "ap_uint<32>* m_mem" in cpp
    assert ("#pragma HLS INTERFACE m_axi port=m_mem offset=slave "
            "bundle=gmem depth=m_mem_depth") in cpp
    assert "#pragma HLS INTERFACE ap_ctrl_hs port=return" in cpp


def test_kernel_lowers_hooks_and_three_array_ops():
    """The body factors the datapath into validate/compute/respond hooks and
    lowers the three array ops to the right typed array_utils bursts."""
    from waveflow.build.hwgen import kernel_to_cpp
    cpp = kernel_to_cpp(HistAccel)
    assert "ap_uint<8> status = hist_impl::validate(cmd);" in cpp
    assert "static float data[max_ndata];" in cpp
    assert "static float edges[max_nbins];" in cpp
    assert "static ap_uint<32> counts[32];" in cpp
    # compute returns an array → declared buffer + out-parameter call.
    assert "hist_impl::compute(data, edges, cmd.ndata, cmd.nbins, counts);" in cpp
    assert "float32_array_utils::read_array_slice<32>(" in cpp
    assert "uint32_array_utils::write_array_slice<32>(" in cpp
    assert "hist_impl::respond(m_out, cmd.tx_id, status);" in cpp


def test_header_constants_and_hook_decls():
    """Header emits the HwParam buffer bounds, the per-port depth, and the hook
    forward declarations (compute's array return becomes a void out-param)."""
    from waveflow.build.hwgen import header_to_cpp
    hpp = header_to_cpp(HistAccel)
    assert "static const int max_ndata = 1024;" in hpp
    assert "static const int max_nbins = 32;" in hpp
    assert "static const int m_mem_depth = max_ndata + max_nbins + max_nbins;" in hpp
    assert "ap_uint<8> validate(HistCmd cmd);" in hpp
    assert ("void compute(float data[1024], float edges[32], int ndata, "
            "int nbins, ap_uint<32> out[32]);") in hpp
    # respond is templated and #include'd once (deduped across its two call sites).
    assert hpp.count('#include "hist_respond_impl.tpp"') == 1
    # typing-only buffer DataArrays contribute no struct header includes.
    assert "hist_data_buf" not in hpp


def test_missing_max_count_fails_loudly():
    """A read with no max_count= has no resolvable buffer bound — fail loudly
    rather than emit an unsized array (no global max_n fallback)."""
    import pytest

    from waveflow.build.hwcodegen import SynthesisError

    comp, mm = _extract()
    ctx = CodegenCtx(comp=comp)
    stmt = mm[0]
    stmt.kwargs = {}   # drop the max_count
    with pytest.raises(SynthesisError, match="no compile-time buffer bound"):
        _emit_mm_array_read(stmt, ctx)


# ---------------------------------------------------------------------------
# Phase 5: generated testbench (HistTBHls -> gen/hist_tb.cpp)
# ---------------------------------------------------------------------------

def _gen_tb() -> str:
    from examples.shared_mem.hist import HistTBHls
    from waveflow.build.hwgen import tb_files_to_str
    files = tb_files_to_str(HistTBHls)
    assert len(files) == 1
    return next(iter(files.values()))


def test_tb_int_expr_reuses_kernel_lowerer():
    """The TB count lowering delegates to the kernel's expression lowerer, so a
    count like ``nbins - 1`` lowers identically on both sides (single source of
    truth — they must never diverge)."""
    import ast
    from waveflow.build.hwgen import _emit_ast_expr, _emit_int_expr
    for src in ("cmd.nbins - 1", "cmd.ndata", "cmd.nbins", "5"):
        node = ast.parse(src, mode="eval").body
        assert _emit_int_expr(node, None) == _emit_ast_expr(node, None)


def test_tb_edges_count_lowers_as_binop():
    """The edges region's count is ``nbins - 1`` (BinOp), reused from the kernel
    lowerer; it must appear verbatim in the read and the alloc/populate."""
    tb = _gen_tb()
    assert "read_uint32_file_array(edges, (data_dir + std::string(\"/edges_array.bin\")).c_str(), cmd.nbins - 1)" in tb
    assert "get_nwords<32>(cmd.nbins - 1)" in tb
    assert "write_array_slice<32>(edges, mem + _cmd_bin_edges_addr_widx, 0, cmd.nbins - 1)" in tb


def test_tb_allocs_clamp_to_one_word():
    """Every region's alloc clamps to >= 1 word — the robustness that lets the TB
    drive the nbins==1 (zero-edge) and validation-failure (zero-count) cases
    without MemMgr::alloc rejecting a 0-word region. Covers all three allocs,
    including counts (the validation-fail path)."""
    tb = _gen_tb()
    for field in ("data", "bin_edges", "cnt"):
        assert (f"mem_mgr.alloc(_cmd_{field}_addr_nwords > 0 ? "
                f"_cmd_{field}_addr_nwords : 1)") in tb


def test_tb_drives_generated_kernel():
    """The TB reads the command, runs the kernel via its m_axi signature, and
    writes the response + counts back for functional verify."""
    tb = _gen_tb()
    assert "streamutils::read_uint32_file(cmd," in tb
    assert "hist(s_in, m_out, mem);" in tb
    assert "write_uint32_file(resp," in tb
    assert "write_uint32_file_array(out," in tb
