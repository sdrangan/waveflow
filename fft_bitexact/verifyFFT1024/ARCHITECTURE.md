# The design under test — what is actually being simulated

The DUT is the **AMD Vitis DSP L1 SSR FFT**, fixed point, used unmodified from the shipped
library.  Nothing in `src/` reimplements the transform; `fft_top.cpp` is a named wrapper so
there is one function for synthesis to target.

## Configuration

Declared in [`src/fft_top.hpp`](src/fft_top.hpp).  Everything not listed is the library default
(`ssr_fft_default_params`, `hls_ssr_fft_enums.hpp:76-89`).

| parameter | value | meaning |
|---|---|---|
| `N` (`FFT_L`) | **1024** | transform length |
| `R` (`FFT_R`) | **4** | radix / SSR factor.  `L = R^5`, so **5 stages** |
| `scaling_mode` | `SSR_FFT_NO_SCALING` | widths grow; nothing is discarded to keep the width down |
| `output_data_order` | `SSR_FFT_NATURAL` | bin `k` at index `k` — no digit-reversal |
| `transform_direction` | `FORWARD_TRANSFORM` | `e^{-j2πnk/L}` |
| `butterfly_rnd_mode` | `TRN` | the default (see "inert options" below) |
| input type | `ap_fixed<16,2>` | 2 integer bits, 14 fractional, signed |
| twiddle table | `ap_fixed<18,2>` | 18 bits, 2 integer — the library default |
| output type | `ap_fixed<27,13>` | *derived*, not chosen: `in_W + log2(L) + 1` |

`SSR` is a throughput property: `R` samples enter per cycle across `R` parallel streams.  It
does **not** change the arithmetic — the same butterflies happen in the same order — which is
why a sequential Python model can be bit-identical to it.

## Data layout on the streams

The library's own convention, taken from its testbench
(`L1/tests/hw/1dfft/fixed/.../main.cpp:110-113`):

    sample n  ->  stream (n % R)  at time (n / R)

`src/fft_tb.cpp` feeds and drains in exactly that order.  Getting it wrong permutes the
spectrum, which looks like a numerical error but is not one.

## Internal number formats

The 16-point case was measured stage by stage (see `../verifyFFT16/ARCHITECTURE.md`).  At 1024
the same architecture runs for **5 stages** instead of 2 — not forked, not tiny, so the same
code path — and the widths follow the same pattern, confirmed for 3 stages at `L=64`:

    stage k:   grows W by 3 and I by 3 (first stage) or 2 (later stages)
    between stages 2..S and at the output:  one fractional bit is dropped (W - 1)

For `L=1024` that gives `16 + 3*5 - 4 = 27` total bits and `2 + 3 + 2*4 = 13` integer bits,
matching the declared output `ap_fixed<27,13>` and the library's own
`OUTPUT_WL = in_W + log2(L) + 1`.

> **The per-stage widths here are extrapolated; the end-to-end result is not.**  The width rule
> was traced at `L=16` (2 stages) and `L=64` (3 stages) and extended to 5 stages.  It predicts
> this design's declared output `ap_fixed<27,13>` correctly, and the Python model built on it is
> **bit-exact against both the C-simulation and the co-simulated RTL here** (16384 values each),
> which no wrong intermediate width would survive.  The individual stage widths at `L=1024` have
> still not been traced directly; `../cpp/dump_modes.cpp` handles any configuration if you want
> them confirmed rather than inferred.

Three details that decide the low bits:

* **The radix-4 DFT is exact.**  `W_4^k` is `{1, -j, -1, +j}`, stored exactly, so those
  multiplies are sign flips and swaps — no rounding.
* **The adder tree grows rather than rounds**, `+1` bit per level.
* **The twiddle rotation is the only lossy step** in the whole transform.  Its partial products
  are truncated into the *first operand's* type before being combined
  (`hls_ssr_fft_complex_multiplier.hpp:29-45`) — three quantization points per component, not
  one.
* **Tree additions wrap at the operand width**, then widen on assignment.  Only inputs at full
  scale reveal this; `input.txt` includes such vectors deliberately.

Note the two quantization sites use **different modes**: the twiddle *table* rounds and
saturates (`AP_RND`/`AP_SAT`), the twiddle *product* truncates and wraps (`AP_TRN`/`AP_WRAP`).

## Options present but inert

`butterfly_rnd_mode` also accepts `CONVERGENT_RND`, which has no Waveflow equivalent.  It
appears unused: the value is threaded through five headers as a template parameter but nothing
dispatches on it, and the twiddle cast is hardcoded to the rounding variant regardless.
Confirmed against the 2025.1 headers; re-check if the toolchain moves.

## Synthesis result (`xc7z020clg484-1`, 10 ns target)

| | 1024-point | 16-point, for scale |
|---|---|---|
| DSP48 | 20 (of 220) | 12 |
| FF | 16381 (of 106400) | 4017 |
| LUT | 21481 (of 53200) | 6922 |
| estimated clock | 7.266 ns | 7.208 ns |
| co-sim latency | 1477 cycles, interval 1478 | 41 |

64x the transform for under 2x the DSPs and ~3x the LUTs: the extra work is three more stages
reusing the same butterfly structure, not more parallelism.  Latency scales with the data, as
expected for a streaming design.

## What the co-simulation covers

Vitis has no separate "RTL simulation" step for a control-driven (`ap_ctrl_hs`) kernel like
this: **`cosim_design` *is* the RTL simulation.**  It elaborates the synthesized Verilog in
`xsim` and drives it with the same `src/fft_tb.cpp`, so `results/output_cosim.txt` is produced
by hardware, not by C++.

(The separate XSI/BFM path in this repo exists for free-running `ap_ctrl_none` designs, which
Vitis co-simulation cannot drive.  It does not apply here.)
