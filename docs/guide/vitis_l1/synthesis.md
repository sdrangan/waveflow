---
title: Synthesizing a Vitis L1 block
parent: Vitis L1 Blocks
nav_order: 2
audience: hls
api: [kernel_task, KernelTask, VitisL1Step, vitis_fft_include_dir, render_tcl]
summary: "How a Waveflow design ends up calling xf::dsp::fft::fft<> itself: kernel_task() hands the body over, VitisL1Step copies a hand-written header rather than generating one, and render_tcl(include_dirs=...) gives the build an include path to the vendor headers, which are never copied. Covers why the body is an adapter (the vendor wants an array of streams, Waveflow supplies separate ones), why OUT_W is a template argument, the measured RTL result, and the absolute-path trap that presents as a SIGSEGV."
---

# Synthesizing a Vitis L1 block

Waveflow has two codegen paths for a leaf's task body, and vendor IP needs the second.

| | generated body | hand-written body |
|---|---|---|
| step | `TaskBodyStep` | a `Buildable` — here `VitisL1Step` |
| where the C++ comes from | **extracted from `run_iter`** | a checked-in `.h` under `waveflow/build/` |
| what the step does | renders `<name>_task.h` | `read_text` → `write_text`, verbatim |

Nothing can extract an `xf::dsp::fft::fft<>` instantiation from a Python body, and nothing should
try. `VitisFft` therefore overrides `kernel_task()` to name a body someone wrote, while `run_iter`
stays as the pysim golden beside it. The two are twins because the conformance work in
`tests/vitis_l1/` makes that claim true, not because they are generated from one source.

## The body is an adapter, not just a call

The vendor signature is `fft<P>(hls::stream<T> p_in[R], hls::stream<T> p_out[R])` — an **array** of
streams. Waveflow hands each AXI-Stream channel over as its own `hls::stream` of raw words, and an
array cannot be formed from separate stream objects. So `waveflow/build/vitis_fft_task.h` is `R`
pump processes in, the vendor core, `R` pump processes out, inside one `DATAFLOW` region.

AMD's own L2 `fftStreamingKernel` has exactly this shape — `convertSuperStreamToArray`, `fft<>`,
`convertArrayToSuperStream` — but it takes a single *wide* stream per side, which is not this
module's port group. So the adaptation belongs to us, and it is not free: it costs 4 cycles of both
latency and interval at `L=16`, which is the `L/R = 4` words each pump moves. See
[Latency and II for a vendor block](timing.md).

`R` is fixed at 4 in the body. A template cannot vary a function's arity, and 4 is the only radix
the model covers — `VitisFft` refuses anything else, so the two agree.

## `OUT_W` is passed in, deliberately

The output width appears in the body's signature, so it cannot be derived inside it. Rather than
recompute the vendor's `OUTPUT_WL` in C++, the body takes it as a template argument and asserts it:

```cpp
static_assert(OUT_W == (int)T_out::value_type::width,
              "OUT_W disagrees with the vendor's ssr_fft_output_type ...");
```

`VitisFft.kernel_task()` passes the width it derived from the model, so "my derivation matches the
library" is checked by the compiler. A wrong width is a build error instead of a wrong answer.

## The vendor headers are never copied

They stay in the Vitis install and are reached with an `-I`. That was impossible until recently:
`render_tcl` hardcoded

```python
set cf "-I{INCLUDE_DIR}"          # INCLUDE_DIR == "include", and that was all
```

with no parameter for an additional path. It now takes `include_dirs`, **empty by default** so
every existing generated top renders byte-for-byte as before — several are gated on exact RTL
cycle counts, and a changed TCL is a changed build.

`vitis_fft_include_dir()` resolves the path: an explicit argument, then `$WF_VITIS_LIBS` (an
environment variable someone set on purpose outranks a guess), then the Vitis install itself. Vitis
ships the DSP library under `tps/xf_dsp`, code-identical to the upstream `v2025.1_re` tag across
all 45 L1 headers, so a user with only Vitis installed needs neither a checkout nor an environment
variable. It raises rather than returning a path that would fail later inside csynth.

## What the RTL does

`tests/vitis_l1/fft/verifyHwModule/` builds the tree from those three pieces and runs
C-simulation → C-synthesis → C/RTL co-simulation:

```bash
python build.py
vitis-run --mode hls --tcl run.tcl      # about a minute
python verify.py
```

**Bit-exact, 192 values, csim and cosim, and the two identical to each other.** So the Python
model, the C++ body and the synthesized hardware agree on every bit. The committed outputs are
re-checked without a toolchain, so a regenerated output that stopped matching could not land
quietly, and the `vitis`-marked gate in `tests/vitis_l1/fft/test_s2_build.py` reproduces the run.

## Two traps worth keeping

**`-argv` paths must be absolute.** `csim_design` and `cosim_design` run from *inside* the project
tree. A relative path opens nothing, the testbench then dereferences a NULL `FILE*`, and Vitis
reports `CSim failed with errors` and `SIGSEGV` — naming neither the file nor the cause. `build.py`
passes absolute paths and the testbench checks both handles.

**The staleness digest hashes the copies.** `rtl_digest` hashes `include/*.{h,hpp,cpp}`, not
`waveflow/build/*.h`. Editing the source body without the copy step re-running is the documented
way to get a stale gate that still passes.

## What is still open

The top in `verifyHwModule/` is a **stand-in** for what the composite generator will emit: the same
call with the same template arguments `kernel_task()` reports, but written by `build.py` rather
than derived from the interface graph. Wiring `VitisFft` through the composite codegen, so
`check(VitisFft)` passes the four codegen gates and the top is graph-derived, is the remaining
work. What the package proves is that the body, the include path, the widths and the RTL are right.
