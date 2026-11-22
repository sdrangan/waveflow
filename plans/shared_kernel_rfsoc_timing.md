# The shared-kernel blocks on the RFSoC part: a timing probe

**Status: DONE 2026-11-21, on the lab machine, Vitis HLS / Vivado 2025.1.** It
answers the item `plans/shared_kernel.md` leaves open in its Stage 2 record: "the 7.82 ns case is open ...
the real target (250 MHz, xczu48dr) has not been tried". That plan is not edited here (another agent owns
it); its Stage 2 record should link this page when it is next touched. Three parts: the stream blocks and
the ring stub (below); `SsrFft` with the direction chosen per frame against its fixed builds; and
(2026-11-22) the stream blocks' resource records and the arbiter's cost per frame. That last part found an
RTL hang in the arbiter (from 3 inputs alone, from 4 in the pair) and fixed its body. The first part's 3-input pair builds the same RTL
with the fixed body (re-probed), so its numbers stand.

## The question

Stage 2's gates synthesize on the generator's default part, xc7z020-clg484-1 at 100 MHz
(`composite_gen.DEFAULT_PART`). There the pair `StreamArbiter -> StreamDemux` (priority, 2-burst frames)
estimated **7.82 ns against a 7.3 ns budget** (csynth, Vitis HLS 2025.1). The RF designs these blocks are
for run at 250 MHz on the RFSoC 4x2's xczu48dr-ffvg1517-2-e (`RFSOC4X2_PART`, `RFSOC4X2_PERIOD_NS = 4`).
Do the pair, the demux and the ring stub meet 4 ns there?

## The answer

**Yes, all of them, after place and route, with at least +0.888 ns of slack (Vivado 2025.1).** No body was
rewritten. csynth's estimate is within its budget for every top (Vitis HLS 2025.1). Its budget is 2.92 ns:
4 ns less the default clock uncertainty of 27%, 1.08 ns. The ring stub is exactly on the budget, at
2.920 ns. Every loop keeps II = 1 (Vitis HLS 2025.1).

The near-budget chains csynth reports are not the routed critical paths (Vivado 2025.1). Each routed
worst path ends on a top-level AXIS register slice and is 80-93% routing.

At 4 ns the stream blocks keep their 100 MHz schedule: every module's FSM states, and every loop's II and
depth (Vitis HLS 2025.1). **The ring stub's mux does not.** Its credit read moves into a state of its own,
so an idle poll costs 3 states instead of 2. The ring stub's pinned 934 / 6282 cycles are 100 MHz numbers,
and moving it to the RFSoC would re-record them.

## How it was measured

`waveflow/streams/rfsoc_probe.py`:

```bash
python -m waveflow.streams.rfsoc_probe work/rfsoc_probe                 # generate and csynth the 7 tops
python -m waveflow.streams.rfsoc_probe work/rfsoc_probe --steps impl    # place and route them
python -m waveflow.streams.rfsoc_probe work/rfsoc_probe --steps         # re-read; no tool
```

- **The tops are the gates' tops** (`waveflow.streams.rtl.make`, `examples/ring_stub`'s `RingStub`), with
  the same bodies. Only the TCL differs: `render_tcl(..., part=RFSOC4X2_PART, period_ns=4)`. Each probe
  builds in its own directory, `work/rfsoc_probe/<name>/`, which is gitignored.
- **The gated builds are unchanged.** `DEFAULT_PART` and every pinned cycle count stay as they are.
  `rtl.generate_for` and `ring_stub_build.gen_top` gained keyword-only `part` / `period_ns` arguments,
  whose defaults are the old values, so the default TCL is byte for byte the same (checked). Re-run on the
  default part with Vitis / Vivado 2025.1: `pytest tests/streams/test_arbiter_demux.py -m xsi`, 4 pass;
  `pytest tests/examples/test_ring_stub.py -m xsi`, 6 pass, the pinned 934 / 6282 cycles included (after
  `ring_stub_build --through csynth`, `inc_csynth` and `post_csynth`: those gates only check a csynth). The
  non-XSI tests of both files pass (33), and so do the probe's own, `tests/streams/test_rfsoc_probe.py`.
- **csynth** reads the estimated clock from `csynth.xml`, each module's loops (achieved II, depth) and
  resources from `<module>_csynth.xml`, and each module's critical path from
  `.autopilot/db/<module>.verbose.sched.rpt`. That report's *Timing violations* section lists every FSM
  state's chain, violating or not.
- **Post-route** uses `export_design -flow impl`: Vivado out-of-context synthesis, placement and routing of
  the csynth RTL at 4 ns, with no I/O constraints. It is the flow of `examples/mimo_cg/hw/impl_check.py`.
  `rtl_ooc.py` and `system_ooc.py` stop at synthesis (`synth_design`; `link_design` + `opt_design`), so
  they cannot give routed timing. The probe reads the export report and
  `*_timing_summary_routed.rpt`. Each run took 7-9 minutes, four at a time.

## csynth on the RFSoC part (Vitis HLS 2025.1, xczu48dr-ffvg1517-2-e, 4 ns: budget 2.92 ns)

All 32-bit, except the ring stub (64-bit). The 100 MHz column is the same top's estimate on
xc7z020-clg484-1 at 10 ns (budget 7.3 ns, Vitis HLS 2025.1), from this machine's gate builds. Those
reproduce the Stage 2 record's four numbers exactly. The ring stub's 7.278 ns is new: the Stage 1 record
gives no estimate.

