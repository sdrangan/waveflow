# FFT verification, 16-point — Vitis simulations → Python model, end to end

A self-contained check that the Python bit-exact FFT model agrees with the real AMD Vitis SSR
FFT: in C-simulation, in synthesized RTL, and with each other.

The 1024-point sibling is [`../verifyFFT1024/`](../verifyFFT1024/); [`../VERIFY.md`](../VERIFY.md)
compares the two.

**Result on this machine: all three comparisons bit-exact over 12 vectors (384 values each).**

Everything here is meant to be run and inspected by hand.  For *what* is being simulated and
why the number formats are what they are, read [ARCHITECTURE.md](ARCHITECTURE.md).

## What is in this folder

```
src/fft_top.hpp      the configuration -- change L, R, widths here
src/fft_top.cpp      the DUT: a named wrapper around the library FFT
src/fft_tb.cpp       the testbench, shared by C-sim and co-sim
run.tcl              drives csim -> csynth -> cosim
data/input.txt       12 input vectors, raw stored integers
encode_input.py      your decimals -> data/user_input.txt  (see 'Bring your own input')
data/user_input.txt  YOUR samples once encoded (gitignored -- yours, not the repo's)
results/             where the two Vitis runs write their outputs
verify.py            compares Vitis output against the Python model
ARCHITECTURE.md      what the DUT is and which formats it uses internally
```

Nothing is generated behind your back: `data/input.txt` and both `results/*.txt` are plain text
you can open, and `verify.py` reads exactly those files.

## Step 0 — environment

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source /home/marco/AmirProjects/waveflow/env/bin/activate
cd /home/marco/AmirProjects/waveflow/fft_bitexact/verifyFFT16
export WF_VITIS_LIBS=/home/marco/AmirProjects/Vitis_Libraries_2025.1/dsp/L1/include/hw/vitis_fft/fixed
```

`WF_VITIS_LIBS` must point at the *fixed-point* FFT headers; `run.tcl` stops with a clear error
if it is unset.  Adjust both paths if your installs differ.

## Step 1 — run the Vitis flow

```bash
vitis-run --mode hls --tcl run.tcl
```

One command runs all three stages in order, each stopping the script on failure:

| stage | what happens | writes |
|---|---|---|
| `csim_design` | the C++ runs natively | `results/output_csim.txt` |
| `csynth_design` | C++ → RTL | `fft_verify_proj/solution1/syn/` |
| `cosim_design` | the **synthesized RTL** runs in `xsim`, driven by the same testbench | `results/output_cosim.txt` |

Expect, in order:

```
WAVEFLOW_CSIM_OK
WAVEFLOW_CSYNTH_OK
WAVEFLOW_COSIM_OK
WAVEFLOW_SUCCESS: csim + csynth + cosim all passed
```

**On "RTL simulation":** for a control-driven (`ap_ctrl_hs`) kernel like this one, `cosim_design`
*is* the RTL simulation — Vitis has no separate step.  `output_cosim.txt` is produced by the
synthesized hardware.

Takes about a minute; the co-simulation dominates.

## Step 2 — verify against the Python model

```bash
python verify.py
```

It checks three things, all on **raw stored integers** (a decimal comparison would hide exactly
the 1-LSB differences that matter):

1. **C-sim vs Python model** — the maths matches
2. **Co-sim vs Python model** — the *synthesized RTL* matches
3. **C-sim vs Co-sim** — C and RTL agree with each other

Check 3 is not redundant: if 1 and 2 both failed identically it would show the model wrong
rather than the flow broken, and vice versa.

Expected tail:

```
  C-sim   vs Python model: BIT-EXACT  (384 values)
  Co-sim  vs Python model: BIT-EXACT  (384 values)
  C-sim   vs Co-sim      : BIT-EXACT  (384 values)

