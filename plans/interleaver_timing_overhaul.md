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

- [ ] **Cleanup** — delete the superseded `gather_toy*` predecessor (py/gen/tcl/includes/test). KEEP
  `mem_stream_*` (standalone MemRStream/MemWStream RTL conformance — relevant to #1).
- [ ] **#3 compute model** — attach a `LinCalibModel` loop model to `IlCompute`; charge it in
  `run_iter`; record firings for fitting. Seeded so the sim runs before any fit.
- [ ] **#2 bus infra** — `interleaver_sim.run_interleaver(platform_dir=...)` → load bus law onto the
  memory. Default to the shipped `zynq7020_bfm_100mhz`.
- [ ] **#4 activity diagram** — `interleaver_figures.py` (committed-figure workflow, like memcpy) with
  band + compute-occupancy panels. Needs a per-stage timeline out of the sim (add lightweight
  per-stage firing timelines, or trace).
- [ ] **#5 calibration** — a compute fixture (direct loop-model fit from RTL compute cycles); ship
  under the platform library keyed by the compute component id.
- [ ] **#1 inband memstream** — the deep codegen/XSI redesign. Deferred until toolchain-available.
- [ ] **docs** — the interleaver docs arc (custom-component fitting), pointing back at guide/calib.

## Constraints this session

No toolchain (Vitis/XSI). Everything landed must be pysim-verifiable. `#1` and any RTL-cosim fit
(real `latency`/`ii`) are scaffolded + seeded, not measured. Cold calib import ≈ 50s per fresh python.
