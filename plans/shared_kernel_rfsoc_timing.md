# The shared-kernel blocks on the RFSoC part: a timing probe

**Status: DONE 2026-11-21, on the lab machine, Vitis HLS / Vivado 2025.1.** It
answers the item `plans/shared_kernel.md` leaves open in its Stage 2 record: "the 7.82 ns case is open ...
the real target (250 MHz, xczu48dr) has not been tried". That plan is not edited here (another agent owns
it); its Stage 2 record should link this page when it is next touched. Two parts: the stream blocks and
the ring stub (below), then `SsrFft` with the direction chosen per frame against its fixed builds.

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

The pair meets 4 ns, so nothing was rewritten. The bodies' "keep this loop's shape" comments stand, and
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
- Only what the gates build: 32-bit words, 3 clients. A wider word or a 16-input arbiter (`ready_bits`
  and `SEARCH` grow with `n`) was not tried.
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
