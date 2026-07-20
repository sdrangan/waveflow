# mem_copy timing calibration (VCD of internal streams)

**Status: probe DONE (step 1 gate met); attribution (step 2) is next.** Everything else about
`mem_copy` is done and gated (design, pysim, codegen, csynth, XSI gate 2908, docs).

## Probe outcome — the de-risk below is settled

The trace *is* producible, and more cheaply than the plan assumed. Answers to the three questions:

- **`$dumpvars` works under `xelab -dll`/XSI.** A second elaborated top (`vcd_dumper.v`, added to
  `xvlog` and passed as `xelab work.<top> work.vcd_dumper`) writes a real VCD. A pass-through wrapper
  is *not* needed, so the XSI top — and every BFM port number — is untouched. Both gates re-run green
  (2908 / 3469) with the dumper elaborated.
- **No `wdb → vcd` conversion needed**, and no BFM-side sampling.
- **`-debug typical` was already in `run.bat`**, so no elaboration change was required.

Two findings that changed the shape of the work:

1. **The internal FIFO nets live in the TOP scope.** Vitis lifts inter-task dataflow channel wires up
   beside the task instances, so `$dumpvars(1, <top>)` — level **1**, this scope only, no descent —
   captures `cmd_dout` / `cmd_empty_n` / `cmd_full_n`, `<producer>_cmd_din` / `_cmd_write`,
   `<consumer>_cmd_read`, *and* every task's `ap_done`. **No hierarchical path resolution is needed**
   for inter-task channels. Cost is bounded: 258 signals for mem_copy, 363 for interleaver_canon —
   not the thousands a level-0 dump would give.
2. **`extract_clock_times` sampled on the rising edge**, which reads the POST-edge value of anything
   the edge changed. This is not a clean one-cycle shift — it invents and destroys handshake
   coincidences. It read `gmem1 AW` as **16** accepted addresses instead of 128, and `W` as 2032
   instead of 2048. Fixed (`clock_sample_times`, mid clock-low); beats are still *labelled* by the
   true edge time. Any previously recorded cosim-derived number may move, and the new value is the
   correct one.