| probe | top | est. 4 ns, Vitis 2025.1 | est. 100 MHz, Vitis 2025.1 | slowest state, Vitis 2025.1 | loops, Vitis 2025.1 | FF / LUT, Vitis 2025.1 |
|---|---|---|---|---|---|---|
| `pair_p_f1` | pair, 3 inputs, priority, 1-burst | 2.655 ns | -- | demux S1, 2.655 ns | all II 1 | 467 / 830 |
| `pair_p_f2` | pair, 3 inputs, priority, 2-burst | 2.655 ns | 7.819 ns | demux S1, 2.655 ns | all II 1 | 581 / 989 |
| `pair_rr_f1` | pair, 3 inputs, round-robin, 1-burst | 2.787 ns | 7.258 ns | arbiter S2, 2.787 ns | all II 1 | 494 / 990 |
| `pair_rr_f2` | pair, 3 inputs, round-robin, 2-burst | 2.787 ns | -- | arbiter S2, 2.787 ns | all II 1 | 608 / 1149 |
| `demux_sel` | demux by `s_sel`, 3 outputs, 2-burst | 1.822 ns | 5.734 ns | demux S1, 1.822 ns | all II 1 | 253 / 491 |
| `demux_hdr` | demux by header bits 16-23, 3 outputs, 1-burst | 0.854 ns | 3.176 ns | `copy_frame` S3, 0.854 ns | all II 1 | 261 / 423 |
| `ring_stub` | the composite (mux, K, post; `fb`, `crd` backward) | **2.920 ns** | 7.278 ns | mux S2, 2.920 ns | all II 1 | 1171 / 1674 |

No DSP anywhere. BRAM_18K is 0 everywhere except the ring stub, which has 14 (Vitis HLS 2025.1): `fb`,
`k_in` and `k_out`, 4 each, and Post's buffer, 2. These are the Stage 1 record's 14 / 1172 / 1764 at
100 MHz, less one FF and 90 LUTs.

### The chains (`.verbose.sched.rpt`, Vitis HLS 2025.1)

| block | state | the chain at 4 ns, Vitis 2025.1 | at 100 MHz, Vitis 2025.1 |
|---|---|---|---|
| demux in the pair | S1 | `s_sel` FIFO read 1.460 -> `idx >= N` compare 1.016 -> select `k` 0.179 = 2.655 ns | the same three, 3.634 + 2.552 + 0.993 = 7.179 ns (1-burst) |
| arbiter, priority | S1 | `s_in_0` empty test 0.497 -> `ready == 0` 0.673 -> `m_sel` FIFO write 1.460 = 2.630 ns | the same, 6.574 ns |
| arbiter, round-robin | S2 | the `next_k` update: select 0.384 -> sparsemux `win` 0.427 -> add 0.781 -> compare 0.781 -> select 0.414 = 2.787 ns | the search in S1: `next_k + j` add 1.780 -> compare 1.825 -> two selects 1.188 + 1.188, plus 1.277 of control = 7.258 ns |
| demux `copy_frame`, 2-burst | S2 | the call into the `COPY` pipeline, 2.493 ns | 7.819 ns, the pair's estimate (the burst count, Stage 2 record) |
| ring mux | S2 | `fb` FIFO read 1.460 -> `tag` FIFO write 1.460 = 2.920 ns | the same in S1, with the credit read: 3.544 + 3.613 = 7.156 ns |
| ring mux `COPY` | S2 | `fb` FIFO read 1.460 -> `k_in` FIFO write 1.460 = 2.920 ns | the same, 3.544 + 3.561 = 7.105 ns |
| ring post `READ` / `FB` | S2 | `k_out` FIFO read 1.460 -> buffer store; buffer load 1.237 -> `fb` FIFO write 1.460 = 2.697 ns | 6.815 / 6.798 ns |
| ring post | S1 | `tag` FIFO read 1.460 -> the call into `READ` 1.280 = 2.740 ns | the same, 3.613 + 3.665 = 7.278 ns, the composite's estimate |

Vitis HLS 2025.1 charges 1.460 ns for each read or write of an internal FIFO on this part. Every
near-budget chain holds one, and the ring stub's two 2.920 ns states are just a FIFO read feeding a FIFO
write. The Stage 2 record called the 100 MHz copy loop "the critical path"; at 4 ns it is the demux's index
test (priority) and the arbiter's `next_k` update (round-robin).

### The schedule: unchanged for the stream blocks, not for the ring mux

**The stream blocks.** For each of the four gated stream tops, every module at 4 ns has **the same number of
FSM states, and each loop the same II and pipeline depth**, as at 100 MHz. The comparison is against this
machine's `tests/streams` gate builds (Vitis HLS 2025.1). Resources differ by at most 10 LUTs. Operations
do move between states: the round-robin arbiter's worst chain sits in S1 at 100 MHz and in S2 at 4 ns, in
the same four states.

**The ring stub's mux changes** (against `examples/ring_stub`'s own 100 MHz build, Vitis HLS 2025.1).
Every other module of the composite keeps its states, II and depth: `ring_inc_task`, `ring_post_task`
(10 states), and every loop. The mux goes from 6 states to 5, because the credit read moves:

| | 100 MHz, Vitis 2025.1 | 4 ns, Vitis 2025.1 |
|---|---|---|
| S1 | the `crd` test and read, the `fb` test, the `fb` header read, the `tag` write | the `crd` test and read alone |
| a firing that moves nothing | S1 -> S4: 2 states | S1 -> S2 -> S4: 3 states |
| a feedback frame | S1 -> S5 -> S6 -> S4, then the copy | S1 -> S2 -> S5 -> S4, then the copy |
| a new job | S1 -> S2 -> S3 -> S4, then the copy | S1 -> S2 -> S3 -> S4, then the copy |

So an idle poll of the mux costs one cycle more at 4 ns, and the credit is absorbed a cycle before the
feedback test rather than in the same cycle. **`test_rtl_loop_timing_is_exact`'s 934 and 6282 cycles
belong to the 100 MHz build.** Moving the ring stub to the RFSoC would very likely change them, and they
would have to be re-recorded. The stream blocks' RTL cycle counts at 4 ns are likely the same as at
100 MHz, given the same schedule. Neither was checked: no XSI run was made at the RFSoC target.

## Post-route (Vivado 2025.1, out of context, xczu48dr-ffvg1517-2-e at 4 ns)

