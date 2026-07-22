# Free-running streaming FIR — sandbox findings

De-risk for [`plans/fir_freerun_sandbox.md`](../../../plans/fir_freerun_sandbox.md): a
**standalone, hand-written** free-running streaming FIR (no Waveflow framework, no codegen),
built to validate the **inter-job pipelining** the control-driven kernel structurally lacked.

Files (additive, under `sandbox/`): `fir_freerun_sandbox.{hpp,cpp}` (kernel),
`fir_freerun_tb.cpp` (testbench), `run_freerun.{tcl,py}` (Vitis driver + capture). Nothing
else touched; nothing committed.

Environment: Vitis HLS 2025.1, `xc7z020clg484-1`, 10 ns clock, driven via
`waveflow.toolchain.run_vitis_hls_result(run_freerun.tcl)`.

## Architecture (what was built, vs the control-driven sandbox)

```cpp
void fir(hls::stream<word_t>& s_in, hls::stream<word_t>& m_out, ap_uint<MEM_DW>* gmem) {
#pragma HLS INTERFACE axis port=s_in / m_out
#pragma HLS INTERFACE m_axi port=gmem offset=slave bundle=gmem
#pragma HLS INTERFACE ap_ctrl_hs port=return
#pragma HLS DATAFLOW
    load(s_in, gmem, ld_ctrl, ld_data);          // each a while(!done) loop
    compute(ld_ctrl, ld_data, cp_ctrl, cp_data); // (END sentinel drains the network)
    store(cp_ctrl, cp_data, gmem, m_out);
}
```

- **Free-running:** `ap_ctrl_hs` top → one `#pragma HLS DATAFLOW` region → three **persistent
  `while(!done)` processes** (`load`/`compute`/`store`) wired by **plain `hls::stream` FIFOs**.
  No `int err = fir_dataflow(cmd); if(err) return;` per-job barrier (the thing that serialized
  the control-driven kernel to ~1467 cyc/cmd).
- **Compute = streaming shift-register FIR:** a `T`-deep fully-partitioned tapped delay line
  (`sr[t] = x[s-t]`) + unrolled MAC, **II=1** per output (csynth: `compute_Pipeline_ROWS_COLS`
  achieved II = **1**). Reads X **sample-by-sample** — no `xbuf[NCOL_MAX]`, no per-row DATAFLOW,
  no `fir_accel_core`. The window **flushes at each row boundary** (the FIR is per row; the first
  `T-1` samples of each row fill the window and produce no output — the "valid" edge). Tap order
  `t=0..T-1` with `sr[t]` reproduces `fir_golden`'s left-to-right accumulation → **bit-exact**.
- **`h` cached per command** in compute's partitioned tap registers (read once per command off
  `ld_data`, reused across the command's rows).
- **§2b at the m_axi boundary only:** `gmem` is `ap_uint<MEM_DW>*` (NOT `float*`); every access
  goes through `read_array_slice<W>` / `write_array_slice<W>` (the only ap_uint↔float cast). The
  internal FIFOs carry **`float`** — already deserialized. **No `real_t*` memory pointer anywhere.**
- **In-band command:** `load` reads the 7-word command off `s_in`, forwards a `Cmd` struct on a
  ctrl FIFO + `h`/`X` floats on a data FIFO. `compute`/`store` read `cmd` first (so they know
  `n_rows`/`n_cols`/`status`), then the data. A bad-size job forwards `Cmd{status=BAD_SIZE}` and
  streams **zero** data, so the FIFOs stay balanced with no global barrier.

## Finding 0 — control-protocol choice: `ap_ctrl_hs` + `while(!done)` (not `hls::task`)

**`ap_ctrl_hs` + `#pragma HLS DATAFLOW` + per-stage `while(!done)`** synthesizes and **csims
clean on the first try** (all 5 scenarios) and csynths to a `dataflow` region with three
concurrent processes (`load_U0` / `compute_U0` / `store_U0` in `fir_csynth.rpt`). The END
sentinel propagates through the network; each stage `break`s; the region completes; the top
returns → `ap_done`. `hls::task` was **not needed** — the `while(!done)`+END-drain form gives a
finite-per-batch region that restarts cleanly on the next `ap_start`, which is exactly what the
host loop wants (a `hls::task` free-runs forever with no `ap_done`, which would *not* give the
per-batch restart the host protocol uses). Recorded as the cleaner choice for this control model.

## Finding 1 — functional bit-exact (csim + cosim, all 5 scenarios PASS)

csim **and cosim**: **all 5 scenarios bit-exact** — `single`, `two`, `three` (N×4×64), `clean`
(varying 4×64 / 2×48 / 3×32), `error` (per-job error + restart). gmem `Y` matches the C++
golden bit-for-bit (same left-to-right tap order; no tolerance), and the per-job responses
`{tx_id, status}` are correct.  RTL co-simulation passed on the **first attempt** for every
scenario (no retry needed — cleaner than the control-driven sandbox, whose error cosim was
flaky on the first xsim launch).

