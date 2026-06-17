# shared_mem (histogram) codegen notes

All C++ for this example is generated from `hist.py`: the kernel + header
(`gen/hist.cpp` / `gen/hist.hpp`) from `HistAccel`, and the testbench
(`gen/hist_tb.cpp`) from `HistTBHls`. `generate_vitis_sources` (in
`hist_build.py`) emits the Vitis include headers and these files; `run.tcl`
compiles them against the hand-written datapath hooks. There are no hand-written
kernel/testbench files — `hist.py` is the source of truth.

## Kernel (`gen/hist.cpp` / `gen/hist.hpp`)

- **Signature / pragmas:** `void hist(s_in, m_out, m_mem)` — two AXI4-Stream
  ports + one `m_axi` pointer; `m_axi` pragma `offset=slave bundle=gmem
  depth=...`, plus `ap_ctrl_hs port=return`.
- **Three array ops** lower to resident-range slice bursts (element coordinates):
  `float32_array_utils::read_array_slice<32>(ptr, 0, n, data)` for data and edges,
  `uint32_array_utils::write_array_slice<32>(counts, ptr, 0, n)` for counts, each via
  `memmgr::byte_addr_to_word_index<32>(cmd.<addr>)`, into `static float data[...]`,
  `static float edges[...]`, `static ap_uint<32> counts[...]`.
- **Datapath is factored into hand-written hooks** — `hist_impl::validate(cmd)`,
  `hist_impl::compute(..., counts)`, `hist_impl::respond(m_out, tx_id, status)`
  (`hist_validate_impl.cpp`, `hist_compute_impl.cpp`, `hist_respond_impl.tpp`).
  These encode the bounds/alignment checks, the binning loop, and the response
  writes — the datapath the Python `forward`/`@synthesizable` hooks describe.
- **Compute returns an array:** `counts = compute(...)` lowers to a declared
  `static ap_uint<32> counts[...]` + a `void` call passing `counts` as the
  trailing out-parameter (HLS can't return arrays by value).
- **Header constants:** the HwParam buffer bounds (`max_ndata`, `max_nbins`), the
  per-port `m_mem_depth`, plus `max_mem_words` and the interface widths /
  `axis_word_t` / `mem_word_t` typedefs the testbench needs (emitted only when an
  `m_axi` master is present, so other examples are unaffected).
- **Edges read is unconditional** (count `nbins-1`, which is `0` when `nbins==1`)
  rather than guarded by `if (nbins > 1)` — the extractor can't lower a `>`
  branch, and a zero-length burst is a no-op.

Functional equivalence to the `HistogramAccel` numpy golden is proven
empirically: C-sim (`test_hist_csim.py`) across nbins==1 / nbins>1 / a
validation-failure case, and RTL cosim + multi-buffer burst extraction
(`test_hist_cosim.py`).

## Testbench (`gen/hist_tb.cpp` from `HistTBHls`)

The TB codegen lowers the same way the kernel does: counts like `nbins - 1` go
through the shared `_emit_ast_expr` lowerer (single source of truth), and each
`MemMgr::alloc` is clamped to `>= 1` word (a 0-word region is meaningless and
`alloc` rejects it; mirrors the SimPy `HistController`'s `max(nedges, 1)`).

### Known limitation — nbins-based validation isn't drivable through the generated TB

The C-sim/cosim coverage uses **`ndata == 0`** (INVALID_NDATA) as the
validation-failure case, **not `nbins == 0`**. The generated TB reads
`count = nbins - 1` edges *unconditionally* (it mirrors the kernel's guard-free
read; the extractor only lowers `==`/`!=` `CaseStmt`s, not an `if (nbins > 1)`
guard). So:

- `nbins == 0` → edges count `-1`, which the file reader rejects (`n0 must be
  non-negative`).
- `nbins > max_nbins` → overruns the fixed `edges[max_nbins]` TB buffer.

`ndata == 0` cleanly exercises the validate→status / early-return / error-response
path and the `>= 1` alloc clamp (data count is 0). The **counts**-alloc clamp is
the same emitted expression (asserted by `test_tb_allocs_clamp_to_one_word`), it
just isn't hit at runtime through this TB.

**Future option (deferred, not a now-task):** the real fix is general `>`/`>=`
guard lowering — the deferred condition-IR work — which would let the TB express
`if (nbins > 1)`. A narrower stopgap would be to lower a clamped count like
`max(nbins - 1, 0)` so the edges read is always non-negative; that alone would
make `nbins == 0` drivable (the kernel rejects it before any real read anyway).
Neither is needed to prove the validation→status codegen path, which `ndata == 0`
already covers.
