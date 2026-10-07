# Lessons learned: CG massive-MIMO paper simulations

Companion to [`plans/mimo_cg/mimo_cg_paper_sims.md`](mimo_cg_paper_sims.md). The doer
appends an entry whenever something surprising is learned: a mistake, a failed
approach, a tool quirk, or a rule worth reusing. Newest entries go at the bottom.

## From setup and planning (2026-09-27 to 2026-09-29)

- **Vitis 2024.1 on Ubuntu 24.04 works with Waveflow, with two adaptations.**
  - Vivado 2024.1 ships the XSI engine as `librdi_simulator_kernel.so`, but
    `waveflow/build/xsi/xsi_bfm.h` loads the 2025.1 name
    `libxv_simulator_kernel.so`. The `.venv/xsi_compat/` symlink, put on
    `LD_LIBRARY_PATH` by `source .venv/bin/activate`, bridges it.
  - `run.sh` finds Vivado through `XILINX_VIVADO`, which the same activation
    exports.

  Without activating the venv, every XSI run fails with "Failed to Load up XSI".
- **2024.1 does not reproduce every 2025.1 number.**
  - Co-simulation reports for AXI-Lite kernels count cycles differently:
    `examples/regmap` gives 49 on 2024.1 against 5 recorded on 2025.1.
  - XSI BFM cycle counts did reproduce: `mem_r_stream` gave 158 on both.
  - `test_amd_tools` exits 1 ("TOO OLD") by design; the 2025.1 floor is
    reported, not enforced.
- **The root `.gitignore` is broad.** It swallows `*.json`, `*.log`, `*.xml`,
  `*.rpt`, `*.bin`, any name containing `summary`, and any directory named `hls/`
  or `syn/`. Committed data goes in CSV, and the C++ goes in `cpp/`. Check with
  `git check-ignore -v <path>` before relying on a file being tracked.
- **`plans/cg.md`'s CG sketch is wrong.** It has two sign errors, a no-op
  `rnorm` update and a 1-D `X`, and its error stays near 1.5. The corrected
  version is in the plan's §9.
- **Pick SNR grids from the convention, not habit.** With per-user transmit SNR
  ρ = Es/σ² and 32–128 antennas, BER 1e-3 is reached between −11 and +11 dB, so
  a 0–30 dB grid misses it in most configurations.
- **Toolchain tests skip, rather than fail, when prerequisites are missing.**
  Only "N passed, 0 skipped" under `-rs` counts as evidence.
- **`SweepRunner` writes only `results/sweep.json`.** Per-point data must be
  written by the stage's own DAG step, and a dry run needs its own `--out`.
- **The build DAG skips up-to-date steps.** A determinism check must re-run with
  `--force`.
- **`FixedField` is capped at 64 bits** (int64 storage). Dot-product
  accumulators grow by log2(length) bits.

## Phase 0 (2026-09-30)

- **Don't pass `-q` to pytest in this repo.** `pyproject.toml` already sets
  `addopts = "-q ..."`, so another `-q` makes it `-qq`, which drops the
  "N passed" summary line. Use `-rs` without `-q` when the pass and skip counts
  are the evidence.
- **The bit-exact conformance suites hold on Vitis 2024.1.** 89 passed, 0 skipped
  in about 9 minutes (roughly 6 s per C-simulation), so the `-m vitis` regression
  in §11 is cheap enough to run at every milestone.
- **Vitis creates a `logs/` directory in its working directory.** Waveflow's
  runner defaults that directory to the TCL's folder (`waveflow/toolchain/toolchain.py`,
  `run_vitis_hls`), so in practice the two coincide. The example `.gitignore`
  covers it; any new directory that runs Vitis needs the same.
- **The readiness gate leaves `xelab.log` and `xelab.pb` in the repo root.** The
  `xelab --version` probe writes them to the current directory. Both are
  gitignored; delete them after the gate.
- **Log overlapping steps as a deviation.** Preparing one step while another's long
  run is in progress breaks "one step at a time" even when the commits stay in
  order. Record it in §15.

