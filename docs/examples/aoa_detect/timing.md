---
title: The timing
parent: Emitter detection and AoA
nav_order: 6
summary: "The cycle model of every block for the timed simulation, and where each number comes from: calibrated on the xczu48dr platform (the dispatcher and the arbiter, the ring's read port and the crossbar), or analytic from loop counts (the new kernels), to be refitted to RTL. The ring's read port was the finding that shaped the design -- the BRAM window's bus port read at half rate, a block's read taking a whole block period -- and is resolved by a full-rate leaf, measured: a channel's block in 1,233 cycles, the port 88% busy at 1.5 jobs a block. Then the decision time with no contention, and the order in which the timed models are built."
---

# The timing

**Status: under construction.** These are the cycle models the timed simulation will use, at the
[proposed parameters](param.md). Every number says where it comes from:

- **calibrated**: measured at RTL on the `xczu48dr_250mhz_vitis2025_1` platform and filed there;
- **measured elsewhere**: measured on another design, part or clock, used until it is measured here;
- **analytic**: loop trip counts and pipeline depths estimated before synthesis, refitted to RTL later,
  as `examples/cg_pool` did for its worker.

All cycles are at 250 MHz. A block is 2,083 cycles.

## The blocks

### Capture and power detector

**Analytic.** One pipelined loop over the block's sample words at one word a cycle: $C \cdot B = 2048$
words of 512 bits (one channel's 16 antennas for one sample), each written to the ring through port B
and added into its channel's energy. The words arrive at $C\, n_{rx} f_s / 16 = 245.8$ M words/s, so
the loop is **98% busy** and keeps up with no slack to spare. At the block's end: the $C$ comparisons
and up to $C$ one-word notifications, about $C + 8 = 16$ cycles. Port B is untimed in pysim
(`MemSlaveBramWindow.port_b_write` is a method call), so the kernel's own model charges the one word a
cycle ([why it matters](arch.md#two-checks-for-the-timing-model)).

### The ring's read port, over the crossbar

**Calibrated** on `xczu48dr_250mhz_vitis2025_1` (xsim 2025.1, `plans/bram_fullrate_port.md`). This
was the block that decided the design.

- **The half-rate problem, and its fix.** The BRAM window's original bus leaf
  (`waveflow/build/rtl/mm_bram_port.v`) keeps one read in flight and costs two cycles a read word.
  The ring now uses the full-rate leaf, `mm_bram_port_pipe.v` (`MemSlaveBramWindow(...,
  bram_leaf="mm_bram_port_pipe")`): a word a cycle with a registered response, the slave adaptor's
  front keeping two reads outstanding. It closes timing at 4 ns on this part behind a 4-master
  crossbar at 128 and 512 bits ([which leaf](../../guide/interface/axi_mm/slave_views.md#which-leaf)).
- **pysim times it from records.** With the platform as its `calib_dir`, the window loads its leaf's
  measured timing -- a read costs 2 cycles plus a cycle a word, a write 1 plus a cycle a word, one at a
  time, and 1 cycle of turnaround after each -- and the crossbar runs at its measured settings at
  250 MHz (a request 2 cycles to the window, all before it contends; a read beat 1 cycle back;
  [the crossbar page](../../guide/interface/axi_mm/crossbar.md#at-250-mhz-on-xczu48dr)).
- **Measured with the units' readers**: `MemRStream`s at 128 bits, 16-beat bursts, csynth'd for this
  part, each reading a 1,024-word block of the window over the crossbar
  (`tests/build/test_bram_window_contention_xsi.py`). A 16-beat burst repeats every 19 cycles at the
  port: 16 beats, 2 of latency and the 1-cycle turnaround, since the front serves one burst at a time.

| | one channel's block, one reader | port busy at 1.5 jobs a block |
|---|---|---|
| the half-rate leaf, `mm_bram_port` | 2,193 cycles | about 157% |
| the full-rate leaf, `mm_bram_port_pipe` | **1,233 cycles** (pysim 1,228) | **about 88%** |

(From the reader's command to the block's last word; the port's own share is 64 bursts of 19 cycles,
1,216 a block.) **One read takes 0.6 of a block period**, and the port keeps up with 1.5 jobs a block
with some room, where the half-rate leaf could not. With several readers at once the port is shared
burst by burst: two readers starting together finish their blocks at 2,430 and 2,449 cycles, four at
4,824 to 4,881, every one within 0.21% of pysim. The other options considered (a 256-bit port A, the
ring in two banks) are not needed for the read rate; a wider port would still halve the read.

### The units' readers

**Analytic, with a calibration on another platform.** A unit reads its block through a memory reader
(`MemRStream`, 16-beat bursts): at RTL $n_w + (n_{trans} - 1)$ plus about 16 cycles of fill. Its
residual is calibrated only on a Zynq-7020 against a DDR model, so on this platform it runs without one
until it is measured.

### Systolic unit: one search

**Analytic.** The block is read into the unit's buffer (16 KB), then:

| Step | 8 × 8 array | 16 × 16 array |
|---|---|---|
| Read (above, full-rate port, measured) | ~1,230 | ~1,230 |
| $\hat{Q}$: the upper tiles of the 16 × 16 Gram, 256 cycles each plus fill | 3 tiles: ~820 | 1 tile: ~290 |
| Scan $W = \hat{Q} U$: 16 × 256 outputs, 16 cycles a tile | 64 tiles: ~1,040 | 16 tiles: ~290 |
| $p_\ell$ and the largest, pipelined behind the scan | ~20 | ~20 |
| **A search** | **~3,110 (1.5 blocks)** | **~1,830 (0.9 block)** |

At 0.75 searches a block, one 8 × 8 unit would be 112% busy and one 16 × 16 unit 66%: **two 8 × 8
units or one 16 × 16**. (At the half-rate port, ~2,180 cycles a read, they were 146% and 100%.)

### Vector unit: one departure check

**Analytic.** Read-bound: each sample's 16 antennas arrive in 4 words, and $u^H x$ with its square takes
the 16 lanes one cycle a sample, so the computation hides behind the read. A check is about the read
plus 20 cycles: **~1,250 cycles** at the full-rate port (~2,210 at the half-rate one).

### Controller

**Analytic.** A message in, a table lookup, a message out: about 4 cycles a message (a notification, a
result), with a pipeline latency of about 8. Small against the units, but it serializes every decision.

### Dispatchers and the arbiter

**Calibrated, at other points.** Their decision cycles are filed on the platform
(`waveflow/streams/timing.py`): the dispatcher 3 to 5 cycles a job at 2 to 4 workers, first free; the
arbiter 2 to 3 cycles a frame at 2 to 4 inputs. They are measured at 32-bit, one-burst frames, and the
arbiter's table is keyed by width and frame, so **our 128-bit messages are a point not yet measured**,
and the blocks refuse it rather than guess. The fixture `python -m waveflow.calib.fixtures.stream_blocks`
adds it (the lab machine: Vitis and Vivado). Until then the two blocks run untimed, which reads a few
cycles short a job. The dispatcher is not needed at one unit a pool.

Inside a composite a decision can cost 2 to 4 cycles less than alone (`timing.py`); the whole design's
gate against RTL is what settles it ([timing a composite](../../guide/timing_model/composites.md)).

## The decision time, with no contention

A search, from the block's arrival to the result message, at the proposed values:

| Step | Cycles |
|---|---|
| The block's end: comparisons and the notification | ~20 |
| Controller and dispatcher | ~10 |
| The unit: read and compute (8 × 8, full-rate port) | ~3,110 |
| The arbiter and the controller's test | ~10 |
| **Total** | **~3,150 cycles, 12.6 µs, about 1.5 blocks** |

About 1,870 cycles (0.9 block) with a 16 × 16 array; at the half-rate port it was about 4,100 (2
blocks). An emitter lasts about 5 blocks, so with contention the decision time is a large part
of its life: the timing changes what is detected.

## What the timed simulation needs, in order

1. **The capture and power detector**, with the ring (`MemSlaveBramWindow`, port B charged by the
   kernel). Gate: its notifications equal the golden's gate decisions, block by block.
2. **The units**, each alone in its harness: a systolic unit and a vector unit reading a ring slot over
   the crossbar, with the analytic models above. Gate: their results equal the golden's `search` and
   `depart_stat`, bit for bit.
3. **The controller**, with the pools (`StreamDispatch`, `StreamArbiter`). Gate: on a scripted
   workload, its result messages equal the golden's decisions when nothing is late or dropped.
4. **The whole**, on the timing workload: the decision time, drops and late reads, and the timing loss
   against the golden on the same samples.
5. **RTL**: each block's HLS body, csynth, and a gate of the whole against pysim. Until then every
   analytic number above is an estimate.

## Open, before building

- ~~**The read path: a full-rate port.**~~ Built and measured (`plans/bram_fullrate_port.md`): the
  full-rate leaf, the front's outstanding reads, the window's calibrated timing in pysim, its records
  and the crossbar's settings on the platform (above).
- **The dispatcher's and arbiter's points at 128 bits**, to calibrate on the lab machine.
- The crossbar's settings measured at 250 MHz on this part, once a design is on it.
