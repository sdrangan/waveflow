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
- [x] **#1 inband memstream (pysim)** — `interleaver_inband.py` (commit 7a93a4d). Composes the stock
  `MemRStream`/`MemWStream(inband)` — **no framework change needed**: the reader already relays forwards
  at the *header* (mem_stream.py:390), which is the two-read framing the gather wants. `cmd_rx` frames
  `[MemRCmd(x_off,nw,fwd=1) | InterleaverCmd | MemRCmd(p_off,nw,fwd=0)]` → reader `m_out = [desc|X|P]`;
  `il_load` fills the SOBs + forwards the descriptor; `IlCompute` reused verbatim; `il_store` frames
  `[MemWCmd | InterleaverCmd | Y]`; `MemWStream` echoes on `s_done`. Custom token dissolves into the
  in-band descriptor + two middle edges. Golden bit-exact (nj∈{1,3,8}, n∈{128,256,512}); ~288 vs 527
  cyc/job (better overlap). Graph = 3 FramedEdge + 2 StreamEdge + 3 SobEdge.
  **DOCS + FIGURES DONE** (commit a283939): interleaver.md + index.md rewritten to the in-band design;
  `MemRStream`/`MemWStream` + the in-band stages record `fire_log`; `interleaver_figures.py variant=`
  renders the in-band six-stage pipeline (reader shows 2 firings/job, ~2100 cyc for 6 jobs vs canon
  ~3700). Compute calib (#5) already threads through via `compute_calib_dir`.
  **DESIGN REWORKED + CODEGEN DRIVER DONE** (commits b304e5f, 5f62fd8):
  - **Descriptor split** — `InterleaverCmd` (plain boundary) vs framed `IlDesc{n, y_off}` (internal), so
    every inter-component stream is framed and NO type needs both method sets (the "both" was a
    conflation). **Variable length** — `IlDesc` carries `n`, every stage computes `nw=ceil(n/LW)` at
    runtime (scenario-independent RTL). Verified: mixed sizes {256,128,64,192} in one composite, all
    bit-exact (`run_interleaver_sizes`).
  - **Four HLS bodies** (`il_cmd_rx_framed_task`, `il_load_inband_task`, `il_compute_inband_task`,
    `il_store_inband_task`) in `waveflow/build/` + `MemStreamStep`, IlDesc + runtime nw + LOOP_TRIPCOUNT.
  - **`composite_top_spec` works with NO composite_gen change** — derives the correct 6-task ap_ctrl_none
    top wiring the framework mem-streams around the custom stages (5 framed_word edges + 3 SOB).
  - **`generate_inband()`** — full gen driver; verified to a temp dir (12 headers consistent, il_desc.h
    framed, top+tcl). `test_inband_codegen_shape` pins it, toolchain-free.
  **REMAINING:** run **csynth** (Vitis 2025.1 present — in progress) + XSI-verify no deadlock; then make
  `InterleaverInband` THE design (retire `InterleaverCanon`), mem stages inherit the shipped residual.
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
