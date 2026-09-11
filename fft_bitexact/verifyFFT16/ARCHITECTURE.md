# The design under test — what is actually being simulated

The DUT is the **AMD Vitis DSP L1 SSR FFT**, fixed point, used unmodified from the shipped
library.  Nothing in `src/` reimplements the transform; `fft_top.cpp` is a named wrapper so
there is one function for synthesis to target.

## Configuration

Declared in [`src/fft_top.hpp`](src/fft_top.hpp).  Everything not listed is the library default
(`ssr_fft_default_params`, `hls_ssr_fft_enums.hpp:76-89`).

| parameter | value | meaning |
|---|---|---|
| `N` (`FFT_L`) | **16** | transform length |
| `R` (`FFT_R`) | **4** | radix / SSR factor.  `L = R^2`, so **2 stages** |
| `scaling_mode` | `SSR_FFT_NO_SCALING` | widths grow; nothing is discarded to keep the width down |
| `output_data_order` | `SSR_FFT_NATURAL` | bin `k` at index `k` — no digit-reversal |
| `transform_direction` | `FORWARD_TRANSFORM` | `e^{-j2πnk/L}` |
| `butterfly_rnd_mode` | `TRN` | the default (see "inert options" below) |
| input type | `ap_fixed<16,2>` | 2 integer bits, 14 fractional, signed |
| twiddle table | `ap_fixed<18,2>` | 18 bits, 2 integer — the library default |
| output type | `ap_fixed<21,7>` | *derived*, not chosen: `in_W + log2(L) + 1` |

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

Measured by instrumenting a copy of the headers (`../cpp/dump_modes.cpp`), not derived from
reading them — two careful readings produced two wrong answers before that.

| | stage 1 | stage 2 |
|---|---|---|
| butterfly in | `(16,2)` | `(19,5)` |
| after `W_R` rotation | `(17,3)` | `(20,5)` |
| adder-tree level 1 | `(18,4)` | `(21,6)` |
| adder-tree level 2 / out | `(19,5)` | `(22,7)` |

The internal `(22,7)` is then cast down to the declared output `(21,7)`.

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

| | |
|---|---|
| DSP48 | 12 |
| FF | 4017 |
| LUT | 6922 |
| estimated clock | 7.208 ns |
| co-sim latency | 41 cycles, interval 42 |

## What the co-simulation covers

Vitis has no separate "RTL simulation" step for a control-driven (`ap_ctrl_hs`) kernel like
this: **`cosim_design` *is* the RTL simulation.**  It elaborates the synthesized Verilog in
`xsim` and drives it with the same `src/fft_tb.cpp`, so `results/output_cosim.txt` is produced
by hardware, not by C++.

(The separate XSI/BFM path in this repo exists for free-running `ap_ctrl_none` designs, which
Vitis co-simulation cannot drive.  It does not apply here.)
