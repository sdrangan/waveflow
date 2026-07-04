# Plan — free-running streaming FIR sandbox (inter-job pipelining + restart + per-job errors)

## Why
The control-driven kernel (the `int err = fir_dataflow(cmd); if(err) return;` while-loop) **serializes jobs**
— the scalar `err` the loop waits on is a per-job barrier, so there is no inter-job overlap. Inter-job
pipelining is a hard requirement. The **free-running streaming pipeline** (load/compute/store as concurrent
processes over `hls::stream`, a **shift-register FIR** in the middle) delivers inter-job overlap **and**
restart **and** per-job error handling. De-risk it in a standalone sandbox first (the Phase-1 discipline:
*build the hand HLS, measure in cosim*), before redoing the Waveflow integration.

**This supersedes the control-driven Phase 2 in `fir-cleanup.md`** (kept for history). The 2a `fir.py`
restructure (control-driven `on_start`, uncommitted) is on hold pending this sandbox — the sim model will
move to **persistent** load/compute/store processes (which naturally express load(N+1) ∥ store(N)).

## Design (converged with the user)
- **Free-running dataflow:** `ap_ctrl_hs` top → `#pragma HLS DATAFLOW` → `load`/`compute`/`store`, each a
  **`while(!done)`** loop (continuous). (The `hls::task` form is the alternative — let the sandbox pick
  whichever cosims cleaner; that control-protocol choice is one of the things to measure.)
- **Channels = plain `hls::stream` FIFOs** — back-pressure + inter-job overlap for free. **NO
  `stream_of_blocks`, NO per-row `#pragma HLS DATAFLOW` row buffer, NO `fir_accel_core`.** The FIR streams
  naturally, so no random-access row buffer is needed.
- **Compute = streaming shift-register FIR:** a **T-deep, fully-partitioned shift register** (tapped delay
  line) + unrolled MAC → **II=1** per output. Reads X **sample-by-sample**; no `xbuf[NCOL_MAX]` row buffer,
  no re-buffering.
- **In-band command:** serialize `cmd · h · X` to compute. Compute reads `cmd` first (so it knows
  `n_cols`/`n_rows`), then `h` (cached in the partitioned tap registers, **per command**, not per row),
  then the X samples. (Realize as a small **meta/ctrl stream** for `cmd` + a **`float` data stream** for
  `h`/`X`, OR a single tagged/word stream — implementer's choice; pick the cleaner typed form.)
- **§2b at the m_axi boundary ONLY:** `gmem` is **`ap_uint<mem_dwidth>*`**; every memory access goes
  through **`read_array_slice<W>` / `write_array_slice<W>`** (which deserialize ap_uint↔float). The
  **internal FIFOs carry `float`** (already deserialized) — serialization lives at the memory interface,
  nowhere else. **No `real_t*` / typed memory pointers.**
- **Row-boundary reset (don't miss this):** the FIR is **per row** — row i's window must not pull in row
  i−1's samples. Compute counts `n_cols` samples/row, emits `n_cols − T + 1` outputs/row, and **flushes the
  shift register at each row boundary**. The window fills over the first T−1 samples of each row (the
  "valid" edge — those produce no output).
- **Restart:** an `END` sentinel flows through the pipeline → each stage drains and `break`s → the region
  completes → the top **returns → `ap_done`** → the host re-asserts `ap_start` for the next batch.
- **Errors = per-job status on the response stream:** `store` emits `resp{tx_id, status}` per job; a
  bad-size job gets an error status and **the pipeline keeps flowing** (no global barrier). (Global
  halt-on-error is *out of scope* — it re-introduces the feedback dependency that serializes; per-job
  status is the model.)

## Kernel skeleton
```cpp
void fir(hls::stream<word_t>& s_in, hls::stream<word_t>& m_out, ap_uint<MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    load   (s_in, gmem, ld_ctrl, ld_data);   // read cmd; read_array_slice X→float; stream cmd+h+X
    compute(ld_ctrl, ld_data, cp_ctrl, cp_data); // shift-register FIR, II=1, per-row reset
    store  (cp_ctrl, cp_data, gmem, m_out);   // write_array_slice Y; emit resp{tx_id,status}
}
// each of load/compute/store is a while(!done) loop; END sentinel drains; ap_done on return.
```

## Validation (cosim — the de-risk; real Vitis 2025.1)
1. **Functional bit-exact** vs `fir_golden`: single + multi-job + clean varying sizes (e.g. 4×64, 2×48, 3×32).
2. **INTER-JOB OVERLAP — the headline.** From the cosim timeline, confirm job N+1's **X-read burst overlaps
   job N's Y-write burst** on the bundle (the overlap the control-driven kernel structurally lacked).
   Report the steady **per-job period** and compare to the control-driven sandbox's sequential ~1467
   cyc/cmd — expect it **lower** (the overlap is the win).
3. **Restart:** `END` drains the pipeline → `ap_done` → re-`ap_start` → a second batch is bit-exact.
4. **Per-job errors:** a bad-size job emits an error `status` in the response; the pipeline keeps flowing;
   subsequent good jobs bit-exact (no global halt needed).
5. **Control protocol:** which of `ap_ctrl_hs` + `while(!done)` vs `hls::task` synthesizes + cosims cleanly;
   record the choice and any `#pragma HLS STREAM depth=` needed for the overlap.
6. **§2b honored:** `gmem` is `ap_uint<mem_dwidth>*`, all memory via the slice helpers; **no `real_t*`**.

## Deliverable
- `examples/rowwise_fir/sandbox/fir_freerun_sandbox.{cpp,hpp}`, `fir_freerun_tb.cpp`, `run_freerun.{tcl,py}`.
- `sandbox/freerun_notes.md` — the findings: control protocol, **measured inter-job overlap + per-job
  period vs the control-driven ~1467**, restart behavior, per-job error behavior, stream-depth /
  DATAFLOW-canonical-form gotchas.
- **Additive** (only new `sandbox/` files); **no framework changes**; **nothing committed** — report and stop.

## Out of scope
- Waveflow integration (`fir.py`/`fir_build.py`) — comes after this validates; it will replace the
  control-driven Phase 2.
- Global halt-on-error (per-job status only).
- `stream_of_blocks` / row-buffer variants (the shift-register streaming form is the design).
