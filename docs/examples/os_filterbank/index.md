---
title: One FFT shared by every channel
parent: Examples
nav_order: 10.8
example_dir: examples/os_filterbank
summary: "The shared-kernel example: a three-channel overlap-save filter bank on ONE SsrFft, used forward and then inverse for every block of every channel. A StreamArbiter merges the channels' new blocks with the feedback frames (priority to the feedback); the arbiter's index sets the FFT's direction per frame; a tag beside the FFT says whose each frame is; each channel's filter is loaded once into a slot; per-channel credits bound the loop so the stage after the FFT never blocks; StreamDemuxes route each answer, status and credit back to its channel. Bit-exact end to end against the FFT's own model and within 0.5% of np.convolve; each stage alone and the bank in pysim and at RTL, the recordings equal; without the credit it deadlocks and the run still returns."
---

# One FFT shared by every channel

A **filter bank**: `C = 3` channels, each with its own FIR filter, filtered by overlap-save on **one**
FFT. For each block of a channel the FFT runs twice -- forward on the block, then inverse on the block's
spectrum times the channel's filter -- so every channel shares one FFT, and every block goes through it
twice. It is the curated example of [sharing a kernel](../../guide/systems/shared_kernels.md), and uses
each part of that pattern once.

```text
s_in_c --> front_c --+                                              +--> dmx_ans --> m_out_c
           (credit)  +--> arb --> pre --> fft --> post -------------+--> dmx_st  --> m_status_c
              ^      ^    m_sel   s_dir    ^        |               +--> dmx_crd --> front_c
              |      +--- fb: X * H_c, the feedback frame (input 0) -+
s_coef ---------------------------------------------------------> post (H_c, loaded once)
```

## What it computes

Overlap-save with `L = 64` and `M = 17` taps: a block is `P = M - 1 = 16` samples kept from the block
before, then `B = 48` new ones. Per block, `y = IFFT(FFT(block) * H_c)` and the first `P` outputs are
dropped; what is left is the next `B` samples of `x * h_c`. Each job is its own signal, so a job's first
block starts from zeros.

The fixed-point formats are the FFT's: samples in `ap_fixed<16,2>`, bins out at `ap_fixed<23,9>`.
`H_c` is stored divided by `H_SCALE = 16`, so the product fits the FFT's input format again, and the
inverse's output -- read through `inv_out_fmt`, `ap_fixed<23,3>`, which is the `1/L` -- is the filtered
signal divided by 16.

**The golden is bit-exact by construction**: the FFT's own model (`model.pipeline`) forward, the
library's `complexMultiply` model for `X * H_c`, the model inverse (`block_golden`). And it is the filter
it claims to be: against `np.convolve(x, h)` in floating point, every job is within 0.35% of the output's
peak (`float_check`; the test allows 0.5%).

## The stages

| stage | what it does | pattern part |
|---|---|---|
| `front_c` (one a channel) | per block: wait for a credit, read the `B` new samples, then send `Tag` + the `P` kept samples + the new ones -- the whole block read first | credits; a frame announced only when complete |
| `arb` | `StreamArbiter`, priority, 4 inputs: input 0 the feedback, 1-3 the channels; 2-burst frames (`Tag`, then the words) | the arbiter, feedback first |
| `pre` | reads the arbiter's index (`m_sel`: 0 is the feedback, so inverse) and the frame; writes the direction to the FFT's `s_dir`, the tag to `post`, the words to the FFT | the direction per frame; the tag beside K |
| `fft` | `SsrFft(L=64, reorder="pingpong", direction="runtime")` | the shared unit |
| `post` | loads the three filters once; then per frame: pass 0 multiplies by `H_c` and sends the product back as a feedback frame; pass 1 answers the block, a `Status` after a job's last block, and a credit to the channel | resident data (slots); K never blocks on a result |
| `dmx_ans`, `dmx_st`, `dmx_crd` | `StreamDemux(route="header")` on the `ch` field of `Resp`, `Status`, `Crd` | results back to their client |

The tag is the design's own: `Tag(tx_id, ch, blk, nblk, npass)`, one word a frame, in the order frames
enter the FFT. The FFT frames by count and carries nothing through, so the tag cannot travel in-band.

## Why it cannot deadlock

- **A channel sends a block only with a credit**, `CR = 2` a channel, and `post` returns one per block
  answered. So at most `C * CR = 6` blocks are in the loop, each with one frame in flight.
- **Every link in the loop holds what those blocks can put there.** The feedback link holds six frames
  (`DEPTH["fb"] = 102` words), the tag and index links seven words. So `post` never blocks writing a
  feedback frame, and the arbiter never waits on a frame that is not coming.
- **Feedback first.** Input 0 of the arbiter is the feedback, so blocks already in the loop finish
  before new ones enter.
- **The witness:** with the credit made ineffective (`CR = 100`) and the loop's links shortened, the
  same bank answers only some of its jobs, and the simulation still ends cleanly
  (`test_without_the_credit_the_loop_deadlocks_and_the_run_still_returns`). The tests assert counts.

## Why its RTL recordings can be compared

The channels finish in an order that depends on timing: the arbiter interleaves them as their blocks
arrive. But each channel's own outputs do not: a channel's blocks enter the FFT in order, and their
feedback frames come back in the same order. So each channel gets its own output and status port, through
the demuxes, and every port's recording is the same in pysim and at RTL.

