---
title: AXI-MM Command Queue (VMAC timing)
parent: Examples
nav_order: 6
has_children: false
---

# AXI-MM Command Queue (VMAC timing)

The previous examples taught one *interface concept* at a time. This one is a
**timing study**: it takes a vector MAC accelerator (VMAC) fed commands over an
AXI-MM command queue and asks a harder question than "does it compute the right
numbers" — *does Waveflow's loosely-timed simulation predict the right **timing**,
and where exactly does it stop being faithful?*

The answer is a named result — **bus utilization ≠ latency** — and the page frames
it in the standard comp-arch vocabulary for what the simulator is: a **loosely-timed
(LT) transaction-level model**. The whole thing is validated against Vitis RTL
cosim, and the headline figure is *committed* and regenerates with **no Vitis**.

## The system

One host, one accelerator, one shared memory, over a single `m_axi` master:

```
   host ──AXI-MM command queue──▶ VMAC ──m_axi (gmem)──▶ shared memory
 (vmac_host.py)                 (vmac_queue_sim.py)        A | B | Y regions
```

The host enqueues commands; VMAC dequeues them, reads its operand matrices `A`
(and `B`) from memory over `m_axi`, computes, and writes the result `Y` back to
the same bundle. Modeled in
[`examples/vmac/vmac_queue_sim.py`](../../../examples/vmac/vmac_queue_sim.py) and
[`examples/vmac/vmac_host.py`](../../../examples/vmac/vmac_host.py); the
synthesizable kernel is
[`examples/vmac/vmac_compute_impl.tpp`](../../../examples/vmac/vmac_compute_impl.tpp).

### Two commands, same opcode

Both scenarios run the **same** `inner_prod` opcode (`R = A·conj(B)`) at PF=1 on
4×4 operands, 100 MHz — they differ only in *where* `B` points:

- **`anorm`** — `b_addr == a_addr`. `B` aliases `A` (an auto-correlation / norm).
  The kernel derives a loop-invariant **`ab_eq`** flag from `a_addr == b_addr` and
  fills `b_lane` from `a_lane` **instead of issuing a second read burst**.
- **`abcorr`** — `b_addr != a_addr`. The ordinary case: both `A` and `B` are read.

`ab_eq` is a *runtime* flag, so HLS fixes **one static II** for the loop at the
worst case (b-read present). It does not reschedule shorter when `ab_eq` holds — it
only **predicates whether the AXI transaction fires**. That is the whole study:
*same cycle count, B's bus beat suppressed.*

## The captured sim timeline

The SimPy run logs a **source-agnostic** timeline — per-command memory
transactions (`rw`/`name`/`addr`/`nwords`/`tstart`/`tend`), latency, read-word
counts, and the command-queue occupancy — to
[`timeline/sim_timeline.json`](../../../examples/vmac/timeline/sim_timeline.json)
(`source:"sim"`, `timebase:"ns"`). The LT model issues **one whole-matrix block
per operand** (16 words in a single bus transaction), so `anorm` shows one `A`
read + one `Y` write, while `abcorr` adds the second `B` read block.

The sim's prediction:

| scenario | read words | latency (sim) |
| --- | --- | --- |
| `anorm` (ab_eq) | 16 | 630 ns |
| `abcorr`        | 32 | 940 ns |

Half the reads **and** lower latency for `anorm` — the intuitive "fewer reads →
faster" reading.

## The cosim overlay — and the result

Vitis RTL cosim measures the truth and emits the **same schema** with
`source:"cosim"`
([`timeline/cosim_timeline.json`](../../../examples/vmac/timeline/cosim_timeline.json)),
so one renderer overlays them. The committed figure, rendered from those two JSONs
alone:

![VMAC ab_eq: loosely-timed sim vs Vitis RTL cosim. Panel (a) read-bus words: anorm 16 vs abcorr 32, half. Panel (b) command latency sim vs RTL: sim predicts anorm 630 < abcorr 940 ns while RTL is fixed-II at 3200 ns for both. Panel (c) per-command memory-transaction timeline: the sim's one whole-matrix block per operand vs the RTL's per-word beats, with B's reads freed under ab_eq.](images/sim_vs_cosim.svg)

**The named result — bus utilization ≠ latency:**

| scenario | read words (sim & RTL agree) | latency (sim) | latency (RTL) |
| --- | --- | --- | --- |
| `anorm` (ab_eq) | **16** | 630 ns | **3200 ns** |
| `abcorr`        | **32** | 940 ns | **3200 ns** |

- **`ab_eq` halves the read-bus traffic in real RTL** — 16 vs 32 read words, panel
  (a). The cosim confirms B's beats are genuinely suppressed.
- **But the RTL latency is FIXED-II** — `anorm` and `abcorr` are *identical* at 347
  cycles / 3200 ns, panel (b). Eliding B's read did **not** make `anorm` faster; it
  only **freed the bus**.

The sim said "fewer reads → faster"; the hardware says **"same latency, freed
bus."** That gap is the named teaching result — and on a shared interconnect the
freed bandwidth is exactly what another master (a CG matmul tile) would consume.
The full numeric writeup is
[`timeline/sim_vs_cosim.md`](../../../examples/vmac/timeline/sim_vs_cosim.md).

## What this says about the model (loosely-timed TLM)

Waveflow's simulator is a **loosely-timed (LT) transaction-level model** in the
SystemC TLM-2.0 sense: each transfer is a single timed *block* that holds a
capacity-1 bus resource for its whole duration, and concurrent masters serialize
on it (an implicit arbiter). This is accepted, standard practice — LT is how
industry virtual platforms run fast — **not** a Waveflow shortcut. Framing the
result in that vocabulary is the point of the example.

**What LT captures faithfully here:**

- Total interconnect **occupancy** (Σ of held block durations).
- Multi-master **contention / arbitration** (serialization on the capacity-1 bus).
- **First-order, burst-granular** memory-access timing — including the `ab_eq`
  read-word halving, which the sim gets *right*.

**What LT abstracts away (the boundary):**

- **Sub-transaction interleaving / exact beat ordering** — a block is contiguous,
  never "every other cycle." The sim's one 16-word block vs the RTL's 16 single-word
  beats in panel (c) is exactly this abstraction.
- Consequently **latency is mispredicted wherever it decouples from transaction
  count** — the `ab_eq` case: transaction-gated latency predicts `anorm` faster; the
  fixed-II RTL shows equal latency.

**Fidelity per validation target:**

| Target | Fidelity under the LT block model |
| --- | --- |
| Queue occupancy | **Robust** — command-level granularity; the block model is plenty fine |
| Per-burst / total memory occupancy | **Robust** — the bus resource sums it correctly |
| `ab_eq` latency | **Recoverable** — only via the II parameter (the cosim calibration) |
| Sub-transaction ordering / fine contention | **Irreducible gap** — the block can't say who's on the bus *which* cycle |

The fix for `ab_eq` latency is **II-decoupling**: advance time by the pipeline
schedule (`II × trips / freq`) while issuing bus requests only for the transfers
actually performed — latency II-driven, occupancy transaction-driven, decoupled.
The `II` is a parameter **fit from cosim**. Escalating from *transaction-gated* to
*II-parameterized* latency is Waveflow's analog of TLM's **LT→AT escalation**: add
just enough timing structure for the question at hand. The `ab_eq` gap is the
calibration target this example hands to that workflow.

### A second calibration target: the absolute-latency underestimate

Panel (b) shows a *second*, distinct gap: the LT sim's absolute latencies
(630–940 ns) sit **far below** the RTL's 3200 ns. The block model counts **transfer
time**, not the kernel's full **pipeline depth** (fill/drain, compute latency), so
it systematically *underestimates* absolute latency. This is separate from the
`ab_eq` finding — `ab_eq` is about the *relative* ordering of two scenarios; this is
about the *absolute* scale of both — and it is the other knob cosim calibration
would fit.

> **Scope note.** Queue occupancy is a **sim-only** quantity here: the cosim kernel
> has an `m_axi` but no command ring, so `cosim_timeline.json` carries
> `queue_occupancy: null`. The figure validates per-burst memory timing and the
> `ab_eq` read-word result against RTL; occupancy is reported by the sim alone and
> not fabricated for cosim.

## The committed figure (regenerates without Vitis)

The figure is rendered by
[`examples/vmac/vmac_figures.py`](../../../examples/vmac/vmac_figures.py) from the
**two committed timeline JSONs alone** — no Vitis, no cosim re-run, no VCD read.
That is the "committed figure" property: the docs build never needs the toolchain.

```bash
cd examples/vmac
python vmac_figures.py          # re-render docs/examples/mmqueue/images/sim_vs_cosim.svg
python vmac_figures.py --check  # render to a temp file, byte-compare vs the committed SVG
```

The SVG is deterministic (a fixed `svg.hashsalt`, no embedded timestamp, mirroring
[`shared_mem_figures.py`](../../../examples/shared_mem/shared_mem_figures.py)), so a
re-render is **byte-identical** when nothing changed — the `git diff` is empty
unless the timelines actually moved. `images/sync_status.json` records the source
JSON hashes and the SVG hash, a cheap staleness signal a docs lint can check
without re-running anything. The cosim timelines themselves were captured once by
[`vmac_cosim_stage3.py`](../../../examples/vmac/vmac_cosim_stage3.py) (the only step
that needs Vitis) and committed.

## Next

- The [Shared Memory (histogram)](../shared_mem/) example — the previous step,
  where the `m_axi` shared-memory pattern is introduced, with its own
  [RTL cosim](../shared_mem/rtlsim.md) and [committed timing figures](../shared_mem/timing.md).
- The [examples index](../) — the pattern progression this capstone sits in.
