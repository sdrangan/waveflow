# Witness: what does a loop inside an `hls::task` cost?

The measured basis for the *"a loop inside the body"* section of `plans/pipelined_ops.md`.
Hand-written, no Waveflow involvement — the point of a witness is that it measures **Vitis**, so a
surprising number cannot be blamed on our generator.

```
task_loop.cpp   eight tops: two body shapes x four loop shapes
run_hls.tcl     csynth each into its own project; `catch` per variant so a REFUSAL is recorded
                rather than aborting the other seven
tb_ingress.v    input-side beats: does the task ever stop READING, and for how long
tb_player.v     output-side beats + a back-pressure phase: continuous emission or store-and-forward
report.py       reads ACHIEVED PipelineII out of syn/report/*_csynth.xml (stdlib only)
run.sh          one script: ./run.sh [all|synth|sim|report]
```

Reproduce (Vitis HLS 2025.1, `xczu48dr-ffvg1517-2-e`, 4.0 ns):

```
./run.sh
```

Set `WITNESS_VITIS` / `WITNESS_VIVADO` to the `bin` directories if the tools are not at
`C:\Xilinx\2025.1` (Windows) or `/tools/Xilinx/2025.1` (Linux).

Expected: `TB-INGRESS: PASS` / `TB-PLAYER: PASS` for all eight, and the two tables below.

## What is being compared

Two body shapes, reduced to their essential work, both real:

| | | today |
|---|---|---|
| `ing` | stream-read → BRAM-write | `RfSampBufIngress`, `fire_cycles = 2` |
| `ply` | BRAM-read → stream-write | `RfSampBufPlayer`, `fire_cycles = 3` — **the binding one for pattern B** |

