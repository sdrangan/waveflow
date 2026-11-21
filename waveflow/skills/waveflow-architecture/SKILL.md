---
name: waveflow-architecture
description: Plan the hardware for an algorithm before any Waveflow code -- from an equation in a paper or a "make this hardware" request to four documents, spec.md (inputs and outputs, the operation and its golden, why hardware as numbers, requirements and workload), arch.md (two or three alternatives, the HW / SW split, blocks as kernels, adaptors and devices, interfaces, existing library blocks first), eval.md (op counts, II, latency, resources and the bottleneck per alternative, every number with its source) and a spec per block -- stopping for the user's decisions. Use whenever someone asks to implement, accelerate or put on an FPGA an algorithm (a detector, an estimator, a filter, a decoder, a transform) and no architecture is decided yet, or asks which blocks, how many, whether it fits or how fast it would be.
---

# Waveflow architecture: from an algorithm to a plan

The person is often an expert in the algorithm (a communications or signal-processing engineer) and new
to hardware. Your job is a **plan they can check**: you do the analysis, they confirm the decisions.
Every other `waveflow-*` skill starts from a decided architecture; this one decides it. Its output is
what a frame's Stage 1 takes (`waveflow_get_process(frame)`), one block at a time.

Do **not** start with code. Unprompted, the usual failure is one kernel that mirrors the paper's
equations, in `float` or arbitrary widths, sized for nothing in particular, with numbers that sound
right and come from nowhere. Each rule below exists against one part of that.

## The rules (all of them, every time)

1. **Four documents, in this order**, under `plans/` in the user's design folder (outside the Waveflow
   repository): `spec.md`, `arch.md`, `eval.md`, `blocks/<name>.md`. Their content is prescribed below;
   the prose is yours. A small design may put the four in one file, as sections in this order.
2. **What, before how much.** `spec.md` is finished before anything is sized: the operation defined
   precisely enough that two engineers would build the same function, by a **golden model** you write
   and run (Python, numpy, untimed: the executable definition, and the golden Stage 1 wants).
3. **Every number names its source**, in the same line or table row: a calibrated model, a formula on a
   guide page, a measured point on a guide page (with its tool version), the standard's table, or a
   stated assumption. A block no model covers is **"unknown -- needs a csynth probe"**, with the
   configuration to probe and what the probe settles. Never a confident guess: a made-up number is the
   failure this rule exists for.
4. **Requirements and workload are different things, and you propose both.** Requirements are what
   must be met (rate, latency, sizes, the part and clock); the workload is what actually arrives (the
   mix of sizes, how often the expensive path runs). Papers rarely state either. Derive what follows
   from a standard from its tables, and show the derivation; propose the rest as assumptions the user
   confirms.
5. **Library blocks first.** For every block, search before designing one: `systems/library.md` (the
   catalog, with each block's models), then `waveflow_search` in the algorithm's own words. Name the
   block, its page and how well its cost is known. A block you build is a stated choice: "no library
   block does X".
6. **Alternatives, priced, then a choice.** Two or three architectures that differ in a real decision
   (the HW / SW split, which operation is computed how often, a library block against a built one), each
   priced in `eval.md` the same way. "Does not fit" sends the design back to `arch.md`: that loop is
   the design work. Recommend one and say why; the user picks.
7. **Count before you size.** The operations per second, weighted by the workload, decide the
   architecture. Find the term that dominates -- it is often not the step the paper spends its pages on
   (a rarely recomputed matrix against a product done for every sample) -- and the data that must move
   (samples per cycle against words per cycle on a stream).
8. **Fixed point, proposed and checked.** The paper's arithmetic is usually floating point; the hardware
   is not. Propose each signal's format (`schema/python/fixpoint.md`) and say how the golden will check
   the loss (an error or SNR against the floating-point golden), never `float` in a datapath.
9. **Stop at the checkpoints.** After `spec.md`: the user confirms the operation, the requirements and
   the workload. After `eval.md`: the user picks an alternative. Stop again before any code: the blocks
   go to their frames one at a time. When you cannot stop (no one answers), write the documents to the
   end with every unconfirmed choice marked **assumed** and listed first under open questions.

## The four documents

**`spec.md` -- what.**
- Inputs and outputs: each one's type, shape, precision, and where it comes from or goes.
- The operation: equations, edge cases (an empty job, a singular matrix, overflow, a size out of
  range), and the golden model's file and how to run it.
- Why it suits hardware, **as numbers**: operations per second; the CPU baseline priced by the
  processor platform (`cpu/dse.md`: `CpuPlatform.sw_function(family).price(...)`, the nearest family
  with the mapping stated); the gap between the two.
- Requirements, each a number with its assumption: rate, block size, latency budget, the dimensions
  (antennas, users, layers, taps), the part and clock.
- Workload: the sizes and their mix, the range of each parameter, how often each path runs.

**`arch.md` -- how.** For each alternative:
- the HW / SW split with a reason (rate-critical inner loops to kernels; control, configuration and
  rare paths to `SwFunction`s on a `Processor`, `cpu/index.md`);
- the blocks as kernels, adaptors and devices in the concept pages' words (`systems/kernels.md`,
  `systems/devices.md`): none monolithic, each with one job;