ALL COMPARISONS BIT-EXACT
```

Exit status is 0 only if every comparison is exact, so it can be scripted.

## Step 3 — look at the numbers yourself

```bash
python verify.py --show 1        # vector 1, sample by sample, with real values
```

Prints the Vitis output beside the model's, plus the model's value as a decimal, so you can
sanity-check magnitudes without the comparison relying on decimals.

To read the raw files directly:

```bash
head -5 data/input.txt
head -5 results/output_csim.txt
diff results/output_csim.txt results/output_cosim.txt && echo "C and RTL identical"
```

## The 12 input vectors, and why these

| v | vector | why |
|---|---|---|
| 0 | ramp | ordinary in-range data |
| 1 | pseudo-random | no structure to hide behind |
| 2 | impulse | a flat spectrum — exercises every twiddle |
| 3 | constant | energy in one bin only |
| 4 | alternating ±full-scale | **on the accumulator boundary** |
| 5, 6 | all most-negative / all most-positive | saturation extremes |
| 7 | half min, half max | a step at full scale |
| 8, 9 | two more pseudo-random | independent confirmation |
| 10, 11 | mixed extremes | boundary crossings from other directions |

Vectors 5–11 were added *after* the model's overflow behaviour was worked out, so they are
confirmation rather than the cases it was fitted to.  Five of them sit on or across the
accumulator boundary — the region where an earlier version of the model was wrong on 25 of 32
values while looking perfect on ordinary data.

## Bring your own input

The 12 shipped vectors were chosen to stress particular corners.  To check bit-exactness on
**your own samples** — C-simulation of the library, the synthesized RTL, and the Python model,
all compared bit for bit — you do not have to touch the shipped data.

### 1. Write your samples as ordinary decimals

One complex sample per line, `re im`, vectors back to back.  Blank lines and `#` comments are
ignored:

```text
# my_samples.txt -- 16 samples = one vector
 0.5      0.0
 0.25    -0.125
 ...
```

### 2. Encode, run, compare

```bash
python encode_input.py my_samples.txt                  # -> data/user_input.txt
WF_INPUT=user_input.txt vitis-run --mode hls --tcl run.tcl
python verify.py --input user_input.txt
```

That is the whole flow.  `data/input.txt` and the shipped `results/` are left alone, so you can
go back to the reference run at any time by omitting both options.

### Why the file on disk holds integers, not decimals

`data/*.txt` stores each sample as its **raw stored integer** — the `ap_fixed` bit field:

```text
a real value  r  in ap_fixed<W,I>  is stored as  round(r * 2**(W-I))
```

For this DUT that is `ap_fixed<16,2>`, so `stored = round(r * 2**14)` and the
representable range is `[-2.0, 1.99993896484375]`.  Bit-exactness is a claim about stored bits, and a decimal
round-trip through text can absorb exactly the 1-LSB differences this package exists to detect —
so the on-disk format is unambiguous by construction, and `encode_input.py` does the conversion
once, in one place.  It reads `W` and `I` from `src/fft_top.hpp`, so it always matches the DUT
you are about to build.

### Things that will bite you

| symptom | cause |
|---|---|
| `has N samples, which is not a multiple of the DUT's L=16` | one vector is `L` samples; pad, or change `FFT_L` |
| `value ... is outside ap_fixed<16,2>` | scale your data, or widen `FFT_IN_W`/`FFT_IN_I` — refused rather than silently wrapped |
| `results/ hold N vector(s) ... stale for this input` | you changed the input but did not re-run the flow; the message gives the command |
| `has N samples but src/fft_top.hpp sets FFT_L=...` | the DUT and the data disagree on the transform length |
| `expected 're im', got 1 field(s)` | a line is missing its imaginary part — write `0` for real-valued data |

### Changing size or precision

Edit the `FFT_*` defines in `src/fft_top.hpp` and re-run both steps.  **`verify.py` and
`encode_input.py` both read that header**, so the model, the encoder and the DUT cannot drift
apart — they used to, when the widths were duplicated as literals in `verify.py`.

The Python model covers any **`L = 4^S`** (16, 64, 256, 1024, …).  Sizes that are not a power of
the radix — 32, 128, 512 at `R=4` — use a different "forked" architecture in the library and are
**not** covered; `verify.py` degrades to the C-sim vs co-sim check and says so rather than
comparing the wrong thing.

## If something fails

* `WAVEFLOW_ERROR: set WF_VITIS_LIBS` — Step 0 not done in this shell.
* `missing results/output_csim.txt` — Step 1 did not get that far; read its output.
* A comparison differs — `verify.py --show <v>` prints the first differing sample. Since C-sim
  and co-sim are written by different execution paths, which of the three checks fails tells you
  whether the model, the synthesis, or the testbench is at fault.