## Finding 2 — INTER-JOB OVERLAP (the headline): 704 cyc/job vs the control-driven 1467

**The inter-job pipelining the control-driven kernel structurally lacked is confirmed.**  RTL
cosim of N back-to-back 4×64 jobs (one batch, one ap_start):

| scenario | jobs | cosim cycles |
|---|---|---|
| `single` | 1 | 1086 |
| `two`    | 2 | 1790 |
| `three`  | 3 | 2494 |

- **Steady per-job period** = the increment per added job:
  `two − single = 704`, `three − two = 704` — **exactly constant at 704 cyc/job**.
- **vs the control-driven sandbox's 1467 cyc/cmd** (dflow_notes.md Finding 4): **2.08× faster**.
- **The overlap proof:** the steady period (**704**) is *less than a single job's standalone
  latency* (**1086**).  If jobs were serialized (the control-driven per-job barrier), the period
  would equal one job's full end-to-end latency (≈ the single-job number, exactly what the
  control-driven kernel showed: 1467 ≈ its single-job 1477).  A period **below** the single-job
  latency is only possible if **multiple jobs are in flight at once** — `load` is reading job
  N+1's X off the bundle while `compute`/`store` are still finishing job N (and store is writing
  job N's Y on the same bundle, full-duplex AR/R vs AW/W).  That read∥write inter-job overlap is
  the win, and the **constant** increment confirms it is continuous (no accumulating per-job
  stall — the period does not grow with N).

> Absolute cycles are stall-inflated (Vitis 2025.1 random-stall cosim m_axi model), so 704 is a
> throughput-period reference, not a tight transfer bound — but the *relative* facts (704 < the
> 1086 single-job latency, 704 constant, 2.08× under the control-driven 1467) are the robust
> evidence of inter-job overlap.

## Finding 3 — restart (END → ap_done → re-ap_start)

The `error` scenario runs **two batches** through **two `ap_start`s**: batch 1 = `[good, good,
BAD-size, good] + END`, batch 2 = `[good] + END`. csim **and cosim PASS**: the END of batch 1
drains the pipeline (each stage `break`s on the END sentinel), the DATAFLOW region completes, the
top returns → `ap_done`; the next `ap_start` (batch 2) runs clean and bit-exact on RTL.  The
free-running region restarts cleanly per batch — `ap_ctrl_hs` + END-drain gives the per-batch
`ap_done`/restart the host loop needs (an `hls::task` would free-run with no `ap_done`).

## Finding 4 — per-job error, no global halt (the model)

In batch 1 the **bad-size job (`tx=302`, `n_cols=4096 > NCOL_MAX`)** is forwarded with
`status=BAD_SIZE` and streams no data; `store` emits `resp{302, BAD_SIZE}` and **keeps going** —
the **subsequent good job `tx=303` still completes bit-exact** (no global barrier).  csim **and
cosim PASS**: responses `(300,OK) (301,OK) (302,BAD_SIZE) (303,OK)` in order, and `tx=303`'s Y is
bit-exact on RTL even though it followed the bad job.  This is the per-job-status model the plan
mandates — global halt-on-error would re-introduce the feedback dependency that serializes (the
control-driven kernel's failure mode), explicitly out of scope.

## DATAFLOW-canonical-form / stream-depth gotchas

- **Data FIFOs sized to hold ~a job** (`#pragma HLS STREAM variable=ld_data/cp_data depth=1024`,
  > a 4×64 job's 264/228 floats) so `load` can run a full job ahead of `store` — this depth is
  what *enables* the inter-job overlap (with shallow FIFOs `load` would block on back-pressure and
  the period would collapse toward the serialized 1086).  The ctrl FIFOs only need the in-flight
  job count (`depth=8`).  These depths worked first try; no deadlock or II degradation.
- **Balanced streams on the error path:** a bad-size job must stream **zero** data on both
  `ld_data` and `cp_data` (load skips the reads, compute skips, store skips) — otherwise the
  consumer blocks forever. The `Cmd.status` on the ctrl FIFO is what each stage checks before
  touching its data FIFO.
- **§2b staging:** `load`/`store` read/write gmem in `CHUNK`-sized `read_array_slice` /
  `write_array_slice` calls (one X-read burst + one Y-write burst per test job, since
  `CHUNK=256` ≥ the test job sizes) — the burst granularity that makes the inter-job
  read∥write overlap visible on the bundle.

## Reproduce
```bash
cd examples/rowwise_fir/sandbox
PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_freerun.py --smoke   # csim + csynth
PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_freerun.py --cosim   # + RTL cosim
# -> results_freerun/measurements.json
```