| probe | CP post-synth, Vivado 2025.1 | CP routed, Vivado 2025.1 | WNS / WHS, Vivado 2025.1 | LUT / FF / SRL, Vivado 2025.1 | worst routed path, Vivado 2025.1 |
|---|---|---|---|---|---|
| `pair_p_f1` | 1.395 ns | 2.609 ns | +1.391 / +0.042 ns | 519 / 766 / 19 | demux FSM -> `m_out_1` reg slice D; 4 LUTs, 80% route |
| `pair_p_f2` | 1.386 ns | 2.613 ns | +1.387 / +0.042 ns | 537 / 883 / 19 | demux `COPY` pipeline enable -> `m_out_1` reg slice CE; 3 LUTs, 85% route |
| `pair_rr_f1` | 1.400 ns | **3.112 ns** | **+0.888** / +0.041 ns | 555 / 787 / 22 | demux FSM -> `m_out_0` reg slice CE; 3 LUTs, 88% route |
| `pair_rr_f2` | 1.418 ns | 2.109 ns | +1.891 / +0.042 ns | 569 / 904 / 22 | arbiter `s_in_1` reg slice ack -> its data CE; 1 LUT, 93% route |
| `demux_sel` | 1.421 ns | 2.474 ns | +1.526 / +0.043 ns | 399 / 669 / 0 | `s_in` reg slice state -> `x0` data CE; 1 LUT, 92% route |
| `demux_hdr` | 1.385 ns | 2.792 ns | +1.208 / +0.039 ns | 344 / 588 / 0 | `m_out_1` reg slice ack -> `m_out_2` reg slice CE; 3 LUTs, 88% route |
| `ring_stub` | 1.718 ns | 2.425 ns | +1.575 / +0.037 ns | 1188 / 1119 / 24 | mux `credit` reg -> `s_in` reg slice CE; 5 LUTs, 70% route |

Every top: timing met, no failing endpoint, setup or hold. No DSP. The ring stub implements in **one
RAMB36**, `fb` (260 x 65 bits; the export report counts it as "BRAM 2", in 18K units). `k_in` and `k_out`
(64 x 65) and Post's buffer go to distributed RAM: 76 + 76 + 64 = 216 LUTRAMs (hierarchical utilization,
Vivado 2025.1). csynth's estimate of 14 BRAM_18K is 12 more than the 2 Vivado uses.

What the post-route numbers say about the csynth ones (Vivado / Vitis HLS 2025.1):

- **csynth's chains are not the routed critical paths.** The ring mux's FIFO-to-FIFO state, exactly
  2.920 ns in csynth, is not the routed worst path. The ring stub's worst path is 2.425 ns, in the mux, but
  from the credit register to the input register slice's clock enable. The round-robin `next_k` chain
  (2.787 ns in csynth) is not the worst path of either round-robin pair.
- **The worst paths are at the top's boundary.** Each ends on the CE or D pins of a `regslice_both_*`,
  the register slice Vitis puts on each AXIS port, and is routing-dominated. Out of context, with no I/O
  constraints, the ports are unplaced. Inside a larger top the same block's ports are internal FIFOs, not
  these slices, so these paths measure the top's port logic as much as the block's own.
- **The spread between equal designs is about 1 ns.** `pair_rr_f1` and `pair_rr_f2` differ only in the
  burst count, and route at 3.112 and 2.109 ns. The worst of the seven is a placement outcome, not a
  logic depth.
- **The demux by header field is csynth's fastest (0.854 ns) and routes at 2.792 ns**, slower than four of
  the others. A csynth estimate well under budget does not rank the routed result.

## No rewrite

