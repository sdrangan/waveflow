---
title: Vitis HLS Code Generation
parent: Histogram with shared memory
nav_order: 4
has_children: false
---

# Vitis HLS Code Generation

The same `HistAccel` Python class that ran in SimPy is the source the Vitis HLS
kernel is generated from. Waveflow emits everything mechanical — the `m_axi`
function signature, the interface pragmas, the multi-buffer burst calls, the
validation-status plumbing — and leaves only the datapath hooks as hand-written
C++. This page walks the generated kernel, the generated testbench, and the seam
between generated and hand-written code.

## Running the generation

For the histogram example the generation is a small helper,
`generate_vitis_sources`, in
[`hist_build.py`](../../../examples/shared_mem/hist_build.py). It
writes the support headers, then lowers the kernel and the testbench:

```python
# examples/shared_mem/hist_build.py — generate_vitis_sources
(gen / "hist.cpp").write_text(kernel_to_cpp(HistAccel))
(gen / "hist.hpp").write_text(header_to_cpp(HistAccel))
for fname, content in tb_files_to_str(HistTBHls).items():
    (gen / fname).write_text(content)          # gen/hist_tb.cpp
```

`kernel_to_cpp(HistAccel)` lowers the `on_start` body; `tb_files_to_str(HistTBHls)`
lowers the testbench's `main()`. Both come from the **same Python source** you
already simulated — there is no second description of the kernel to keep in sync.

After it runs, `gen/` holds three framework-owned files, and three hand-written
hook files sit next to the Python source:

| File | Owner | Lifecycle |
| ---- | ----- | --------- |
| `gen/hist.hpp` | framework | rewritten each run |
| `gen/hist.cpp` | framework | rewritten each run |
| `gen/hist_tb.cpp` | framework | rewritten each run |
| `hist_validate_impl.cpp` | user | committed — the validation logic |
| `hist_compute_impl.cpp` | user | committed — the binning datapath |
| `hist_respond_impl.tpp` | user | committed — the response write |

`gen/` is `.gitignored` — generated files are never committed; the hooks **are**
committed because they hold logic no codegen pass reproduces.

## The kernel signature and pragmas

The generated top-level function is where the three Python ports become the three
AXI interfaces:

```cpp
// gen/hist.cpp
void hist(
    hls::stream<streamutils::axi4s_word<32>>& s_in,
    hls::stream<streamutils::axi4s_word<32>>& m_out,
    ap_uint<32>* m_mem
) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=m_mem offset=slave bundle=gmem depth=m_mem_depth
#pragma HLS INTERFACE s_axilite port=return       bundle=control
```

- **`s_in` / `m_out`** become AXI4-Stream ports (`axis`) — the command in, the
  response out.
- **`m_mem`** becomes the AXI memory-mapped master: a plain `ap_uint<32>*` with an
  `m_axi` pragma. `offset=slave` means the base address is itself programmed over
  the control interface; `bundle=gmem` puts all of this kernel's memory traffic on
  one shared port.
- **`depth=m_mem_depth`** sizes the interface for co-simulation. That constant is
  the **sum of the buffer maxima** — the worst-case number of words the kernel
  could touch:

  ```cpp
  // gen/hist.hpp
  static const int m_mem_depth = max_ndata + max_nbins + max_nbins;
  ```

  i.e. `data` (`max_ndata`) + `edges` (`max_nbins`) + `counts` (`max_nbins`).
  The `max_ndata` / `max_nbins` HwParams from the Python model drive this directly.
- **`s_axilite port=return`** is the start/done handshake, exposed as registers in the
  control slave — the same control contract as the [regmap example](../regmap/).
  It comes from `HistAccel` being a `HostActivated` with a **control-only**
  `VitisRegMap({})`: no application registers, because the command rides in-band on
  `s_in`. Note the slave is *not* new — `offset=slave` on the `m_axi` pragma above
  already forced one into existence to hold `m_mem`'s base address at `0x10`, and it
  declared `0x00` as *reserved*. Declaring the kind is what fills `0x00` with
  `ap_start`/`ap_done`, so one CPU both programs the base address and launches the
  kernel, over the same interface.

## The body: validate, then multi-buffer bursts

The lowered `on_start` reads exactly like the Python it came from:

```cpp
// gen/hist.cpp
HistCmd cmd;
cmd.read_axi4_stream<32>(s_in);
ap_uint<8> status = hist_impl::validate(cmd);
if (status != (ap_uint<8>)static_cast<unsigned int>(HistError::NO_ERROR)) {
    hist_impl::respond(m_out, cmd.tx_id, status);
    return;
}
static float data[max_ndata];
float32_array_utils::read_array_slice<32>(m_mem + memmgr::byte_addr_to_word_index<32>(cmd.data_addr), 0, cmd.ndata, data);
static float edges[max_nbins];
float32_array_utils::read_array_slice<32>(m_mem + memmgr::byte_addr_to_word_index<32>(cmd.bin_edges_addr), 0, cmd.nbins - 1, edges);
static ap_uint<32> counts[32];
hist_impl::compute(data, edges, cmd.ndata, cmd.nbins, counts);
uint32_array_utils::write_array_slice<32>(counts, m_mem + memmgr::byte_addr_to_word_index<32>(cmd.cnt_addr), 0, cmd.nbins);
hist_impl::respond(m_out, cmd.tx_id, status);
```