## Phase 1 (2026-09-30)

- **Ruff 0.16.9 here goes beyond the E/F defaults.** It flags `RUF046`
  (`int(round(x))`: `round` already returns an int) and `FURB161` (use
  `int.bit_count()`). Write new code that way from the start.
- **Black 26 warns "Python 3.12 cannot parse code formatted for Python 3.15".**
  The warning is harmless as long as `black --check` reports the files unchanged.
- **Near convergence, CG's rounding sets any "equals" tolerance.** Two correct CG
  implementations (gemm vs gemv, or 1 vs 32 columns in one gemm) agree to about
  1e-15 early on, but only to about their own convergence error (up to 1.6e-10
  here) at nit = K. Compare iterates tightly before convergence and loosely at
  it, or compare each against the exact solve.
- **Worker processes barely beat multithreaded BLAS here.** A serial timing that
  already used all 8 cores through BLAS predicted about 5 minutes with 8 pinned
  workers; the real run took 29 minutes. Time a single-threaded worker before
  estimating a parallel run.
- **`--through X` runs only X's ancestors.** A final step must consume every table
  its acceptance criterion expects, or those tables silently go unbuilt.
- **Render the figures and look before committing.** The PNG preview caught a
  title/legend collision, a curve hidden under another, and a legend label valid
  for only one panel. None of those show in tests or checksums.
- **Pick a statistical test's operating point from the theory table.** A point
  above the ZF crossing gives too few errors for a comparison to mean anything.

## M1 review (2026-09-30)

- **The DAG decides staleness from source-file timestamps.** A docstring-only edit
  to `mimo_cg.py` or `detectors.py` re-runs the 30-minute tables. Batch edits to
  model sources, and run `--status` before a build to see what will run.
- **Intervals counted over channel blocks are too wide to compare detectors.**
  They are valid, but at BER 1e-3 the 99.9% interval spans two orders of
  magnitude. Compare detectors on identical samples (paired), and use
  theory-crossing agreement as the accuracy evidence.
- **One scale for every detector is not neutral.** Exact MMSE's μ mis-scales CG
  iterates, because CG is nonlinear in its right-hand side, which leaves CG
  slightly pessimistic at 2–3 iterations. A "same post-processing for all"
  argument needs checking, not assuming.
- **After stopping a background run, check for orphaned workers and verify the
  outputs by hash.** `write_table` writes only at the end of a step, so a stopped
  run left the committed tables intact.

## Phase 2 (2026-09-30)

- **Read the vendor header, not only the guide.** `ap_fixed_base.h` gives the
  exact `RType::div` result format and the operation (`(a << Fb) / b`, truncated
  toward zero). It also shows the C-simulation divide running at the shifted
  dividend's width, which makes most-negative / −1 LSB wrap. Only an edge-case
  conformance pair exposed that.
- **A fixed-point division keeps the dividend's fraction bits.** Quotient
  precision comes from widening the dividend first, not from the divisor or the
  target format.
- **Check a format's LSB before writing a test value.** `s16_10` has 6 fraction
  bits, so 2⁻¹⁰ quantizes to 0, and the zero guard then hides the mistake as a
  "wrong" result.
- **A squared quantity needs about twice the fraction bits.** rᴴr = Σ|r|² and pᴴAp
  get their integer bits from the first iteration but shrink quadratically as CG
  converges. With the same relative precision as the vectors they lose precision
  early and stall α and β. Both have to be widened; widening one barely helps (M2
  review).
- **"Fits 64 bits" has to cover every shift, not just the arithmetic.** The
  quantize up-shift after a division can need more bits than the division itself,
  so check it in the same feasibility function.
- **Edge widths find latent bugs.** `truncate` had always overflowed at 63 bits;
  nothing used 63-bit wraps until a widened division did.
- **Anchor plan edits at line start.** `| 2.3 |` also appears in the §9 numbers
  table. Match `\n| 2.3 |`, and check that the commit includes the plan file.
