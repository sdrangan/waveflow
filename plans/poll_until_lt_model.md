# Design proposal: an LT polling-overhead model — `m_mem.poll_until(...)`

**Status: steps 1–4 IMPLEMENTED (2026-06-21); step 5 gated/out of scope.** The Phase-0 design
pass is done and the four decisions (D1–D4) are settled. The sim-side LT model (the `PollCond`
value type, `MMIFMaster.poll_until`, the per-bus poller registry + occupancy derating in the
crossbar, and the `AXIMMQueue.get` rebuild) is landed — see "Implementation status (steps 1–4
landed)" below. Step 5 (the `@synthesizable` twin) stays blocked on the condition-IR
rhs-as-runtime-var extension. The `vmac-top-autosynth` prereq is merged on `main`.

## Motivation

Waveflow has no model for the cost of **polling**. A master that polls a memory location every
N cycles (e.g. a queue consumer watching the ring tail) both (a) steals bus bandwidth with its
poll reads and (b) incurs a discovery latency between the watched event becoming true and the
next poll observing it. Today this is unmodeled; VMAC band-aids it with a coarse structural
`poll_cycles=64` that merely shifts dequeue times without representing either cost. The idea
below models both costs in **O(transactions)** rather than simulating every poll cycle.

## The primitive

A master endpoint method:

```python
res = yield from m_mem.poll_until(addr, cond, poll_interval)   # poll_interval in CYCLES
```

Blocks until `cond(read(addr))` is true, returning the satisfying read value. Conceptually the
master reads `addr` every `poll_interval` cycles and checks `cond`. The sim does NOT actually
loop every `poll_interval` cycles; it applies the aggregate model below.

This is the loosely-timed twin of the hand-written C++ ring-poll already in
`waveflow/build/aximm_queue_impl.tpp` (`while (head==tail) tail = gmem[...]`). The queue
dequeue would be expressed as `poll_until(tail_addr, lambda t: t != head, poll_interval)`.

## The LT model (with refinements baked in)

Two distinct costs, modeled separately:

**1. Bandwidth steal (throughput).** Each active poller `i` consumes a fraction `d / poll_interval[i]`
of bus beats, where `d` is the poll read's beat-occupancy (**default `d=1`, but parameterize**).
Per shared bus/slave, sum over the *currently active* pollers:

```
ov = Σ d_i / poll_interval[i]
```

Real (non-poll) transactions on that bus have their **occupancy (per-word) component** — NOT
the fixed init/address latency — stretched by `1/(1-ov)` (effective bandwidth = `1-ov`). This
reuses the existing `latency_init` vs `latency_per_word` split: derate only the per-word part.

**2. Discovery latency.** After `cond` becomes true, add the event-to-next-poll gap. Default
**deterministic mean** `(poll_interval-1)/2`; optional **seeded-stochastic** `U[0, poll_interval-1]`.

### Refinements (these are the non-obvious correctness points)

1. **Determinism first.** The discovery delay must default to the deterministic mean, not bare
   randomness — this project relies on reproducible timelines (committed `sim_timeline.json`
   baselines, the byte-identical "metrics hold" gate, the calibration regression). A stochastic
   mode is opt-in and **seeded from the `Simulation`**. Never unseeded `random`.
2. **`ov ≥ 1` diverges — clamp and warn loudly.** `1/(1-ov)` blows up when polling alone
   exceeds bus bandwidth (a polling-bound config; e.g. a 1-cycle poll is `ov=1`). Clamp
   `ov < 1` and emit a clear warning rather than producing garbage timing. (Nice property: this
   model flags the exact 1-cycle-poll mistake `poll_cycles=64` was added to avoid.)
3. **`ov` is time-varying and per-bus.** Each `poll_until` call is a finite active interval;
   track the active-poller set and evaluate `ov` at each transaction's time, not once. Sum `ov`
   **per shared slave/bus** (the crossbar knows the topology) — independent ports must not
   derate each other.
4. **Derate occupancy, not init latency** (see model point 1).
5. **Parameterize the poll beat-cost `d`** (default 1).