The pieces this example exists to exercise:

1. **Multi-buffer, multi-type lowering.** Three `read_array_slice`/`write_array_slice` calls
   target the **one** `m_mem` pointer at three different command addresses, with
   **two** element types — `float32_array_utils::read_array_slice` for `data` and
   `edges`, `uint32_array_utils::write_array_slice` for `counts`. Each Python
   `read_array(Float32, ...)` / `write_array(Uint32Field, ...)` chose its
   array-utils namespace from the schema's element type.
2. **Byte address → word index.** Every burst wraps the command's byte address in
   `memmgr::byte_addr_to_word_index<32>(...)` before indexing the word pointer —
   the same conversion the SimPy `DirectMMIF` did.
3. **The runtime count is the Python expression, verbatim.** `cmd.ndata` and
   `cmd.nbins - 1` are lowered straight from the model's `read_array` arguments —
   including the `nbins - 1` edge count, a no-op burst when `nbins == 1`.
4. **Compute returns an array via an out-parameter.** HLS cannot return an array
   by value, so the generator declares the `static ap_uint<32> counts[...]` buffer
   in the kernel and passes it to `hist_impl::compute(..., counts)` — the Python
   `compute` "returns" the counts; the C++ fills them in place.
5. **Validation is a status, checked once.** `validate` returns an `ap_uint<8>`;
   the `!=` against `NO_ERROR` is the one branch, and on failure the kernel
   responds and returns before touching memory.

The header also declares the three hooks the body calls into:

```cpp
// gen/hist.hpp
namespace hist_impl {
    ap_uint<8> validate(HistCmd cmd);
    template <int out_bw>
    void respond(hls::stream<streamutils::axi4s_word<out_bw>>& m_out, int tx_id, ap_uint<8> status);
    void compute(float data[1024], float edges[32], int ndata, int nbins, ap_uint<32> out[32]);
}
```

## The hand-written datapath

The hooks supply the bodies. `hist_compute_impl.cpp` is the binning datapath — the
loop that turns samples into counts:

```cpp
// examples/shared_mem/hist_compute_impl.cpp — hist_impl::compute
for (int i = 0; i < ndata; ++i) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=max_ndata
    float sample = data[i];
    int bin = 0;
hist_search:
    for (int b = 0; b < nbins - 1; ++b) {
#pragma HLS LOOP_TRIPCOUNT min=1 max=max_nbins
#pragma HLS PIPELINE II=1
        if (sample >= edges[b]) bin = b + 1;
    }
    out[bin] = out[bin] + 1;
}
```

This is the same rule as the Python golden — the bin is the number of edges a
sample meets or exceeds — written once, by hand, with the HLS pragmas that make it
synthesize well. `hist_validate_impl.cpp` and `hist_respond_impl.tpp` similarly
hold the bounds/alignment checks and the response write. They are committed; the
generated `gen/` files that call them are not. (See
[`CODEGEN_NOTES.md`](../../../examples/shared_mem/CODEGEN_NOTES.md) for the full
generated-vs-hand-written breakdown.)

## The generated testbench

`tb_files_to_str(HistTBHls)` lowers the testbench `main()` into `gen/hist_tb.cpp`.
It allocates the three regions in the same order the SimPy controller did, and —
because a runtime count can be `0` (`nbins-1` edges when `nbins == 1`, or an
invalid size) — it **clamps every allocation to at least one word**:

```cpp
// gen/hist_tb.cpp (one of three regions)
const int _cmd_data_addr_nwords = float32_array_utils::get_nwords<32>(cmd.ndata);
const int _cmd_data_addr_widx = mem_mgr.alloc(_cmd_data_addr_nwords > 0 ? _cmd_data_addr_nwords : 1);
cmd.data_addr = _cmd_data_addr_widx * (32 / 8);
float32_array_utils::write_array_slice<32>(data, mem + _cmd_data_addr_widx, 0, cmd.ndata);
```

A 0-word region is meaningless and the allocator rejects it, so the testbench
reserves one word it never uses — the same `max(nedges, 1)` clamp the SimPy
controller applied. The generated TB writes the kernel's response and counts back
out as `.bin` files, which the [C and RTL simulation](rtlsim.md) flow checks
against the golden.

## Running the codegen

The generation needs no Vitis — it is pure Python:

```bash
cd examples/shared_mem
python -c "from hist_build import generate_vitis_sources; generate_vitis_sources('.')"
ls gen/        # hist.cpp  hist.hpp  hist_tb.cpp
```

After it lands, inspect `gen/hist.cpp` and confirm the three hook `.cpp`/`.tpp`
files exist in the example directory.

## Next

- [C and RTL simulation](rtlsim.md) — compiling the generated kernel + testbench
  with Vitis HLS, the four-case C-sim coverage, C-synthesis, RTL co-simulation,
  and the multi-buffer burst extraction.
