# Plan: timed phases inside a module -- load, compute, store, timed by events at its ports

**Status: PLANNED 2026-10-14; Stage 1 DONE 2026-10-14** (a decision record, no code: the
[Stage 1 record](#stage-1-record-2026-10-14)).  The defect was recorded 2026-08-16 (below, from
[The defect](#the-defect) on, kept as the evidence).  The design came out of the
`plans/unit_harness.md` discussion: a unit harness can only calibrate a module whose timing is stated
between events it can observe, and today's pipelined ops state it with arithmetic on times that never
happened.  `unit_harness.md` is **paused** until this plan's Stage 5 (the fit generated from poly's
declarations) works.

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

## The design in one paragraph

A module **declares** its timed phases once, beside its endpoints: a `TimedPhase` names its spans --
each a pair of endpoint events (`ep.first` / `ep.last`, the first and last beat of a message) -- the
features its duration depends on, and the family of its timing model.  Its **body** reads and writes
its endpoints as it always has, and between them calls the phase's own `timeout(...)` (sequential)
or `pipeline(...)` (pipelined), which wait the predicted durations and record the phase.  From the
declarations alone the framework generates the RTL probes, the pysim logs and the calibration corpus,
and fits the model.  The endpoints appear in both the declaration and the body -- the body stays
readable -- and the framework **checks** that the transfers the body performed are on the endpoints
the declaration names, so the two cannot drift.

## The declared layer

```python
@dataclass(frozen=True)
class TimingEvent:
    """A marker on one of the module's own endpoints: the first or last beat of each message."""
    ep: InterfaceEndpoint
    edge: Literal["first", "last"]
    channel: str | None = None      # an m_axi endpoint: which AXI channel ("R", "W"); a stream: None

@dataclass(frozen=True)
class TimedPhase:
    """Rule 1: spans between endpoint events, the features known before the first, one model."""
    name: str
    spans: dict[str, tuple[TimingEvent, TimingEvent]]   # target name -> (start event, end event)
    features: tuple[str, ...]
    tm: TimingModelFamily = LinearTiming()               # the law's SHAPE; never a number or a path
```

- **Every endpoint provides its events**: `self.s_in.first`, `self.s_in.last` are `TimingEvent`s.  A
  phase that names them declares them; a module's events are the union of its phases'.
  `add_timing_event(ep, edge)` stays for an event to observe with no phase on it yet.
- **An event names an operation on an endpoint, never a role.**  "Load" and "store" are roles a
  pipeline gives transfers, not properties of an endpoint, so there is no `s_in.load.first`:
  - an endpoint with **one** operation (a stream slave or master, a queue view's port) has `first` /
    `last` -- the common, readable case;
  - an endpoint with **two** (an `m_axi` master that reads and writes, a register bank) has
    `read_first` / `read_last` / `write_first` / `write_last`, each fixing its AXI channel (R for a
    read's beats, W for a write's), and its bare `first` **raises**, naming them.  Not
    `m_mem.read.first`: `read` is already a method of `MMIFMaster`;
  - a **composite** endpoint uses its sub-endpoints: `self.m_u.fwd_ep.first`.
- **An event does not name a message.**  `self.s_in.first` is "the first beat of *a* transfer on
  `s_in`" -- poly's `s_in` carries both its header and its samples.  The message is bound at run time
  (the phase takes its start endpoint's most recent transfer; `pipeline` uses the request it is given),
  which is what lets one declaration serve every message.
- **Registered like an endpoint.**  Construct, assign, register -- `add_timing_phase` returns `None`,
  as `add_endpoint` and `add_comp` do, so there is one way to write it, and phases can share a loop:

  ```python
  self.compute_phase = TimedPhase(
      "compute", spans={"compute_time": (self.s_in.last, self.m_out.first)}, features=("n",))
  self.add_timing_phase(self.compute_phase)
  ```

  Registering sets the phase's **owner** (as `add_endpoint` sets `endpoint.comp`): through it the
  phase reaches the simulation clock and the module's logs, so it can time the body itself.
- **The phase times; its model predicts.**  `phase.timeout(...)` and `phase.pipeline(...)` run in a
  simulation; the learned function is a separate member, `phase.tm` (`predict`, `fit`), usable in a
  calibration step with no simulator -- as `FitTimingStep` uses models today.
- **Features are keyword arguments**, checked against the declared names: a missing or misspelled
  feature fails at the call, never silently fits the wrong thing.
- **A phase and its model are one object.**  There is no phase without a model (by Rule 1 a phase *is*
  what a model learns; before a fit it predicts from its seed) and, once migrated, no model without a
  phase.  One model may predict several spans that share features -- the pipeline's two -- so the
  spans are the model's **targets**, named: `predict` returns named durations and a fit report says
  "latency: +-2 cycles", not "target 0".  This is the slot `TimingModel` already reserves
  (`num_targets > 1`, "a delay between internal stages", raising today for want of a measurement).
- **`tm=` names the family, not an instance.**  The parameters live in the calibration store under
  (module identity, phase name), loaded lazily, seeded before any fit.  The declaration says *what* is
  timed and *what shape* its law has; the store says how well it is known.  So today's required
  `component=` / `calib_dir=` become derived, and the same class in two configurations cannot share a
  directory by accident.
- **The target is the span's duration, not a residual.**  The residual form (`rtl - pysim + current_dly`) exists because a firing's pysim span mixes the model's delay with whatever else pysim
  charged.  Here an unstalled span's pysim duration *is* the prediction.
- **A span uses only the module's own boundary endpoints.**  A span from module A's port to module B's
  belongs to the composite holding both, declared there: leaves calibrate their phases, composites
  declare their own.  A point inside an HLS body cannot be a span by design -- if a phase cannot be
  stated between port events, the module is split.

## The runtime layer

- **`Xfer`** -- one message on one endpoint, logged by the endpoint's transfer primitive, **only for
  endpoints that carry a declared event** (an untimed endpoint costs nothing):

  ```python
  @dataclass
  class Xfer:
      ep: InterfaceEndpoint     # whose transfer (the port name for alignment)
      index: int                # the k-th message on that endpoint, counted by the primitive
      data: Any                 # load: the words, typed if the request asked for a type; store: what was written
      nwords: int
      first: float              # first beat (set by begin)
      last: float               # last beat (set by end; a property that RAISES while in flight)
      stalled: bool             # held up by the other side in between: not a clean fitting sample
  ```
- **A phase record** -- one occurrence of a phase, written by `phase.timeout` / `phase.pipeline`: `(phase, per span: start (ep, index, edge) and end (ep, index, edge), features, predicted, stalled)`.  It holds
  *references* to `Xfer`s, not times.

## Pattern 1: sequential load, compute, store

The input is complete before the compute starts, and the output starts after it: one span.

```python
# declared once, in __post_init__
self.compute_phase = TimedPhase(
    "compute", spans={"compute_time": (self.s_in.last, self.m_out.first)}, features=("n",))
self.add_timing_phase(self.compute_phase)

# the body, per message
x = yield from self.s_in.get_array(Float32, n)          # returns at s_in.last
y = compute(x)
yield self.compute_phase.timeout(n=n)                   # waits compute_time; opens the record
yield from self.m_out.write(y)                          # its first beat closes the record
```

`timeout` mirrors SimPy's `self.timeout(dt)`: its bookkeeping runs when it is called, and it returns
one ordinary event, so a plain `yield`.  At the call it takes each span's start from its declared endpoint's **most recent** transfer (`s_in`'s,
just completed), waits the predicted duration, and binds the span's end to the **next** transfer on its
declared end endpoint (`m_out`), then returns `env.timeout(duration)`.  If `s_in` had not just
completed a transfer, or the next transfer lands on another endpoint, it raises, naming the phase and
both endpoints.  Today's `get` / `write` are
unchanged here: the sequential pattern needs no `begin` / `end`.

## Pattern 2: pipelined load, compute, store

The HLS loop reads a word, computes, writes a word, at one per II: output starts a latency after the
**first** input word, and the input and output overlap.  Two spans:

```python
# declared once
self.compute_phase = TimedPhase(
    "compute", spans={"latency":   (self.s_in.first, self.m_out.first),
                      "fill_time": (self.m_out.first, self.m_out.last)},
    features=("n",))
self.add_timing_phase(self.compute_phase)

# the body, per message
y = yield from self.compute_phase.pipeline(fn=lambda x: array(Float32, poly_eval(c, x)),
                                           load=self.s_in.req(Float32, n), store=self.m_out, n=n)
```

`pipeline` is a `yield from`: it is a sequence of waits, not one event -- the rule SimPy already
follows (a method returning one event is `yield`, one running several steps is `yield from`).

**Streaming vs block is which event a span starts at** -- `s_in.first` (pipelined) or `s_in.last`
(Pattern 1, an FFT frame) -- so no separate anchor flag is needed.  `pipeline` checks that `load`'s
endpoint starts `latency` and `store`'s ends it and carries `fill_time`, then runs:

```python
lx = yield from load.begin()           # L.first; lx.data is already here
y = fn(lx.data)
latency, fill_time = phase.tm.predict(**feats)
yield self.timeout(latency)            # L.first + latency   (relative: nothing waited since L.first)
sx = yield from store.begin(y)         # S.first, when the bus allows (room is taken at end)
yield self.timeout(fill_time)          # S.first + fill_time (relative: nothing waited since S.first)
yield from load.end(lx)                # L.last
yield self.until(lx.last + latency)    # the tail -- the last output needs the last input; no-op if past
yield from store.end(sx)               # S.last: the burst is delivered
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
- **`load` is a request** (`self.s_in.req(type, count)`), not a running `get_array(...)`, which only
  returns at the end: `pipeline` drives the transfer's two halves itself.  A body of another shape
  uses the same `begin` / `end` directly.
- **Several pipelines may share endpoints** -- poly has two per message on the same ports, header ->
  response and samples -> results: two declared phases.  Each endpoint's `Xfer` index counts every
  transfer on it; which transfers make up an occurrence comes from the phase record, never from index
  arithmetic.

## What is generated from the declarations

1. **RTL probes.**  Walk the modules in the cut -- or the children inside it, for observation in place
   -- and collect their `TimingEvent`s.  Each such endpoint gets `beat`, `last`, `stall` (`VALID && !READY`) and one new probe kind, `starve` (`READY && !VALID`); the existing `Probe` -> net
   resolution makes them `ProbePin`s, inside a sub-top if need be (`probe_home`).  Nobody names a net.
2. **RTL `Xfer`s.**  Each endpoint's probe runs decoded into messages -- by TLAST on a framed stream,
   by pysim's `Xfer.nwords` on an unframed one or a multi-burst `m_axi` transfer -- into the table
   pysim logs: `(ep, index, first, last, stalled)`.
3. **The corpus, joined mechanically: pysim supplies the pairing and the features, RTL supplies the
   times.**  For each pysim phase record, each span's start and end `(ep, index)` are looked up in the
   RTL `Xfer`s.  Legal because Rule 1 puts the features before the first event; it needs both sides to
   carry the same message sequence per port, so **functional equivalence is a precondition of timing
   calibration** (the bit-exact check runs first anyway).  This replaces the VCD-firing path
   (`ExtractBurstsStep` -> `CollectTimingStep`); `FitTimingStep` and `TimingModel` stay, and
   `discover_timing_models` becomes a walk over declared phases.
4. **The sweep's requirements.**  The declared features say what a sweep must vary, so the grid
   discipline (at least three values a feature, an interior point held out) is checked mechanically.
   What cannot be generated is a scenario that *produces* given feature values: the module or example
   supplies a "workload for these features" hook -- the one hand-written piece of a calibration.

## Compatibility

- **The primitive is fully compatible.**  `begin` / `end` go into the stream's transfer primitive
  (`StreamIF._transfer`, `QueuedTransferIFSlave._take_burst`), and today's `get` / `write` become
  `begin` immediately followed by `end`.  Every existing pysim cycle count must be unchanged; if one
  moves, the refactor is wrong.
- **`TimingModel(component=..., calib_dir=...)` keeps working** for its current users
  (`StreamTimingModel` on the mem streams, the Vitis FFT model) until each is migrated to declared
  phases in Stage 6.
- **The pipelined API is not kept, in the end.**  `get_pipelined` / `write_pipelined` /
  `read_pipelined` sit beside the new form while poly proves it, then are retired (Stage 6): two ways
  to state a pipeline is the undiscoverability this file records.  Today they are used by about 18
  framework modules (mem streams, queue and regbank views, the RF buffers, BRAM, SSR FFT, locked
  memory), 5 examples (`stream_inband`, `mm_fir`, `bram_access`, `vmac`, the RF ones through the
  framework) and about 12 docs pages -- not a flag day.

## Stages

**Stage 1 -- the begun burst.  DONE 2026-10-14.**  How a burst that has begun and not ended counts
against the reader's queue, with today's events per message as the cost baseline.  The answer and the
evidence are the [Stage 1 record](#stage-1-record-2026-10-14): a begun burst holds **no** room;
admission stays where it is today.

**Stage 2 -- `Xfer`, `begin` / `end` in the stream primitive.**  As the Stage 1 record decides: the
stamp FIFO beside `data_buffer`, the pending list, the writer's and the reader's `begin` / `end`;
`get` / `write` and every typed read on top of them rebuilt on it.  Gate: every pysim cycle count
unchanged (the fast suite's gates; markov's and mm_fir's pysim numbers; the calibration keys); a plain
`get` / `write` adds **no** SimPy event (measured as in the Stage 1 baseline); the stamps reproduce
the Stage 1 table (a stamp equals the old back-calculation exactly on the "agree" reads).

**Stage 3 -- the declared layer.**  `TimingEvent` (`ep.first` / `ep.last`, and the qualified names
on two-operation endpoints), `TimedPhase`, `add_timing_phase` (returns `None`; sets the owner), the
model family and its store lookup; `Xfer`s logged for declared endpoints; `phase.timeout` (Pattern 1)
with its endpoint check and its feature-name check.  Gate: unit tests of the check (a phase whose body
uses the wrong endpoint raises, naming both), and an undeclared endpoint logs nothing.

**Stage 4 -- `phase.pipeline` (Pattern 2), `req`, `until`; poly migrated.**  Poly declares two phases
(header -> response, Pattern 1; samples -> results, Pattern 2); its `body` keeps its function and loses
its formula (`tstart`, `t_out_start`, `write_pipelined`).  Gate: poly bit-exact as before; its pysim
timing within the existing tolerance of cosim with the models seeded by hand.

**Stage 5 -- generated probes, RTL `Xfer`s, and the fit.**  The probes generated from poly's
declarations; its cosim (or XSI of the kernel alone) decoded into `Xfer`s; the corpus joined; the
spans fitted over at least three `nsamp` values with an interior point held out
(`project-cycle-model-training`'s grid discipline).  Gate: **the fit is generated from poly's
declarations alone**, and predicts cosim at least as well as the hand-fitted `proc_latency = 40`, with
no number typed in.  `unit_harness.md` resumes after this.

**Stage 6 -- retire the old API.**  Migrate the users of `get_pipelined` / `write_pipelined` /
`read_pipelined` and the undeclared `TimingModel`s, then remove the old forms and the extraction
prohibition they caused (`hwcodegen._check_not_pipelined`,
`tests/hw/test_extract_pipelined_forbidden.py`).  Gate: no user left.

**Stage 7 -- docs.**  One guide page on timed phases -- the two rules, the declaration, Pattern 1 and
Pattern 2 with poly as the running example, what is generated from a declaration, and how a phase is
calibrated -- beside `docs/guide/timing_model/` (whose `streaming.md` teaches `get_pipelined` today
and is rewritten to point at it).  Poly's pages (`docs/examples/stream_inband/`, its cosim-timing page
above all: the hand-fitted `proc_latency = 40` becomes the fitted model) are rewritten when Stage 5
lands; the other pages that teach the pipelined ops (about 12, see [Compatibility](#compatibility))
move to the new construct as Stage 6 migrates their code.  Flow steps are named and linked, never
numbered.  Gate: no docs page teaches `get_pipelined` / `write_pipelined` / `read_pipelined`; the
docs snippet harness runs the guide page's code; the markdown integrity checks pass.

**Stage 8 -- the speed check.**  When everything above has landed, re-run the scripts behind
`docs/evaluation/sim_speed/` -- `python -m examples.mem_copy.mem_copy_runtime` and
`python -m examples.ssr_fft.ssr_fft_runtime --rtl` -- with the page's method (best of N, alternating
old and new in fresh processes, outcomes compared, not just bits).  The cost model there is pysim time
= the model's time + SimPy events x a per-event cost, so the question is how many events this arc
added per transaction and what each costs.  Gate: the per-copy and per-frame numbers re-measured and
the pages updated with them -- whatever they are; an increase is reported with its cause, not hidden.

## Stage 1 record (2026-10-14)

**What pysim does today** (`waveflow/hw/interface.py`).  A writer takes the slave's `bus` (one
resource per slave: writers to one slave are serialized), waits the transfer, `(latency_init + n)`
cycles, **then** waits for room (`_admit_blocking`: `min(n, capacity)` words, the rest parked in
`ntx`), puts the whole burst into `data_buffer` and releases the bus.  The reader's `_take_burst`
takes the burst and only then retires its words, freeing the room.  A burst is one object, delivered
at once.  `mm_queue` reads the buffer's contents (it sums the bursts' lengths for its occupancy and
interrupt), so a buffer item must stay a plain word array.  Both producers (`write` and `offer`) hold
the slave's bus while they put.

**The cost baseline** -- SimPy events (`env._eid`) and stream transfers, counted on whole runs
(`plans/witness/pipelined_ops/event_baseline.py`, run from the repo root):

| run | events | stream writes | events / write | messages | events / message |
|---|---|---|---|---|---|
| poly `timing` (w32) | 51 | 5 | 10.2 | 3 | 17.0 |
| poly `multi_data` (w32) | 67 | 13 | 5.2 | 7 | 9.6 |
| markov `direct` (4 jobs x 300) | 343 | 80 | 4.3 | 4 | 85.8 |
| markov `mm` (4 jobs x 300) | 806 | 112 | 7.2 | 4 | 201.5 |

**What today's back-calculation gets wrong.**  The writer knows when its transfer starts, so the first
word reaches the reader at `start + (latency_init + 1)` cycles and the last at `start + (latency_init +
n)`.  `get_pipelined` instead back-calculates `first = now - (n - 1)` cycles when the read returns.
Every read on the runs above, classified (`plans/witness/pipelined_ops/stamp_check.py`):

| run | agree | burst sat in the buffer before the read | delivery held by back-pressure |
|---|---|---|---|
| poly `timing` | 4 | 1 | 0 |
| poly `multi_data` | 8 | 3 | 2 |
| markov `direct` | 51 | 29 | 0 |
| markov `mm` | 64 | 41 | 5 |

The back-calculation is right on about 60% of reads.  On the middle column it dates the first word
**after** it arrived, and pysim reads a buffered burst in **zero** time where the RTL reads it at a word
a cycle.  This is the arithmetic the design removes, measured.

**The decisions.**

1. **Every burst carries stamps: its first word's arrival (`start + (latency_init + 1)` cycles), its
   nominal last (`start + (latency_init + n)`), and its delivery (when admission let it in).**  They
   live in a FIFO beside `data_buffer` (not on the item: `mm_queue` sums the items), appended under the
   bus hold that already serializes the slave's producers, so the FIFO cannot fall out of order with the
   buffer.  Both producers stamp (`offer` too).  Stamps are plain floats: no SimPy event.
2. **A begun burst holds no room.**  Admission stays exactly where it is -- at the writer's `end`, after
   the transfer time -- because moving it to `begin` changes today's cycle counts (a full queue would
   then delay the transfer's start rather than its delivery).  The plan's earlier proposal (admission at
   `begin`) is withdrawn.
3. **A pending list beside the buffer makes a burst visible at the writer's `begin`.**  The writer
   posts it (words and stamps) when it takes the bus and removes it at delivery.  `get_nb` and
   `mm_queue` see only delivered bursts, as today.
4. **The writer's `begin` / `end`.**  `begin` takes the bus and posts the pending burst (`S.first` = the
   transfer's start); `end` waits the rest of the transfer, admits (waiting for room), delivers, and
   releases the bus (`S.last` = delivery).  `write` = `begin` then `end` at once: the same waits in the
   same order as today.  The writer's record is `stalled` when admission had to wait.
5. **The reader's `begin` / `end`.**  `begin` takes the oldest delivered burst, else the oldest pending
   one, else waits for one to be posted -- it never waits for room -- with `L.first = max(first-word
   stamp, when the reader asked)`; the words are in hand.  `end` waits for that burst's delivery and
   retires its words.  `get` = `begin` then `end` at once: it returns at delivery, as today.
6. **Reading a buffered burst costs its beats only where a phase declares it -- decided with the user,
   2026-10-14.**  A pipelined load's `end` waits until `max(delivery, L.first + (n - 1) cycles)`, so a
   burst that sat in the buffer is read at a word a cycle, as the RTL reads it; a plain `get` does the
   same **only on an endpoint that starts a declared Pattern 1 span** -- declaring that phase says the
   body really is sequential.  Every other `get` keeps today's zero-time read.  Charging everywhere was
   measured and rejected (`plans/witness/pipelined_ops/charge_cost.py`):

   | run | events today | charged everywhere | cycles today | charged everywhere |
   |---|---|---|---|---|
   | poly `timing` | 51 | 52 (+2%) | 150 | 155 |
   | poly `multi_data` | 67 | 71 (+6%) | 234 | 255 |
   | markov `direct` | 343 | 368 (+7%) | 1811 | 2082 |
   | markov `mm` | 806 | 842 (+4.5%) | 1926 | 2200 |

   The events are the smaller reason (one wait per buffered read, 2-7% more).  The deciding one:
   markov's pysim moves from 1926 to 2200 cycles against an RTL 1870 -- from +3% to +18%, outside its
   5% gate, **away** from RTL.  Its kernels are pipelined loops in hardware, written read-then-compute
   in pysim with the overlap folded into hand-fitted per-chunk overheads that assume a zero-time read;
   charging the read counts it twice.  A charge is right only for a body that is sequential, which is
   what the declaration states.  So the opt-in costs existing code **no** event and no cycle, and a
   migrated module pays one wait per buffered read on the endpoints it declared.
7. **No new wait on the write path, so no new deadlock.**  PR #185's deadlock was a producer waiting
   for room that only a consumer retiring a *delivered* burst could free, while the consumer waited for
   a burst the producer never delivered.  Here a pending burst holds no room, so nothing but its own
   reader ever waits on it; the reader's `begin` waits only for a burst to exist, and its `end` only for
   that burst's delivery -- which needs room freed by the reader's *previous* `end`, already done (one
   reader per slave, transfers in order).  A burst larger than its queue still waits for an empty queue
   at admission, and the queue is empty once the previous burst is retired: the "one burst in flight"
   row is unchanged.

**Events per message, expected.**  `get` / `write`: none added (stamps and the pending list are plain
lists).  `phase.pipeline` against today's `get_pipelined` + timeout + `write_pipelined` (about 3
events): the two predicted timeouts, the store's remaining transfer, and at most a wait for the load's
delivery and one for the tail -- about 3 more a message.  Stage 2 measures it.

## Deferred, written down

- **Several loads or stores per stage** -- the HLS loop that reads `in_stream1` and `in_stream2` each
  iteration.  The construct extends: several `load=` requests begun **concurrently** (chained
  `yield from`s would serialize what the loop interleaves), `latency` measured from the latest first
  beat, the tail from the latest last beat.  Not now: load-compute-store first.
- **`m_axi` loads and stores** (`MMIFMaster.read_pipelined`): the same split in the memory-mapped
  primitive, where a message may be several bursts -- its markers then split RTL beats by pysim's word
  counts, as for an unframed stream; a `TimingEvent` names its AXI channel.
- **Lowering `phase.pipeline` in the extractor** to the pipelined HLS loop, closing point 3 below.
- **The silent case stays possible**: a plain `get` + `write` with no phase still simulates as a
  sequence with no compute time.  A unit-harness check could flag it per port (pysim's span far below
  the RTL's).

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

| site                                                | symptom                                                                                |
| --------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `examples/stream_inband`                          | the original case; correct use requires knowing the construct exists                   |
| `examples/rf_loopback` `RfSampIngress.run_iter` | burst-granular twin, rate-blind; the hardware body is hand-written *because* of this |
| `examples/rf_capture` `RfCapIngress.run_iter`   | same shape, and `fire_cycles` exists as a hand-declared patch for exactly this gap   |

`fire_cycles` is worth noticing: it is a **declared constant standing in for a rate the model cannot
derive**. That it had to be invented is the clearest evidence the gap is real.

## Measured 2026-08-16: a hooked leaf's `run_iter` CAN use pipelined ops

The question was whether a `run_iter` on a module that overrides `kernel_task()` is exempt from the
prohibition. **It is — but by a different mechanism than "the extractor checks the hook", and that
distinction matters.**

| probe                                                                          | result                                                    |
| ------------------------------------------------------------------------------ | --------------------------------------------------------- |
| `check(leaf, 'composite_kernel')`, plain body                                | `True`                                                  |
| `check(leaf, 'composite_kernel')`, `get_pipelined` body, **no hook** | `True` ← **the gate never extracts a leaf body** |
| `extract_kernel(comp)`, `get_pipelined` body, **hook overridden**    | `SynthesisError`                                        |
| `extract_kernel(comp)`, `get_pipelined` body, **no hook**            | `SynthesisError`                                        |
| `extract_kernel(comp)`, **today's shipped `RfSampIngress`**          | `IndexError`                                            |
| `composite_top_spec(RfSampPassThrough)`, ingress with `get_pipelined`      | **OK**                                              |

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

What works instead is one line: **charge the firing cost.** `yield self.timeout(nwords * fire_cycles * clk.period)` after the burst. Both terms scale with the burst length, so the drop
threshold lands exactly on `samp_per_word * f_axis / fire_cycles` — the same number `check_rate`
refuses against — and the static check and the simulation now agree.

Measured on `examples/rf_samp_buf_rx` at 256 MSa/s, one sample per word (the configuration whose
first RTL run lost 1695 of 4096):

| twin                             | pysim `dropped`      |
| -------------------------------- | ---------------------- |
| burst-granular (the predecessor) | **0**            |
| paced (shipped)                  | **1536 of 4096** |

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

| variant                | cycles/word (RTL) | vs. today               |
| ---------------------- | ----------------- | ----------------------- |
| `ing_1` / `ply_1`  | 2.000 / 3.000     | 1.00x                   |
| `_n8` (both shapes)  | 1.375             | 1.45x / 2.18x           |
| `_n64` (both shapes) | 1.047             | 1.91x / 2.87x           |
| `_w` (both shapes)   | **1.000**   | 2.00x / **3.00x** |

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
