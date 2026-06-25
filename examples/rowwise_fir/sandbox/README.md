# Row-wise FIR — free-running streaming HLS sandbox

Standalone, hand-authored Vitis HLS de-risk for the rowwise_fir **free-running streaming**
dataflow kernel (see [`plans/fir_freerun_sandbox.md`](../../../plans/fir_freerun_sandbox.md)).
No Waveflow framework code — the ground-truth kernel the later Waveflow `dataflow` exec_model
reproduces.

**Design:** `ap_ctrl_hs` top → one `#pragma HLS DATAFLOW` → three persistent `while(!done)`
processes (`load` / `compute` / `store`) over plain `hls::stream` FIFOs; `compute` is a streaming
**shift-register FIR** (a `T`-tap tapped delay line, II=1, no row buffer); `ap_uint<MEM_DW>*` memory
accessed only via `read_array_slice` / `write_array_slice`; an `END`-sentinel drains the pipeline →
`ap_done` (per-batch restart); per-job error status on the response stream (no global halt).

**What it proves** (cosim, real Vitis HLS 2025.1): bit-exact vs the golden; **inter-job overlap**
— steady **704 cyc/job**, *below* the 1086 single-job latency, i.e. jobs are in flight simultaneously
(load reads job N+1's X while store writes job N's Y on the full-duplex bundle), **2.08× faster** than
the older control-driven sequential kernel; clean restart; per-job errors without a global halt. Full
findings: **[`freerun_notes.md`](./freerun_notes.md)**.

## Files
| File | Role |
|---|---|
| `fir_freerun_sandbox.hpp` / `.cpp` | the free-running streaming FIR kernel |
| `fir_freerun_tb.cpp` | self-contained TB (single / multi / clean-varying / error / restart; bit-exact golden) |
| `run_freerun.{py,tcl}` | Vitis driver (csim + csynth + cosim) → `results_freerun/measurements.json` |
| `freerun_notes.md` | the findings (inter-job overlap, restart, per-job errors, §2b serialization) |

## Run
```bash
PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_freerun.py --smoke   # csim + csynth
PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_freerun.py --cosim   # + RTL cosim
```

> The earlier **control-driven** (per-job err-return barrier) and **Phase-1** (per-row DATAFLOW,
> the AXI full-duplex finding) sandboxes were removed once this free-running design superseded them;
> their findings live in the project memory + git history.
