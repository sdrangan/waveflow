# Plan: an incremental XSI runner, and cheap workload sweeps at RTL

**Status:** drafted 2026-10-10; Stages 0-5 done 2026-10-10 (branch `incremental-xsi`).

## Motivation

The timing events (`waveflow.events`, `docs/guide/build/timing_events.md`) split an XSI run into
its phases.  On the examples:

| phase | `mem_r_stream` (158 cycles) | two-kernel bus system |
| --- | --- | --- |
| compile RTL (`xvlog`) | 3.6 s | 5.0 s |
| elaborate (`xelab -dll`) | 11.1 s | 16.0 s |
| compile the testbench (`g++`) | 4.5 s | 6.4 s |
| **simulate** | **0.2 s** | **0.3 s** |
| total | 19.5 s | 28.0 s |

Re-running the already-built testbench executable directly took **0.08–0.10 s**, same result.  But
`run.bat` / `run.sh` redo every phase on every call, and the build steps always call them.  So an
RTL evaluation costs ~20–30 s even when nothing in the design changed.

That matters for design-space exploration.  There are two kinds of sweep:

- **workloads on a fixed design** (job lengths, traffic, scenarios): the testbenches already read
  their scenario from files under `vectors/`, so a compiled snapshot could be re-run with new
  vectors in about a tenth of a second plus simulated cycles;
- **design parameters** (bus width, depths, a new kernel): these pay csynth of the changed tops,
  then elaborate and compile -- minutes per point, where pysim's seconds win.

This plan makes the first kind cheap, and makes the second pay only for what changed.

## Decisions

- **D1 -- split the runner into build and run.**  `run.bat <top> <tb> build` compiles,
  elaborates and builds the testbench; `run.bat <top> <tb> run [vectors_dir]` only runs.  The
  default (no verb) stays "both", so existing callers and the committed copies keep working.
- **D2 -- skip by content, not by timestamp.**  A stamp file in `xsi/` records a hash of every input
  to each phase: the RTL file list's contents (including IP sources) for compile and elaborate; the
  testbench sources and the generated headers it includes for the testbench build.  A phase is
  skipped only when its hash matches.  Timestamps are not enough: the repo has been burned by
  stale RTL passing for current (`reference-src-shrink-stales-consumer-rtl`,
  `project-xsi-staleness-hash`), and a stale snapshot measured silently is worse than a slow one.
- **D3 -- the traced build is a different snapshot.**  `trace` elaborates the VCD dumper as a second
  top; give it its own snapshot name (`<top>_trace`) so traced and untraced builds coexist and
  neither invalidates the other.
- **D4 -- a run's outputs go where the caller says.**  The generated testbench `main` takes the
  vectors directory (inputs and outputs) as an argument, defaulting to `vectors/`.  Two runs can
  then share one snapshot without overwriting each other's outputs -- the prerequisite for
  parallel workload sweeps.
- **D5 -- one Python entry point.**  `XsiSnapshot(work_dir, top, tb)` with `.build()` (incremental)
  and `.run(vectors_dir) -> output`, used by `run_xsi`, `RtlSimStep`, `xsi_workspace` and the
  system flow, so every caller gets the same skipping and the same timing spans.

## Stages

**Stage 0 -- the re-run exit code.**  Re-running `mem_r_bfm_tb.exe` directly printed the right
result (`cycles=158`) but exited 1, where the full runner exits 0.  Find out why before building on
re-runs: a stale output bundle the testbench refuses to overwrite, a check it does on a previous
run's files, or something in the environment `run.bat` sets.  Gate: a bare re-run exits 0, or the
reason is written down and handled.

**Stage 1 -- build / run split, with content stamps (D1, D2, D3).**  Edit
`waveflow/build/xsi/run.bat` and `run.sh`; refresh the committed copies (the copy test in
`tests/build/test_xsi_workspace_copies.py` enforces it).  Gates: every XSI gate passes with its
cycle count unchanged; a second build with no change skips compile, elaborate and the testbench
build (visible as missing `WF_PHASE` spans in the events); touching one RTL file re-runs compile
and elaborate but not the testbench build; touching the testbench re-runs only that.

**Stage 2 -- the vectors directory (D4).**  The testbench generators (`render_tb_main` in
`waveflow/build/composite_gen.py`, and the system harness in `waveflow/build/system_xsi.py`) take
the directory as an argument.  Gate: two runs into two directories from one snapshot, outputs
identical to a single run's; all gates unchanged.