Also added for this arc: `extract_fifo_bursts` (the `write`/`full_n`, `read`/`empty_n` vocabulary),
`split_framed_word` (the `last` bit above the payload — there is no TLAST on an internal FIFO), and
`tlast` made genuinely optional (mem_copy's `s_cmd` boundary port has no TLAST wire at all).

### Measured, corrected, against the 16-job scenario

| channel                | packets | beats | per job                          |
| ---------------------- | ------- | ----- | -------------------------------- |
| `s_cmd` (boundary in)  | —       | 32    | 2 words                          |
| `cmd` (seq → r)        | 48      | 80    | 3 packets, 5 words               |
| `copy_data` (r → w)    | 48      | 2096  | 3 packets, **131** = 128 + 3     |
| `gmem0` AR / R         | 128     | 2048  | 8 bursts of 16, 128 words        |
| `gmem1` AW / W         | 128     | 2048  | 8 bursts of 16, 128 words        |
| `s_done` (boundary out)| —       | 16    | 1 `CopyResp`                     |

`s_done` cadence is a clean **183** cycles (178, 361, 544, …), matching the plan's steady-state
figure. The in-band descriptor beats of item 2 below are now *counted*: exactly 3 words/job on
`copy_data` ride ahead of each 128-word payload.

Reproduce: `examples/mem_copy/xsi/run_trace.bat mem_copy mem_copy_bfm_tb` (same for
`interleaver_canon`). The VCDs are gitignored — they are ~1–2 MB and regenerate per run.

## The gap being closed

Same 16-job scenario (128 words/job), measured both rungs:

|                         | pysim | RTL (XSI) | pysim / RTL |
| ----------------------- | ----- | --------- | ----------- |
| fill (first completion) | 156   | 163       | 96%         |
| steady-state period     | 140   | 183       | 77%         |
| total, 16 jobs          | 2256  | 2908      | 78%         |

Both decompose as `first + 15 × period`. **The fill is nearly right; the gap is almost entirely the
steady-state period** — 140 vs 183, i.e. ~43 cycles/job the RTL spends that the transaction-level model
does not account for. That is the number to explain, and "explain" means *attribute it to named
signals*, not fit a fudge factor.

## Why mem_copy is the right calibration vehicle

`mem_copy` **computes nothing** — it is pure data movement. Under the two-level calibration model
([[project-two-level-calibration]]: bus transfer is a *platform* property calibrated once; only kernel
*compute* is per-accelerator), mem_copy's entire cost is the bus-transfer level. So whatever is fit here
should be a **platform constant reusable by every other accelerator**, not a mem_copy-specific number.
If the fit turns out to need per-design tuning, that is itself a finding worth recording — it would
contradict the two-level premise.

## First de-risk — SETTLED, see "Probe outcome" above

*(Kept for the reasoning; the questions are answered.)*

**XSI currently writes a `.wdb`, not a VCD.** `waveflow/build/xsi/xsi_bfm.h`'s `XsiSim` takes a
`wdbFileName`. So step one is getting per-signal traces out of an XSI run at all:

- can `xsim`/`xelab` be told to dump VCD directly (trace flags at elaboration), or
- is a `wdb → vcd` conversion needed (Vivado ships converters), or
- does the BFM need to sample-and-dump the internal signals itself?

Settle this with a **small probe** on the existing `mem_copy` RTL before touching any framework code —
same discipline as the `ap_axis`-on-internal-FIFO probe that saved a rewrite ([[project-memcpy-inband-arc]]).
Do not design the calibration flow around a VCD you have not yet produced.

**What already exists** (do not rebuild): `waveflow/utils/vcd.py` (VCD parsing) and
`waveflow/utils/timing.py`. The pysim side already dumps VCD for the committed docs figures
(`examples/rowwise_fir/vcd/dump.vcd`, `examples/shared_mem/vcd/dump.vcd`; see
[[project-committed-figure-workflow]]). So parsing is solved — *producing* the RTL-side VCD is the
unknown.

## Signals worth tracing

The interesting ones are the two **internal framed FIFOs** and the two memory bundles:

- `cmd` (Sequencer → MemRStream) and `copy_data` (MemRStream → MemWStream) — `framed_word` FIFOs, so
  each carries `data` + `last`; handshake is the FIFO's full/empty + read/write enables.
- the `gmem0` (read) and `gmem1` (write) `m_axi` channels — AR/R and AW/W/B.

Per job, the decomposition to look for in the 183:

1. AXI **burst setup** latency (AR→first R, AW→first W) — the most likely bulk of the 43, and exactly
   the kind of thing a transaction-level model under-counts;
2. the **in-band descriptor beats** — `MemRCmd`(2w) + `MemWCmd`(2w) + `CopyResp`(1w) riding ahead of
   each 128-word data burst;
3. **backpressure stalls** on `copy_data` (writer not draining while it buffers/stores);
4. the writer's **serialization** — read `MemWCmd` → buffer response → write data → emit response.

Sanity anchors already measured: a single `mem_w_stream` write alone is **176** cycles (the `-m xsi`
gate), and `mem_r_stream` alone is **158**. So one job's write in isolation ≈176 while the pipelined
period is 183 — the reads hide almost entirely behind the writes, and only ~7 cycles/job of the period
is *not* the write. That reframes the question: **it may be that pysim under-models the WRITE itself
(140 vs ~176-183), not the overlap.** Check that first — it is a much narrower hypothesis than "the
pipeline is wrong", and the standalone 176 gives a direct, already-gated target to calibrate against.

## Suggested order

1. ~~Probe: produce a VCD (or equivalent per-signal trace) from an XSI run. Gate: a parseable trace
   with the FIFO + m_axi signals present.~~ **DONE** — see "Probe outcome".
2. Attribute: per-job timeline for one steady-state job — where do the 183 cycles go?
3. Compare: the same decomposition from the pysim timing model; find which term is under-counted.
4. Fit: adjust the bus-transfer parameters ([[reference-calib-statedict-artifacts]] — CalibModel's
   uniform `state_dict` + DAG-tracked artifact + seed is the pattern to reuse); re-check against the
   standalone 158/176 gates as well as mem_copy's 2908.
5. Record: whether the fitted numbers are platform constants (two-level premise holds) or not.

## Notes

- The `-m xsi` gate asserts **2908 exactly**. Calibration must not change RTL behaviour — if that number
  moves, something other than the model changed.
- Derive any cycle claim as `first + 15 × cadence` and confirm measured == derived before recording it;
  that discipline caught a bad estimate during the in-band arc.
- Related memories: [[project-two-level-calibration]], [[project-cycle-model-training]],
  [[reference-calib-statedict-artifacts]], [[project-memcpy-inband-arc]].
