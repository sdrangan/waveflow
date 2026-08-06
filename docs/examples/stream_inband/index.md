---
title: Streaming polynomial
parent: Examples
nav_order: 3
has_children: true
summary: "Control moves off the register map and onto the data stream. A polynomial accelerator packetizes a variable-length AXI4-Stream with TLAST, carries its command in-band as a header ahead of the samples rather than in registers, and runs as a persistent loop over back-to-back transactions that halts cleanly on an END command. Kernel and testbench are both generated from the one Python source, and the RTL cosim cycle count is checked against the pysim estimate."
---
# Streaming polynomial

End-to-end Waveflow tutorial for a small polynomial accelerator —
covering every stage from the Python golden model through RTL cosim
timing verification.  Every stage downstream of the Python source is
*derived* and *verified against* the Python golden, which is the
philosophy this example is meant to teach.

## Learning Objectives

In going through this example, you will learn to:

- **Packetize** a variable-length data stream over **AXI4-Stream**, using `TLAST` to
  mark transaction boundaries.
- Carry **control in-band** on the stream — a `PolyCmdHdr` (`DATA` / `END`) header
  ahead of the samples — instead of in a register map.
- Model a **persistent-loop** accelerator that processes back-to-back transactions,
  halts cleanly on an `END` command, and reports errors through the regmap.
- Build the polynomial golden from schemas + a SimPy protocol simulation, then generate
  the Vitis kernel **and** testbench from that same Python source.
- Verify functional parity in Vitis C-sim, estimate resources / II in C-synth, and
  compare measured RTL cosim cycles against the pysim estimate (±20-cycle tolerance).
- Analyze **AXI4-Stream handshake timing** from a cosim VCD.

## The five-group narrative arc

The full pipeline reads as five conceptually distinct groups, in
[`examples/stream_inband/poly_build.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_build.py)
expressed as docstring markers in the DAG-construction function:

```
   Python golden model        →  build_inputs, py_sim, extract_py_timing
        │
        ▼
   HLS codegen                →  gen_include, gen_kernel, gen_tb
        │
        ▼
   C-sim functional verify    →  csim, validate_csim
        │
        ▼
   C-synth resource estimate  →  csynth, inspect_synth
        │
        ▼
   RTL cosim timing verify    →  extract_cosim_timing, validate_timing
```

Each group has its own dedicated page in this tutorial:

1. [Python golden model](./01_python_golden_model.md) — schemas + sim + Python-side cycle measurement.
2. [HLS code generation](./02_hls_codegen.md) — kernel + testbench C++ generated from the same Python sources.
3. [C-sim functional verification](./03_csim_verification.md) — Vitis runs the generated kernel, outputs compared to the Python golden.
4. [C-synth resource estimation](./04_csynth_resources.md) — C-synthesis report, resource + II tables.
5. [RTL cosim timing verification](./05_cosim_timing.md) — measured RTL cycles vs PySim cycles, ±20 cycle tolerance.

A supplementary page covers
[AXI4-Stream timing analysis from VCD](./poly_axi_stream.md), useful
once cosim has run with tracing enabled.

## The polynomial accelerator protocol

- The host writes polynomial coefficients to the accelerator's AXI-Lite
  register map (a `VitisRegMap`), then writes `ap_start` to launch the
  kernel.
- The host sends a `PolyCmdHdr` (`cmd_type = DATA`) carrying the
  transaction ID and sample count over the input AXI-Stream.
- The host streams `nsamp` input values (`x`).
- The accelerator returns a `PolyRespHdr` echoing the transaction ID,
  then streams `nsamp` result values (`y`).
- The host sends a `PolyCmdHdr` (`cmd_type = END`) to break the kernel's
  persistent loop cleanly.
- On error the kernel halts: it sets `halted = 1`, `error = <code>`,
  `tx_id = <offending txn>` in the regmap and returns.

## Files

| File                                                                                                                                             | What it holds                                                                      |
| ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| [`examples/stream_inband/poly.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly.py)                               | Schemas,`PolyAccel`, `PolyTB` (SimPy), `PolyTBHls` (codegen-source) |
| [`examples/stream_inband/poly_build.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_build.py)                   | Build DAG — the five groups above plus step definitions                           |
| [`examples/stream_inband/poly_evaluate_impl.tpp`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly_evaluate_impl.tpp) | Hand-written Horner evaluation hook (sticky impl)                                  |
| [`examples/stream_inband/run.tcl`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/run.tcl)                               | Vitis HLS TCL driver consumed by `CSimStep` / `CSynthStep`                      |

`gen/` (kernel + TB C++) and `include/` (schema + utility headers) are
generated artifacts — they are `.gitignored` and rebuilt by the DAG.

## Running the full pipeline

```bash
python -m examples.stream_inband.poly_build --through validate_timing --force --live-output
```

The last step (`validate_timing`) emits `results/timing_verdict.json`
with both measured cycle counts and a `pass` bit.

---

Next: [Python golden model →](./01_python_golden_model.md)
