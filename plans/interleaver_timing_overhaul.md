# Interleaver timing overhaul

Bring the interleaver example up to everything learned from memcpy. Interleaver's teaching value =
**fitting a CUSTOM component** (the `IlCompute` gather), which memcpy has none of.

## Requested changes (user, 2026-07-23)

1. **Use the inband message-forwarding memstream** — replace the custom `IlMemR`/`IlMemW` with the
   framework `MemRStream`/`MemWStream(inband=True)`. DEEP: read side loads P *and* X (two regions),
   which MemRStream does one-region-per-command, so it's a redesign, not a swap. Touches the composite
   graph + `composite_gen` codegen + XSI. **Toolchain-gated — deferred; do not do blind.**
2. **Use the pre-computed infra timing models** — load the shipped platform bus law onto the sim's
   memory (`mem.s_mm.bus_timing`), so m_axi transfers are charged. Works TODAY regardless of #1 (the
   bus model attaches to the memory, not the mem-stage). Sim-verifiable.
3. **Add a timing model to the compute stage** — `IlCompute` gets a loop model
   (`latency + ii·(nw−1)`, insertion.md pattern: `cycles = tm.predict({...}); yield timeout(...)`),
   so the loosely-timed compute charges a realistic delay. THE centerpiece. Sim-verifiable.
4. **Use the activity diagrams** — an `interleaver_figures.py` rendering the per-stage activity +
   compute-occupancy via `waveflow.utils.timing.ActivityDiagram`. Sim-verifiable.
5. **Use the new timing infrastructure** — a calibration path for the compute model (a fixture / a
   direct LinCalibModel fit), storing the fit; wire the platform selection. Partly sim-verifiable.

## Plan / status

- [x] **Cleanup** — deleted the superseded `gather_toy*` predecessor (py/gen/tcl/includes + its
  `@vitis` test). KEPT `mem_stream_*` (standalone MemRStream/MemWStream RTL conformance — relevant to
  #1). Commit 5f628f8.
- [x] **#3 compute model** — `IlCompute` carries a `LinCalibModel` loop model (`cycles = latency +
  ii·(nw−1)`), charged in `run_iter` (insertion.md pattern), seeded, `calib_dir` loads a fit. Records
  per-firing start/end/span. Commit 9e6a9c9.
- [x] **#2 bus infra** — `run_interleaver(platform_dir=...)` loads the shipped bus law onto the memory;
  period 514→527 cyc/job. Commit 9e6a9c9.
- [x] **#4 activity diagram** — per-stage `fire_log` on all six stages + `interleaver_figures.py`
  rendering the six-stage pipeline overlap via `ActivityDiagram`, straight from the pysim (no trace).
  Commit 084a302.
- [x] **#5 calibration** — `calibrate_compute.py`: direct loop-model fit into the platform library
  (`components/il_compute_task/`); roundtrip tested. Cycle counts are placeholders pending cosim.
  Commit 4279b41.
- [ ] **#1 inband memstream** — DEFERRED (toolchain + redesign). **Analysis:** the read side loads P
  *and* X (two regions), but `MemRStream` reads one region per command and forwards, so it is not a
  swap: the sequencer would frame two read descriptors (or the design splits), the reader forwards the
  inband descriptor through compute to `MemWStream`, and the whole composite graph +
  `composite_gen` codegen + XSI harness change. Do this with the toolchain available so the generated
  RTL can be XSI-verified — do NOT do it blind. Once done, the mem stages inherit the shipped
  mem-stream residual (the bus is already wired in #2).
- [ ] **docs** — the interleaver docs arc (the custom-component fitting story), pointing back at
  guide/calib. `interleaver_figures.py` renders PNG to `results/`; switch to committed SVG when the
  docs page exists.

## What's shippable vs seeded

Landed + pysim-verified: the compute loop-model plumbing, the bus-law wiring, the activity diagram, the
direct-fit machinery. **Seeded, not measured:** `IL_COMPUTE_LATENCY_SEED`/`II_SEED` and
`calibrate_compute.NW_TO_CYCLES` — real numbers need an `il_compute` cosim sweep. Until then the pysim
period (~527) is an estimate, not calibrated to the RTL 414.

## Constraints this session

No toolchain (Vitis/XSI). Everything landed must be pysim-verifiable. `#1` and any RTL-cosim fit
(real `latency`/`ii`) are scaffolded + seeded, not measured. Cold calib import ≈ 50s per fresh python.