**Stage 3 -- `XsiSnapshot` and its callers (D5).**  Route `run_xsi`, `RtlSimStep`,
`XsiWorkspace` and `system_xsi` through it.  Gate: the fast suite and `pytest -m xsi` pass; the
events of a no-change rebuild show only `simulate`.

**Stage 4 -- a workload sweep at RTL.**  Wire it into `SweepRunner` (`docs/guide/build/sweep.md`)
as a stage whose points share one snapshot.  Demonstrate on `mem_copy` or the bus system: fifty
job-length points at RTL and in pysim, with the per-point cost of each from `analyze_events`.
Gate: the sweep's RTL points cost about the simulate time each, not the full runner time.

**Stage 5 -- docs.**  `docs/guide/build/xsi.md` (the build/run split, the stamps, the snapshot API)
and `timing_events.md` ("Today this is done by hand" becomes how it is done).  Name steps and link
them.

## Open questions

- **Vivado's own incremental compile.**  `xvlog` / `xelab` may have incremental options that reuse
  unchanged design units.  If they work in this flow, a one-module change could cost less than a
  full elaboration.  Evaluate after Stage 1; content stamps are the safe baseline either way.
- **Parallel runs on one snapshot.**  Does `xsim` write into `xsim.dir/` at run time (logs, a
  waveform database)?  If so, parallel runs need per-run scratch directories.
- **Vitis cosim.**  Its ~3 min fixed cost is a SystemVerilog harness rebuilt on every run, which this
  plan does not touch.  Whether the cosim harness can be reused across runs is a separate question.

## Progress log

