---
name: waveflow-architecture
description: Answer the architecture question for an algorithm before any Waveflow code -- from an equation in a paper or a "make this hardware" request -- in a quick chat answer (the structure that collapses the work, a block diagram, the key trade-offs an expert asks in the first minute, one rate check and one memory check with sourced numbers, a recommendation, two or three open questions). Use whenever someone asks to implement, accelerate or put on an FPGA an algorithm (a detector, an estimator, a correlator, a filter, a decoder, a transform) and no architecture is decided yet, or asks how to parallelize it, what to store and what to compute, how many blocks, whether it fits or how fast it would be.
---

# Waveflow architecture: the first-minute answer

The person is often an expert in the algorithm and new to hardware. What they want first is what an
experienced hardware architect says in the first minute: **where the work really is, how it
parallelizes, what is stored and what is computed, and whether it fits** -- in a chat answer, in under
a minute. Not documents, not code, not a survey of options.

**Your first reply is this answer, then you stop** -- even when the request says "build" or
"implement", even with no one there to answer. Do not open a frame's process, write a file or read
an example first. Building starts only when the person, having read the answer, asks for it.

## The answer, in chat

Answer in the conversation. Read at most a page or two first (`systems/library.md` for the cost of a
hard block; nothing else unless a number needs it). No files, no golden, no checkpoints. The answer has:

1. **The structure that collapses the work.** Before counting, look for what makes the naive count
   wrong: shared factors (many sequences that are shifts or rotations of a few roots), a matrix computed
   once per block and applied per sample, symmetry, sparsity, an operation that is a convolution and so
   an FFT. Say it in one or two sentences: "the 64 preambles are cyclic shifts of N roots, so the work
   scales with the roots, not the preambles".
2. **A block diagram**: a few kernels in a line, as text (`in → FFT → × conj(root) → IFFT → peak →
   out`), each with what it computes. Say what is time-shared and what is replicated.
3. **The key trade-offs, as questions with your answer.** The two or three decisions an expert would
   raise immediately, each with the deciding number: how to parallelize (one time-shared pipeline, or
   `k` copies, and why), **store or regenerate** (a table in BRAM against computing it, and what else
   that computation needs), fixed point or the precision that matters, what runs in software.
4. **One rate check and one memory check**, each a short calculation with its source: the cycles
   available a job at the clock against the cycles the pipeline needs; the bits to store against the
   part's BRAM (a BRAM36 is 36 Kb). A library block's numbers from `systems/library.md`; a kernel you
   write by its lane loop (one word an iteration at II = 1); a standard's numbers from its table, cited.
5. **A recommendation** in one sentence, and **two or three open questions**, most important first.

Keep it to a screen. If the person wants more, they will ask: answer that question the same way, briefly.

## Which blocks: reuse only what is hard

- **Reuse a library block when it is hard to build well**: an FFT (`SsrFft`), a large complex matrix
  product (`SystolicEngine`), the memory adaptors, the RF converters (`systems/library.md`).
- **Write a dedicated kernel for simple work**: an element-wise multiply, a magnitude, a threshold, a
  peak search, a reduction. Fuse neighbouring simple steps into one kernel. Agents write these well,
  and a dedicated kernel is smaller and faster than a general unit.
- **The programmable vector unit (`VecUnit`) only when programmability is the requirement** (one
  datapath must run several different programs), never as a stand-in for a simple kernel.

## Rules

- **Every number names its source**: a model or a page (`systems/library.md`), a formula, a standard's
  table, or a stated assumption. A block no model covers is "unknown -- needs a csynth probe", not a
  guess.
- **Count after collapsing the structure**, and name the term that dominates: it is often not the step
  the paper spends its pages on.
- **No code** until the person has the answer and asks for it.

## Pages, when a number needs one

| for | page |
|---|---|
| the hard blocks and how well their cost is known; the part | `systems/library.md` |
| a kernel's II from its loop | `vectorization/hls/loop_optimization.md` |
| the flow the blocks then take, once the person wants to build | `systems/flow.md` |

## The shape of an answer

> **Structure.** A range-Doppler map is 256 range FFTs (one a chirp) then 1,024 Doppler FFTs a frame;
> the corner turn between them, not the FFTs, sets the memory.
> **Blocks.** `ADC → range FFT (SsrFft L=1024) → corner-turn buffer → Doppler FFT (SsrFft L=256) → |·|² → CFAR → detections`;
> one FFT of each, time-shared.
> **Trade-offs.** One range FFT keeps up: 256 cycles a chirp at L/R (`dsp/ssr_fft/timing.md`) against
> 500 at a 2 µs chirp (assumed) and 250 MHz. One Doppler FFT too: 1,024 × 64 = 65,536 cycles against the
> frame's 128,000. The corner turn holds 256 × 1,024 × 32 bits = 8.4 Mb, about 228 BRAM36 (36 Kb each),
> 456 double-buffered of the part's 1,080: on chip is possible but heavy -- UltraRAM, or single-buffer
> with a read-before-write order, is the question.
> **Recommendation.** Two FFTs, the corner turn in UltraRAM, CFAR as a dedicated kernel.
> **Open.** The chirp period; whether detections need angle (a third FFT across antennas).

## Before you answer, check

- [ ] The structure that collapses the work is named, and the count is made after it.
- [ ] Two or three key trade-offs, each answered with its deciding number.
- [ ] One rate check and one memory check, each number sourced.
- [ ] Library blocks only for hard blocks; simple steps as dedicated, fused kernels.
- [ ] A chat answer of about a screen: no files, no code.
