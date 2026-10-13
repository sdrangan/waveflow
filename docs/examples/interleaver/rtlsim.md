---
title: RTL simulation
parent: Composite kernel interleaver
nav_order: 6
summary: "The rung that proves the one thing only RTL can show for a free-running design: the six-stage pipeline runs to completion without deadlock, and running, is bit-exact. Nothing but the in-band descriptor throttles these un-paced tasks, so a wiring bug that dropped a token would not give a wrong answer — it would hang, and the pysim can miss that class entirely."
---

# RTL simulation

The [system flow](./system.md) drives the synthesized RTL through real handshakes, one clock at a
time, in Vivado `xsim`. This is the rung that proves the one thing only RTL simulation can show for
a free-running `ap_ctrl_none` design: the six-stage pipeline **runs to completion, without
deadlock**, and, running, is **functionally bit-exact** (`Y = X[P]` out of the real Verilog).

That first half is not free. Every stage is an un-paced `hls::task`, so nothing but the in-band
descriptor throttles the pipeline. A wiring bug that dropped a token, or a `fwd_bursts=0` read
without its relay guard, would not produce a wrong answer; it would **hang**. The pysim can miss
that class of bug; the RTL cannot.

## Running it

It needs Vitis HLS to C-synthesize the top, and Vivado `xsim` with a C++ compiler to run it. One
command runs the system flow end to end:

```bash
python -m examples.interleaver.interleaver_inband_system_build
```

The steps are the same as every system's: `scenario`, `csynth` (the kernel's `interleaver_inband.tcl`,
skipped when its RTL is current), `pysim`, `system_rtl`, `system_xsi`, `compare`. Each run starts from
a fresh copy of the scenario, and a cached elaboration is reused only if its content stamp matches
the RTL just written: a stale file list plus a cached simulation library is how an XSI run goes green
while proving nothing.

The gate is a pytest target, because it asserts:

```bash
pytest -m xsi tests/examples/test_interleaver_inband_xsi.py
```

It runs the flow with `synth="check"`, so a missing or stale csynth fails the run and names it.

## The twelve-job gate

The gate runs **twelve jobs of 64**, and the number is not arbitrary. The interleaver reads twice per
job (`P`, then `X`) and writes once. When the generated top passed its `m_axi` pointers
`offset=slave`, Vitis gave each pointer-owning task a small FIFO of its own, refilled in lockstep by
one entry process and popped once per firing. The reader drained its FIFO twice as fast as the
writer, the writer's filled, and the pipeline stopped after **six** jobs with every bus idle. It was
bit-exact until then, so a one- or two-job test could never see it. The generator now emits
`offset=off` + `stable`, which removes the entry process and the FIFOs, and the gate keeps it that
way by running well past the old wall (`plans/maxi_pointer_fifo.md`).

It asserts three things on the RTL's recordings:

1. **all twelve jobs completed**: fewer is that deadlock coming back;
2. **every `Y` region is bit-exact** against `golden`, with one echoed descriptor per job
   (`check_xsi_outputs`);
3. **the last completion lands at cycle 1300**: 98 cycles a job at n = 64 after a 222-cycle first
   job. Exact, like every XSI gate, so a move gets a human look.

The completions and the final arena are also compared with pysim, word for word.

## The result

The free-running RTL **runs to completion** — the deadlock check only XSI can give — and the gather is
**bit-exact through real RTL**: every `Y` region equals `X[P]`, one commit-timed done per job. The
architectural shape the pysim predicted holds in the Verilog: the reader (`MemRStream`, `gmem0`) fires
**twice per job** — `P` then `X` over the one read bus — so the pipeline is **reader-bound**, and the
gather stage has visible slack.

The detailed cycle numbers (the steady-state period, the reader's two ≈151-cycle firings, and the
side-by-side against the pysim's modeled timeline) live with the timing pages: see
[RTL timing and the comparison](./rtltiming.md). Those pages read a **traced** run of the kernel
(`build_rtl_trace` in `measure_compute_spans.py`, driven by
`python examples/interleaver/interleaver_figures.py --through rtl_timing`), the measurement path the
timing model is fitted from.

## Next

The RTL is the ground truth; the [pysim](./pytiming.md) has to *predict* it without running it. That
prediction rests on a per-stage timing model, and the gather is the one stage this design must fit
itself. [The timing model](./timing_model.md) opens that arc.