**Stage 0 -- the re-run exit code (2026-10-10).**  Not the testbench, not the environment: the
**scenario**.  `mem_r_stream` and `mem_w_stream` share one workspace (`examples/interleaver/xsi`) and
both write `vectors/cmd` and `vectors/golden`.  Whichever gate ran last owns them, so a bare re-run of
`mem_r_bfm_tb.exe` after the `mem_w_stream` gate read mem_w's command and golden: it still
collected 128 words in 158 cycles, but checked them against mem_w's golden -- `FAILED test: 128
mismatches` -> exit 1.  After
`write_mem_r_xsi_bundles`, the same bare re-run printed `PASSED` and exited 0.  This is D4's case
exactly, found before D4 was built: two runs sharing one `vectors/` overwrite each other.

**Stage 1 -- build / run split (D1, D2, D3).**  `run.bat` / `run.sh` take
`[trace] [all|build|rtl|tb|run] [vectors_dir]` in any order; no verb is `all`, so every existing
caller and every gate is unchanged.  Each phase deletes its own outputs (the snapshot directory, the
testbench `.o` and binary) before rebuilding, so a failed phase cannot leave an old artifact behind.
`trace` elaborates into its own snapshot `<top>_trace`.

*Deviation from D2, deliberate:* the scripts do not hash.  Content hashing in `cmd` means
`certutil` per file inside `for /f` loops with delayed expansion -- the trap the script's own comments
already warn about -- and a second implementation of the same rule in bash.  The stamps are computed
in Python (`XsiSnapshot`, Stage 3), the one caller that decides; the scripts only *invalidate* them
(each phase deletes the stamp that vouches for the outputs it is about to replace), so a by-hand
`run.bat` that rebuilt from other inputs can never leave a stamp that still matches.  The design's
stamp lives inside `xsim.dir/<snapshot>/`, so a gate that deletes the snapshot to force a clean build
deletes its stamp with it.

*D3 without regenerating any testbench:* every testbench opens `ports::DESIGN_DLL`, a literal
`xsim.dir/<top>/xsimk.dll`.  `XsiSim` (the one place a design is opened) now honours
`WF_XSI_DESIGN`, which the runner sets for a traced run.  One testbench binary serves both snapshots.

*A stale comment corrected:* `run.bat` and `RtlSimStep` said re-running the built executable does
NOT regenerate the VCD.  It does -- when the snapshot it loads is the traced one.  The observation
behind the comment was a re-run against an untraced snapshot.  Measured: two consecutive runs of the
traced `mem_copy` snapshot each wrote a fresh 1.0 MB VCD; a run of the untraced one wrote none.

Measured on `mem_r_stream`: cold build 10.9 s; a no-change build 6 ms; editing one RTL file -> only
`rtl` stale (rebuild 7.6 s); editing the testbench -> only `tb` (5.9 s); run 0.14 s, `cycles=158`.

**Stage 2 -- the vectors directory (D4).**  *Deviation, deliberate:* no generator change.  All
bundle I/O goes through one header (`xsi_bundle.h`), and every bundle path a testbench names starts
`vectors/` (checked across all 45 committed mains).  So `BurstBundle` maps a leading `vectors` to
`WF_VECTORS_DIR` when set -- the runner sets it from its vectors-directory argument -- and
hand-written and generated testbenches honour it alike, with nothing regenerated.  `XsiSim` puts the
run's `.wdb` there too.

Open question answered -- **parallel runs on one snapshot are safe.**  At run time xsim writes only
`xsim.dir/<snapshot>/xsimkernel.log` and the `.wdb` (now per run).  Eight concurrent runs of
`mem_copy` into eight directories took 0.27 s in total, and every `out` / `s_done` bundle (with
`cycles.bin`) was byte-identical to a single run's.

**Stage 3 -- `XsiSnapshot` (D5).**  `waveflow/build/xsi_snapshot.py`: `.stale()`, `.build()`,
`.run(vectors_dir)`.  Stamps hash the runner, `rtl_<top>.f`, every listed file and every file of
each `--include` directory (and the dumper, traced); the testbench's hash the runner, `<tb>.cpp`,
`xsi_loader.cpp`, every quoted include transitively (resolved like the compiler would: beside the
includer, the workspace, then `-I` dirs), and `WF_TB_CXXFLAGS`.  A stamp is written only after a
phase succeeds *and* if its inputs did not change while it ran.  Routed through it: `XsiWorkspace`
(so `system_xsi` too -- its forced `rmtree` of the snapshot is gone, the stamps replace it),
`RtlSimStep`, and the `ssr_fft` / `vitis_l1` RTL runners.  `run_xsi` stays the timing wrapper the
snapshot itself calls.  The `-m xsi` gates in `tests/examples/` still call the runner directly with
no verb -- a full, forced build -- by design: "routing a green gate through new code is how a gate
quietly stops meaning what it meant" (`RtlSimStep`'s docstring).

Gates: `tests/build/test_xsi_snapshot.py` (fast; the runner faked: every skip decision),
`tests/build/test_xsi_snapshot_xsi.py` (`-m xsi`, `mem_r_stream`: a no-change rebuild's events are
`simulate` alone; an RTL edit is seen and undone by content; four parallel runs into separate
directories byte-identical to a single run; traced and untraced coexist).  `WANT_XSI_GATES`
163 -> 167.  The `rtl_digest` lint now counts `XsiSnapshot` as driving RTL.

*Full `-m xsi` after Stage 3: 162 passed, 1 failed, 1 error, 2 skipped -- all three explained, then
fixed and re-run green.*  The skips were mine (a scratch script had deleted `mem_copy_trace.vcd`).
The failure and the error were the same finding: **xelab exits 1 after a successful build** when it
cannot delete its scratch `obj/` (`Could not remove the obj directory ... being used by another
process`, `xsim_N.c` still held open on Windows), with `Built XSI simulation shared library` printed
just before.  The runner never looked at xelab's status, so this was always happening and always
harmless; `XsiSnapshot` reads it, so it surfaced.  The build now accepts exactly that case (library
built, that message, no `ERROR:` line) and still fails on a real elaboration error
(`test_xelab_failing_only_its_obj_cleanup_is_a_built_design`).

**Stage 4 -- a workload sweep at RTL.**  The plan's premise -- "the testbenches already read their
scenario from files under `vectors/`" -- was *half* true, and the false half was the expensive one.
The data came from files; the scenario's **sizes** did not.  mem_copy's generated testbench baked in
`h.run(3400)` and the arena `mem(24640, 8)` (and a dump of `{0, 24640}`), and `CodegenTbStep`
regenerated them per point -- so every point of the old sweep was a new testbench, 5 s of g++ on top
of the simulation even with the snapshot reused.

Fixed generically: `wfbfm::run_param(key, generated_default)` (`xsi_bundle.h`) reads the run's
`vectors/run.json` (`waveflow.utils.burst_io.write_run_params`) and prints any override
(`WF_RUN_PARAM`).  `render_tb_main` emits `h.run(wfbfm::run_param("n_cycles", N))`;
`MemoryMod.bfm_model` emits the arena as `wfbfm::run_param("mem_words", N)`; a whole-arena dump is
`MemSeg(0, 0, ...)` -- `len 0` = "to the end", as `len 0` already meant "the whole bundle" for a load
(fir_block, interleaver_inband and mem_copy all dump the whole arena).  The 18 committed generated
mains and 3 harnesses were rewritten to the new emission (mechanically; the codegen tests that compare
committed against generated agree).  mem_copy's `CodegenTbStep` no longer takes the scenario: the
testbench is generated for the design once, and `MemCopySim.write_scenario` -- the one scenario writer
for both backends -- writes `run.json` at every scenario, the default too, so a previous point's sizes
cannot outlive it.

Demonstration: `examples/mem_copy/mem_copy_workload_sweep.py`, through `SweepRunner`, 50 job lengths
(16..800 words x 4 jobs), each point pysim + traced RTL + `RtlCheckStep` (bit-exact, and the RTL
completion cycle recorded beside the pysim's).  Per point, from the sweep's own events:

| step | first point | median | max |
| --- | --- | --- | --- |
| `pysim` | 0.66 s | 0.10 s | 0.66 s |
| `rtlsim` | 0.19 s | 0.49 s | 1.36 s |
| of which `simulate` | 0.10 s | 0.33 s | 0.75 s |

All 50 clean in 36 s; **no compile, elaborate or testbench phase ran at any point** (the snapshot was
built by the two-point trial before it -- which also re-ran mem_copy's csynth once, its RTL having been
stale by mtime).  An RTL point costs its simulation plus ~0.15 s (runner start, hashing, the VCD
check), against 20-30 s when every run rebuilt -- the gate.  The RTL is the larger cost per point
here only because these traces are long (up to 3728 cycles traced) and pysim is uncalibrated (no
platform: `pysim_end_cycles` is end-of-simulation without the bus law, so it is *not* comparable to
`rtl_done_cycle` -- the calibration sweep is `mem_copy_sweep.py`).

Gate: `test_one_mem_copy_testbench_serves_every_workload` (`-m xsi`) -- one build, three scenarios
(the gate's 16x128 at its 2908, 4x1024 whose 33344-word arena exceeds the generated 24640, 4x16),
each `simulate` alone and bit-exact.  `WANT_XSI_GATES` 167 -> 168.

**Stage 5 -- docs.**  `docs/guide/build/xsi.md`: "Building once, running many times" (the verbs, the
stamps, `XsiSnapshot`, vectors directories, `run.json`) replaces the stale "Automation status" section
(which still called the XSI rung a standalone script with no BuildStep).  `timing_events.md`: "Today
this is done by hand" is now how it is done, with the measured costs.  `sweep.md`: workload axes at
RTL.  `timing/trace_steps.md`: the VCD warning corrected (only the traced snapshot writes it).

## Still open

- **Vivado's own incremental compile** -- not evaluated.  The content stamps make a no-change build
  free; what remains is a *one-module* change paying a full elaborate (7-13 s).  Worth measuring
  `xelab`'s incremental options only if a design-parameter sweep makes that the bottleneck.
- **Vitis cosim harness reuse** -- untouched, as planned.
- **The other sweeps** (`fir_block`, `vecmult`) are build-axis sweeps; nothing to share at RTL.
- A traced run's VCD name is fixed in the dumper (`<top>_trace.vcd` in the workspace), so *traced*
  runs of one snapshot cannot run in parallel; untraced ones can.
- **`tests/utils/test_trace.py` reads whatever `examples/mem_copy/xsi/mem_copy_trace.vcd` is on
  disk.**  The second full `-m xsi` after Stage 4 failed its two tests because the 50-point sweep had
  left its *last* point's trace (4x800) there; restoring the gate scenario's traced run (2908) made
  them pass, and the other 166 gates were green.  Pre-existing fragility, not new -- any traced run of
  another scenario does it -- but the cheap workload sweep makes it more likely.  **Fixed:** the two
  tests now produce their own trace through a module fixture (`gate_trace`: the gate scenario into a
  temp vectors dir, a traced `XsiSnapshot` run, checked at 2908) -- ~0.2 s with the snapshot built.
