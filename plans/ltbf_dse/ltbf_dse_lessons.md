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
- **Building another example appends to its tracked calibration** (spike 1.2 e). `markov_build`
  writes timing records into `examples/markov/calib/modules/*/timing/records.jsonl` (tracked) and
  leaves its `*_proj` csynth directories untracked but not ignored. After using another example as
  evidence, `git status` and restore what it touched.
- **2024.1 keeps the HLS headers apart** (spike 1.2 e): `ap_int.h` is in `Vitis_HLS/2024.1/include`,
  not beside `Vitis/2024.1/bin/vitis-run`; `find_vitis_include_dir` now looks there too. Running a
  module as `__main__` through `runpy` breaks dataclasses whose `ClassVar` annotations are strings
  (the class's module is the driver): call the module's functions instead.
- **One format triple needs power-of-two gains** (step 3.1, spec amendment 1). CG's `X` outgrows `D`
  by about `1/λ_min`, the real-time `Y`, `Z` are 100x the long-term values, and the vector unit's
  formats need integer bits >= 1 (`VecUnitConfig`); a profile-driven `2^-s` on `Ĥα`, `2^q` on the
  right-hand side, `2^-k` on `Y`, `2^-g` on `G'` and `2^-m` on `X_b` keep every hand-off in one triple,
  each removed exactly downstream. Without them W <= 14 collapsed to zero.
- **Framework usage that cost iterations** (step 4.1): a `FreeRunMod` subclass needs `@dataclass`
  for its fields to be constructor arguments; `build_mm_device`'s `views` are keyed by the declared
  view name, not the prefixed one; a `MemoryMod(inline=False)` has no storage until `mem.alloc(n)`
  (its `Memory` is `mem._mem`); a stream slave reads with `get(nwords_max=n)`; the views' streams take
  the bus width, so every control word in a device is a bus word.
- **The software host's queue writes are at most 64 bits wide** (`mm_host.py:427` concatenates a 1-D
  header with `(n, 2)` 128-bit words). The system runs a 64-bit bus, as markov and mm_fir do; 128-bit
  engine ports are measured at engine level and modeled.
- **A `CreditIn` view's stream holds old values** (step 4.3): the view hands its newest value to the
  kernel only when the kernel's credit stream (depth 2) has room, so a kernel that drains the stream
  only now and then reads the two oldest values ever written and acts on a stale credit (the Ingest
  DMA dropped a symbol it had room for). Keep the credit in a register a separate process refreshes
  (the RTL's non-blocking read every cycle). Also: a register bank's config is
  `BoundMemSlaveAdaptor.stream_master(name)`, its status `.status(name).read()` (decoded); the host
  writes a credit with `m.write(words, layout["credit"].base)`.
- **Forked workers keep the parent's BLAS threads** (step 3.2): setting `OPENBLAS_NUM_THREADS` after
  `import numpy` does nothing, and a forked worker inherits the started pool (8 threads each here,
  seen with `threadpoolctl.threadpool_info()`). Set the variables, then spawn (mimo_cg's `_pool`).
- **A static memory plan needs dependencies to reuse space** (step 4.4): a plan safe under every
  dispatch order can share two buffers only when the graph orders them; independent symbols or CG
  jobs never share. Windows (`cg_window`, `rt_window`) are the edges that make reuse possible.
- **A drop at the end of a stream has no gap after it** (step 4.4): the scheduler learns of a dropped
  symbol from the next record's `seq`; the last ones need the DMA's status.
- **`HwSystem`'s participants are the simulation's root SimObjs** (step 4.5): anything outside the DUT
  needs a `bfm_model()`, an `xsi_model()` or to be a `SwHost`; a `Processor` the host charges is the
  host's (its `parent`). A `SwFunction`'s `cycles` is a number or a model with `predict_feat`, not a
  function; `SwQueue(owner, capacity=None)`; `MatmulOp` lives in `waveflow.linalg.systolic`.
- **A one-row-tile `A` is re-read for every column tile** (step 4.5): with `Mmax = S` the projection's
  `G'_stack` is read again for each of a chunk's `J` tiles, so `Z` jobs are bus-bound (28.9 µs at 64
  bits); a wide `Y` chunk costs `J` reads of `G'`.
- **Drops must not follow order edges** (step 4.5): a window edge carries no data; dropping through it
  dropped jobs whose chunks had arrived, their ring slots were never freed and the ring leaked.
- **Never stop processes by grepping their command line from a shell** (step 3.2): the grep matched
  the shell's own command and the kill took the shell's process group with it. Record each detached
  job's process group when it starts (`$!` after `setsid`) and stop exactly those.
- **Print validation alone, then score the test once** (step 4.6): a fit that prints both reads the
  test score before the model form is settled; a revision then needs a fresh registered test set.
- **A composite's `boundary` names pair with its unwired child endpoints in walk order** (step 5.1):
  children in `add_comp` order, then each child's ports in `add_endpoint` order
  (`composite_gen.derive_boundary`). pysim looks ports up by attribute and never notices a list in
  another order; the generated top silently wires the wrong streams. Check
  `[(n, e.name) for n, e in comp.boundary]` before csynth. A composite's top name is its
  `cpp_kernel_name`.
- **Do not carry state across firings of a free-running task in statics** (step 5.1, Vitis HLS 2024.1):
  the Ingest DMA's `seq`, then `taken`, came out of RTL one firing stale (Resps and ring slots a
  symbol late) while C simulation was right; an `empty()` polling loop before the first read made it
  worse (a hang). A body that never returns (`for (;;)` over symbols, the state loop-carried locals),
  as the Python twin's `run_iter`, passed XSI = pysim. Check C simulation (g++ with the Vitis HLS
  include directory) first, then a VCD (`add_system_steps(trace=True, trace_bus=True)`).
- **Scenario bursts must be built as `uint64`** (step 5.1): `np.concatenate([[kind], words])` promotes
  to float64 and rounds 64-bit words to 53 bits; only the C++ twin reads the scenario, so pysim
  never notices.
- **Never request more `burst_maxi` reads than the adapter can hold before reading** (step 5.1, Vitis
  HLS 2024.1): the systolic feeder requested all `2P` panels of a tile, then read; the `m_axi` adapter
  takes a request only while its queue (`USER_MAXREQS` 7) has room, a request leaving it only as its
  bursts fit the 256-word read buffer, which drains only as the kernel reads -- a deadlock at `P > 8`.
  C simulation's request queue is unbounded and pysim reads panel by panel, so only RTL shows it. Keep
  the requests ahead of the reads bounded (the feeder now runs 6 ahead, one more a panel). To find
  it: in the VCD, the task's `ARVALID` rises only with the adapter's `ARREADY`, so a task waiting on a
  full queue shows both low -- compare the requests taken with the words read.
