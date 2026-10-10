# Plan: an incremental XSI runner, and cheap workload sweeps at RTL

**Status:** drafted 2026-10-10.

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

(empty)
