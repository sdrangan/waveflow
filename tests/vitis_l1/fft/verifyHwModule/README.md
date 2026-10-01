# `verifyHwModule/` — S2 and S3 for `VitisFft`

Takes the **generated** top for `waveflow.vitis_l1.hw.VitisFft` through C-simulation,
C-synthesis and C/RTL co-simulation, and compares both outputs against the vendor golden.

**Result: bit-exact, 192 values, csim and cosim, and the two identical.** So the Python model, the
C++ body and the synthesized RTL all agree on the bits.

```bash
source /tools/Xilinx/2025.1/Vitis/settings64.sh
source /tools/Xilinx/2025.1/Vivado/settings64.sh
python build.py                         # include/, gen/, run.tcl  — all from framework pieces
vitis-run --mode hls --tcl run.tcl      # csim -> csynth -> cosim, ~55 s
python verify.py
```

`build.py` writes nothing by hand: the body header comes from `VitisL1Step`, the vendor include
path from `vitis_fft_include_dir()`, and the TCL from `render_tcl(include_dirs=...)`. If any of
those regress, this package stops working — which is the point of building it this way.

## What it had to solve

Waveflow hands each AXI-Stream channel over as its own `hls::stream` of raw words, while
`xf::dsp::fft::fft<>` wants `hls::stream<T> p_in[R]` — an **array** of streams, which cannot be
formed from separate stream objects. So `waveflow/build/vitis_fft_task.h` is an adapter: `R` pump
processes in, the vendor core, `R` pump processes out, inside one `DATAFLOW` region. The vendor's
own L2 `fftStreamingKernel` has the same shape, but takes a single *wide* stream per side, which is
not this module's port group — so the adaptation is ours.

The packing — **real part in the low bits**, matching `std::complex`'s first member — was a
documented choice in S1 and is now *measured*: this flow reproduces the golden through the real
vendor types, so the convention is pinned.

`OUT_W` is a template argument rather than derived in the body, because it appears in the
signature. That is deliberate: the Python module derives the same number from the model, and the
body `static_assert`s it against the vendor's own `ssr_fft_output_type`, so a wrong width is a
**compile error** rather than a wrong answer.

## Measured cycles, and why they are not what the plan assumed

| | latency (min/avg/max) | interval (min/avg/max) |
|---|---|---|
| this top (`L=16`) | **45 / 45 / 46** | **46 / 46 / 47** |
| `../verifyFFT16` (array ports) | 41 / 41 / 42 | 42 / 42 / 43 |

Two findings worth carrying into S6:

* **The adapter costs +4 cycles** of both latency and interval — exactly the `L/R = 4` words each
  pump moves. Explainable, and the price of the port group.
* **Interval ≈ latency, so frames do not overlap.** `plans/vitis_l1_hwmodule.md` seeds S6 with
  `II ≈ N/R`, which at `L=16` would be 4. The measurement is 46. Configured with the real numbers,
  `VitisFft` reports **one** frame in flight (`ceil(45/46)`), which matches the hardware;
  configured with the plan's seed it would have claimed six, over-promising throughput sixfold.
  This is why `latency_cycles`/`ii_cycles` have no defaults.

**C-synthesis cannot supply these numbers.** The plan expects to seed them from
`csynthparse` (`PipelineII` / `Latency` out of `csynth.xml`), but for this top every latency and
interval field reads `undef` — it is a `DATAFLOW` region, so Vitis does not bound it statically.
Co-simulation is the only source, which makes S3 load-bearing rather than merely confirmatory.

## A trap worth keeping

`csim_design` and `cosim_design` run from **inside** the project tree, so every `-argv` path must
be absolute. A relative one opens nothing, the testbench then dereferences a NULL `FILE*`, and
Vitis reports `CSim failed with errors` / `SIGSEGV` — naming neither the file nor the cause.
`build.py` passes absolute paths and `src/tb.cpp` checks both handles.

## Files

```
build.py              writes include/, gen/, run.tcl from the framework pieces
verify.py             compares results/ against data/golden_output.txt
src/tb.cpp            the testbench, shared by csim and cosim
data/input.txt        the 12 L=16 golden input vectors (raw stored integers)
data/golden_output.txt   what the vendor produced for them
results/              output_csim.txt, output_cosim.txt — committed, they are the evidence
```

`include/`, `gen/`, `run.tcl` and the Vitis project tree are generated and gitignored.

## What is still open

The top here is a **stand-in** for what the composite generator will emit: the same call, with the
same template arguments `VitisFft.kernel_task()` reports, but written by `build.py` rather than by
the generator. Wiring `VitisFft` through the composite codegen — so `check(VitisFft)` passes the
four codegen gates and the top is graph-derived — is the remaining S2 work. What this package
proves is that the body, the include path, the widths and the RTL are right.

S3's XSI/BFM gate with an asserted cycle count is also open; the cycles above come from Vitis
co-simulation. Per the plan, an XSI gate would need `WANT_XSI_GATES` in `tests/conftest.py` raised
and its assertions read out of the VCD, since XSI discards `$display`.
