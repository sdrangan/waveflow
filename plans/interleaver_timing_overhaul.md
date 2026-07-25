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
  (`components/il_compute_task/`); roundtrip tested. Commit 4279b41.
  **MEASURED + SHIPPED** (commit ce06c1d): re-keyed the model `nw`→`n` (typed-SOB gather trips once per
  ELEMENT at II=1), then measured the real cost from RTL. `measure_compute_spans.py` runs the whole
  interleaver in XSI with a VCD trace over a size sweep and reads `il_compute`'s gather span off the
  waveform — the contiguous `y_blk` write-enable window, gated on a SINGLE contiguous region (a dip =
  output backpressure → firing dropped). This is the "full-pipeline fire-span, gated on no-stall" method
  (no separate fixture). Measured `{128:128, 256:256, 512:512}` — every size span == `n`, single burst,
  so the gather is a clean II=1 element loop: **cycles = n** (ii=1, latency=1). `n=512` exercised the
  runtime-n RTL at a size it had never run. `N_TO_CYCLES` + seeds become the measured law (latency 8→1);
  fit shipped to `components/il_compute_task/params.json`. Verified: fitted + seed models predict `n`;
  full pysim charges gather `job_span_cyc = n` per firing, landing on the RTL write-burst.
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
  - **csynth PASSED** (Vitis 2025.1, first try, WAVEFLOW_CSYNTH_OK): ap_ctrl_none free-running top, all 6
    tasks synth, 2 m_axi bundles, 5 framed FIFOs + 2 PIPO block RAMs, **Fmax ~111 MHz** at 100 MHz target.
    `!! UNVERIFIED` banners on the 4 bodies replaced with `csynth-verified`.
  - **XSI HARNESS BUILT + RUN** (commit 52bf62f): `interleaver_inband_sim.py` (InterleaverInbandTB graph
    + InterleaverInbandSim procedure) + `generate_tb`/`write_xsi_bundles`/`check_xsi_outputs`, the memcpy
    generated-TB pattern. Full flow ran end to end (csynth → xvlog → xelab → g++ BFM → xsim):
    **xelab errorlevel=0, g++ errorlevel=0, XSI_EXITCODE=0 — the free-running RTL runs to completion, NO
    DEADLOCK** (the critical result only XSI can give). BUT the golden **FAILS**: Y[0] got
    0x00000073000000ca vs golden 0x1fe6beab171590ae — a FUNCTIONAL bug in the hand-written C++ bodies (the
    got lanes 0xca=202 / 0x73=115 look like P-index values, not gathered X data). The bodies pass csynth
    and the pysim run_iter is correct, so the bug is C++-body-specific.
  - **XSI GOLDEN GREEN** — root-caused via VCD trace and FIXED. NOT a data bug and NOT a fundamental
    SOB/hls::task limit (my first diagnosis of "two-firing deadlock" was WRONG). The real cause: the RTL
    reader `mem_r_stream_framed_task` relayed forwards with a **bare `do-while`** (`do{read s_cmd}while
    (seen<nfwd)`), which runs ONCE even at `nfwd==0` — so `cmd_rx`'s 2nd read (`fwd_bursts=0`) made the
    reader read a **phantom word**: blocks (1 job) or steals the next job's word (4-job garbage). The
    writer already had the `if(nfwd>0)` guard; the reader didn't. pysim relay is `for _ in
    range(fwd_bursts)` (correct at 0) → pysim passed while RTL wedged. **Fix:** wrap the reader relay in
    `if(nfwd>0)`. The interleaver keeps the **two-command** read (P `fwd=1` + X `fwd=0`) — the
    transactional-arbiter model (N reads/job) is validated. **XSI GOLDEN: PASS** at n=16 (1 job) and
    n=256 (4 jobs), `Y=X[P]` bit-exact through real RTL. (Contiguous single `[P|X]` read also works —
    fewer m_axi transactions, an optimization not a requirement.) See [[reference-memrstream-one-region-
    per-firing]]. Reproduce: scratchpad/xsi_inband.py, scratchpad/xsi_small.py.
  - **TYPED-SOB refactor DONE** (element blocks): SOBs are now `ap_uint<32>[N]` (32-bit ELEMENT blocks)
    not `ap_uint<MEM_DW>[NW]` word blocks. `il_load`/`il_store` (de)serialize at the boundary via the
    generated `read_framed_stream_lane` / `write_framed_stream_lane` (needed [[reference-array-serialization-api]]
    framed_word support first), so `il_compute` is the bare gather `yb[i] = xb[pb[i]]` — no elem_read, no
    `.range()`. **XSI GOLDEN PASS** (n=16 + n=256 4-job); csynth fits (timing closes, 3× 4 BRAM_18K = same
    bits as word blocks); **cycles = 302/job, IDENTICAL to the word version** (measured same-method:
    reader-bound, so the element-granular compute is hidden). Zero regression. pysim (de)ser via numpy
    twins `_words_to_elems`/`_elems_to_words`.
  - **CANON RETIRED** (2026-07): `InterleaverCanon` + its 6 word-packed tiles (`cmd_rx`/`il_mem_r`/
    `il_load`/`il_compute`/`il_store`/`il_mem_w`), `generate_canon`, the committed gen/tcl/xsi/RTL, and
    `test_interleaver_canon.py` all deleted; the `-m xsi` gate drops the `interleaver_canon` entry (user
    chose drop-not-replace); `MemStreamStep` canon keys removed. `interleaver.py` is now just the SHARED
    schema (InterleaverCmd/IlElem/constants). Every fixture that used the canon (test_elaborate/
    int_channel/trace_manifest/trace_steps/trace/toy/freerun_kind/compute_calib) migrated to
    InterleaverInband/IlComputeInband; the plain-stream + no-hook-default assertions (canon-only legacy
    paths) were dropped. sim/figures/calibrate default to InterleaverInband. **InterleaverInband is the
    one and only interleaver.**
  **REMAINING:** mem stages inherit the shipped mem-stream residual; timing calibration (compute cosim
  sweep for real II/latency).
- [x] **docs — timing arc** (commits 40b6e12 / fd1010d / 35fe23b / a34b2d0). `timing_model.md` (declare the
  loop model on il_compute), `timing.md` (Visualizing timing — RTL vs pysim, committed activity SVG, ~0.7%
  agreement), `timing_fit.md` (measure→fit→ship recipe). Plus the infra that surfaced: `guide/calib/
  memstream.md` (the mem-stream residual + the fixture layer) and `guide/timing/sob.md`
  (`add_sob_signals`/`extract_sob_span`). `interleaver_figures.py` now emits a deterministic committed SVG
  from the fully-calibrated pysim.
  - **Reader fixture landed** (40b6e12): `waveflow/calib/fixtures/mem_r_stream.py` — the mem_r_stream
    residual was never fit (mem_copy is writer-bound); the reader-bound interleaver forced it. Closes the
    pysim↔RTL gap ~10%→~0.7% (273→300 vs RTL 302). Shipped to the tracked platform.
  - **Still open (build-up pages):** the Python / testbench / codegen / rtlsim pages (nav 2–7) that parallel
    mem_copy — the timing arc (model→viz→fit) is complete; the earlier construction pages are not yet
    written.

## What's shippable vs seeded

Landed + verified: the compute loop-model plumbing, the bus-law wiring, the activity diagram, the
direct-fit machinery — **and now the measured compute law itself.** `IL_COMPUTE_LATENCY_SEED`/`II_SEED`
and `calibrate_compute.N_TO_CYCLES` are the RTL-measured `cycles = n` (XSI fire-span, `measure_compute_
spans.py`), shipped to the platform library. The custom-component half of the calibration story is
closed; what remains is the docs arc (below).

## Constraints

Vitis HLS 2025.1 + Vivado xsim ARE available here (`-m xsi` / `-m vitis` really run — source
`C:\Xilinx\2025.1\Vitis\settings64.bat`). `#1` (inband memstream) is done and the compute fit is
measured; both went through real csynth + XSI. Cold calib import ≈ 50s per fresh python.
