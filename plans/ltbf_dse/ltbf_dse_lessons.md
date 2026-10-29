# Lessons: LTBF on a shared-memory accelerator (`plans/ltbf_dse/ltbf_dse.md`)

What this work taught that the next one should not have to rediscover: mistakes, failed approaches,
surprises, and rules worth keeping. One entry per lesson, newest last, each with the step it came from.

## From planning (2026-10-28)

- **`Processor` is not a bus master.** `waveflow.cpu.Processor` prices compute; the bus traffic of a
  software host goes through a `SwHost` (or, as in `examples/cpu_sched`, dispatchers) holding the
  `MMIFMaster`. A host that both talks to the bus and charges A53 cycles combines the two. (plan review)
- **`SystolicEngineMem`'s formats are build parameters**, so a register write cannot switch them; one
  format triple serves every job of a build. `MUL` and `MUL_AH` read A in different block shapes, so a
  matrix used both ways is stored twice. (plan review)
- **The vector unit's programs are straight-line** (control ops SETVL, QPOP, QPUSH, SETRS) and its
  registers live one job; per-iteration work through memory reloads its state and recomputes its
  scalars. Divides only on the scalar lanes (`wf.sdivz`). A single reader feeding all its input streams
  in a fixed order deadlocks. (plan review)
- **Ingest is part of the real-time budget.** A grid at 64 antennas arrives at about 22.7 GB/s; one
  128-bit port at 250 MHz carries about 4 GB/s. A diagram without an ingest path overstates real time.
  (plan review)
- **The Denver drops' absolute delays exceed the 120 kHz CP** (0.59 µs): render with a per-UE timing
  advance; about 2.9% of links still exceed the CP afterwards. (plan review)

## From execution

- **The editor publishes new branches by itself** (step 0.2). VS Code's git extension ran
  `git push -u origin paper/ltbf_dse` three seconds after the branch's first commit (its `Git.log`
  under `~/.vscode-server/data/logs/<session>/exthost1/vscode.git/`). Earlier branches show it pushing
  later commits too. Check `git status -sb` right after the first commit, not only at milestones, and
  never tell the user a push was "one-time" without reading that log.
- **Gate floors count quarantined tests as present** (step 0.2). Measure `WANT_XSI_GATES` with
  `WAVEFLOW_NO_QUARANTINE=1 pytest -m xsi --collect-only -q` (the rule
  `tests/build/test_xsi_session_gate.py` uses); without it the count is lower by the quarantined gates
  (229 vs 246 here). This repo's `-q` prints no pass/skip summary line: read the progress dots, and
  never pipe pytest through `tail` when its exit code matters.
- **`SystolicEngineMem` crashes Vitis HLS 2024.1** (spike 1.2 b). The redesigned engine (Part B of
  `plans/systolic_characterization.md`) was only ever built with 2025.1. With 2024.1, csynth of the
  unchanged centre configuration segfaults (`Abnormal program termination (11)`, `hs_err_pid*.log`,
  `FreeRunPipe::getBlockCondOfMemFifoPort`) right after HLS turns the core's `STEP` pipeline into a
  free-running pipeline (control fanout about 23k). Adding `config_compile -pipeline_style flp` to the
  csynth script, with the source unchanged, avoids it: 49 s, 144 DSP, 32,937 LUT, 37,278 FF,
  29 BRAM18, 3.35 ns estimated.
- **Render a sector's signals as `A @ D`, not path by path** (spike 1.2 a). The synthetic array shares
  one delay per (UE, path) across a sector's antennas, so delaying each (UE, path) waveform once with
  rfenv's own kernel and applying the array as one matrix product equals `reference_apply` to 4e-16
  and renders a 32x32 slot of three sectors in 0.38 s, against about 48 s through `ChannelApplier`.
  The kernel index: `convolve(x, c)[n - k + half - 1]`, not the reversed kernel.
- **The three sectors are co-located**, so after a per-UE timing advance no path of any sector
  arrives before the window; 2.96% of UE-sector links (13,122 over 100 drops x 2 snapshots) spread
  beyond the 0.586 us CP, at most 4.2 us. (spike 1.2 a)
- **Vector-unit pass rules shape the programs** (spike 1.2 c). A pass reads and writes at most one
  group a bank, and a group once (`vlse S` then `vadd S, S, D` in one pass is refused); the broadcast
  form copies **lane 0 to every lane** (not an element along a vector), and `.vs` scalars are real;
  so a per-subcarrier complex weight cannot be applied across symbols: give `h` a resource element.
  `waveflow.vecunit.asm.check(cfg, progs)` lists every rule a program breaks; run it first.
- **The queue-pointer limit caps joint CG at 1024 antennas** (spike 1.2 c):
  `Kmax * ceil(Nmax / L) <= 4096` and a column's dot product needs all `N_rx` rows in one block, so
  at `N_rx = 1024` a job holds at most 64 columns (`L = 16`).
- **`Processor` frees a core at the unrounded A53 time** (spike 1.2 e). Padding a call to a host-clock
  edge after `execute` makes its own time match the C++ twin's `compute(n)`, but a call queued behind
  it starts up to one host cycle earlier than behind the twin's core semaphore. Exact with one
  scheduler thread (the RTL gate); within a host cycle a call otherwise.
