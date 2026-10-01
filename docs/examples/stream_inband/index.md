---
title: Streaming polynomial
parent: Examples
nav_order: 3
has_children: true
example_dir: examples/stream_inband
summary: "Control moves off the register map and onto the data stream. A polynomial accelerator packetizes a variable-length AXI4-Stream with TLAST, carries its command in-band as a header ahead of the samples, and runs as a persistent loop that halts cleanly on an END command. The kernel is written hook-first: Waveflow generates its boundary, the body is hand-written C++, and a pure Python model, pysim, C simulation and RTL co-simulation are all checked bit-exactly against the same independently computed expected responses."
---
# Streaming polynomial

End-to-end Waveflow tutorial for a small polynomial accelerator, from the Python
model through RTL co-simulation timing.  It is written **hook-first**, the default
way to build a host-launched kernel:

- **Waveflow generates the mechanical parts:** the schemas' C++ headers and
  serializers, and the kernel's *boundary* -- its prototype, every interface pragma,
  and a register map that matches what Vitis builds.
- **You write the design:** the kernel body in C++, a pure bit-exact model in Python,
  the test scenarios, and an ordinary C++ testbench.
- **Every stage is checked against the same expected responses**, computed from what
  each scenario *intends* rather than from any implementation, so two implementations
  cannot agree by sharing a mistake.

## Learning Objectives

In going through this example, you will learn to:

- **Packetize** a variable-length data stream over **AXI4-Stream**, using `TLAST` to
  mark transaction boundaries.
- Carry **control in-band** on the stream -- a `PolyCmdHdr` (`DATA` / `END`) header
  ahead of the samples -- instead of in a register map.
- Model a **persistent-loop** accelerator that processes back-to-back transactions,
  halts cleanly on an `END` command, and reports errors through the register map.
- Declare a **body-only kernel**: Waveflow generates the boundary, and the whole kernel
  is one hand-written C++ hook.
- Keep a **bit-exact Python model** of it, as pure functions a system simulation can call.
- Drive the Python model and the C++ kernel from **one stimulus file**, including
  malformed transactions, and check both against the expected responses.
- Compare the RTL **co-simulation cycle count** with the pysim timing model's estimate.

## The pipeline

[`poly_build.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_build.py)
runs it as one build graph:

```
   Python model               →  scenarios, py_model, check_model,
                                 py_sim, check_pysim, extract_py_timing
        │
        ▼
   Code generation            →  gen_include, sources, gen_kernel
        │
        ▼
   C simulation               →  csim, check_csim
        │
        ▼
   C synthesis                →  csynth, inspect_synth
        │
        ▼
   RTL co-simulation timing   →  check_cosim, extract_cosim_timing,
                                 validate_timing, summary
```

Each group has its own page:

1. [Python model](./01_python_golden_model.md) -- schemas, the pure model, the
   scenarios and their expected responses, and pysim's cycle estimate.
2. [Code generation](./02_hls_codegen.md) -- the generated boundary, the hand-written
   body, and the hand-written testbench.
3. [C simulation](./03_csim_verification.md) -- every scenario through Vitis, checked
   bit-exactly, error paths included.
4. [C synthesis](./04_csynth_resources.md) -- loops, II and resources.
5. [RTL co-simulation timing](./05_cosim_timing.md) -- the measured cycle count
   against pysim's estimate.

A supplementary page covers [AXI4-Stream timing analysis from a VCD](./poly_axi_stream.md).

## The polynomial accelerator protocol

- The host writes the polynomial coefficients to the accelerator's AXI-Lite register
  map (a `VitisRegMap`), then writes `ap_start` to launch the kernel.
- For each transaction, the host sends a `PolyCmdHdr` (`cmd_type = DATA`) carrying the
  transaction ID and sample count, then `nsamp` input values `x` with TLAST on the
  last one.  A transaction with `nsamp = 0` has no sample burst.
- The accelerator returns a `PolyRespHdr` echoing the transaction ID, then `nsamp`
  results `y = c0 + c1 x + c2 x^2 + c3 x^3`, with TLAST on the last one.
- A `PolyCmdHdr` with `cmd_type = END` ends the kernel's persistent loop cleanly.
- If a sample burst's TLAST comes early, or is missing on its last word, the kernel
  returns what it computed, sets `halted = 1`, `error = <code>` and
  `tx_id = <offending transaction>` in the register map, and stops.

## Files

| File | What it holds | Written by |
| --- | --- | --- |
| [`poly.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly.py) | Schemas; the pure model (`poly_eval`, `poly_stream_model`); the module `PolyAccel`; the pysim testbench `PolyTB` | you |
| [`scenarios.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/scenarios.py) | The scenarios, their stimulus, their expected responses, and the checker | you |
| [`poly_body_impl.tpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_body_impl.tpp) | The whole kernel body, in C++ | you |
| [`poly_tb.cpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_tb.cpp) | The C++ testbench: plays each scenario's stimulus into the kernel and records the response | you |
| [`poly_build.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_build.py), [`run.tcl`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/run.tcl) | The build graph and the Vitis driver | you, mostly from stock steps |
| `gen/poly.hpp`, `gen/poly.cpp` | The kernel boundary | Waveflow |
| `include/*.h` | Schema headers, serializers, stream and testbench helpers | Waveflow |

`gen/`, `include/`, `data/` and `results/` are build output and are not committed.

## Running the full pipeline

```bash
python examples/stream_inband/poly_build.py --through check_pysim   # Python only
python examples/stream_inband/poly_build.py --through summary       # everything, with Vitis
```

The last step writes `results/summary.json`: every check by stage and scenario, the
synthesis tables, and the timing verdict.

---

Next: [Python model →](./01_python_golden_model.md)