The pair meets 4 ns, so nothing was rewritten for timing. (A later part, *Sharing against replicating*,
adds `#pragma HLS INLINE` to the arbiter's `copy_frame` to fix an RTL hang; the loop keeps its shape.) The
bodies' "keep this loop's shape" comments stand, and
Stage 2's two faster loop rewrites, which hung at RTL, were not retried. On this part, the pair's 100 MHz
estimate (7.82 ns over a 7.3 ns budget) does not carry over. The Stage 2 record's "open" item can close
with this page: the blocks meet the RF clock on the RF part.

## csynth's warnings at 4 ns (Vitis HLS 2025.1)

They are the same as at 100 MHz: those in the Stage 1 record for the ring stub, and, for the round-robin
arbiter, the two in this machine's gate builds:

- ring stub: `HLS 200-657` (channels `fb` and `crd` flow backwards); `HLS 200-656` (`ring_inc_task`, a
  flushable pipeline in an `ap_ctrl_none` region); `HLS 200-1020` (`start_for_ring_post_task` raised to
  depth 3); `RTGEN 206-101` (`credit` is power-on initialization).
- round-robin arbiter: `RTGEN 206-101` (`next_k` is power-on initialization) and `SYNCHK 200-23` at
  `stream_arbiter_task.h:97`, the `ready[k]` with a variable `k` ("may cause suboptimal QoR"). Both
  round-robin pairs meet 4 ns with it after routing.

## Not done

- **No XSI at the RFSoC target.** The 4 ns RTL is synthesized and routed, not simulated. For the stream
  blocks the unchanged schedule makes the same cycle counts likely; for the ring stub the changed mux makes
  different ones likely. Neither is measured. Moving a gate to the RFSoC means re-recording its pinned
  counts. That is a separate decision; this probe changes no gate.
- Only what the gates build: 32-bit words, 3 clients. Wider words and 16 ports are in the last part,
  *Sharing against replicating*.
- Out of context: no I/O timing, and no placement against neighbours. In a full RF design the ports join
  other logic, and the boundary paths above change.
- The ring stub's K is the stub `Inc`, not `SsrFft`. `SsrFft` on this part, the direction per frame
  against the fixed builds, is the next section.

## `SsrFft` with the direction chosen per frame (Vitis HLS / Vivado 2025.1, xczu48dr at 4 ns)

Asked by the planning session (2026-11-21): what does `SsrFft(direction="runtime")` (`plans/shared_kernel.md`
Stage 3) cost in timing and resources against the fixed builds, forward and `inverse=True`? Measured at
L = 64, the one length Stage 3 gated, and at L = 1024. All six use the ping-pong reorder, the `RadixWord`
boundary, `ap_fixed<16, 2>` input and `<18, 2>` twiddles, as in Stage 3's table.

**The answer: no timing cost, and about 10% more FF and LUT in csynth (Vitis HLS 2025.1), 2-6% routed
(Vivado 2025.1). No DSP, no BRAM.** Every build meets 4 ns after place and route. The worst slack is
+0.399 ns, on the *fixed* inverse at L = 1024, a placement outcome. The runtime builds have +0.962 ns
(L = 64) and +0.886 ns (L = 1024) (Vivado 2025.1). csynth's estimate is the fixed builds' at both
lengths, and every loop is II = 1 (Vitis HLS 2025.1). The cost in time is latency: one pipeline stage
more in the input register and in one or two butterfly stages.

**How.** The probe gained six tops (`fft64_fwd`, `fft64_inv`, `fft64_rt`, `fft1024_*`), built by
`waveflow.dsp.ssr_fft.rtl.generate` with `reorder="pingpong"`. That function already takes keyword
`part` / `period_ns`, and its defaults are already `RFSOC4X2_PART` at 4 ns, so `waveflow/dsp/ssr_fft/` is
unchanged. At L = 64 the probe's TCL and generated top are byte for byte those of
`tests/dsp/ssr_fft/test_xsi.py` (checked, and kept by `tests/streams/test_rfsoc_probe.py`). The probe's
csynth equals that gate's own build on this machine, number for number (Vitis HLS 2025.1). Re-run:
`pytest tests/dsp/ssr_fft -m "not vitis and not xsi"`, 133 pass; `pytest tests/dsp/ssr_fft/test_xsi.py
-m xsi`, 12 pass. Those 12 cover every frame bit-exact, the interval 16 and csynth slack >= 0, for the
forward, SOB, inverse and runtime builds.

### csynth (Vitis HLS 2025.1, xczu48dr-ffvg1517-2-e, 4 ns: budget 2.92 ns)

| build | estimate, Vitis 2025.1 | slowest state, Vitis 2025.1 | DSP, Vitis 2025.1 | BRAM_18K, Vitis 2025.1 | FF, Vitis 2025.1 | LUT, Vitis 2025.1 |
|---|---|---|---|---|---|---|
| L = 64, forward | 2.855 ns | `st2` S3, 2.855 ns | 24 | 6 | 8,306 | 10,396 |
| L = 64, inverse | 2.855 ns | `inv_st2` S3, 2.855 ns | 24 | 6 | 8,303 | 10,396 |
| L = 64, runtime | 2.855 ns | `rt_st2` S3, 2.855 ns | 24 | 6 | 9,168 (+10.4%) | 11,531 (+10.9%) |
| L = 1024, forward | 2.901 ns | `st3` S3, 2.901 ns | 48 | 50 | 24,713 | 21,400 |
| L = 1024, inverse | 2.901 ns | `inv_st3` S3, 2.901 ns | 48 | 50 | 24,704 | 21,400 |
| L = 1024, runtime | 2.901 ns | `rt_st3` S4, 2.901 ns | 48 | 50 | 26,236 (+6.2%) | 23,513 (+9.9%) |

Percentages are against the forward build. The forward rows reproduce `examples/ssr_fft/measured.json`
exactly: 2.855 ns, 24 / 6 / 8,306 / 10,396 at L = 64; 2.901 ns, 48 / 50 / 24,713 / 21,400 at L = 1024.
Those are the numbers `docs/guide/dsp/ssr_fft/synthesis.md` and `docs/examples/ssr_fft/resource.md`
quote, filed 2026-10-06 with no tool version recorded. They are now confirmed at Vitis HLS 2025.1.
Stage 3's table gives 10,402 and 11,537 LUTs for L = 64 forward and runtime: those are `csynth.rpt`'s text
table, which reads 6 LUTs above `csynth.xml` in the same run (Vitis HLS 2025.1). The FF agree.

**Where the extra goes** (per module, L = 64, forward -> runtime, Vitis HLS 2025.1). The commutators,
transposers and the reorder are identical, module for module. Only the input register and the stages
change:

| module | FF, Vitis 2025.1 | LUT, Vitis 2025.1 | pipeline depth, Vitis 2025.1 | estimate, Vitis 2025.1 |
|---|---|---|---|---|
| `in_reg` (runtime: the direction's input register) | 133 -> 148 | 53 -> 141 | 2 -> 3 | 1.460 -> 1.664 ns |
| `st0` | 774 -> 967 | 2,134 -> 2,448 | 4 -> 4 | 2.732 -> 2.813 ns |
| `st1` | 848 -> 1,195 | 1,734 -> 2,125 | 4 -> 5 | 2.810 -> 2.810 ns |
| `st2` (no multiply) | 393 -> 403 | 1,142 -> 1,285 | 3 -> 3 | 2.855 -> 2.855 ns |

At L = 1024 the same pattern holds: `in_reg` depth 2 -> 3, `st2` 4 -> 5, `st3` 5 -> 6, the other stages
unchanged in depth, and every other module identical (Vitis HLS 2025.1). The modules account for 565 of
the 862 extra FF and 936 of the 1,135 extra LUT at L = 64, and for 1,028 of 1,523 FF at L = 1024. The rest,
297 FF / 199 LUT and 495 FF, is in the top itself: its channels, the direction FIFOs among them.

**The chains** (`.verbose.sched.rpt`, Vitis HLS 2025.1). The slowest state is the same in all three
builds: the twiddle product's subtract feeding the butterfly's two adds. At L = 64 that is
1.027 + 0.914 + 0.914 = 2.855 ns in `st2`; at L = 1024, 1.032 + 0.934 + 0.934 = 2.901 ns in `st3`. The one
chain the runtime build lengthens is `st0`'s: a select (the direction's swap or sign) before the twiddle
multiply, 0.360 + 2.453 = 2.813 ns, where the fixed `st0` is 2.732 ns. It stays under the slowest stage,
so the estimate does not move.

**Latency.** Every loop is II = 1, so the interval does not change (16 at L = 64, the XSI gate). The
extra depth is latency: at L = 64, `in_reg` and `st1` add one stage each. Stage 3's XSI run measured the
first frame done at 111 (fixed) and 112 (runtime), one cycle, not two. So the depths alone do not account
for Stage 3's untraced extra cycle; they say where to look (Vitis HLS 2025.1).

### Post-route (Vivado 2025.1, out of context, xczu48dr-ffvg1517-2-e at 4 ns)

| build | CP routed, Vivado 2025.1 | WNS / WHS, Vivado 2025.1 | LUT (of which LUTRAM), Vivado 2025.1 | FF, Vivado 2025.1 | BRAM, Vivado 2025.1 | worst routed path, Vivado 2025.1 |
|---|---|---|---|---|---|---|
| L = 64, forward | 3.005 ns | +0.995 / +0.015 ns | 6,204 (619) | 7,943 | 0 | `st1` register -> DSP `A` input; 5 CARRY8, 57% route |
| L = 64, inverse | 3.084 ns | +0.916 / +0.024 ns | 6,207 (619) | 7,943 | 0 | `inv_st0` register -> DSP pre-adder; 9 levels, 54% route |
| L = 64, runtime | 3.038 ns | +0.962 / +0.036 ns | 6,605 (619), +6.5% | 8,218, +3.5% | 0 | `rt_st0`'s direction register `iv` -> through the DSP to its ALU output; 6 levels, 46% route |
| L = 1024, forward | 3.084 ns | +0.916 / +0.018 ns | 22,795 (2,406) | 22,164 | 3 RAMB36 + 8 RAMB18 | `st2` register -> DSP `A` input; 5 CARRY8, 65% route |
| L = 1024, inverse | **3.601 ns** | **+0.399** / +0.004 ns | 22,800 (2,406) | 22,164 | 3 RAMB36 + 8 RAMB18 | `inv_rc` (the reorder) register -> register, 1 LUT, 94% route |
| L = 1024, runtime | 3.114 ns | +0.886 / +0.006 ns | 23,424 (2,406), +2.8% | 22,490, +1.5% | 3 RAMB36 + 8 RAMB18 | `rt_cm0`'s LUTRAM read mux (MUXF7/F8), 87% route |

Every build: timing met, no failing endpoint, setup or hold. 24 and 48 DSPs, as csynth said. Each run
took 8-17 minutes, four at a time.

What the post-route numbers add (Vivado / Vitis HLS 2025.1):

- **The runtime build's routed cost is about half its csynth cost.** At L = 64, +275 FF and +401 LUT
  routed, against +862 and +1,135 in csynth. At L = 1024, +326 FF and +629 LUT, against +1,523 and
  +2,113.
- **The runtime's critical path at L = 64 is the one csynth flagged.** It runs from the direction register
  `iv` in `st0` into the multiplier, the select-before-multiply chain, with +0.962 ns to spare. At
  L = 1024 its worst path is elsewhere, in a commutator's memory read.
- **The worst of the six is a fixed build.** At L = 1024 the inverse, the same logic as the forward with
  other ROMs, routes 0.52 ns slower on a 1-LUT path that is almost all routing. The ~0.5 ns spread is
  placement, as the stream blocks' ~1 ns was.
- **csynth's BRAM is not what Vivado builds.** At L = 64, csynth's 6 BRAM_18K become 619 LUTs of
  distributed RAM, and no BRAM at all. At L = 1024, its 50 become 3 RAMB36 and 8 RAMB18 (14 x 18K) plus
  2,406 LUTRAMs, the same in all three builds. So csynth overstates LUT at L = 64 (10,396 against 6,204
  routed) and understates it at L = 1024 (21,400 against 22,795): the memories it counts as BRAM land in
  LUTs.

### Not done (SsrFft)

- L = 1024 is synthesized and routed, not simulated: the runtime build stays pysim-only at 256-4096, as in
  Stage 3. Only L = 64 runs under XSI.
- The extra latency cycle at L = 64 is located to two modules' depths but not traced to the one cycle the
  XSI run sees.
- Out of context, as for the stream blocks: no I/O timing, no neighbours.

## Sharing against replicating: the stream blocks' records and the arbiter's cost per frame (Vitis HLS / Vivado 2025.1)

Asked by the planning session (2026-11-22): the resource and timing figures a design needs to weigh sharing
a kernel (an arbiter, a demux, their FIFOs, a credit) against replicating it. There are three parts: csynth
records filed in the platform library, post-route spot checks, and the arbiter's idle cycles between
frames at RTL against pysim's.

**The answer.**

- **48 configurations are filed** in the packaged `xczu48dr_250mhz_vitis2025_1` (csynth, Vitis HLS 2025.1,
  xczu48dr at 4 ns). They cover `StreamArbiter` and `StreamDemux` at 2 and 4 ports, 32, 64 and 128 bits,
  1- and 2-burst frames, and both policies or routes. `LookupResourceModel` answers `EXACT` for each one.
- **8 and 16 ports are not filed.** The filing cannot attribute them: Vitis names their RTL differently
  (gap 1 below). Following the brief, the sweep stopped there instead of hand-writing records. No resource
  model is fitted, because the fit cannot see the policy or the route (gap 2).
- **16 inputs at 128 bits still meet 4 ns.** The round-robin arbiter routes with +0.342 ns of WNS, and the
  16-output demux with +0.874 ns (Vivado 2025.1).
- **pysim does not reproduce the arbiter's cost per frame.** At RTL, consecutive backlogged frames are
  3 to 8 cycles apart, depending on `n_in` (default part at 100 MHz, Vitis HLS / xsim 2025.1). In pysim
  they are 1 cycle apart. The model would need a delay of 2 to 7 cycles per firing. It is not changed.
- **The arbiter hung at RTL, and is fixed.** With three or more inputs it stopped once input 0 ran dry.
  Stage 2's gates (a 3-input pair) did not catch it. `#pragma HLS INLINE` on `copy_frame` fixes it. That
  costs +37% to +45% routed LUT and +24% to +85% routed FF (Vivado 2025.1) where it applies. It leaves the
  first section's 3-input pair unchanged, digit for digit (re-probed).

### How

`waveflow/streams/calib.py` is the sweep. It is a `waveflow.build.sweep` grid (`SweepRunner`, `ParamGrid`,
`sweep_cli`), the same machinery the other calibrations use. Each point runs three steps:

1. `codegen`: the block alone as its own `ap_ctrl_none` top, with every port an AXIS port. It targets the
   platform's part and clock (`tcl_target`: xczu48dr at 4 ns).
2. `csynth`.
3. `InspectSynthStep` with `require_ii1=True`, which files one record under the block's module key.

The records go to the untracked work tier. `Platform.resolve` treats the packaged platform as a read-only
fallback, so a build writes under `<platforms_root>/<name>`. `waveflow_calib publish` then promotes them.
Its dry run listed 144 new files, 48 keys times (`module.json`, `resource/`, `integration/`
`records.jsonl`), with `platform.json` unchanged. Nothing outside the 48 `stream_arbiter-*` /
`stream_demux-*` keys was touched.

The module key is the elaborated structure, so `policy` and `route` get keys of their own even though
neither is an `HwParam`. Their `module.json` lists only `n_in` / `n_out`, `bitwidth`, `frame_bursts`
(and `sel_lsb` / `sel_bits`). `tests/streams/test_rfsoc_probe.py` checks that every swept configuration
up to 4 ports is `EXACT`, on 24 distinct keys a block, and that 8 and 16 stay `UNCALIBRATED`.

```bash
python -m waveflow.streams.calib arbiter        # 48 points, ~16 s each (Vitis HLS 2025.1)
python -m waveflow.streams.calib demux
waveflow_calib publish work/stream_calib/calib/work/xczu48dr_250mhz_vitis2025_1 \
                       waveflow/calib/platforms/xczu48dr_250mhz_vitis2025_1          # then --apply
python -m waveflow.streams.rfsoc_probe work/rfsoc_probe --only arb16_w128_rr arb4_w64_rr \
                       dmx16_w128_sel dmx4_w64_sel --steps csynth impl                # spot checks
python -m waveflow.streams.arbiter_timing        # the gaps (XSI: VIV set to the Vivado install)
```

The probe gained four tops (`rtl.make` gained the arbiter alone, `kind="arbiter"`), all with 2-burst frames.
`waveflow/streams/arbiter_timing.py` measures the gaps.

### The records (csynth, Vitis HLS 2025.1, xczu48dr-ffvg1517-2-e, 4 ns)

Every loop is II = 1, and there is no DSP or BRAM anywhere. The arbiter's LUT count is the same at every
width:

| arbiter | LUT, 1-burst, Vitis 2025.1 | LUT, 2-burst, Vitis 2025.1 | FF, 1-burst, 32 / 64 / 128 bits, Vitis 2025.1 | FF, 2-burst, 32 / 64 / 128 bits, Vitis 2025.1 |
|---|---|---|---|---|
| 2 inputs, priority | 251 | 343 | 53 / 93 / 173 | 127 / 231 / 439 |
| 2 inputs, round-robin | 357 | 449 | 64 / 104 / 184 | 138 / 242 / 450 |
| 4 inputs, priority | 458 | 642 | 60 / 100 / 180 | 208 / 376 / 712 |
| 4 inputs, round-robin | 677 | 861 | 100 / 140 / 220 | 248 / 416 / 752 |

| demux | LUT, 1-burst, 32 / 64 / 128 bits, Vitis 2025.1 | LUT, 2-burst, Vitis 2025.1 | FF, 1-burst, Vitis 2025.1 | FF, 2-burst, Vitis 2025.1 |
|---|---|---|---|---|
| 2 outputs, by `s_sel` | 447 / 479 / 543 | 467 / 499 / 563 | 213 / 397 / 765 | 215 / 399 / 767 |
| 2 outputs, by header field | 397 / 397 / 397 | 417 / 417 / 417 | 219 / 403 / 771 | 221 / 405 / 773 |
| 4 outputs, by `s_sel` | 520 / 552 / 616 | 540 / 572 / 636 | 293 / 557 / 1,085 | 295 / 559 / 1,087 |
| 4 outputs, by header field | 470 / 470 / 470 | 490 / 490 / 490 | 303 / 567 / 1,095 | 305 / 569 / 1,097 |

Reading the tables (Vitis HLS 2025.1):

- **Round-robin costs LUTs, and little else.** At 2 inputs it adds 106 LUT and 11 FF; at 4 inputs, 219 LUT
  and 40 FF. It is the `next_k` search.
- **A second burst adds about 46 LUT an input to the arbiter, and 20 LUT to the demux.** The arbiter's FF
  grow about 4x faster with width at 2 bursts than at 1: each input's `COPY` pipeline is a stage deeper.
- **The arbiter's estimate charges each input's `COPY` pipeline a fixed LUT count at any width,** for
  example 73 LUT at 64 bits. Its FF do scale with width. The routed spot checks below show what the
  fabric actually needs.
- **The demux by header field has the fewest LUT, and they do not grow with width.** Routing by `s_sel`
  costs 50 LUT more at 32 bits and 146 more at 128 bits.
- **csynth does not count the AXIS register slices** that a standalone top gets on each port. In the
  4 x 64 arbiter, its "Instance" FF (284 of 416) are the four `COPY` pipelines alone; only the muxes that
  feed the slices appear. So the records are the block's own logic, which is what a composite pays, since
  its internal ports are FIFOs. They are not the standalone top's routed size. Two `InspectSynthStep`
  details: a record is the attributed task row, from `csynth.rpt`'s module table, and it reads up to
  8 LUTs above `csynth.xml` for the same run (861 against 853 at 4 x 64 round-robin).

### Post-route spot checks (Vivado 2025.1, out of context, xczu48dr-ffvg1517-2-e at 4 ns)

All four have 2-burst frames. The arbiters are the fixed body.

| top | est., Vitis 2025.1 | slowest state, Vitis 2025.1 | csynth FF / LUT, Vitis 2025.1 | routed LUT / FF, Vivado 2025.1 | CP routed, Vivado 2025.1 | WNS / WHS, Vivado 2025.1 | worst routed path, Vivado 2025.1 |
|---|---|---|---|---|---|---|---|
| arbiter, 16 x 128, round-robin | 2.487 ns | S3: the search's `next_k + j` add 0.789 -> compare 0.548 -> two selects, 0.384 + 0.384 | 5,363 / 3,913 | 5,319 / 9,252 | 3.658 ns | **+0.342** / +0.042 ns | `s_in_15` register slice state -> its data D; 1 LUT, 95% route |
| arbiter, 4 x 64, round-robin | 2.633 ns | S2: the `win` selects 0.414 + 3 x 0.384 -> sparsemux 0.476 -> `m_sel` write | 416 / 853 | 794 / 1,120 | 3.145 ns | +0.855 / +0.044 ns | `s_in_1` register slice state -> its data D; 1 LUT, 93% route |
| demux, 16 x 128, by `s_sel` | 2.829 ns | S1: `s_sel` empty test -> `idx >= N` compare 1.548 -> select `k` 0.414 | 3,038 / 1,174 | 2,835 / 7,436 | 3.126 ns | +0.874 / +0.030 ns | from `m_out_3`'s register slice; 5 LUTs, 79% route |
| demux, 4 x 64, by `s_sel` | 2.046 ns | S1: the same, compare 1.088 | 559 / 570 | 596 / 1,341 | 2.865 ns | +1.135 / +0.042 ns | the demux FSM -> ...; 4 LUTs, 82% route |

Every top met timing, with no failing endpoint for setup or hold. Each run took 6-8 minutes.

- **Sixteen inputs meet 4 ns with room to spare.** The 16-input arbiter's worst path is a 1-LUT path in
  an input's register slice, almost all routing. Its csynth chain, the round-robin search, is 2.487 ns, and
  ranks below the 4-input arbiter's 2.633 ns. The 16-input task has 13 FSM states against the 4-input
  task's 5, so its search is spread over more states. Its slowest state, S3, holds the search's add,
  compare and selects; the 4-input task's S2 runs the search into the `m_sel` write (Vitis HLS 2025.1).
- **Routed FF are about twice csynth's for a standalone top.** The difference is the AXIS register slices
  csynth does not count (above): 9,252 routed against 5,363 at 16 x 128, and 7,436 against 3,038 for the
  demux.
- **The INLINE fix's cost, routed** (old body -> fixed, Vivado 2025.1). At 16 x 128 round-robin: LUT
  3,661 -> 5,319 (+45%), FF 5,007 -> 9,252 (+85%), WNS +0.378 -> +0.342 ns. At 4 x 64 round-robin: LUT
  580 -> 794 (+37%), FF 903 -> 1,120 (+24%), WNS +0.888 -> +0.855 ns. In csynth, the 16-input build goes
  from 498 FF / 1,589 LUT to 5,363 / 3,913: one `copy_frame` module (135 FF, 73 LUT) becomes sixteen
  `COPY` pipelines (302 FF, 128 LUT each, depth 3).

**Sharing against replicating, from these numbers** (Vivado 2025.1, routed, standalone tops, so an upper
bound). Sharing among 4 clients at 64 bits costs the arbiter and the demux, 794 + 596 = 1,390 LUT and
1,120 + 1,341 = 2,461 FF, with no DSP or BRAM, plus the link FIFOs and the credit. Replicating means three
more copies of the kernel. For `SsrFft` at L = 64 (forward, routed, the section above) that is 3 x 6,204 LUT,
3 x 7,943 FF and 3 x 24 DSP. At 16 clients and 128 bits the two blocks route at 8,154 LUT and 16,688 FF, about
1.3 L = 64 FFTs' LUT and 2.1 of their FF, against 15 more FFTs. Sharing wins by a wide margin whenever the
kernel's interval can serve every client. The arbiter's cost per frame, next, is what eats into that
interval.

### The arbiter's cost per frame: RTL against pysim

The setup: the arbiter alone, with every input pre-loaded with `frames_of(k, 1, "long")` (12 frames of
8 words each) and `m_out` and `m_sel` always ready. The RTL runs on the default part at 100 MHz, as in its
gates, through `system_xsi` (Vitis HLS 2025.1 csynth, Vivado 2025.1 xsim). The pysim run is the model alone,
with deep links. A gap is the number of idle cycles between one frame's last word and the next frame's first,
read from the `n_in`-th frame on. Every gap in a run was the same value, and no frame had an idle cycle
inside it.

| `n_in` | priority, RTL, Vitis / xsim 2025.1 | round-robin, RTL, Vitis / xsim 2025.1 | pysim, both policies | cycles per 8-word frame, RTL |
|---|---|---|---|---|
| 2 | 3 | 3 | 1 | 11 / 11 |
| 4 | 3 | 4 | 1 | 11 / 12 |
| 8 | 7 | 8 | 1 | 15 / 16 |
| 16 | 8 | 7 | 1 | 16 / 15 |

The order was right in every run. Priority takes input 0's 12 frames back to back, then input 1's, and so
on. Round-robin cycles 0, 1, ..., n-1.

**Where it comes from** (the 100 MHz schedules, Vitis HLS 2025.1):

- **At 2 and 4 inputs:** a firing tests every input's `empty()` and searches, writes `m_sel`, and only
  then enters the chosen input's `COPY` pipeline. That pipeline has depth 1, so a frame costs its words
  plus 3 or 4 cycles.
- **At 8 and 16 inputs:** Vitis HLS 2025.1 stops inlining `streamutils::write_boundary_word`, which
  `streamutils_hls.h` declares `inline` but gives no pragma. It becomes a module of its own,
  `write_boundary_word_axis_..._s` (latency 1), called for `m_sel` and inside every `COPY` loop, and the
  `COPY` depth goes from 1 to 3. The same build loses its template-mangled name (gap 1).
- **At 16 round-robin:** 7 is one less than at 8. It was measured, not traced.

At 8-word frames the arbiter therefore runs at 73% of its port rate with 2 inputs, and at 50-53% with 8 or
16. A kernel shared through it should be fed frames long enough that 3-8 cycles are small next to them.

**What pysim would need. It is not changed, as asked.** pysim's `run_iter` waits on `wait_any`, writes
`m_sel`, then copies, and has no decision cost of its own. The 1 cycle is the link's. To reproduce the RTL,
a firing needs `gap - 1` more cycles: 2 / 2 / 6 / 7 for priority and 2 / 3 / 7 / 6 for round-robin, at
n = 2 / 4 / 8 / 16. The repository already has the mechanism. `FreeRunMod.add_timing_model` plus
`yield self.timeout(self.timed_delay({"n_in": n, "policy": ...}))` in `run_iter`, between the win and the
`m_sel` write, are calibrated from RTL runs by `CollectTimingStep` (`plans/timing_model.md`). Two caveats
for that calibration:

- **These are 100 MHz numbers.** At 4 ns the 4-input `COPY` pipeline is depth 2, not 1 (the spot check
  above). The RFSoC's gaps are likely a cycle larger at small `n`, so the calibration belongs to the
  platform's clock.
- **It is not yet measured at 2-burst frames, or at other widths.** With 2 bursts, `COPY` runs straight
  across the burst boundary, so no gap is expected inside a frame. That was not checked.

A cheaper fix for the step at 8 inputs is `#pragma HLS INLINE` in `write_boundary_word`. It is not done
here: every kernel calls that function, so it needs every XSI gate re-run, and their pinned counts may move.

### The hang, and the fix

**The symptom.** While measuring the gaps, the arbiter alone stopped at RTL. Each case below is the
default part, Vitis HLS 2025.1, xsim 2025.1, and the old body:

| run | bursts delivered |
|---|---|
| 3 inputs, priority, long workload | 12 of 36 |
| 3 inputs, round-robin | 34 of 36 |
| 4 inputs, round-robin | 45 of 48 |
| 16 inputs, round-robin | 177 of 192 |
| 4 inputs, priority, Stage 2's test workload | 6 of 22 |

In every case `m_sel`'s last word announces an input other than 0 right after input 0's last frame, and
no word of that frame follows. 2 inputs complete. Through the pair (arbiter -> demux) the failures were:

- 4 and 8 inputs, priority: input 0's 12 frames delivered, and nothing of the others'.
- 4 inputs, round-robin: 11 of 12 frames on each of inputs 1-3.
- 3 inputs, both workloads: no failure. That is why Stage 2's gates (`n = 3`) passed.

**The cause, as far as it was traced.** For these builds, Vitis HLS 2025.1 does not inline
`copy_frame`. It keeps one `copy_frame_<W>_<NB>_..._s` module for every input, with the input stream muxed
into it per call. That module hangs on the first frame it copies from another input once input 0 has run
dry. The trace stops there: the module's handshakes were not followed in a waveform.

**The fix.** `#pragma HLS INLINE` in `copy_frame` (`waveflow/streams/src/stream_arbiter_task.h`, with a
comment) gives each input its own `COPY` pipeline (`stream_arbiter_task_..._Pipeline_COPY`,
`..._COPY1`, ...). The loop keeps the shape Stage 2's comment protects. The following complete under xsim
(Vitis / Vivado 2025.1):

- the lone arbiter: 3 inputs priority, 4 round-robin, 16 priority and 16 round-robin with the long
  workload; 4 inputs priority with 2-burst frames;
- the pair, with `compare` clean against pysim: 4 and 8 inputs priority and 16 round-robin with the long
  workload; 4 inputs round-robin with 2-burst frames on the test workload;
- `pytest tests/streams/test_arbiter_demux.py -m xsi`: 4 pass on the default part.

The records above and all the gaps are the fixed body's; the old body's records were deleted before the
re-sweep.

**A gate is still missing.** It should be the pair at 4 inputs with the long workload
(`rtl.build_block_dag(..., "pair", 4, workload="long")`). It is not added here: a new XSI gate raises
`WANT_XSI_GATES` in `tests/conftest.py`, which Stage 3 and Stage 5 edit, and the count is theirs to keep.

**The 3-input pair is unchanged by the fix.** The first section's four `pair_*` tops (3 inputs, 32 bits)
were re-probed with the fixed body. They give the same module list, the same estimates, the same csynth FF
and LUT, and the same routed LUT, FF and slack, to the digit (Vitis HLS / Vivado 2025.1). In the pair at
3 inputs, Vitis 2025.1 had already given each arbiter input its own pipeline
(`stream_arbiter_task_Pipeline_COPY`, `..._COPY1`, `..._COPY2`); the one shared `copy_frame_*` module
there is the demux's. That is why Stage 2's 3-input gates never hung, and why the first section's numbers
stand. The shared arbiter module appeared in the lone arbiter from 3 inputs, and in the pair from 4.

### The gaps in the flow (proposed, not built)

1. **Attribution, from 8 ports up.** `rtl_prefix` predicts the task's RTL name from its template arguments,
   for example `stream_arbiter_task_32_1_0`. With 8 or more AXIS ports, Vitis HLS 2025.1 names the
   module plain `stream_arbiter_task` / `stream_demux_task`, so `InspectSynthStep` finds no row and
   (rightly) fails. It also reports a non-inlined `write_boundary_word_..._s` row that nothing claims. The
   extension belongs in `waveflow/calib/synth_report.py` `_classify`: when no row matches the prefix, fall
   back to the bare function name when exactly one such row exists, and give helper rows under the task
   (`write_boundary_word_*`) to it as sub-blocks. It needs a unit test on a report with plain names, then
   a re-run of the sweep at 8 and 16.
2. **Fitting across a body choice.** `VitisResourceModel.corpus()` re-elaborates each row from
   `module.json`'s parameters, and those are `HwParam`s only. `policy` and `route` are `build_fields`, so
   two keys with the same parameters and different bodies are indistinguishable to a fit. The extension is
   to record `build_fields` in `ModuleIdentity.params` and pass them back in the corpus re-elaboration. A
   `resource_structure()` for each block (per-input `COPY` pipelines, the search, the demux's index test)
   would then let a model predict 8 and 16 from 2 and 4. Until then, the lookup answers exactly what was
   built, and nothing else.
3. **The arbiter's decision cost in pysim** (above): a timing model through `timed_delay`, calibrated at
   the platform's clock.

### Not done (the stream blocks' records)

- The 8- and 16-port records (gap 1). The routed spot checks are the only figures at 16.
- No XSI at the RFSoC clock, so the gaps are 100 MHz numbers.
- No gap measured at 2-burst frames or at widths other than 32.
- Out of context, as before. The routed figures include the standalone top's AXIS register slices.
