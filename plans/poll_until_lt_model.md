# Design proposal: an LT polling-overhead model — `m_mem.poll_until(...)`

**Status: future design proposal. DO NOT START YET.** This touches `AXIMMQueue`, the MM
master endpoint, and the crossbar/slave — the same files the `vmac-top-autosynth` branch
(Phases 3–4) is actively changing. Sequence this **after** that work lands and merges to
`main`, to avoid conflicts. This file is a captured design, not an implementation brief.

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

## Open decisions (resolve in a Phase-0 design pass before implementing)

- Default discovery delay: deterministic mean (recommended) vs seeded stochastic — and whether
  to expose both.
- Saturation (`ov ≥ 1`): clamp-and-warn (recommended) vs a back-pressure model.
- Poll beat-cost `d`: fixed 1 vs per-call parameter.
- API surface: is `cond` a Python callable (sim) with a separate lowerable spec for synth, or a
  single restricted expression usable by both?

## Coordination

Prereq: `vmac-top-autosynth` (Phases 1–4) merged to `main`. This work modifies
`AXIMMQueue` / the MM master / the crossbar and would collide with that branch. Start only
after it lands.