## Where it lives

- `MMIFMaster.poll_until(addr, cond, poll_interval)` — registers the master as an active
  poller (adds to the bus's `ov`) for the wait's duration; on `cond` true, unregisters and
  applies the discovery delay; returns the value.
- The **crossbar/slave** (`AXIMMCrossBarIF` / the MM slave) owns the active-poller registry and
  applies the `1/(1-ov)` occupancy derating to each transaction it times — it has the global,
  per-bus view that the derating needs.

## Synthesizable twin

`poll_until` is also a legitimate hardware primitive (the C++ ring-poll). Make it
`@synthesizable` with the same shape as `AXIMMQueue.get`, but **restrict `cond` to the
lowerable subset** the extractor already supports (`==` / `!=` vs a value — the condition-IR).
The Python predicate is the sim form; the C++ is the poll loop. This generalizes the
`AXIMMQueueGetStmt` hook into a reusable polling primitive.

## VMAC integration (the payoff)

- Rebuild `AXIMMQueue.get`'s blocking poll on `poll_until` (sim) over the same ring-poll hook
  (synth). Retire the `poll_cycles=64` band-aid: a poll interval now *means* something — an
  aggressive poll shows up as derated bus throughput, not just shifted dequeue times, and the
  calibration can reflect polling cost.
- Expect the queue-sim headline (drain time, occupancy) to **shift** when this lands — record a
  new baseline; the per-command invariants (rho, ab_eq 16/32, II-driven latency) should hold.

## Resolved decisions (Phase-0 design pass — 2026-06-21)

The four open decisions are resolved as follows; these are now the implementation contract.

**D1 — Default discovery delay: deterministic mean; stochastic DEFERRED.** v1 implements only
the deterministic mean `(poll_interval-1)/2`. The API reserves a `discovery="mean"` parameter
slot so a seeded-stochastic mode is purely additive later. Stochastic is *not* exposed in v1:
it would require adding a seed/rng to `Simulation` (which has none today —
`waveflow/simulation/simulation.py`) and threading it into the crossbar, and the whole
verification spine (byte-identical `sim_timeline.json`, the "metrics hold" gate, the
calibration regression) is deterministic. No current consumer needs randomness. When stochastic
lands it MUST draw from a `Simulation`-derived seed — never unseeded `random`.

**D2 — Saturation `ov ≥ 1`: clamp-and-warn.** A real back-pressure model would make pollers
occupy actual bus beats in the SimPy `Resource`, defeating the O(transactions) design goal.
Instead clamp `ov` to a ceiling just under 1 (default `0.99` → a visible ~100× per-word stretch
that unmistakably reads as "polling-bound") and emit **one loud warning per bus per saturation
onset** (not per-transaction — avoid log spam; warn on the transition into saturation). This is
the property the model is meant to expose: it flags the exact 1-cycle-poll mistake that
`poll_cycles=64` was bolted on to dodge.

**D3 — Poll beat-cost `d`: per-call parameter, default 1.** Expose `poll_beat_cost: int = 1`
(beats of per-word bus occupancy charged per poll read). Default 1 matches a single-word poll;
a user who wants the FULL-bus address phase counted can raise it. The bandwidth-steal sum stays
`ov = Σ poll_beat_cost_i / poll_interval[i]`.

**D4 — `cond` API: a single restricted `Cond` object usable by both sim and synth.** `cond` is a
small declarative value (`PollCond`/condition-IR), op ∈ {`==`, `!=`}, rhs a literal **or a prior
runtime-read value**. Sim evaluates `cond.eval(read_value)`; the extractor lowers the SAME object
to the C++ poll loop. One source of truth — no sim/synth drift, which the project has been bitten
by before. Sim expressiveness is intentionally limited to `==`/`!=` vs a value, which is exactly
what real polls are (`tail != head`). **Implementation cost to flag:** the existing condition-IR
lowers only `==`/`!=` vs *constants* (see the deferred condition-IR note); the queue dequeue
polls `tail != head` where `head` is a runtime-read local, so the condition-IR must be extended
so rhs can be a runtime variable, not just a literal. That extension is a prerequisite for the
synthesizable twin (it is NOT needed for the sim-only `Cond.eval` path, so the LT model can land
first and the synth lowering follow).

### Derating mechanics (grounded in the current latency model)

A FULL read in `AXIMMCrossBarIF.read` is `latency_init` (request leg) + slave `rx_read_proc` +
`(latency_read_return + nwords)` (return leg). The **only** per-word/occupancy term is `nwords`;
`latency_init` and `latency_read_return` are fixed init/address latency. "Derate occupancy, not
init" therefore means: stretch **only the `nwords` term** by `1/(1-ov)` —
`ret_dly = (latency_read_return + nwords/(1-ov)) / clk.freq` — leaving both fixed legs untouched.
The same rule applies to the FULL write's `nwords` term in `cycles = latency_init + nwords`.

### Implementation ordering implied by the resolutions

1. `Cond`/`PollCond` value type with `.eval(value)` (sim) — op ∈ {==,!=}, rhs literal or HwVar.
2. `MMIFMaster.poll_until(addr, cond, poll_interval, *, poll_beat_cost=1, discovery="mean")` —
   registers/unregisters the master as an active poller on its bound bus and applies the mean
   discovery delay on `cond` true.
3. Active-poller registry + per-bus `ov` + per-word derating in `AXIMMCrossBarIF` (the crossbar
   has the per-bus topology view), with the `ov<1` clamp-and-warn.
4. Rebuild `AXIMMQueue.get`'s blocking poll on `poll_until`; retire `poll_cycles=64`; record the
   new (expected-to-shift) queue-sim baseline, asserting the per-command invariants still hold.
5. (Later, gated on the condition-IR rhs-as-runtime-var extension) make `poll_until`
   `@synthesizable` and lower the `Cond` to the C++ ring-poll, generalizing `AXIMMQueueGetStmt`.

## Superseded open decisions (kept for history)

- Default discovery delay: deterministic mean (recommended) vs seeded stochastic — and whether
  to expose both.  → **D1: deterministic mean; stochastic deferred.**
- Saturation (`ov ≥ 1`): clamp-and-warn (recommended) vs a back-pressure model.  → **D2:
  clamp-and-warn, ceiling 0.99, warn once per bus.**
- Poll beat-cost `d`: fixed 1 vs per-call parameter.  → **D3: per-call `poll_beat_cost`, default 1.**
- API surface: is `cond` a Python callable (sim) with a separate lowerable spec for synth, or a
  single restricted expression usable by both?  → **D4: single restricted `Cond` object.**

## Implementation status (steps 1–4 landed)

Steps 1–4 are implemented (sim-only); **step 5 (the `@synthesizable` twin) is deliberately
not done** — see the blocker below.

- **Step 1 — `PollCond`/`Eq`/`Ne`** (`waveflow/hw/memif.py`). Frozen value type, op ∈
  {`==`,`!=`}, `rhs` a literal or a prior runtime-read value (a plain int in sim).
  `cond.eval(read_value)` is the sim form; the same object is the single source of truth a
  future extractor lowers (D4).
- **Step 2 — `MMIFMaster.poll_until(addr, cond, poll_interval, *, poll_beat_cost=1,
  discovery="mean")`**. `poll_interval` is in **cycles**. Registers the master as an active
  poller on its bound bus, waits, applies the deterministic mean discovery delay
  `(poll_interval-1)/2` on `cond` true, returns the satisfying word. `discovery="mean"` is the
  only mode (D1); anything else raises.
  - **Sim wait mechanism (the O(transactions) part):** rather than stepping every
    `poll_interval` (or every cycle), the wait blocks on a **per-slave write-notify event** the
    interconnect fires after each completed write, then **untimed-peeks** the watched word via a
    new `MMIFSlave.peek_read` hook (populated by `MemComponent`). So the loop wakes once per
    *write* to that slave and re-checks — O(transactions), and it pins the exact event time `T`
    so the discovery delay is a clean deterministic add (no poll-phase artifact). The peek costs
    no bus time; the poll's bus cost is the `ov` contribution below. A slave without `peek_read`
    raises a clear error when polled.
- **Step 3 — registry + `ov` + derating** (`_MMPollSupport` mixin on `AXIMMCrossBarIF` *and*
  `DirectMMIF`). `ov = Σ poll_beat_cost_i / poll_interval[i]` **per slave/bus** (`ep_name`),
  evaluated at each transaction's time. The crossbar derates **only** the `nwords` term:
  read `ret_dly = (latency_read_return + nwords/(1-ov))/freq`, write
  `cycles = latency_init + nwords/(1-ov)`; `latency_init` / `latency_read_return` untouched.
  `ov` is clamped to `POLL_OV_CEILING = 0.99` with one `warnings.warn` per bus per saturation
  onset (gated on the False→True transition, not per-transaction). `DirectMMIF` carries the
  registry/peek/notify so `poll_until` works over it, but does **not** derate (point-to-point,
  single master — there is no other port to steal from).
- **Step 4 — `AXIMMQueue.get` rebuilt on `poll_until`**. The empty-ring wait in
  `_get_raw_slots` is now `poll_until(tail_addr, Ne(head), poll_interval)` (the LT twin of the
  C++ `while (head==tail) tail = gmem[...]`). The queue's `poll_interval` is now in **cycles**
  (it feeds `poll_until`); the producer full-wait `_write_raw` and the host drain barrier
  convert cycles→seconds via the bound clock. `poll_cycles=64` is retired from `VmacAccel`
  (removed the `HwParam`; replaced by the module constant `RING_POLL_CYCLES = 64`, now a
  *meaningful* modeled interval rather than a 1-cycle-saturation band-aid).
  - **New baseline (recorded, `examples/vmac/timeline/sim_timeline.json`):** dequeue times moved
    ~13 ns earlier (`[2160.0, 6083.9] → [2147.1, 6071.0]` ns) — the model now wakes exactly when
    the producer advances `tail` plus a `(64-1)/2 = 31.5`-cycle discovery delay, vs the old coarse
    64-cycle poll grid; `complete_t 5623.9 → 5611.0` ns. The **per-command invariants still
    hold** (asserted by `vmac_queue_sim.run_and_check`): `rho` matches numpy, `ab_eq`
    anorm=16 / abcorr=32 reads, II-driven `anorm latency == abcorr latency`, conservation
    `48 == 3·nm`, occupancy peak 2. The shift is small here because the consumer rarely blocks
    (capacity 8 ≫ 3 commands) and `ov = 1/64 ≈ 0.016` is a ~1.6 % per-word stretch.
  - **Unit-test note:** the existing `tests/hw/test_aximm_queue.py` SPSC tests pass
    `poll_interval=1.0`/`0.5` at `clk.freq=1.0` — now interpreted as 1 / 0.5 **cycles**, i.e.
    exactly the `ov ≥ 1` polling-bound case. They pass functionally and now emit the loud
    clamp-and-warn (the model flagging the precise 1-cycle-poll mistake, as intended by D2).

### Step 5 (synthesizable twin) — NOT done; blocker recorded

Making `poll_until` `@synthesizable` and lowering `PollCond` to the C++ ring-poll is **out of
scope for this run** and remains gated on the **condition-IR rhs-as-runtime-var extension**
(D4's flagged cost): the existing condition-IR lowers `==`/`!=` only against *constants*, but
the queue dequeue polls `tail != head` where `head` is a runtime-read local. The sim-only
`PollCond.eval` path (steps 1–4) does **not** need it — `Ne(head)` just captures the int — so
the LT model lands first and the synth lowering follows once the condition-IR accepts a runtime
`HwVar` rhs. `PollCond`/`Eq`/`Ne` are already shaped as the single lowerable object so that
extension is purely additive (no sim/synth source split).

## Coordination

Prereq `vmac-top-autosynth` (Phases 1–4) is merged to `main` (2026-06-21) — DONE. This work
modifies `AXIMMQueue` / the MM master / the crossbar; with the prereq landed there is no
longer a conflicting branch in flight, so it is clear to start.
