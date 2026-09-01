# FFT verification — Vitis simulations → Python model, end to end

A self-contained check that the Python bit-exact FFT model agrees with the real AMD Vitis SSR
FFT: in C-simulation, in synthesized RTL, and with each other.

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
cd /home/marco/AmirProjects/waveflow/fft_bitexact/verify
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

Takes a couple of minutes; the co-simulation dominates.

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

## Changing the test

* **Different inputs** — edit `data/input.txt` (header is `<n_vectors> <n_samples>`, then one
  `re im` pair per line as raw stored integers) and re-run both steps.  `verify.py` re-runs the
  model on whatever inputs it finds, so nothing needs regenerating.
* **Different size or precision** — edit the `FFT_*` defines in `src/fft_top.hpp`.  Note the
  Python model currently supports **L=16, R=4 only**; `verify.py` will stop with a clear width
  mismatch rather than silently comparing the wrong thing.

## If something fails

* `WAVEFLOW_ERROR: set WF_VITIS_LIBS` — Step 0 not done in this shell.
* `missing results/output_csim.txt` — Step 1 did not get that far; read its output.
* A comparison differs — `verify.py --show <v>` prints the first differing sample. Since C-sim
  and co-sim are written by different execution paths, which of the three checks fails tells you
  whether the model, the synthesis, or the testbench is at fault.
