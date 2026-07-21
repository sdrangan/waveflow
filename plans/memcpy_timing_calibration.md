# mem_copy timing calibration (VCD of internal streams)

**Status: LOOP CLOSED.** The whole arc is built into the framework and validated end to end on real
data: trace → measure → fit the writer's residual on uncontended firings → re-run pysim with the
bounded `copy_data` channel → the calibrated pysim period reproduces the RTL period **to 0.0%** at
both n_words=128 (183) and n_words=512 (615). The ~30 cycles/job of backpressure *emerge* from the
depth-2 FIFO; they are never fitted.

Shipped: measurement (#111/#112), the `TimingModel` engine (#113), the wiring
(FreeRunComp/MemWStream/DAG, #114), the sweep parameterization (#115), and the FIFO-depth
single-source change (this arc) — the last piece, because without a bounded `copy_data` the writer's
fitted delay is absorbed into idle-wait and the period does not move. Committed proof:
`tests/examples/test_mem_copy_calibration.py` (toolchain-free, using the measured RTL spans); the
full real-data sweep (`scratchpad/close_loop.py`) gave 0.0% at both points.

**The result in one line:** the 43-cycle/job gap is a fixed control cost + a per-AXI-burst term; with
it and the real FIFO depth in the model, backpressure *emerges* rather than being fitted, reproducing
the RTL across a 16× range of job sizes. See "The model".

**One caveat on the decomposition:** two collinear features (`nwords`, `num_trans = nwords/16`) fit
from two points are underdetermined — the automated fit landed on `~20 + 0.12·nwords`, the hand
analysis on `20 + 2·num_trans`; both reproduce the sweep points exactly but attribute the slope
differently. A third size (or fixing the burst coefficient from the measured AXI gap) would
disambiguate bus-term vs control-term. Prediction at the swept sizes is exact regardless.

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

### Verdict on those four (measured)

1. **REFUTED.** `AR → first R` and `AW → first W` are **1 cycle**, on every burst. The XSI BFM memory
   answers immediately, so there is no memory latency in this platform at all. A consequence worth
   carrying: **this setup cannot calibrate a memory system** — what it calibrates is the HLS `m_axi`
   *adapter*.
2. **CONFIRMED and counted**: exactly 3 words/job ride ahead of the payload on `copy_data`
   (131 = 128 + 3), and 5 words/job on `cmd`.
3. **CONFIRMED**: 30 cycles/job, and *emergent* — see the model below. It is a symptom of 4, not an
   independent term.
4. **CONFIRMED — the writer is the bottleneck**, but not for the reason the anchors suggested. The
   standalone-176 reasoning was misleading because it compared against a mis-drawn window (see
   "Measure from `ap_done`").

## Measure from `ap_done` — not from the last output beat

Three windows were drawn before the right one, and each told a *different* story about which stage
was the bottleneck:

| window end | MemWStream firing | conclusion it supported |
| ---------- | ----------------- | ----------------------- |
| `s_done` (last stream output) | 155 | "the reader is the bottleneck; the writer idles 28" |
| last `B` response | 180 | "~3 cycle restart latency" |
| **`ap_done`** | **183** | **the writer is 100% utilised; there is no idle** |

The cause is that an `m_axi` store is **posted**: it retires when the adapter accepts the word, not
when the beat is on the bus. Measured on job 8 — `s_done` at 1642, last `W` at 1666, last `B` at
1667, `ap_done` at 1670, next firing at 1671. So the writer works for 24 cycles *after* its final
stream output. There is no loop reordering: zero `s_done` beats occur before `S2A`'s last input, and
`ECHO` starts +5 cycles after it.

`ap_done` has none of that ambiguity — HLS holds it until the firing's outstanding writes have
responded. **A free-running top has no control interface, but each `hls::task` instance inside it is
an ordinary `ap_ctrl_hs` block with `ap_start`/`ap_continue` tied to `1'b1`** (see the `assign
..._ap_start = 1'b1;` lines in the generated top), so `ap_done` still pulses once per firing, and
Vitis lifts the pin into the top scope. `BoundTrace.component_firings()` anchors on it.

**Design note, separate from timing:** the task header states the response "must not go out before
the data is stored". Program order into the adapter does not give that — `CopyResp` fires ~25 cycles
before the write lands. Harmless for this TB (it checks memory at end of sim) but a consumer acting
on `CopyResp` by reading the destination would race the write tail. If the response must mean
durable, it has to ride the task boundary, not a stream write inside it.

## The model (step 2 + 3 + 4: DONE)

Swept `n_words` ∈ {32, 64, 128, 256, 512} (16× range, job counts 16/16/16/8/4 to stay inside the
generated TB's `h.run(3400)` and 24640-word arena). MemWStream's firing span, measured:

| n | 32 | 64 | 128 | 256 | 512 |
| --- | --- | --- | --- | --- | --- |
| span | 75 | 111 | 183 | 327 | 615 |
| AXI bursts | 2 | 4 | 8 | 16 | 32 |
| dead cycles between W bursts | 2 | 6 | 14 | 30 | 62 |

Exactly linear at **1.125 cycles/word, intercept 39**, at every point. It decomposes with no
residual:

```
span = 41  +  n  +  2 × (ceil(n/16) − 1)
       ↑      ↑     ↑
   fixed   one per  per burst boundary
  control   word    (HLS max_burst_length = 16)
```

`gaps = bursts − 1`, each costing exactly **2** cycles — read straight off the AXI VCD, not fitted.

pysim's own periods are `19 + 1.0·n` (also exactly linear), so the missing term is:

```
delta(n) = 20 + 2 × ceil(n/16)          # 20 fixed + 2 per AXI burst
```

### Where each term belongs

| term | home | why |
| ---- | ---- | --- |
| `nwords + 2·(bursts−1)` | **`BusTiming` on the `MMIFSlave`** (`memif.py`) | AW/AR turnaround at each 16-beat boundary — a property of the `m_axi` adapter that *any* component streaming through it pays. Its feature row is already `{num_trans, nwords}`, exactly this law's shape, and its docstring already says these rates "live here, on the slave — not hand-rolled on the component". |
| **41 cycles fixed** | **MemWStream** (component) | descriptor read, relay buffering, first AW issue, drain-to-`ap_done`. Genuinely per-accelerator. |

**This is why pysim showed 1.0 cycles/word:** `BusTiming.read`/`.write` default to `None`, and
`None` means *no model* — the slice falls back to the plain `word_bw` span. The burst term was never
wrong in pysim; it was **absent**, because no `BusTiming` was configured and `num_trans` is not
passed at the slice call.

Design point for the implementation: the component should pass `num_trans` meaning **logical**
transactions (mem_copy: 1 contiguous transfer) and let `BusTiming` split by its own
`max_burst_len`. Baking `ceil(n/16)` into the component would put a bus property in every m_axi
component and break them all on a pragma change.

### Validation (the part that makes it a model, not a fit)

Calibrated **only on uncontended firings** — MemRStream's job 0 (the one backpressure-free firing,
153) and MemWStream's uniform span. The queue stays bounded at its real depth of 2, so the ~30
cycles of backpressure must still *emerge*:

| n | RTL | flat `+36` | err | **two-term** | err |
| --- | --- | --- | --- | --- | --- |
| 32 | 75.0 | 87.0 | +12.0 | **75.0** | −0.0 |
| 64 | 111.0 | 119.0 | +8.0 | **111.0** | +0.0 |
| 128 | 183.7 | 183.0 | −0.7 | **183.0** | −0.7 |
| 256 | 327.3 | 311.0 | −16.3 | **327.0** | −0.3 |
| 512 | 617.0 | 567.0 | −50.0 | **615.0** | −2.0 |

Worst error **27.3% → 1.1%**. The single-point calibration (`+36`, fitted at n=128) looks perfect at
its calibration point and fails badly elsewhere — that failure is what exposed the slope term. A
minimum of **two sizes** is therefore required to calibrate at all.

Also required and previously missing: **`queue_size` on the internal edges.** `mem_copy.py` creates
both `StreamIF`s without it, and `QueuedTransferIFSlave.queue_size=None` means *unbounded* — so no
coupling loss could emerge. The defaults disagree across backends: `None` is depth **2** in codegen
(`FramedEdge.depth` → HLS default, measured as `fifo_w65_d2`) and **infinity** in pysim. Those are
the same physical thing and should come from one declaration.

## Suggested order

1. ~~Probe: produce a VCD from an XSI run.~~ **DONE** — see "Probe outcome".
2. ~~Attribute: where do the 183 cycles go?~~ **DONE** — see "The model".
3. ~~Compare against pysim; find the under-counted term.~~ **DONE** — `BusTiming` absent, plus a
   per-component fixed cost.
4. ~~Fit.~~ **DONE by hand.** Next: implement it for real — pass `num_trans` at the slice calls,
   configure `BusTiming` on the `MMIFSlave`, wire `queue_size` from the same declaration codegen
   uses for FIFO depth, and add the per-component fixed latency. Then **automate** the fit
   ([[reference-calib-statedict-artifacts]] — `CalibModel`'s `state_dict` + DAG-tracked artifact +
   seed): sweep ≥2 sizes, regress `{nwords, num_trans}` into `BusTiming`, leave the residual as
   component latency.
5. Record: **the two-level premise holds so far.** The burst term is a platform property of the
   `m_axi` adapter; only the 41-cycle control cost is per-accelerator. Caveat: calibrated against an
   idealised BFM memory (1-cycle AR→R), so it is an *adapter* constant, not a memory-system one.
6. Re-check against the standalone 158/176 gates, which this work has not yet revisited.

## Notes

- **Re-running the `.exe` does NOT regenerate the VCD** — only the full `run.bat` path (which
  re-elaborates) does. A sweep that re-ran the binary per point silently re-measured the *previous*
  trace and produced a period of 183.0 at every `n_words`, which is impossible and was the only
  reason it got caught. Always delete the VCD first and assert it reappears.
- The `-m xsi` gate asserts **2908 exactly**. Calibration must not change RTL behaviour — if that number
  moves, something other than the model changed.
- Derive any cycle claim as `first + 15 × cadence` and confirm measured == derived before recording it;
  that discipline caught a bad estimate during the in-band arc.
- Related memories: [[project-two-level-calibration]], [[project-cycle-model-training]],
  [[reference-calib-statedict-artifacts]], [[project-memcpy-inband-arc]].
