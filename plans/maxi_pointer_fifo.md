# Plan: the m_axi pointer-FIFO deadlock in free-running composites

**Status:** drafted 2026-10-10.  Nothing is fixed yet; the mechanism is confirmed in generated RTL,
and whether a reference example is exposed is not yet known.

## What was found

A blind test of the `freerun_pipeline` frame (`scale_copy`: read a vector, scale it, write it) hung
at RTL on job 11 of 16, after jobs 0–10 came out bit-exact.  pysim passed.  The agent's diagnosis
(`waveflow_blind_tests/frames_scale_copy/design.md`, "Amendment made in Stage 2"):

- In a free-running composite top, each task with an `m_axi` port gets the port's base pointer
  through a **small FIFO of its own**, refilled by one generated **entry process** that writes
  *all* of these FIFOs together, one round at a time.
- A task pops its FIFO **each time it fires**.
- `scale_copy` fired the reader twice for a job whose length is not a multiple of four (an extra
  one-word read of the destination's last word, because the writer has no byte strobes) and the
  writer once.  The reader's FIFO drained faster than the writer's.  The entry process blocked on
  the writer's full FIFO, so it stopped refilling the reader's, and the pipeline stopped.
- Its fix: a zero-length `MemWCmd` so the writer also fires twice.  Firings balanced, all 16 jobs
  ran.

**Confirmed in the RTL** (`frames_scale_copy/scale_copy_proj/solution1/syn/verilog/`):
`scale_copy_entry_proc.v` writes `m_in` into FIFO `m_in_c` (`scale_copy_fifo_w64_d3_S`, depth 3) and
`m_out` into `m_out_c` (depth 5); the reader task's `m_mem_read` pops `m_in_c`.

**Not yet confirmed:** that the pop is per firing (rather than, say, per memory command), the exact
job count at which a given imbalance hangs, and whether other generated tops share the structure.

## Why it matters

- **Invisible before RTL.**  pysim and csim do not model these FIFOs; they are an artifact of how
  Vitis passes pointer arguments into tasks.
- **Data-dependent.**  It needs enough jobs with the imbalance to fill a FIFO, so short tests pass.
- **A hang on hardware**, not an error.
- **A reference example may be exposed.**  The interleaver (`examples/interleaver/interleaver_inband.py`)
  fires its `MemRStream` twice per job (`P`, then `X`) and its `MemWStream` once.  Its RTL tests run
  one job (`write_xsi_bundles(sizes=(256,))`, the XSI gate) and two
  (`tests/examples/test_interleaver_inband.py`), possibly too few to fill the FIFO.  It is the
  `freerun_pipeline` frame's reference for compute stages.

## Decisions

- **D1 -- reproduce before fixing.**  A fix is judged against a hang that is shown, at a predicted
  job, not against a diagnosis.
- **D2 -- fix it in the generated top, not in each design.**  Asking every design to balance its
  firings by hand is a trap that will be stepped in again.  The likely fix is
  `#pragma HLS stable variable=<ptr>` on each `m_axi` pointer argument of the generated top, which
  tells Vitis the argument does not change while the top runs, so it need not be re-synchronized
  per firing.  This is an expectation to verify in RTL, not a known fact.
- **D3 -- if the structure cannot be removed, detect it.**  pysim knows how many times each memory
  task fires per job; a check can flag unequal counts before RTL (the `check(subject, target)`
  family, `project-codegen-check-family`).
- **D4 -- the cycle gates must not move unexplained.**  `mem_r_stream` 158, `mem_w_stream` 176,
  `mem_copy` 2908.  A fix that changes them is explained in the commit, not re-baselined silently.

## Stages

**Stage 0 -- reproduce on the interleaver.**  Build `interleaver_inband`'s RTL and run XSI with a
longer scenario: twelve jobs of mixed sizes (`write_xsi_bundles(sizes=(64,) * 12)`, or similar).
Predict from the FIFO depths in its generated RTL at which job it should hang if the diagnosis is
right.  Gate: either a hang at the predicted job, or a clean run that disproves exposure (then say
why: different depths, a pop per invocation rather than per firing, ...).  Record the generated
FIFO names and depths.

**Stage 1 -- a minimal reproducer.**  `mem_copy` with an optional extra reader firing per job (a
test-only knob), so the deadlock is reproducible in seconds of XSI with no compute in the way.
Gate: it hangs at the predicted job; with the knob off, 2908 cycles unchanged.

**Stage 2 -- understand the mechanism.**  Read the generated `entry_proc` and task RTL: what
pops each FIFO, and when.  Check the Vitis HLS user guide on pointer and scalar arguments of
`hls::task` / dataflow tops (`stable`, `ap_ctrl_none`, `offset=direct` vs `offset=slave`).
Write the finding down before changing code.

**Stage 3 -- the fix in codegen.**  In `waveflow/build/composite_gen.py` (`render_top`), emit the
fix for every `m_axi` pointer argument.  Regenerate and re-synthesize every free-running top.
Gates: the Stage 0 and Stage 1 reproducers run clean; every XSI gate (`pytest -m xsi`, 158 and its
companions) passes, cycle counts unchanged or the change explained; the system flow
(`markov`, `mm_fir`) unchanged.

**Stage 4 -- guard against regression.**  A multi-job interleaver XSI gate with an imbalance
built in (the Stage 0 scenario), so the trap stays covered.  If Stage 3 could not remove the
structure, the pysim firing-count check of D3 instead, wired into the free-running build.

**Stage 5 -- docs and the frame.**  A section in `docs/guide/comp_codegen/freerunning_composite.md`:
how pointers reach tasks, the deadlock, and the fix.  Update the `freerun_pipeline` frame's trap
(`waveflow/mcp/frames/freerun_pipeline/process.md`): remove it if fixed, point to the page if not.
Re-run the `scale_copy` blind test without the amendment to confirm.

## Open questions

- Is the bus-system flow exposed?  `markov`'s chain writes memory through a `MemWStream` inside a
  composite; check its generated top for the same entry-process structure.
- Do the RF examples with memory tasks (`rf_shot_*`, `rf_samp_buf_*`) fire their memory tasks
  unequally?
- Does `stable` change the II or the cycle counts of the existing tops?

## Progress log

(empty)
