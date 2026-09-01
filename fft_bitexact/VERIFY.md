# Verification folders — 16-point and 1024-point

Two self-contained packages that run the real AMD Vitis SSR FFT through C-simulation,
C-synthesis and C/RTL co-simulation, and compare the results against the Python model.

Same structure, same commands, same file formats — only the transform length differs.

| | [`verifyFFT16/`](verifyFFT16/) | [`verifyFFT1024/`](verifyFFT1024/) |
|---|---|---|
| length | 16 (`L = R^2`, 2 stages) | 1024 (`L = R^5`, 5 stages) |
| radix | 4 | 4 |
| input | `ap_fixed<16,2>` | `ap_fixed<16,2>` |
| output | `ap_fixed<21,7>` | `ap_fixed<27,13>` |
| vectors | 12 | 8 |
| C-sim vs Python model | ✅ bit-exact | ✅ bit-exact |
| Co-sim vs Python model | ✅ bit-exact | ✅ bit-exact |
| C-sim vs Co-sim | ✅ bit-exact (384) | ✅ bit-exact (16384) |
| DSP / FF / LUT | 12 / 4017 / 6922 | 20 / 16381 / 21481 |
| co-sim latency | 41 cycles | 1477 cycles |
| runtime | ~2 min | ~10 min |

Both use the identical flow:

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source ../../env/bin/activate
export WF_VITIS_LIBS=/home/marco/AmirProjects/Vitis_Libraries_2025.1/dsp/L1/include/hw/vitis_fft/fixed

vitis-run --mode hls --tcl run.tcl     # csim -> csynth -> cosim
python verify.py                        # compare against the Python model
```

Each folder has its own `README.md` (the walkthrough) and `ARCHITECTURE.md` (what the design is
and which number formats it uses internally).

## Both are full checks

The Python model covers any `L = 4^S`, so both folders run all three comparisons.

The 1024-point one is the stronger evidence — not because it is bigger, but because two
behaviours appear there that `L=16` structurally cannot expose:

* the twiddle table is only a **quarter wave** past `L=16` (`L/4` entries), rebuilt by
  `readQuaterTwiddleTable` with an exact `-1` substituted at `L/4` and `3L/4`
* a stage's output is **narrowed before** the twiddle rotation, not inside the multiply

A model validated only at 16 passes both folders' first check while being wrong at every larger
size.  See `PLAN.md`, "S5".

`verify.py` still reports honestly when handed a length the model does not cover, so pointing
either folder at, say, `L=512` degrades to the synthesis-only check rather than lying.

## Which sizes take this code path

With radix 4, sizes that are **powers of 4** use the architecture both folders exercise:

    16, 64, 256, 1024, 4096      same architecture, more stages
    32, 128, 512                 a different "forked" architecture -- not examined

Confirmed from the library's own dispatch: forked when `log2(L) % log2(R) != 0`.