- an interface table: each connection, its kind (stream, memory, bus view), what it carries (a schema
  sketch), its width and the rate it must sustain; command-response by default
  (`systems/command_response.md`);
- per block: the library block and its page, or "built: no library block does X".

**`eval.md` -- does it fit.** For each alternative, no simulation: operations weighted by the workload;
per block its II, latency and resources, each with its source; the bottleneck; the replication or
pipelining that meets the rate; the utilization of the part (`systems/library.md`, *The part*); the
csynth probes that would settle each unknown. A library block's numbers come from its model or its
page (`systems/library.md`, *Pricing a point*); a kernel you write is priced by the lane loop
(`vectorization/hls/loop_optimization.md`). Timing and resource skills will do that lookup once they
exist; until then, the pages are the place.

**`blocks/<name>.md` -- one per block.** Its ports and messages, its rate, its II and latency budget,
its resources, the library block it uses if any, and which frame it goes to (`waveflow_list_frames`).

**Each document ends with open questions**: what the user still has to decide, most important first.

## Which page to read before which step

Read with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout. If a name is not on these pages,
`waveflow_search` it: **do not guess an API name or a block's cost.**

| step | page |
|---|---|
| what exists, and how well each block's cost is known | `systems/library.md` |
| a kernel, an adaptor, a device: the words for the blocks | `systems/kernels.md`, `systems/devices.md` |
| the protocol on every interface | `systems/command_response.md` |
| the CPU baseline and software beside the hardware | `cpu/index.md`, `cpu/dse.md` |
| a kernel's II from its loop: words per iteration | `vectorization/hls/loop_optimization.md` |
| resource models, their confidence, composing them | `resource_model/index.md` |
| models price a point; a sweep is the later step | `systems/dse.md` |
| fixed-point formats, rounding and saturation | `schema/python/fixpoint.md` |
| the flow the blocks then take | `systems/flow.md` |

## The shape of a number

```markdown
| block | II (cycles a frame) | latency | DSP | LUT | source |
|---|---|---|---|---|---|
| range FFT, `SsrFft` L=1024 | 256 | ~1,276 | 48 | 21,400 | `dsp/ssr_fft/timing.md` (by construction, L/R); resources measured at L=1024, Vitis 2025.1 |
| CFAR, built (no library block) | 256 | ~20 | ? | ? | lane loop, 4 samples a word at II=1; resources **unknown -- csynth probe: one CFAR at PF=4** |
```

## Before you finish, check your own work

- [ ] `spec.md` defines the operation with a golden that runs, edge cases included.
- [ ] Every requirement and the workload is a number with its assumption, and was confirmed (or is
      marked **assumed**).
- [ ] The operations per second are counted, weighted by the workload, and the dominant term is named.
- [ ] The CPU baseline is priced by the processor platform, the mapping stated.
- [ ] Two or three alternatives, each with its HW / SW split and blocks as kernels, adaptors and
      devices; the interfaces with kind, contents, width and rate.
- [ ] For every block, the library was searched; the block used, or "no library block does X".
- [ ] Every number in `eval.md` names its source; every uncovered block is "unknown -- needs a csynth
      probe", with the probe named.
- [ ] Formats proposed for every signal, and how the golden checks them.
- [ ] Each document ends with open questions, most important first; you stopped where rule 9 says.
