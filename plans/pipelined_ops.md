# Plan: pipelines inside a module -- load, compute, store, timed by the markers at their ports

**Status: PLANNED 2026-10-14, nothing built.**  The defect was recorded 2026-08-16 (below, from
[The defect](#the-defect) on, kept as the evidence).  The design came out of the
`plans/unit_harness.md` discussion: a unit harness can only calibrate a module whose timing is stated
between events it can observe, and today's pipelined ops state it with arithmetic on times that never
happened.  `unit_harness.md` is **paused** until this plan's Stage 4 (the mechanical fit) works on poly.

## The rules

- **Rule 1 -- a timed phase.**  A *time-calibratable phase* is the time between two events whose
  duration is a deterministic function of features known before the first event.  A *timing model*
  is a learned version of that function: its **targets** are observable durations; its parameters (a
  latency, an II) are internal to it and never appear in module code.  Nothing else is passed: the two
  events, the features, the model.
- **Rule 2 -- every event is measurable.**  Each event of a phase has a mechanical measurement at RTL
  (the handshake of an endpoint: its first and last beat of a message) and is logged by pysim's own
  transfer primitive.  A phase's duration is deterministic only while the environment does not stall
  the module inside it, so every record also carries whether it did (an input gap, output
  back-pressure) -- mechanical on both sides too.  A stalled record tests a prediction; it is not
  training data.

## The pattern: load, compute, store

The minimum a module must express is a pipeline of three stages, as the HLS body is written (a
straight-line loop per message, `docs/guide/vectorization/hls/loop_optimization.md`):

1. an **endpoint transaction** -- the load;
2. a **compute** -- the only stage with a timing model;
3. an **endpoint transaction** -- the store.

The four **markers** are the two transactions' first and last beats: `L.first`, `L.last`, `S.first`,
`S.last`.  They are the endpoints' own handshakes, so the unit harness reads them at its boundary (the
bus models' timestamps) and, in place, from probes on the module's ports (`system_top.beat` /
`last`, which resolve a pysim endpoint to its net) -- never from a net someone had to find.

The compute's model predicts two targets, each a difference of markers:

| target | measured as |
|---|---|
| `latency` | `S.first - L.first` |
| `fill_time` | `S.last - S.first`, when neither the input nor the output limited it |

## The module code

```python
lx = yield from load.begin()           # L.first; lx.data is already here
y = compute.fn(lx.data)
latency, fill_time = compute.tm.predict(feats)
yield self.timeout(latency)            # L.first + latency   (relative: nothing waited since L.first)
sx = yield from store.begin(y)         # S.first, when the bus and the queue's room allow
yield self.timeout(fill_time)          # S.first + fill_time (relative: nothing waited since S.first)
yield from load.end(lx)                # L.last
yield self.until(lx.last + latency)    # the tail -- the last output needs the last input; no-op if past
yield from store.end(sx)               # S.last: the burst is delivered
```

wrapped for the common case as

```python
y = yield from self.pipeline(load=Load(self.s_in, Float32, n),
                             compute=Compute(fn, tm=self.poly_tm, feats={"nsamp": n}),
                             store=self.m_out)
```

- **The rule of the waits:** a relative `timeout` only straight after the event it is measured from;
  anything else is an absolute `until`, which takes the max for free.  Sequential relative waits add
  where the hardware takes a max.
- **The data arrives at `begin`.**  The writer holds every word when it calls `write`, so a begun
  burst carries them; compute is functional, and the waits enforce the timing.  No deferred-data write.
- **`store.begin` takes the bus and marks `S.first` without delivering; `store.end` delivers** at the
  later of now and the interface's own minimum (`S.first` + its beats), waiting for room, and marks
  `S.last`.  So delivery is never earlier than anything the module waited for.
- **Nothing is backdated.**  Every wait is "now + a prediction" from a real event, so a shared bus is
  held when it is really used.  This is why the design is two events per transfer and not markers
  recorded after the fact: a backdated write (`write_pipelined`'s `tstart`) cannot take a contended
  bus in the past.
- **`load` is a description** (`Load(endpoint, type, count)`), symmetric with `store=self.m_out`:
  `pipeline` drives the transfer's two halves itself, which a running `get_array(...)` -- which only
  returns at the end -- cannot.

`Xfer` is the handle `begin` returns and `end` completes, one per message per endpoint:

```python
@dataclass
class Xfer:
    ep: InterfaceEndpoint     # whose transfer (the port name for alignment)
    index: int                # the k-th message on that endpoint, counted by the primitive
    data: Any                 # load: the words, typed if the Load asked for a type; store: what was written
    nwords: int
    first: float              # first beat (set by begin)
    last: float               # last beat (set by end; a property that RAISES while in flight)
    stalled: bool             # held up by the other side in between: not a clean fitting sample
```

The endpoint logs each `Xfer` when it ends -- pysim's half of Rule 2.  pysim and RTL records join on
`(endpoint, index)`; the model is fitted on the feature point, as `waveflow.calib.timing_model`
already joins its two sides.

## Compatibility

- **The primitive is fully compatible.**  `begin` / `end` go into the stream's transfer primitive
  (`StreamIF._transfer`, `QueuedTransferIFSlave._take_burst`), and today's `get` / `write` become
  `begin` immediately followed by `end`.  Every existing pysim cycle count must be unchanged; if one
  moves, the refactor is wrong.
- **The API is not, in the end.**  `get_pipelined` / `write_pipelined` / `read_pipelined` sit beside
  the new form while poly proves it, then are retired (Stage 5): two ways to state a pipeline is the
  undiscoverability this file records.  Today they are used by about 18 framework modules (mem
  streams, queue and regbank views, the RF buffers, BRAM, SSR FFT, locked memory), 5 examples
  (`stream_inband`, `mm_fir`, `bram_access`, `vmac`, the RF ones through the framework) and about 12
  docs pages -- not a flag day.

## Stages

**Stage 1 -- the begun burst.**  Decide how a burst that has begun and not ended counts against the
reader's queue.  Today (`_admit_blocking`): a writer waits for room for `min(n, capacity)` and the
whole burst then lands at once; a burst larger than its queue is "one burst in flight at a time"; the
reader takes the burst *first* and only then retires its words (`_take_burst`), which is why chunking
deadlocks (PR #185).  The proposal to test: admission at the writer's `begin` (the first word enters
the FIFO then), the reader may take a begun burst at its own `begin`, and retires its words at its
`end`, after the writer's.  Also measure today's events per message on poly and markov -- the
baseline for Stage 2's cost.  Gate: the rule written down, with the deadlock case of PR #185 argued
through it.

**Stage 2 -- `Xfer`, `begin` / `end` in the stream primitive.**  `get` / `write` and every typed read
on top of them rebuilt on it.  Gate: every pysim cycle count unchanged (the fast suite's gates;
markov's and mm_fir's pysim numbers; the calibration keys), and the added events per message measured.

**Stage 3 -- `self.pipeline`, `Load`, `Compute`, `until`; poly migrated.**  The four markers logged by
the primitive.  Poly's `body` keeps its function and loses its formula (`tstart`, `t_out_start`,
`write_pipelined`).  Gate: poly bit-exact as before; its pysim timing within the existing tolerance of
cosim with the model seeded by hand.

**Stage 4 -- the markers at RTL, and the fit.**  The four markers per message from poly's cosim (or XSI
of the kernel alone), aligned by `(endpoint, index)`; `latency` and `fill_time` fitted over at least
three `nsamp` values with an interior point held out (`project-cycle-model-training`'s grid
discipline).  Gate: the fitted model predicts cosim at least as well as the hand-fitted
`proc_latency = 40`, with no number typed in.  `unit_harness.md` resumes after this.

**Stage 5 -- retire the old API.**  Migrate the users of `get_pipelined` / `write_pipelined` /
`read_pipelined`, then remove them and the extraction prohibition they caused
(`hwcodegen._check_not_pipelined`, `tests/hw/test_extract_pipelined_forbidden.py`).  Gate: no user
left; docs teach one construct.

## Deferred, written down

- **Several loads or stores per stage** -- the HLS loop that reads `in_stream1` and `in_stream2` each
  iteration.  The construct extends: `load=(...)` begins every load **concurrently** (chained
  `yield from`s would serialize what the loop interleaves), `latency` is measured from the latest
  first beat, the tail from the latest last beat.  Not now: load-compute-store first.
- **Block compute** (an FFT frame, a frame buffer): output starts after the *last* input, so
  `latency` is measured from `L.last`.  One anchor word on `Compute`; needed before an FFT migrates.
- **`m_axi` loads and stores** (`MMIFMaster.read_pipelined`): the same split in the memory-mapped
  primitive, where a message may be several bursts -- its markers then split RTL beats by pysim's
  word counts, as for an unframed stream.
- **Lowering `self.pipeline` in the extractor** to the pipelined HLS loop, closing point 3 below.
- **The silent case stays possible**: a plain `get` + `write` without `pipeline` still simulates as a
  sequence.  A unit-harness check could flag it per port (pysim's `S.first - L.first` far above the
  RTL's).

## The defect

Expressing a stream-processing loop's timing correctly requires `get_pipelined` /
`write_pipelined`. **No user — and no AI agent — has yet found this without manual intervention.**
It pre-dates the RF work: `examples/stream_inband` has the same trap, and the RF arc only made it
expensive enough to name.

## Why documentation cannot fix it

Three things compound, and the third is the one that closes the door.

**1. The failure is silent.** A plain `get()` in a loop simulates fine and produces correct *data*.
Only the rate is wrong, and rate is invisible without an RTL run to compare against. This is not
hypothetical — it is the mechanism that hid 72 dropped words in `rf_loopback` while pysim reported
zero, and 1695 of 4096 in `rf_capture`.

**2. The correct construct looks like the advanced option.** It is more verbose and returns
`(data, tstart)`, so it reads as a special case rather than the default a normal loop should use.

**3. It is forbidden exactly where most users write code.**
`waveflow/build/hwcodegen.py::_check_not_pipelined` raises `SynthesisError` for a pipelined op in any
**extracted** body:

> Pipelined stream operation 'get_pipelined' … Pipelined ops are only legal inside `@synthesizable`
> hook bodies (their C++ lowering requires hand-written pipelined loops with `#pragma HLS PIPELINE`).
> Refactor to call a hook that takes the stream as an argument and does the pipelined I/O internally.

`tests/hw/test_extract_pipelined_forbidden.py` enforces it.

So **the timing-fidelity path and the extracted-body path are mutually exclusive.** A user who
discovers `get_pipelined` and puts it in a synthesizable body is told to go hand-write C++. Any
amount of prose explaining the construct runs into that wall.

## What a real fix would have to do

Make the *default* correct, rather than documenting the exception. Two shapes, neither designed:

- **Lower the idiom.** Recognize a loop-over-stream in the extractor's vocabulary and lower it to a
  pipelined loop, so `for … get() … write()` means what it looks like it means.
- **Derive timing from structure.** Take the pysim rate model from the loop shape rather than from
  which method was called, so a plain `get()` in a loop is not silently rate-blind.

Anything smaller leaves the trap in place.

## Where it bites today

| site | symptom |
|---|---|
| `examples/stream_inband` | the original case; correct use requires knowing the construct exists |
| `examples/rf_loopback` `RfSampIngress.run_iter` | burst-granular twin, rate-blind; the hardware body is hand-written *because* of this |
| `examples/rf_capture` `RfCapIngress.run_iter` | same shape, and `fire_cycles` exists as a hand-declared patch for exactly this gap |

`fire_cycles` is worth noticing: it is a **declared constant standing in for a rate the model cannot
derive**. That it had to be invented is the clearest evidence the gap is real.

## Measured 2026-08-16: a hooked leaf's `run_iter` CAN use pipelined ops

The question was whether a `run_iter` on a module that overrides `kernel_task()` is exempt from the
prohibition. **It is — but by a different mechanism than "the extractor checks the hook", and that
distinction matters.**

| probe | result |
|---|---|
| `check(leaf, 'composite_kernel')`, plain body | `True` |
| `check(leaf, 'composite_kernel')`, `get_pipelined` body, **no hook** | `True` ← **the gate never extracts a leaf body** |
| `extract_kernel(comp)`, `get_pipelined` body, **hook overridden** | `SynthesisError` |
| `extract_kernel(comp)`, `get_pipelined` body, **no hook** | `SynthesisError` |
| `extract_kernel(comp)`, **today's shipped `RfSampIngress`** | `IndexError` |
| `composite_top_spec(RfSampPassThrough)`, ingress with `get_pipelined` | **OK** |

Three findings, in order of how much they should change behaviour:

**1. The build path permits it.** `composite_top_spec` — the real generator — succeeds with a
pipelined `run_iter` in `RfSampIngress`, because a leaf whose `kernel_task()` names a hand-written
header is never extracted. So the RF ingresses can get honest pysim twins today, and
`RfSampIngress`'s docstring claim that the word relay "has no pysim expression" is **false**.

**2. The exemption is unguarded.** `extract_kernel` does *not* consult the hook — it raises either
way. The exemption exists only because the composite generator never calls it for a hooked leaf. That
is a property of the code path, not a stated rule, so a change to `composite_gen` could withdraw it
silently. Anyone relying on it should pin it with a test.

It is already load-bearing: **today's shipped `RfSampIngress` body also fails `extract_kernel`**
(`IndexError`), so the current build already depends on never extracting it.

**3. `check(leaf, 'composite_kernel')` does not run gate 4 on the body.** A leaf with an
unambiguously illegal body returns `True`. The docstring says gate 4 "runs the real extraction";
for this combination it does not. Separate defect, worth its own look.

**Not established:** that a `get_pipelined` twin *models the word rate correctly*. Only that codegen
survives it. That is a simulation question and needs its own measurement.

## Measured 2026-08-16 (second pass): `get_pipelined` CANNOT express the RF ingress twin

Finding 1 above said the RF ingresses "can get honest pysim twins today" using `get_pipelined`.
**The mechanism is refuted; the goal is not.** `RfSampBufIngress` now has an honest, rate-modelling
twin — it just is not made of pipelined ops. Two independent reasons, both measured:

- **`get_pipelined(count=N)` zero-pads to N.** It unpacks through `read_array(shape=N)`, which
  forces the length, so it cannot read a burst whose size is not known in advance. This ingress has
  no such number: it reads *one word* in hardware, and in pysim the burst is the converter's block,
  which the module does not and should not know. Asking for a generous cap returns a padded array,
  not a short one.
- **Its `tstart` is the wrong anchor for a converter-fed port.** It is back-calculated as
  `now - (nwords-1) * fabric_period`, which assumes an II=1 **fabric-paced** producer. A converter
  delivers at `samp_rate / samp_per_word` words per second, far slower. Pacing from that anchor
  discounts `(nwords-1)` fabric cycles from every firing and moves the drop threshold from the
  *design's* capacity to the *port's* — which is precisely the confusion the design-capacity check
  exists to catch.

What works instead is one line: **charge the firing cost.** `yield self.timeout(nwords *
fire_cycles * clk.period)` after the burst. Both terms scale with the burst length, so the drop
threshold lands exactly on `samp_per_word * f_axis / fire_cycles` — the same number `check_rate`
refuses against — and the static check and the simulation now agree.

Measured on `examples/rf_samp_buf_rx` at 256 MSa/s, one sample per word (the configuration whose
first RTL run lost 1695 of 4096):

| twin | pysim `dropped` |
|---|---|
| burst-granular (the predecessor) | **0** |
| paced (shipped) | **1536 of 4096** |

pysim quantises to whole blocks — it drops an offer or takes it — so it under-reports against the
RTL's 1695 and cannot see loss *inside* a block period at all. That finer blind spot is real and
unchanged; it is `rf_loopback`'s 72-of-512 case, a different module.

**The exemption is now pinned**, as this file asked: `tests/hw/test_rf_samp_buf_twin.py` asserts both
that `composite_top_spec` succeeds and that `extract_kernel` raises on the same leaf, so the
exemption is demonstrably load-bearing rather than incidental — and separately that a `get_pipelined`
body in a hooked leaf still survives composite codegen, since a future body may want one.

## Measured 2026-08-17: what a loop INSIDE a task body actually costs

`plans/witness/task_loop/` — standalone, hand-written, no Waveflow involvement. Eight tops on
`xczu48dr-ffvg1517-2-e` at 4.0 ns: two body shapes (`ing` = stream-read → BRAM-write, the ingress;
`ply` = BRAM-read → stream-write, the player) × four loop shapes (1 word per firing, bounded `N=8`
and `N=64` at `PIPELINE II=1`, and `while (1)`). csynth for the II, xsim for the behaviour.

**The witness reproduces the shipped constants before it is asked anything new**: `ing_1` measures 2
cycles/word and `ply_1` 3, in csynth *and* RTL, which are exactly `RfSampBufIngress.fire_cycles` and
`RfSampBufPlayer.fire_cycles`. Independent code, same numbers.

### 1. `while (1)` is legal — accepted at II=1, with a warning

`WARNING: [XFORM 203-561] ... is an infinite loop`, on both shapes. It synthesizes, runs, sustains
one word per cycle indefinitely, and passes a ramp check. The report carries `TripCount = inf` and no
overall latency: Vitis cannot bound a firing that never ends, which is correct, and is why the
witness prints *unbounded* rather than inventing a cycles-per-firing.

### 2. A bounded pipelined loop emits CONTINUOUSLY — it does not store-and-forward

This is the belief that was load-bearing and untested, and it holds. Two independent measurements,
because either alone is weak:

* **Throughput.** Store-and-forward spends N cycles computing and N draining, so it cannot exceed
  0.5 words/cycle. Measured 0.956 at N=64 and 1.000 for `while (1)`.
* **Back-pressure.** `TREADY` held low 40 cycles; count BRAM reads during the stall. A body holding
  N buffered outputs would keep reading into them. Measured **1–3** — the pipeline stalls as a unit
  within its own depth. There is no hidden buffer.

Every beat is checked to be `previous + 1`, so a run that hit its throughput by dropping or
reordering could not have passed.

### 3. The loop boundary costs **3 cycles**, flat, independent of N

Read off the gap histogram and separated using the two N values — `period = a + b·N` with
`11 = a + 8b` and `67 = a + 64b` gives **b = 1, a = 3**. One word per cycle inside the loop; three
idle cycles at the boundary. As an inter-beat gap it reads 4 (3 idle, plus the cycle that would have
carried a beat).

| variant | cycles/word (RTL) | vs. today |
|---|---|---|
| `ing_1` / `ply_1` | 2.000 / 3.000 | 1.00x |
| `_n8` (both shapes) | 1.375 | 1.45x / 2.18x |
| `_n64` (both shapes) | 1.047 | 1.91x / 2.87x |
| `_w` (both shapes) | **1.000** | 2.00x / **3.00x** |

**The loop erases the difference between the two body shapes.** `ply_1` costs 3 against `ing_1`'s 2 —
the extra cycle is the BRAM read latency — and once pipelined both are identical at every N. The
shape that is more expensive today has the most to gain, which is the one that binds pattern B.

### The `latency + 1` convention does NOT extend to a looped body

This file's neighbours calibrate `fire_cycles = latency + 1`. That is right for a one-word body
(confirmed twice here) and **wrong by 2 for a looped one**: csynth reports latency 12 at N=8 and 68 at
N=64, so the convention predicts 13 and 69, while RTL measures 11 and 67. A plausible mechanism —
*not measured, so not asserted* — is that the runtime overlaps the next firing's fill with the current
one's drain at pipeline depth 2. The error is in the safe direction (pessimistic) but it is still an
error, and anyone deriving a looped body's rate from csynth latency should measure instead.

### What it means for the pattern-B ceiling

At `samp_per_word = 4` and `f_axis = 250 MHz` the port carries 1000 MSPS, and the player's design
ceiling is `4 · 250e6 / (cycles per word)`: **333 MSPS today**, 727 at N=8, 955 at N=64, **1000 with
`while (1)`** — at which point the *port* binds, not the body, and raising it needs a wider word or a
faster fabric rather than a better loop.

The boundary is not free at the ingress, though. Its 3 idle cycles are a window in which the task is
not reading, and the boundary port is 2 deep whatever the Python says (see this file's neighbour on
`depth=`), so per firing the ADC loses `max(0, 3·r − 2)` words at converter word rate `r`. At today's
`r = 0.25` that is nothing at any N; at `r = 1.0` it is one word per firing — 1.6% at N=64, 12.5% at
N=8. **`while (1)` never stops reading and loses nothing at any rate**, which is the argument for
building the ingress from it rather than from a bounded loop.

### Not measured

Resources, timing closure beyond csynth's estimate, a loop with real arithmetic in it (both bodies
are a move, not a computation), two looped tasks sharing a BRAM, and `while (1)` under reset — which
matters, because `adc_model.md` records that a task writing before it reads advances its state during
reset, and an infinite loop changes the shape of that question.

## Relationship to other plans

`adc_model.md`'s two-design-patterns section rests on this: pattern A forces a hand-written pipelined
body on every user precisely because of point 3, which is the main argument for pattern B being the
default.
