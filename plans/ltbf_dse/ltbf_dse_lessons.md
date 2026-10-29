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
