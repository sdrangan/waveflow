---
title: C and RTL Simulation
parent: Histogram with shared memory
nav_order: 5
has_children: false
---

# C and RTL Simulation

This page picks up where [Code Generation](codegen.md) leaves off: the generated
kernel and testbench are in `gen/`, the hand-written hooks sit next to them, and
the support headers are written. From here Vitis HLS drives the design through:

1. **C-simulation** — compile the generated kernel + testbench + hooks and run
   them as a C++ program, checked bit-exact against the numpy golden, across four
   coverage cases.
2. **C-synthesis** — lower the kernel to RTL.
3. **RTL co-simulation** — run the synthesized hardware in a simulator to get a
   measured cycle count and a waveform.
4. **Burst extraction** — pull the AXI-MM read/write bursts out of the cosim
   waveform and confirm the multi-buffer layout.

The payoff is twofold: the generated kernel produces the same histogram the
Python model did, *and* the RTL waveform shows the three buffers moving over one
`m_axi` port exactly as designed.

## Two ways to run it

The flow is a `BuildDag` of dataclass `BuildStep`s (`hist_build.py`), driven by
the same regmap-style CLI (`run_dag_cli`) the other examples use:

```bash
cd examples/shared_mem
python hist_build.py --through cosim            # gen_sources -> ... -> csim -> cosim
python hist_build.py --through extract_bursts --trace-level port
python hist_build.py --list-steps               # the full ordered step list
```

`--through STEP` runs the DAG up to and including that step (skipping anything
already fresh). The build steps are `gen_sources` → `build_inputs` → `py_sim` →
`csim` → `cosim` → `generate_vcd` → `extract_bursts` (plus the
[committed-figure](timing.md) steps). The Vitis stages all share `run.tcl`,
which compiles `gen/hist.cpp` + `gen/hist_tb.cpp` against the three hand-written
hooks.

The **pytest** path runs the Vitis stages as gated tests — the `-m vitis` marker
so they only run where Vitis is installed:

```bash
pytest -m vitis tests/examples/test_hist_csim.py    # the four-case C-sim
pytest -m vitis tests/examples/test_hist_cosim.py   # cosim + burst extraction
```

`run.tcl` is the Vitis driver both use; it compiles `gen/hist.cpp` +
`gen/hist_tb.cpp` against the three hand-written hooks and runs the requested
stage range.

## C-sim functional verification — four cases

The histogram's correctness is not one number; it is a small coverage set, because
the interesting behavior is at the edges. `test_hist_csim.py` runs the generated
kernel through C-sim on four vectors:

```python
# examples/shared_mem/hist_build.py — CSIM_CASES
CSIM_CASES = [
    HistCase(ndata=37, nbins=1),    # one bin: the nbins-1 = 0 edges read is a no-op
    HistCase(ndata=37, nbins=6),    # normal multi-bin binning
    HistCase(ndata=200, nbins=12),  # more data, more bins
    HistCase(ndata=0,  nbins=6),    # invalid: ndata == 0 -> INVALID_NDATA
]
```

Each case writes its inputs (`cmd.bin`, `data_array.bin`, `edges_array.bin`),
runs `run.tcl` through C-sim, then `HistCase.check_outputs` compares what the
kernel wrote back:

- For the three valid cases, it deserializes `counts_array.bin` and asserts it
  equals `golden_counts(data, edges, nbins)` — bit-exact against the same numpy
  golden the [SimPy run](pysim.md) used.
- For the invalid case, it asserts the response `status` is `INVALID_NDATA` — the
  validation→status path, confirmed in real compiled C++.

The two edge cases earn their place: **`nbins == 1`** proves the unconditional
`nbins-1 = 0` edges read is a genuine no-op in hardware (not a buffer underrun),
and the **`ndata == 0`** case proves the kernel rejects bad input and responds
with a status instead of dereferencing a buffer. A kernel that passed only the
"normal" case could be hiding either bug.

## C-synthesis and RTL co-simulation

`--through cosim` continues past C-sim: `csynth` lowers the kernel to RTL, and
`cosim` runs that RTL in a simulator driven by the same generated testbench. The
cosim run does two things the C-sim cannot:

- It produces a **measured cycle count** for one transaction — the real latency of
  reading the buffers, binning, and writing back, including the `m_axi` burst
  handshakes the SimPy model only approximated.
- It writes a **waveform** (a VCD) of every interface signal, which is the raw
  material for the burst and timing diagrams.

Because the same generated testbench drives C-sim and cosim, a kernel that passes
C-sim and then mismatches in cosim is a synthesis or interface issue, not a logic
one — the functional check is already behind you.

## Multi-buffer burst extraction

This is the step unique to the shared-memory example. `test_hist_cosim.py` (and
`--through extract_bursts`) re-runs the RTL to dump a port-level VCD, then
`HistTest.extract_bursts` parses the `m_axi` signals into discrete read and write
**bursts** and checks them against what the command should have produced:

```python
# examples/shared_mem/test flow
ht = HistTest(example_dir=tmp_path, ndata=37, nbins=6)
ht.simulate()
vcd_path = ht.generate_vcd(trace_level="port")
report = ht.extract_bursts(vcd_path=vcd_path)
assert report["validated"]
```

For the `ndata=37, nbins=6` vector the report shows the multi-buffer pattern the
whole example was built to demonstrate — **two read regions** (`data`,
`bin_edges`) and **one write region** (`counts`), each at its allocated byte
address, with `data`'s 37 words split into several bursts at the AXI maximum burst
length. The extractor asserts the per-burst address-and-length layout matches the
expected allocation, not merely that "some bursts happened."

The [timing page](timing.md) renders this report — and the transaction
waveform — as the two committed figures, and explains how to read them.

## What a green run proves

After a full `--through extract_bursts`:

- **C-sim** — the generated kernel computes the same counts as the Python golden,
  across the four coverage cases, and reports the right status on bad input.
- **cosim** — the synthesized RTL reproduces that behavior with a measured cycle
  count.
- **burst extraction** — the `m_axi` traffic is exactly the three buffers, at the
  right addresses, in the right order.

Same source, same outputs, and a hardware waveform that proves the multi-buffer
memory access pattern is what the Python model described.

## Next

- [Viewing timing and bursts](timing.md) — the committed timing/burst figures and
  the workflow for refreshing them.
