# Plan: the m_axi pointer-FIFO deadlock in free-running composites

**Status:** 2026-10-10 -- reproduced on the interleaver (6 of 12 jobs), fixed in codegen
(`offset=off` + `stable` on every pointer), gated (12-job interleaver XSI gate); the `scale_copy` blind
test runs clean without its amendment.  Stage 1 skipped; see the progress log.

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

**Stage 0 -- reproduced; the prediction was off by one, and the correction retrodicts both cases.**
csynth of `interleaver_inband` (n=256): `m_in_c` = `interleaver_inband_fifo_w64_d3_S` (depth 3),
`m_out_c` = `fifo_w64_d7_S` (depth 7).  The depths follow task position + 2 in every top seen
(`mem_copy` reader t1 -> 3, writer t2 -> 4; `scale_copy` t1 -> 3, t3 -> 5; `fir_block` 3 / 5).
Predicted before the run: 7 jobs, then a hang.  Measured (12 x n=64, XSI with a VCD): **6 jobs**, then
nothing for 19 000 cycles; at the end `m_in_c` empty, `m_out_c` 7/7; the reader had popped 13 times
(2 x 6 + job 6's `P` read), the writer 6.  The pop is **per firing**, at the firing's first state --
which waits for the task's first input word, so the writer had popped only j-1 when the reader needed
pop 2j.  The rule: *the pipeline wedges at the first reader firing whose pop count exceeds the writer's
pops so far plus the writer's FIFO depth.*  For the interleaver: 2j > (j-1) + 7 -> job 7 (1-indexed)
cannot start its `X` read: 6 done.  For `scale_copy` (depth 5, the reader's extra firing only on a
partial job, and `sc_scale` framing the writer's command *before* the source read, so the writer has
already popped j): the 6th partial job, job 11 -- as observed.  The interleaver's first five
completions came on the same cycles with and without the fix (222, 320, 418, 516, 614); the sixth was
already 26 cycles late (738 vs 712), the FIFO throttling before it wedged.  `measure_compute_spans`'s
default six-job sweep passed exactly at the wall.

**Stage 1 -- skipped.**  The interleaver reproduces in ~2 minutes of XSI, and the corrected rule was
checked against a second, independent design (`scale_copy`) rather than a knob in `mem_copy`'s
framework sequencer, which would have been test-only C++ in a framework body.

**Stage 2 -- the mechanism, and D2's expectation refuted.**  `entry_proc`'s only state blocks on
`~m_in_c_full_n | ~m_out_c_full_n`; `ap_start` is tied 1 (it free-runs); each task's `m_mem_read` is
high in its `state1`.  `stable` was *already* on `m_in` in every generated top, and `m_in_c` existed
anyway.  Experiments on copies of the interleaver: **E1** `stable` on both pointers, `offset=slave` --
the entry process and both FIFOs remain, hang at 6 jobs; **E2** `offset=off` + `stable` on both -- no
entry process, no pointer FIFOs, no `s_axi_control` slave; 12 of 12 jobs bit-exact, 98 cyc/job.
UG1399 (*HLS Task Library*): non-stream task arguments must be `stable`, and "top pointers with m_axi
interface can be passed only with the offset=off option".  `offset=slave` is outside what Vitis
supports for a task; it synthesizes, and this is what it builds.

**Stage 3 -- the fix.**  `composite_gen._maxi_port` emits `offset=off` + `stable` on every pointer
(read and write); `mm_writer_gen` likewise.  The control slave's pins are now derived from the pragmas
(`composite_gen.has_control_slave`) in both `render_ports_h` and `system_top.kernel_pins`, rather than
assumed for any `m_axi`.  The cost is the relocatable base: the bus address is the command's word
coordinate times the word size, from 0 -- which is what every flow already ran (the XSI testbenches and
the system top tied the control slave off).  Re-synthesized: `mem_r_stream`, `mem_w_stream`,
`mem_copy`, `fir_block`, `markov_chain`, `mm_credit_writer_64`, `mm_queue_writer_64_128` -- none has an
entry process or a control slave now.  **D4: `mem_r_stream` 158, `mem_w_stream` 176, `mem_copy` 2908,
`rf_pass_through` 1066 -- all unchanged.**  `test_system_top` (derived pins == csynth'd ports) passes.
**An intermittent silent misbuild, guarded, not explained.**  Twice in ~20 csynth runs Vitis lowered
the `m_axi` pointers to plain register ports (`HLS 214-450 Ignore address on register port 'm_mem'`,
`214-464 Skipping array undecay`, no `<top>_<bundle>_m_axi.v`, no AXI pins) while the pragma report
listed them as valid: `mm_queue_writer_64_128` (which then *failed* with HLS 214-208, its `ap_axis`
`s_in` no longer an interface port) and `interleaver_inband` in `examples/interleaver/` (which
*reported success*; XSI then died with `FATAL: port 'ap_rst_n' not found`).  Both were the first csynth
in a repo directory right after codegen had rewritten its sources; an unchanged re-run was correct both
times, and eight back-to-back scratch runs of the same top (`offset=off` with and without `stable`,
four parallel, four sequential) were all correct.  Both bad builds were `offset=off`, but so were ~18
good ones, and the pre-fix builds were too few to say `offset=slave` is immune.  Because the bad build
can report success, `composite_gen.check_maxi_lowered` now runs right after every csynth that stamps
(`render_rtl_f(stamp_sources=True)` and `system_dag.CsynthStep`): every `m_axi` bundle the top's source
declares must have its adapter in the RTL, or the build fails before the stamp vouches for it.
Regenerating also refreshed committed artifacts that were stale against their generators before this
change (the `-Isrc` csynth flag in the `.tcl`s, the PR #240 TLAST fix in four `examples/interleaver`
headers) -- committed separately.

**Exposure (the open questions).**  Only tops with two or more pointer-owning tasks can fall out of
step.  In the repo those are `interleaver_inband` (exposed: 2 reads / 1 write per job), `mem_copy` and
`fir_block` (balanced: one read, one write -- `fir_block` writes `len=0` when it has nothing to store).
`markov`'s tops and the RF examples have at most one pointer each.  `stable` changes nothing measurable;
`offset=off` changed no gate's cycle count.

**Stage 4 -- the gate.**  `tests/examples/test_interleaver_inband_xsi.py`: twelve n=64 jobs
(`XSI_GATE_SIZES`), every `Y` bit-exact, twelve completions, the last at an exact cycle (1300).  The RTL
is built by `interleaver_inband.build_xsi_gate_rtl()` into `examples/interleaver/`.  Passes;
`WANT_XSI_GATES` 162 -> 163.  The full `pytest -m xsi` before the gate was added: 162 passed, 0 skipped.

**Stage 5 -- docs.**  `freerunning_composite.md` gains "How a pointer reaches a task" (the mechanism,
the rule, the fix, the cost); the snippets in it and the three example codegen pages show `offset=off`;
`xsi_system.md`, `endpoint_kinds.md`, `axi_mm/master.md`, `axi_mm/modeling.md` say what a pointer lowers
to now.  The `freerun_pipeline` frame's "balance the firings" trap is replaced by "imbalance is fine;
`offset=slave` is not".  **The `scale_copy` blind test without its amendment** (on a copy: the
zero-length `MemWCmd` removed from `sc_scale_task.h` and its pysim twin, regenerated with the new top):
16/16 responses and the whole 3068-word output image bit-exact, last response at cycle **2163** against
the amended design's recorded 2229 -- the five balancing writer firings cost 66 cycles, and are no longer
needed.  (Its exact-cycle gate failed on that move, as it should; the blind-test directory itself was
not touched.)  The comments in `waveflow/build/mem_{r,w}_stream_task.h` (and `hwgen._reject_m_axi_task`, `freerunning_override.md`) no longer mention an `offset=slave` base; the copies were refreshed and every top hashing them re-synthesized -- `pytest -m xsi` 163 passed, 0 skipped.
