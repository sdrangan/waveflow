---
title: The timing
parent: Emitter detection and AoA
nav_order: 6
summary: "The cycle model of every block for the timed simulation, and where each number comes from: calibrated on the xczu48dr platform (only the dispatcher and the arbiter), measured on another design or part (the crossbar, a BRAM read over it), or analytic from loop counts (the new kernels), to be refitted to RTL. The finding that shapes the design: the BRAM window's bus port reads at half rate (two cycles a word), so one channel's block takes about 2,100 cycles to read, a whole block period, and at 1.5 jobs a block a single read port is about 150% busy. Then the decision time with no contention, and the order in which the timed models are built."
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

**Measured elsewhere.** This is the block that decides the design.

- **The BRAM window's bus port reads at half rate.** Its RTL (`waveflow/build/rtl/mm_bram_port.v`)
  costs $LAT + 1 = 2$ cycles a read word at the leaf, one transaction in flight. In pysim its port A
  costs nothing (`mm_bram.py`), so the rate has to come from the crossbar model's settings.
- **The crossbar**: request latency 4 cycles and travel 2 (measured on `examples/mm_fir`, within 4.4%,
  at 100 MHz; `examples/xbar_profile` found the same at 250 MHz on an xczu7ev: a 16-beat read 20
  cycles). Several masters reading one slave are served whole burst by whole burst, first come first
  served.
- **Modelled as** in `examples/ltbf_cg_mem_dse`, whose shared memory read at the BRAM rate came within
  1.5% of RTL: a serialized slave, 16-beat bursts, a burst at $2n + 2 = 34$ cycles, a 1-cycle turnaround.

**One channel's block** is $B\, n_{rx} = 4096$ samples, 1,024 words of 128 bits, 64 bursts: about
$64 \times 34 \approx 2{,}180$ cycles. **One read takes a whole block period**, twice the 1,024 cycles
assumed on [The parameters](param.md#what-they-imply).

**At 1.5 jobs a block, one read port is about 150% busy**, whatever the number of units. Every job
reads its block, so the units would wait on the port, and the ring would fill with late reads. The
read path has to change, which is a real design axis rather than a detail:

| Option | Read of one block | Port busy at 1.5 jobs a block | What it costs |
|---|---|---|---|
| as is: half-rate port, 128 bits | ~2,180 cycles | ~150% | -- |
| a 256-bit port A | ~1,090 | ~80% | a wider crossbar path |
| a full-rate port (one word a cycle, pipelined reads) | ~1,100 | ~80% | **an extension of `mm_bram_port.v`** |
| both | ~550 | ~40% | both |
| the ring in two banks, one read port each | ~2,180 per bank | ~75% each | a second memory and a routing rule |

A full-rate bus port for the BRAM window is something Waveflow does not have: it would be an
extension, with a pipelined read in `mm_bram_port.v`, the window's pysim timing, and an RTL gate, built
like the existing window.

### The units' readers

**Analytic, with a calibration on another platform.** A unit reads its block through a memory reader
(`MemRStream`, 16-beat bursts): at RTL $n_w + (n_{trans} - 1)$ plus about 16 cycles of fill. Its
residual is calibrated only on a Zynq-7020 against a DDR model, so on this platform it runs without one
until it is measured.

### Systolic unit: one search

**Analytic.** The block is read into the unit's buffer (16 KB), then:

| Step | 8 × 8 array | 16 × 16 array |
|---|---|---|
| Read (above, half-rate port) | ~2,180 | ~2,180 |
| $\hat{Q}$: the upper tiles of the 16 × 16 Gram, 256 cycles each plus fill | 3 tiles: ~820 | 1 tile: ~290 |
| Scan $W = \hat{Q} U$: 16 × 256 outputs, 16 cycles a tile | 64 tiles: ~1,040 | 16 tiles: ~290 |
| $p_\ell$ and the largest, pipelined behind the scan | ~20 | ~20 |
| **A search** | **~4,060 (1.9 blocks)** | **~2,780 (1.3 blocks)** |

At 0.75 searches a block, one 8 × 8 unit would be 146% busy and one 16 × 16 unit 100%: **two units
are needed either way**, unless the read gets faster. With a full-rate or wider port, a search is about
2,960 cycles on 8 × 8 and 1,680 on 16 × 16.

### Vector unit: one departure check

**Analytic.** Read-bound: each sample's 16 antennas arrive in 4 words, and $u^H x$ with its square takes
the 16 lanes one cycle a sample, so the computation hides behind the read. A check is about the read
plus 20 cycles: **~2,200 cycles** at the half-rate port, ~1,120 at full rate.

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
| The unit: read and compute (8 × 8, half-rate port) | ~4,060 |
| The arbiter and the controller's test | ~10 |
| **Total** | **~4,100 cycles, 16 µs, about 2 blocks** |

About 2,960 cycles (1.4 blocks) with a full-rate port, and 1,700 (0.8 block) with that port and a
16 × 16 array. An emitter lasts about 5 blocks, so with contention the decision time is a large part
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

- **The read path**: which option above. A full-rate port is a Waveflow extension.
- **The dispatcher's and arbiter's points at 128 bits**, to calibrate on the lab machine.
- The crossbar's settings measured at 250 MHz on this part, once a design is on it.