## Its gates

`tests/examples/test_os_filterbank.py`:

- pysim: the chain against the filter (`float_check`); the bank on two scenarios against the golden,
  every block answered and every job's status counted; a front alone and `post` alone in their
  harnesses; more blocks than credits on every channel; the deadlock witness; the generated top
  carrying the loop as an ordinary channel.
- RTL (`-m xsi`): a front alone, `post` alone, and the bank on both scenarios, every recording equal to
  pysim's.

`pre` has no harness of its own: its two outputs into the FFT carry no TLAST, so there is no burst to
compare, and it is a copy. It is checked inside the bank. The arbiter and the demuxes have
[their own gates](../../guide/streams/arbiter.md#its-gate), and the FFT
[its own](../../guide/dsp/ssr_fft/index.md).

## At RTL

Vitis HLS / Vivado 2025.1, the RFSoC 4x2's xczu48dr at 250 MHz, cycles on the XSI testbench's clock.
Every recording equals pysim's, and none is empty.

| scenario | blocks | frames through the FFT | statuses, channels 0 / 1 / 2 (last at cycle) | FFT busy |
|---|---|---|---|---|
| `test` | 13 | 26 | 2 / 1 / 3 (821 / 1067 / 888) | 39% |
| `long` | 36 | 72 | 3 / 1 / 4 (2349 / 2371 / 2424) | 48% |

**The channels answer out of order.** In `test`, channel 2's first job (one block) is answered at cycle
481, before channel 0's first (three blocks, cycle 737); channel 1, with one five-block job, finishes
last. `test_rtl_timing_is_exact` pins each channel's last status.

**The FFT is busy under half the time, and the FFT's own interval after a gap is why.** A frame takes the
FFT 16 cycles (`L/R`) back to back, but a frame that follows its predecessor after any idle cycle leaves
`max(2L/R, L/R + g)` after it ([its timing](../../guide/dsp/ssr_fft/timing.md)), and here `pre` leaves at
least a cycle between frames: in the traced RTL the FFT's frames leave exactly 32 cycles apart throughout
`long`, so it can be at most half busy. `post` comes next: 39-40 cycles a forward frame (it reads and
multiplies the frame, 16, then writes the whole product back, 17, one after the other, as the
store-and-forward rule asks) and 19-22 an inverse one, about 30 on average, so it is busy 91% of `long`
and would bind if the FFT kept its rate. "FFT busy" is the frames times 16 over the run, from the counts.
(Measured on the traced run, Vitis / xsim 2025.1: `python -m examples.os_filterbank.fidelity`,
`plans/osfb_pysim_fidelity.md`; this paragraph used to name `post` alone.) Both are open: the FFT's half
rate after a gap (`plans/ssr_fft_gap_throughput.md`), and splitting `post` in two -- a multiply stage, and
a stage that writes the feedback frame from a buffer. The example keeps the simpler stage.

csynth (estimates) for the bank: 40 DSP (24 in the FFT, 16 in `post`'s multiply), 80 BRAM (mostly the
loop's FIFOs, the feedback link's 102 words the largest), 16193 FF, 19243 LUT. Every loop at II = 1,
worst slack 0.00 ns at 250 MHz: met, with no margin.

**After place and route** (out of context, Vivado 2025.1, the same part at 4 ns), the margin is real: the
critical path is 3.096 ns, WNS +0.904 ns, and it is inside the FFT, not in the sharing logic. The bank
routes at 12,424 LUT (2,297 of them LUTRAM), 15,553 FF and 40 DSP, and **no BRAM**: csynth's 80 BRAM_18K
all land in distributed RAM.

**What sharing the FFT costs, and saves.** The FFT is half the bank: 50% of its routed LUT, 49% of its FF,
60% of its DSP. The sharing logic -- the arbiter, the demuxes, `pre` and the loop's links -- routes at
3,074 LUT and 4,679 FF with no DSP, about half an L = 64 FFT (38% / 45% leaving out the bank's own
output-port register slices). The arbiter itself is small (325 LUT, 550 FF); the 184-bit answer demux and
the feedback link are the largest pieces. Without sharing, each block's two passes need two fixed FFTs a
channel -- six -- about 37,200 LUT, 47,700 FF and 144 DSP at the standalone fixed size, against 9,343 LUT,
12,236 FF and 24 DSP for the one shared FFT and its overhead. The overhead's share at larger `L` is not
measured: the FFT grows faster than the arbiter, but the feedback link grows with the frame
(`plans/shared_kernel_rfsoc_timing.md`).

## Building it

```bash
python -m examples.os_filterbank.os_filterbank_build --through pysim          # pysim only
python -m examples.os_filterbank.os_filterbank_build                          # the bank through compare
python -m examples.os_filterbank.os_filterbank_build --through long_compare   # the long scenario
pytest tests/examples/test_os_filterbank.py -m xsi                            # the RTL gates
```

The three stage bodies are hand-written (`src/os_front_task.h`, `os_pre_task.h`, `os_post_task.h`).
Everything else is generated: the schemas' C++, the FFT's sources, the stream blocks' bodies, a header
of the example's constants, and the tops. Built for the RFSoC 4x2 at 250 MHz, with Vitis 2025.1.
