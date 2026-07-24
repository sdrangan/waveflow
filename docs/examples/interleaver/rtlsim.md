---
title: RTL simulation
parent: Interleaver (gather)
nav_order: 6
---

# RTL simulation

The [generated BFM harness](./codegen_tb.md) drives the synthesized RTL through real handshakes, one
clock at a time, in Vivado `xsim`. This is the rung that proves the one thing only RTL simulation can
show for a free-running `ap_ctrl_none` design: the six-stage pipeline **runs to completion — no
deadlock** — and, running, is **functionally bit-exact** (`Y = X[P]` out of the real Verilog).

That first half is not free. Every stage is an un-paced `hls::task`, so nothing but the in-band
descriptor throttles the pipeline; a wiring bug that dropped a token — or a `fwd_bursts=0` read
without its relay guard — would not produce a wrong answer, it would **hang**. The pysim can miss that
class of bug; the RTL cannot.

## Running it

The interleaver's RTL run is **toolchain-gated** (an `-m xsi`-class rung): it needs Vitis HLS to
C-synthesize the top and Vivado `xsim` + a MinGW `g++` to elaborate and drive it. Unlike `mem_copy`,
csynth is not a separate prior step — the build rung does the whole sequence itself. It is the same
rung the [RTL timing page](./rtltiming.md) consumes:

```bash
python examples/interleaver/interleaver_figures.py --through rtl_timing
```

Under the hood this is `build_rtl_trace` (in
[`measure_compute_spans.py`](../../../examples/interleaver/measure_compute_spans.py)): it generates the
DUT and the harness, runs Vitis csynth, regenerates the `xvlog` file list from the RTL actually on
disk, writes the scenario bundles, clears the cached `xsim.dir`, and then invokes `xsi/run.bat` in
`trace` mode. Regenerating the file list and clearing the `.dll` first is not incidental — a stale file
list plus a cached `xsimk.dll` is exactly how an XSI run goes green while proving nothing.

Note that the interleaver is **not** one of the four tops in the exact-cycle
[`-m xsi` gate](../../../tests/examples/test_xsi_bfm.py) (that gate covers the framework
`mem_r_stream` / `mem_w_stream` mem-streams and `mem_copy`). The interleaver was retired from it; its
RTL correctness is checked instead by its own `Y = X[P]` harness — `check_xsi_outputs` in
[`interleaver_inband.py`](../../../examples/interleaver/interleaver_inband.py) reads the dumped output
bundle and asserts every `Y` region equals the golden and that exactly one done landed per job. Like
the whole XSI flow, the rung skips loudly when the RTL is absent rather than passing on stale output.

## Under the hood: `run.bat`

The four steps `run.bat` runs are the XSI recipe (see
[Tracing a kernel run](../../guide/timing/trace_steps.md)):

1. **`xvlog`** compiles the C-synthesized Verilog (the file list `rtl_interleaver_inband.f`);
2. **`xelab -dll`** elaborates the top into `xsim.dir/interleaver_inband/xsimk.dll`;
3. **`g++`** builds the [BFM harness](./codegen_tb.md) `main` against that DLL;
4. **`xsim`** runs the executable, which steps the clock for the fixed loop bound.

The BFM is the driver: it toggles the clock and, each cycle, drives and samples the boundary
handshakes — the `s_cmd` command stream in, the two `m_axi` bundles (`gmem0` read, `gmem1` write)
against a flat memory model, and the `s_done` completions out. Every value crossing the boundary is a
burst bundle written *before* the run (`vectors/s_cmd`, `vectors/mem_in`, `vectors/golden`) and read
*back* after it (`vectors/out`, `vectors/s_done`) — written by `InterleaverInbandSim.write_scenario`, the
same scenario writer that harness's own pysim run uses, so the RTL run cannot describe a different test
than the model it is checked against.

## The result

The free-running RTL **runs to completion** — the deadlock check only XSI can give — and the gather is
**bit-exact through real RTL**: every `Y` region equals `X[P]`, one commit-timed done per job. The
architectural shape the pysim predicted holds in the Verilog: the reader (`MemRStream`, `gmem0`) fires
**twice per job** — `P` then `X` over the one read bus — so the pipeline is **reader-bound**, and the
gather stage has visible slack.

The RTL run does **not** assert an exact cycle count at this rung (the four gated tops do; the
interleaver does not). The actual cycle numbers — the steady-state period, the reader's two ≈151-cycle
firings, and the side-by-side against the pysim's modeled timeline — live with the timing pages, not
here: see [RTL timing and the comparison](./rtltiming.md).

## Next

The RTL is the ground truth; the [pysim](./pytiming.md) has to *predict* it without running it. That
prediction rests on a per-stage timing model, and the gather is the one stage this design must fit
itself. [The timing model](./timing_model.md) opens that arc.