Four loop shapes: `_1` (one word per firing, today's shape), `_n8` and `_n64` (bounded, `PIPELINE
II=1`), `_w` (`while (1)`, same pragma).

**Two values of N on purpose.** With one, the boundary cost and the per-word cost cannot be
separated — any (period, N) pair is consistent with infinitely many splits. With two, the boundary
falls out of the difference.

**The witness reproduces the shipped constants before it is asked anything new.** `ing_1` measures
2 cycles/word and `ply_1` measures 3, in both csynth and RTL, which are exactly
`RfSampBufIngress.fire_cycles` and `RfSampBufPlayer.fire_cycles`. Independent code, same numbers —
that is what makes the loop rows worth believing.

## 1. Is `while (1)` legal inside a task body?

**Yes — it synthesizes, at achieved II=1, with a warning.** Both shapes:

```
WARNING: [XFORM 203-561] 'VITIS_LOOP_62_1' (task_loop.cpp:62:22) in function 'ing_body_w'
                         is an infinite loop.
WARNING: [XFORM 203-561] 'VITIS_LOOP_97_1' (task_loop.cpp:97:22) in function 'ply_body_w'
                         is an infinite loop.
```

The report carries `TripCount = inf` and no overall latency — Vitis cannot bound a firing that never
ends, which is correct and is why `report.py` prints *unbounded* rather than inventing a number. The
RTL runs, sustains one word per cycle indefinitely, and passes its ramp check.

## 2. Continuous, or store-and-forward?

**Continuous.** Two independent measurements agree, which matters because either alone is weak.

*Throughput.* A store-and-forward firing spends N cycles computing and N draining, so it cannot
exceed 0.5 words/cycle. Measured: `ply_n64` sustains **0.956**, `ply_w` **1.000**.

*Back-pressure.* `TREADY` held low for 40 cycles mid-stream; count BRAM reads during the stall. A
body that buffered N outputs would keep reading into its buffer — up to 40 reads. Measured **1–3**,
i.e. the pipeline stalls as a unit within its own depth. There is no hidden buffer.

The ramp check is what makes this more than a rate: every beat is `previous + 1`, so a run that
achieved its throughput by dropping or reordering could not pass.

*And it resumes.* After the stall is released, beats over the next 200 cycles are 67 / 146 / 191 /
200 for `_1` / `_n8` / `_n64` / `_w` — which is each variant's own phase-1 throughput, arrived at a
second time and by a different route. A design that deadlocked on back-pressure would have looked
identical in every other number on this page.

## 3. What does the loop boundary cost?

**3 cycles of lost throughput per firing**, for both body shapes and both values of N.

Measured as an inter-beat gap it reads **4**: the last beat of one firing and the first of the next
are 4 cycles apart, which is 3 idle cycles plus the one that would have carried a beat.

It falls out of the two N values rather than being asserted. Writing `period = a + b·N`:

```
N =  8:  period = 11    11 = a +  8b
N = 64:  period = 67    67 = a + 64b     ->   b = 1, a = 3
```

so the loop runs at exactly one word per cycle and the boundary is a flat 3 cycles, independent of N.
(`period` is read off the gap histogram, not inferred: at N=8 the run is 350 gaps of 1 and 49 of 4,
which sums to the measured 546-cycle span exactly.)

### The csynth latency convention does not extend to a looped body

`fire_cycles = latency + 1` is the calibration this arc uses throughout. It is **right for a
one-word body and wrong by 2 for a looped one**:

| variant | csynth latency | `latency + 1` | RTL period | error |
|---|---|---|---|---|
| `ing_1` | 1 | 2 | **2** | 0 |
| `ply_1` | 2 | 3 | **3** | 0 |
| `*_n8` | 12 | 13 | **11** | +2 |
| `*_n64` | 68 | 69 | **67** | +2 |

A plausible mechanism — **not measured, so not asserted** — is that the task runtime overlaps the
next firing's fill with the current one's drain, and the pipeline depth is 2. Whatever the cause, a
looped body's throughput taken from `latency + 1` is pessimistic by 2 cycles, which is the safe
direction but is still wrong.

## The numbers

### Achieved II and firing cost, from `syn/report/*_csynth.xml`

**Achieved `PipelineII`, not the summary report's Interval column** — they are different numbers and
the summary has been misread twice in this arc. Every II below is the achieved one.

| variant | words/firing | body latency | loop | trip | achieved II | pipeline depth |
|---|---|---|---|---|---|---|
| `ing_1` | 1 | 1 | *(no loop)* | – | – | – |
| `ing_n8` | 8 | 12 | `VITIS_LOOP_53_1` | 8 | **1** | 2 |
| `ing_n64` | 64 | 68 | `VITIS_LOOP_53_1` | 64 | **1** | 2 |
| `ing_w` | ∞ | *unbounded* | `VITIS_LOOP_62_1` | inf | **1** | 3 |
| `ply_1` | 1 | 2 | *(no loop)* | – | – | – |
| `ply_n8` | 8 | 12 | `VITIS_LOOP_88_1` | 8 | **1** | 2 |
| `ply_n64` | 64 | 68 | `VITIS_LOOP_88_1` | 64 | **1** | 2 |
| `ply_w` | ∞ | *unbounded* | `VITIS_LOOP_97_1` | inf | **1** | 3 |

**Every loop met II=1**, in both shapes. Not one missed its target, so the trap this witness was
warned about — reading a target where the achievement is worse — did not arise here. It was still
read from the right field, because next time it might.

### Measured in RTL

| variant | words/cycle | gaps of 1 | boundary gaps | gap size | cycles/word | vs. today |
|---|---|---|---|---|---|---|
| `ing_1` | 0.501 | 2 | 397 | 2 | **2.000** | 1.00x |
| `ing_n8` | 0.731 | 350 | 49 | 4 | **1.375** | 1.45x |
| `ing_n64` | 0.956 | 393 | 6 | 4 | **1.047** | 1.91x |
| `ing_w` | **1.000** | 399 | **0** | – | **1.000** | 2.00x |
| `ply_1` | 0.333 | 0 | 399 | 3 | **3.000** | 1.00x |
| `ply_n8` | 0.731 | 350 | 49 | 4 | **1.375** | 2.18x |
| `ply_n64` | 0.956 | 393 | 6 | 4 | **1.047** | 2.87x |
| `ply_w` | **1.000** | 399 | **0** | – | **1.000** | **3.00x** |

**The loop erases the difference between the two body shapes.** `ply_1` costs 3 cycles/word against
`ing_1`'s 2 — the extra one is the BRAM read latency — and once pipelined both are identical at every
N. The shape that is more expensive today has the more to gain, which is convenient, because it is
the one that binds.

## What this means for the pattern-B ceiling

Max sustainable sample rate is `samp_per_word · f_axis / (cycles per word)`. At `samp_per_word = 4`
and `f_axis = 250 MHz`, the **port** carries 1000 MSPS and the design capacity is:

| player body | cycles/word | design ceiling | fraction of port |
|---|---|---|---|
| `_1` (today) | 3.000 | **333 MSPS** | 33% |
| `_n8` | 1.375 | 727 MSPS | 73% |
| `_n64` | 1.047 | 955 MSPS | 96% |
| `_w` | 1.000 | **1000 MSPS** | **100%** |

Past that the **port** binds, not the body: 1000 MSPS is `samp_per_word · f_axis`, and raising it
needs a wider word or a faster fabric, not a better loop. The converters on this part run far faster
than either.

### And the boundary is not free at the ingress

The 3 idle cycles are a window in which the task is not reading. Words arriving in that window go to
a boundary port that Vitis fixes at 2 deep, so per firing the ADC loses

```
max(0, 3 · r − 2)        r = converter word rate in words per fabric cycle
```

At today's `r = 0.25` that is 0.75 words arriving into 2 slots — **nothing is lost**, at any N. At
`r = 1.0` (the port's own ceiling) it is 3 arriving into 2 slots, so **1 word per firing is lost**:
1.6% at N=64, 12.5% at N=8. `while (1)` never stops reading and loses nothing at any rate.

That is the whole argument for which variant to build from, and it does not need its own testbench —
it follows from the gap.

## What was NOT measured

- **Resources.** No LUT/FF/BRAM comparison. A pipelined loop costs registers, and `while (1)` at
  depth 3 costs more than `_1` at depth 0; none of that is here.
- **Timing closure.** csynth's estimated Fmax only. Nothing was placed or routed, so "II=1 at 4.0 ns"
  is a schedule, not a closed design.
- **A loop with real work in it.** Both bodies are a move, not a computation. A body with a
  multiply-accumulate or a wider datapath may not reach II=1, and this witness says nothing about it.
- **The interaction with a second task.** Every top here has exactly one task. Whether two looped
  tasks sharing a BRAM keep their II is a different experiment.
- **`while (1)` under reset.** Not exercised. `plans/adc_model.md` records that a task which writes
  before it reads advances its state during reset; an infinite loop changes the shape of that
  question and it was not asked here.
