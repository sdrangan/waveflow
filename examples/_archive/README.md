# Archived examples

This directory holds **frozen snapshots** of examples that are no longer canonical and are
**not built or tested in CI**. They are kept for history and reference only.

A snapshot here:

- uses the **retired array-serialization API** (the old `read_array` / `write_array` bulk and
  `*_elem` methods that `waveflow/hw/arrayutils.py` no longer emits), and/or a **pre-component HLS
  style** (hand-written Vitis C++ driven by a `run.tcl`, not an `HwComponent` → codegen → hook), so
- it will **not build against the current generator**, and
- it is **excluded from pytest collection** (`norecursedirs` + `--ignore-glob=examples/_archive/*`
  in `pyproject.toml`; the moved tests also carry a module-level `pytest.mark.skip`).

## conv2d

`conv2d/` is hand-written Vitis C++ (`conv2d.cpp` / `conv2d_df.cpp` / `conv2d.hpp`, driven by
`run.tcl`) — it predates the `HwComponent` model and is the last example that called the old bulk
serialization methods. It is **not** referenced by any doc and is not canonical for any interface
(poly = stream, hist = `m_axi` + stream, regmap = `s_axilite`, vmac = `m_axi` compute already cover
the surface).

It will **return rewritten from scratch** as a proper `HwComponent` once the systolic array exists,
so the current code is not the right starting point — it is archived rather than migrated.

> The separate `waveflow/mcp/corpus/**` conv2d (the AI example corpus) is deferred and reworked
> independently; it is unrelated to this snapshot.

## rowwise_fir

`rowwise_fir/` is the matrix-LT / per-row FIR built on the **`#pragma HLS DATAFLOW`** architecture — a
single kernel owning a load → compute → store pipeline over a partitioned-BRAM ping-pong and an
`hls::stream` FIFO (`fir_dataflow.tpp`, `fir_build.py`, the cosim calibration in `fir_calibrate.py`).
That dataflow architecture is being retired: double-buffering is now modeled by composing
`Load` / `Compute` / `Store` as concurrent sub-components over a **stream of blocks**, and the compute
sub-component is timed like a plain block process. So this example — and its docs and the
`custom_hooks/dataflow.md` guide — no longer describe the canonical path.

It is archived rather than migrated: the premise (a per-row resident window realized as one DATAFLOW
kernel) is superseded, so the current code is not the right starting point. A future FIR will return as
an SOB-composed `HwComponent`.