- **Don't use `std::complex<ap_fixed>` for a bit-exact model.** Its `operator*`
  assigns each partial product back to the element type, which rounds where the
  model keeps full precision. Keep re and im as separate `ap_fixed` values and write
  the products out.
- **Let Python emit only the types; hand-write the algorithm.** The C++ template
  and the Python golden share one documented register order, and Python generates
  the exact accumulator typedefs. Both matched on the first C-sim run.
- **Batch conformance cases into one C-sim run.** One compile per case set (about
  8 s) handles 51 problems, against roughly 6 s per case for one run each.

## M2 review (2026-09-30)

- **Test a diagnosis by changing one thing at a time.** "rᴴr sets the floor" looked
  right from one experiment. Widening rᴴr alone, then pᴴAp alone, showed that both
  are needed.
- **Scan feasibility limits over every integer.** Trying only even guard widths put
  the cap at 10 when it was 11.
- **`np.abs` on int64 overflows at −2⁶³.** Build truncating division from
  `floor_divide` and a remainder correction.
- **Coverage needs a count, not just a pass.** Bit-exact on 580k words still said
  nothing about α/β saturation until a stress set forced it and a test checked it
  happened.

## Phase 3 (2026-09-30)

- **Don't name a sweep axis or step parameter `config`.** `BuildStep.run(self,
  config, **params)` already takes `config` (the BuildConfig), so the DAG passes it
  twice and every point fails.
- **Seed by chunk to parallelize a point without moving samples.** A generator
  keyed by (point, chunk index) lets any worker rebuild any chunk, so only error
  counts cross process boundaries.
- **Edit scripts must survive black.** Matching exact text that black has since
  reformatted failed twice. Match on stable tokens or patterns, and stop with
  nothing written when a match fails.
- **Estimate parallel runtime under the real concurrency, not one process alone.**
  The fixed-point CG is memory-bandwidth bound; on 4 physical cores, 8 workers
  were no faster than 1. Benchmark N = 1, 2, 4, 8 concurrent processes before
  quoting a time. (The same trap as Phase 1's float sweep; this time it cost a
  stopped run.)
- **Small chunks beat big ones on a memory-bound workload.** 131k-bit chunks with
  2 workers gave about 3× the throughput of 1-Mbit chunks with 8.
- **Spawned workers re-import `__main__`.** A benchmark piped through stdin
  breaks the pool; use a script file with an `if __name__ == "__main__"` guard.
- **Background tasks have a time limit (30 min by default, 2 h at most).** A long
  sweep launched with the default was killed at 30 min. Make long runs resumable
  and split them into subsets (here `--case` halves) that each fit the limit.
- **Resolve the decision boundary, not just the curve.** A stop rule of 100 errors
  resolves a BER curve well, but a 0.5 dB budget decided from two curves' crossings
  moved by about 0.06 dB between seed streams, enough to flip near-boundary
  headlines. Spend extra samples only at the SNR points that bracket the decision,
  and report σ and a fragile flag with every threshold result.
- **Narrow a ParamGrid with `GRID.subset`, never a fresh one-value grid.** A
  single-value axis is dropped from point labels, so per-case refinement runs
  collided in the summary and `--resume` skipped them. A merge-time check of each
  point's recorded budget caught it.
- **Patch-and-raise doesn't prove a call never happened.** `subprocess_result`
  swallows exceptions, so a "never calls Vitis" test must record calls and assert
  the record is empty.

## Phase 4 (2026-10-01)

- **Don't trust a threaded C-sim of a stream-of-blocks composite.** In Vitis 2024.1's
  C-sim model, `write_acquire` already hands the block to the reader, and `full()` is
  always false. The matmul store read blocks the systolic array had not filled. Fire the
  task bodies in dependency order instead; the RTL (XSI) has the real ping-pong semantics.
- **A `HwParam` must be an integer** (or a bool). Index a registry by integer id
  rather than passing a name.
- **A stream-of-blocks depth is part of its C++ type.** Every task body that touches
  one needs the depth as a template argument, or the depth knob will not compile.
- **Widen registers to an aligned memory format.** A 12-bit complex register would put
  2.5 elements in a 64-bit word, and the serializer splits `re` from `im` across words.
  A 16-bit format with the same integer bits is exact and stays on proven ground.
- **In one HLS composite, a job must write as often as it reads.** HLS feeds the `m_axi`
  pointer arguments to the mem-stream tasks through FIFOs that one `entry_proc` fills
  in lockstep, so the reader can lead the writer by only about seven firings. One extra
  read per job deadlocked the RTL after six jobs, in XSI only: the Python sim and C-sim
  cannot see it. Balance with a zero-length final write that carries the echo. Test
  more jobs than any FIFO is deep. Within a job, the reads needed before the first write,
  minus the writes already done, must also stay within the writer's pointer-FIFO depth,
  which HLS chooses (7 in the units, 8 in the detector).
- **A guarded divide is a serial divide.** `(d == 0) ? 0 : n / d` inside an unrolled loop
  made HLS run the L dividers one after another. Divide by a safe divisor and select
  instead: it is bit-exact and the dividers run side by side.
- **Trace the top before guessing.** The VCD dumper (run.sh `trace`) shows each task's
  done count and each FIFO's full/empty state at the hang. Two pages of output named
  the culprit after two blind hypotheses had only narrowed it down.

## Gate 5.0 (2026-10-04)

- **A unit's job interval is not the block's time.** In a per-block unit, a fast block
  waits for memory. Two of eight probes were memory-bound: the job interval was not even
  linear in nit (residuals of 61–64 cycles). Measure a block from its own
  stream-of-blocks handshakes in a level-1 trace.
- **csynth's latency report is not the RTL's cycle count.** For the two CG blocks it
  overstates the measured per-iteration cycles by a constant 85 at K = 4, 8 and 16.
  Use it for nothing that will be quoted.
- **A task's csynth row does not depend on the top.** The vector unit and the matmul have
  identical rows in their unit builds and in the detector. So resources compose exactly
  at the csynth level, and the integration term is only channels, FIFOs and adapters.
- **Narrow multiplies leave the DSPs.** At W = 8 the vector unit has 3 DSPs for 12
  multiplies. "One DSP per multiply" holds at W = 12 and above, not below.
- **csynth and implementation disagree by a lot.** On the probe design: 805 → 340 LUT,
  1,431 → 279 FF, 32 → 26 DSP. State which one a number is.
- **The RTL run's cost is elaboration, not simulation.** A 6-job run of a 120k-LUT build
  took 4–5 minutes, almost all of it in xelab; a small build takes about one.
- **A 32-bit memory word doubles the done words.** `CgDesc` is two words at 32 bits, so
  the done log has two cycles per job; take the last.

## Phase 5 (2026-10-04)

- **The XSI harness runs exactly `n_cycles` cycles.** It has no early stop. The gate 5.0
  probes asked for 4 million and paid for all of them (1–5 minutes each); with a budget
  sized to the scenario the same runs take 10–30 seconds. Budget generously, check the
  done count, and double on a miss.
- **A stream-of-blocks writer only waits at the hand-over.** It writes its block freely;
  the `_write` pulse is what waits for `i_full_n`. So an output stall shows as the channel
  becoming free in the same cycle as the hand-over, not as a late first write.
- **Depth 2 means one pending block.** `i_full_n` drops after a single hand-over and
  rises when the reader releases that block.
- **Take the smallest clean span.** A task that finds its input already waiting spends one
  more cycle (its loop-back state) than a task that was idle. In the detector the blocks
  wait for each other, so the idle figure is the one that composes: 208 + 985 = 1,193.
- **The csynth FIFO table has no LUTs.** The summary counts them (468 for seven FIFOs);
  the detail rows say 0. Keep the difference as its own row or the parts will not add up.
- **A report has two "+ Detail" sections.** The latency one comes first and its Instance
  table has a two-line header. Slice from "== Utilization Estimates".
- **"One DSP per multiply" holds only from 12 bits.** Below that the tool builds a plain
  multiply from LUTs and keeps only multiply-add patterns in DSPs. Read the report's
  instance names (`mac_muladd_…`, `mul_8s_8s_…`) before writing a DSP rule.
- **A stream-of-blocks is one memory, not one per bank.** Its banks share a
  simple-dual-port memory, so a wide element uses the 36-bit shape: 96 bits cost 3 blocks,
  not 6. It splits into one memory per bank when a task touches two groups per cycle.
- **Break a module into its report rows before regressing it.** The csynth report has a
  row per pipelined loop. The matmul's sweep, output loop and registers each follow a
  simple law; their sum does not look like one. The loaders only became fittable from
  their loop rows, where two instances of the same code give twice the points.
- **The report can be re-read; the synthesis need not be re-run.** Keeping the build
  directories let the attribution gain detail after the campaign, for free.
- **Unit jobs that are block-bound differ from waiting jobs by one cycle.** That is why the
  span is the smallest clean sample, and why it composes exactly in the detector.
- **csynth LUTs are not implemented LUTs, and the ratio depends on the module.** On the
  detector csynth is 3.4–3.7× high overall, but 20× high on the matrix loader and about
  right on the channels. A model can match csynth to 2% and still mislead about where
  the LUTs are. Check a few designs through place and route before ranking by LUT.
- **`export_design -flow impl` reuses the csynth project.** Open the project and solution
  without `-reset`; it took 15–17 minutes per detector, three at a time.
- **Raise `WANT_XSI_GATES` with every new `xsi` test.** `tests/conftest.py` records how
  many RTL gates the suite has, and a collection test fails when the number is stale.
  Phase 4 added seven gates without raising it; only a full-suite run shows it.

## M5 review (2026-10-04)

- **A uniform held-out draw can miss a corner.** With 12 draws from 225, no K = 16
  vector-unit build came out (K = 16 is a third of the space). Stratify the draw on the
  knobs that drive size, or fix a minimum per stratum, before the seed is set.
- **Check which knob values a fit design skips, and write them down.** The matmul design
  dropped two lane counts to stay at 26 builds, and its LUT model then extrapolated at 16
  lanes (16% low) without saying so. A coverage test should state the gaps.
- **"Exact on 34 of 34" needs its denominator explained.** Most of those comparisons were
  zero against zero. Report how many cases were non-trivial.
- **A held-out build can share a block with a fit build of another top.** Exclude block
  keys across tops when drawing, or disclose the overlap and score without it.
- **Run the whole fast suite at every milestone.** Running only this example's tests hid
  a failure in a shared test for a whole phase.
- **A reality check on one knob setting supports claims about that setting only.** Three
  implemented designs with the same lanes and format say nothing about narrow formats.
- **A stratified supplement found what the uniform draw could not.** Six builds aimed at
  the thin strata confirmed the resource models at K = 16 and exposed a regime the cycle
  model lacks: a fast loop behind slow memory, where job time is set by loading the
  matrices. Twenty-six detectors had never entered it.
- **"Linear in nit with zero residual" is a property of the builds measured.** It held in
  every detector until one was fast enough to outrun its memory. Check the residual of
  every new design, and treat a non-zero one as a regime change, not noise.
- **Vivado undoes part of what csynth reports at narrow widths.** csynth built the matmul's
  8-bit multiplies from LUTs (32 DSPs); Vivado put most of them back into DSPs (60). The
  DSP saving of a narrow format is real but smaller than csynth says.

## Gate 6.0 and Phase 6, steps 6.1–6.4 (2026-10-04)

- **Check that an acceptance measure can fail before agreeing to it.** AC6's first form
  asked whether the model finds the minimum-DSP design. DSP was counted exactly and the
  question had no latency constraint, so the answer was always the smallest hardware and
  the measure passed by construction. A prototype of the measure on predicted numbers,
  before the gate, showed it.
- **Size a gate's options with a scratch prototype.** Joining the accuracy table with the
  estimator took an hour and settled three questions with numbers: the memory-bound regime
  touches no accuracy-feasible design, every scenario has a design at W ≤ 16, and 23% of
  the frontier sits where the matmul LUT model was weak.
- **The report's sub-block rows say where a knob acts.** The lane count changes one loop
  of the matmul (the one that rounds and writes S); the sweep and the rest do not move.
  Three terms in the right place beat v1's three guessed ones (leave-one-out 7.3% → 2.2%).
- **Count what has a trip count; fit only the rest.** The matmul's cycles are two passes
  of K·N/L, a sweep of K + R + C − 2 per tile, and a per-tile overhead. Fitting the
  trip counts too gave a sweep coefficient of 0.91 and nine parameters.
- **The tool's loop merging is a threshold, not a rule you can assume.** HLS merged the
  tile loops into the sweep at C = 4 and 8 and not at 16. One extra build at C = 8 found
  the boundary; assuming it cost 28% at C = 16.
- **A second calibration round can overlap an earlier held-out set.** Repeating the first
  design's corners at 2 lanes made a calibration build out of the matmul of a supplementary
  held-out detector. Check new calibration builds against held-out *blocks*, not only
  against held-out builds of the same top.
- **A refit can be worse somewhere.** v2's matmul span is 4.4% off on a held-out build
  that v1 had within 0.5%. It was scored after the freeze, so it stays, and it is reported
  beside the improvements.
- **Write the scoring code before the data.** The rule for "right", its edge cases (a pick
  that fails to build, a budget nobody meets, ties) and the tests on made-up measurements
  were committed with the decision set, before the first brute-force build.
- **A long campaign needs four things a short one does not:** builds pruned as they finish
  (1,440 unpruned builds would not fit the disk), a simulation budget near what the run
  needs (the model-free bound was 3–7× too long), builds that are skipped once measured
  (so a pilot counts and a restart is free), and processes detached from the session.
- **Pilot on a sample that visits every knob value.** A stride of 120 over the grid met
  only one memory width and three of five word widths; a stride coprime to the grid's
  inner loops (113) matched the grid's mean size within 1%.
- **A file named `*summary*` is ignored by this repo.** It happened twice
  (`model_validation_summary.csv`, `dse_summary.csv`). Check `git status` after writing
  a new table.

## Phase 6, steps 6.5–6.9 and the M6 review (2026-10-05)

- **Do not run the test suite beside Vivado.** Three place-and-route jobs and the fast suite
  together left 262 MB free; one Vivado segfaulted and took a runner process with it.
- **A tool's report file is not a completion marker.** `export_design` writes its report
  early and fills it in at the end. Judge completion by the tool's return and its log.
- **A long run's merge can destroy derived state.** Merging the brute force rebuilt the
  calibration work store empty, because the store was rebuilt from "the calibration builds
  in this merge" and there were none. A rebuild step should do nothing when it has no input.
- **Record when a run started from its own start file, not from a later status check.**
  The log said 21:34 for a run that started at 20:55, and a "committed before any result"
  claim was false by 40 minutes.
- **Push pre-registration commits before the run.** Locally the order is clear, but the
  commits reached the remote together with the results, so nothing outside this machine
  attests it.
- **Code that scores must not change once any data exists, or the change must be shown
  harmless.** Two additions went into the scoring module after the pilot; re-running the
  module as pre-registered on the final data showed the same rows.
- **Say what a headline covers wherever it is quoted.** "99.8% of the decisions" was a
  1.3% slice of the space with 3 of 17 lane and column pairs; the summary, the results
  table and the finding each needed that clause.
- **A median hides a spread.** "Guard bits cost nothing in DSPs" was the median of pairs
  of which 62 used more DSPs and 7 fewer. Give the range, and count the pairs that are the
  same design twice.
- **"None of these was seen before" needs a set difference, not a memory.** Seven of the
  twelve finalists had the knobs of a brute-force build.
- **Check a measure against a crude predictor.** The reviewer scored "cost = lanes" and
  random costs (1–17% right) and noisy models (fail at 10% noise). That is what shows a
  99.8% is not built in.
- **Timing closure is a claim about the designs that were routed.** The largest routed
  design had 384 csynth DSPs and 0.09 ns of slack; a fifth of the frontier is larger.
- **A new assertion's bound should come from the data, then be stated.** Guessing "within
  5%" for a value that was 5.08% failed the test; so did a hand-typed 7.6% for 7.55%.

## Gate 7.0 and Phase 7 (2026-10-06)

- **Probe a design question in the flow that made the committed numbers, and reproduce one
  committed row first.** Swapping the core's body inside the example's own build answered
  the four gate 7.0 questions in a few minutes of csynth (estimated 45–60), and the
  unmodified body giving the committed `CgMm` row exactly showed the comparison was fair.
- **Loop timing of a dataflow task is in the per-loop reports.** The top csynth report lists
  no loops; `*_Pipeline_*_csynth.xml` has each loop's II and depth.
- **A schema field must not share a name with the generated struct's API.** A header field
  named `nwords` serialized fine in Python and broke C-sim: the generated struct already
  has a static `nwords<W>()`. Building the C++ is the only check of the names.
- **A type that only widens needs no rounding or saturation mode.** With the register's
  modes, two registers that differ only in their modes made two memory element types of
  identical bits; without them, they share one element type and one array-utils file.
- **A probe answers the question it built.** Gate 7.0 measured the conjugate transpose with
  the example's K-lane rows of `A`; with L-lane groups the same transpose ran csynth out of
  memory. When a decision rests on a probe, list what the probe held fixed.
- **csynth's memory, not the design, can be the limit.** Run-time-indexed writes into
  partitioned arrays (a lane to any of 16 banks, a row to any slot) made csynth climb past
  10 GB in minutes. Write every bank through constant indices with a fixed source, and watch
  memory: a watchdog that kills at 8–10 GB turns a 27-minute crash into a 3-minute answer.
- **Move work out of the hard place.** No in-core form of the transpose was both safe and
  small; moving it to whoever writes the block made the core's load 1.1k LUT.
- **Measure the clever version before keeping it.** Conjugating in the three-multiply
  pre-adders saves a bit of width on paper and cost twice the LUTs of the array in csynth.
- **`pytest tests/linalg/` runs the toolchain tests too.** Nothing deselects `vitis` by
  default; the quick loop is `-m "not vitis and not xsi"`.
- **`pkill -f` matches its own shell.** A pattern that appears in the command line kills the
  command; kill by PID.
- **In one generated top, the in-band memory reader and writer must fire equally often.** The
  `m_axi` pointer FIFOs couple their firing counts; a writer one firing short per job stalled
  the RTL after eight jobs (its FIFO is 9 deep) while pysim ran on. Count firings per job when
  a bench reads more often than it writes, and pad with empty writes.
- **A gate that stops at a round number is a capacity, not a bug in the last job.** Bisecting
  by content (reordering, ten identical jobs) showed the count, then the 9-deep FIFO.
- **Placement by a run-time lane index costs more than it looks.** Writing each value into
  lane `e % L` of a group cost 4k LUT per loop at II 2; shifting values through a group
  register cost 620 at II 1, bit for bit the same.
- **C-simulation cannot see undefined behaviour that `ap_uint` defines.** A shift by a
  register's full width is zero in C-sim and undefined in synthesis; the RTL wrote zeros
  while every C-sim test passed. Only an RTL run at that configuration catches it, so a
  calibration campaign that checks bit-exactness at RTL is also a test campaign.
- **Read the framework's fit defaults before trusting a fit.** Two defaults, each sensible
  elsewhere, broke this one: FF fitted without the builds whose arrays land in LUT RAM (most
  of them), and only the terms non-zero for the first sample. A 60% error is a data problem
  before it is a model problem.
- **Let the data choose between max and sum before the held-out set runs.** The pre-registered
  `max(compute, I/O)` was physically plausible and 14% off; the stages serialize in a
  memory-fed design, and the sum is 2% off. One sign was free: `Aᴴ`'s extra load time showed
  up even on compute-bound jobs.
- **A rule copied from another datapath is a hypothesis.** "Plain multiplies under 12 bits are
  LUTs" held for the example's matmul and not for this one, where the tool packs them.

