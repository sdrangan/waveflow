# Waveflow `HwModule`s for the Vitis L1 blocks — the plan

**Status:** S0 done (the models are ported and gated in this repo).  S1 onward is open.
**Owner:** Amir Reza Kiani.
**Prerequisite, now satisfied:** bit-exact Python models of the Vitis L1 SSR FFT and BLAS `gemv`.

---

## What this is for

You have proved something the framework needs: given the same input bits, a Python model can
reproduce a Vitis L1 block's output bits exactly.  That closes the gap that made "the pysim and
the RTL are twins" an approximation for vendor IP.

What does **not** exist yet is the thing a Waveflow user would actually instantiate: an
`HwModule` with the Vitis block's interface, which simulates through your model and synthesizes
into a design that calls the vendor library.  That is what this plan builds.

Read this next to [`docs/guide/comp_codegen/freerunning_override.md`](../docs/guide/comp_codegen/freerunning_override.md)
— the mechanism in S2 is the one that page describes.

### The one thing your models do not carry

They model **the arithmetic, not the parallelism** — deliberately, and the FFT docstring says so.
That is correct for bit-exactness and insufficient for an `HwModule`, which also has to predict
*when* data appears.  Bits come from your model; cycles have to come from somewhere else.  S6
handles it, and it is the stage most likely to be underestimated, so do not leave it implicit in
the earlier stages' gates.

---

## S0 — where things live  ✅ DONE

```
waveflow/vitis_l1/          INSTALLED — the models
  fft.py  twiddle.py  cxquant.py  gemv.py  fixed.py
  (S1 adds hw.py here)

tests/vitis_l1/             NOT installed — the conformance apparatus
  fft/   gemv/              test_*.py, golden/, data/, cpp/, verify*/, PLAN.md, VERIFY.md
```

`pytest tests/vitis_l1/` → **92 passed, 2 skipped**, no toolchain needed.

Three things changed in the move, all recorded in [`tests/vitis_l1/README.md`](../tests/vitis_l1/README.md):

* `cpp/vendor_debug/` (45 headers) became `cpp/overlay/` (**3** headers + `wf_trace.hpp`).  Vitis
  ships the DSP library at `tps/xf_dsp/`, code-identical to upstream `v2025.1_re` across all 45
  L1 headers — only the copyright-line glyphs differ — so the other 42 were redundant.  The
  overlay shadows just the three that take `WF_TRACE` calls.
* `regen_golden.sh` now auto-discovers Vitis on Linux and Windows, and passes
  `-std=gnu++14 -D_USE_MATH_DEFINES` (strict ISO hides `M_PI` on mingw).  Both scripts are
  idempotent — verified by regenerating every golden and diffing.
* The goldens needed a `.gitignore` re-include past the blanket `*.json` rule, following the
  existing `tests/calib/golden` precedent.

> **Decide the module home before S1, not after.**  `structure_signature` encodes each object as
> `f"{type(value).__module__}.{type(value).__qualname__}"` (`waveflow/build/elaborate.py:327`), so
> moving an `HwModule` between files later invalidates every
> structure signature and stales the RTL of anything that instantiates it.  This plan puts them in
> `waveflow/vitis_l1/hw.py`.  If you would rather they sat in `waveflow/hw/` beside `Rfdc`, change
> it now, while the cost is a `git mv`.

---

## S1 — the FFT as a pysim `HwModule`

**Goal:** `VitisFft` simulates, with the L1 interface, and no C++ anywhere.

The DUT interface you already synthesized is

```cpp
void fft_top(hls::stream<T_in> p_in[FFT_R], hls::stream<T_out> p_out[FFT_R]);
```

— `R` parallel streams, the SSR bundle.  In Waveflow that is an **R-wide AXI-Stream port group**,
not one port carrying an R-element word.  The precedent is `Rfdc`'s `n_ch` ports
(`waveflow/hw/rfdc.py`); follow its shape rather than inventing one, including how it refuses a
mismatched channel count (`rfdc.py:276`).

Make it a `FreeRunMod`: the block streams continuously and is not host-launched.

`run_iter()` is the pysim golden — one firing = one transform.  It reads `L/R` super-samples from
the `R` input ports, calls `fft_general(...)` from `waveflow.vitis_l1.fft`, and writes the result
back across the `R` output ports.  **Do not reimplement any arithmetic here.**  If `run_iter`
contains a multiply, something has gone wrong.

### Which parameters bind when

`L` is **fixed at build time**.  It is `ssr_fft_param_struct::N`, a `static const int` consumed as
a template argument, and in L2 it also sizes the kernel's port arrays
(`ap_uint<512> p_fftInData[N * NUM_FFT_MAX / R]`).  There is no runtime `N`: two lengths are two
bitstreams.  That is exactly what `HwParam` means, so `L: HwParam[int]`.

The one genuinely runtime knob in the whole block is L2's `n_frames` — see S4.

| | binding | why |
|---|---|---|
| `L`, `R`, `in_w`, `in_i`, `tw_w`, `tw_i` | `HwParam[int]` | C++ template arguments; distinct values are distinct artifacts |
| `scaling_mode`, `output_order` | plain field holding an `IntEnum` | still template arguments, but see the mechanical note below |
| `n_frames` (L2 only) | runtime | a scalar kernel argument → an `s_axilite` register |

**Why the enums are not `HwParam`.**  `HwModule.__post_init__` rewrites every `HwParam` value as
`HwParamValue(int(value))` (`waveflow/hw/hw_module.py:858`), so an enum member would be flattened
to a bare int and lose the name `run_iter` needs to select the model's mode.  They are still
template arguments — `kernel_task()` passes `int(...)` explicitly.  This is the same constraint
that made `Rfdc.word` a plain field, recorded there at `rfdc.py:110`.

### The class

```python
# waveflow/vitis_l1/hw.py
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.vitis_l1 import fft as fft_model


class ScalingMode(IntEnum):
    """``xf::dsp::fft::scaling_mode_enum`` (``hls_ssr_fft_enums.hpp:71``), in DECLARATION order.

    ``GROW_TO_MAX_WIDTH`` is **1** and ``SCALE`` is **2** — the vendor's own doc comment four
    lines above the enum lists them the other way round.  Transcribing the comment instead of the
    declaration silently selects the wrong mode, which csynth accepts.
    """
    NO_SCALING = 0
    GROW_TO_MAX_WIDTH = 1
    SCALE = 2


class OutputOrder(IntEnum):
    NATURAL = 0
    DIGIT_REVERSED_TRANSPOSED = 1


_MODE_TO_MODEL = {
    ScalingMode.NO_SCALING:        fft_model.NO_SCALING,
    ScalingMode.SCALE:             fft_model.SCALE,
    ScalingMode.GROW_TO_MAX_WIDTH: fft_model.GROW_TO_MAX_WIDTH,
}


@dataclass
class VitisFft(FreeRunMod):
    """AMD Vitis DSP L1 SSR FFT, fixed point — ``L`` points across ``R`` lanes each side.

    ``R`` is both the butterfly radix and the SSR factor, so it is also the **port count**:
    ``R`` AXIS slaves in, ``R`` AXIS masters out, matching
    ``fft_top(hls::stream<T_in> p_in[R], hls::stream<T_out> p_out[R])``.

    Bits come from :mod:`waveflow.vitis_l1.fft`, which is bit-exact against the vendor library;
    :meth:`run_iter` performs no arithmetic of its own.
    """

    cpp_kernel_name: ClassVar[str | None] = "vitis_fft"
    cpp_namespace: ClassVar[str | None] = "vitis_fft_impl"

    # -- build-time; each one is a C++ template argument -----------------------------------
    L: HwParam[int] = 1024      # ssr_fft_param_struct::N — fixed, no runtime equivalent
    R: HwParam[int] = 4         # radix AND SSR factor AND the number of ports per side
    in_w: HwParam[int] = 16     # input ap_fixed<W, I>
    in_i: HwParam[int] = 2
    tw_w: HwParam[int] = 18     # twiddle_table_word_length
    tw_i: HwParam[int] = 2      # twiddle_table_intger_part_length (the vendor's spelling)

    # -- build-time too, but not HwParam; see "Why the enums are not HwParam" --------------
    scaling_mode: ScalingMode = ScalingMode.NO_SCALING
    output_order: OutputOrder = OutputOrder.NATURAL

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        L, R = int(self.L), int(self.R)

        # Refuse loudly rather than approximate: each of these is a case the MODEL does not
        # cover, so silently accepting it would produce confident wrong bits.
        if R != 4:
            raise NotImplementedError(
                f"{type(self).__name__}: only R=4 is modelled (got {R}). Radix 2/8/16 change "
                f"the butterfly matrix and tree depth — see tests/vitis_l1/fft/PLAN.md S6.")
        # `_log` is private today; promote it (or inline the check) rather than
        # reaching through the underscore from another module.
        self.n_stages = fft_model._log(L, R)        # raises unless L == R**S
        if self.output_order is not OutputOrder.NATURAL:
            raise NotImplementedError(
                f"{type(self).__name__}: only SSR_FFT_NATURAL is modelled; the golden and every "
                f"gate pin that ordering.")
        if L != 16 and self.scaling_mode is not ScalingMode.NO_SCALING:
            raise NotImplementedError(
                f"{type(self).__name__}: all three scaling modes are pinned at L=16 only; "
                f"fft_general covers NO_SCALING for any L=4^S (got L={L}, {self.scaling_mode!r}).")

        # The output type is NOT the input type: it widens with L, by the library's own
        # OUTPUT_WL = in_W + log2(L) + 1.  Derive it rather than restating it, so changing L
        # cannot silently leave the port at the wrong width.
        #
        # Take it from `fft_general`'s THIRD return value, which is the format the model
        # actually produced.  Do NOT use `stage_formats(...)[-1][1]`: that is the last stage's
        # tree output, one bit WIDER than the port, because the library applies a final
        # narrowing cast after the last stage (22 vs 21 at L=16).  Verified against
        # OUTPUT_WL at L = 16 / 64 / 256 / 1024.
        _z = np.zeros(L, dtype=np.int64)
        *_, self.out_fmt = fft_model.fft_general(
            _z, _z, L, int(self.in_w), int(self.in_i),
            int(self.tw_w), int(self.tw_i), _MODE_TO_MODEL[self.scaling_mode])

        in_bw = 2 * int(self.in_w)          # std::complex<ap_fixed<W, I>> — re and im
        out_bw = 2 * int(self.out_fmt.W)

        self.s_in = [StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in_{i}",
                                   bitwidth=in_bw, has_tlast=True) for i in range(R)]
        self.m_out = [StreamIFMaster(sim=self.sim, name=f"{self.name}_m_out_{i}",
                                     bitwidth=out_bw, has_tlast=True) for i in range(R)]
        # Indexed ATTRIBUTES as well as lists: KernelTask.signature and BfmModel.ports name
        # endpoints by attribute name, and `s_in[0]` is not one.  Two views, one set of objects.
        for i, ep in enumerate(self.s_in):
            setattr(self, f"s_in_{i}", ep)
        for i, ep in enumerate(self.m_out):
            setattr(self, f"m_out_{i}", ep)
        for ep in (*self.s_in, *self.m_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        """Hand the vendor call over as the body — Waveflow will not extract an
        ``xf::dsp::fft::fft<>`` instantiation from Python, and should not try."""
        return KernelTask(
            "vitis_fft_task", "vitis_fft_task.h",
            tuple([f"s_in_{i}" for i in range(int(self.R))]
                  + [f"m_out_{i}" for i in range(int(self.R))]),
            template_args=(int(self.L), int(self.R), int(self.in_w), int(self.in_i),
                           int(self.tw_w), int(self.tw_i),
                           int(self.scaling_mode), int(self.output_order)))

    def run_iter(self):
        """The pysim golden — one firing = one transform.  No arithmetic lives here."""
        ...   # read L/R super-samples across s_in, call fft_model.fft_general, write m_out
```

Two things to settle while writing `run_iter`:

* **`template_args` is typed `tuple[int, ...]`** (`waveflow/hw/mem_stream.py:203`) and its values
  end up in the task instance name (`composite_gen.py:106`).  Passing `int(self.scaling_mode)`
  keeps that honest; the C++ side casts back with
  `static const scaling_mode_enum scaling_mode = (scaling_mode_enum)SCALING;`.
* **Eight template arguments is a lot**, and the instance name carries all of them.  Consider
  collapsing the six numeric ones into a generated params struct in `vitis_fft_task.h` and
  templating on `L`/`R` only — decide before S3, because the name feeds the XSI staleness hash.

**Gate:** a pysim run of `VitisFft` at `L=16` and `L=1024` reproduces
`tests/vitis_l1/fft/golden/` exactly, compared as stored integers.  Add it as
`tests/vitis_l1/fft/test_hwmodule.py`.  This gate needs no toolchain, so it must stay in the fast
suite.

---

## S2 — synthesis: hand the vendor call over as the task body

**Goal:** `VitisFft` generates HLS that calls `xf::dsp::fft::fft<>`, and csynths.

Waveflow will not extract a vendor template call from a Python body, and should not try.  The
mechanism for "the body is C++ someone else wrote" is a **`kernel_task()` override**.  The working
template is `MemRStream` (`waveflow/hw/mem_stream.py:368`):

```python
def kernel_task(self) -> "KernelTask":
    return KernelTask("vitis_fft_task", "vitis_fft_task.h",
                      ("s_in", "m_out"), template_args=(self.L, self.R, ...))
```

`run_iter()` stays as the pysim golden; the two are twins, and your conformance work is what makes
that claim true rather than hopeful.  The C++ side is a thin `vitis_fft_task.h` that instantiates
the vendor template with the parameters the Python module declares — essentially
`tests/vitis_l1/fft/verifyFFT1024/src/fft_top.cpp`, parameterized.

### How the C++ actually reaches the build

There are **two** codegen paths, and the `kernel_task()` override selects the second:

| | generated body | hand-written body ← **this is S2** |
|---|---|---|
| step | `TaskBodyStep` (`hwcodegen_steps.py:179`) | a `Buildable`, e.g. `MemStreamStep` (`streamutils.py:416`) |
| where the C++ comes from | **extracted from `run_iter`** | a checked-in `.h` under `waveflow/build/` |
| what the step does | renders `<name>_task.h` into `include/` | `read_text` → `write_text`, verbatim |

So: a separate `BuildStep`, yes — but one that **copies**, not one that extracts.  Extraction is
the path we are deliberately not using, because nothing can extract an `xf::dsp::fft::fft<>`
instantiation from Python.  `mem_r_stream_task.h` and its nine siblings already sit in
`waveflow/build/` for exactly this reason, and their docstring says why: *"they own `m_axi`, so all
stay hand-written and copied rather than generated."*

**S2 therefore adds a `VitisL1Step(Buildable)`** beside `MemStreamStep`, whose `build_outputs`
names `vitis_fft_task.h` and whose `generate()` returns it from `waveflow/build/`.  That is about
twenty lines, and `MemStreamStep` is the template.

### The one thing neither path handles today

The **vendor headers are not copied anywhere** — they stay in the Vitis install and are reached
with an `-I`.  Your own working TCL does this:

```tcl
set cf "-I$here/src -I$vlib -std=c++14"       # $vlib = $WF_VITIS_LIBS
```

but `render_tcl` (`composite_gen.py:2715`) hardcodes

```python
set cf "-I{INCLUDE_DIR}"                       # INCLUDE_DIR == "include", and that is all
```

with no parameter for an additional include path, and no caller in the repo passes one.  **So S2
has a second, genuine framework deliverable: extend `render_tcl` with an `extra_cflags` (or
`include_dirs`) argument.**  Default it to empty so every existing generated top renders
byte-for-byte as before — several are gated on exact RTL cycle counts, and a changed TCL is a
changed build.

Resolve the path the way `regen_golden.sh` now does — prefer the Vitis-shipped
`tps/xf_dsp/L1/include/hw/vitis_fft/fixed`, fall back to `$WF_VITIS_LIBS` — so a user with only
Vitis installed needs no checkout and no environment variable.

Two related notes:

* `render_tcl`'s `solution_config` already carries `config_rtl -reset state`, and its docstring
  records *why*: an `hls::task` that writes before it reads advances during reset, and
  `#pragma HLS reset` **in the body does not close it** (measured, Vitis 2025.1).  If the FFT task
  emits before its first read, you need the solution-level setting, not the pragma.
* The RTL staleness digest hashes `include/*.{h,hpp,cpp}` — the **copies**, not
  `waveflow/build/*.h` (`rtl_digest.py:19`).  Editing the source without the copy step re-running
  is the recorded way to get a stale gate that still passes.

Traps worth reading before you write the task, all of them previously paid for in this repo:

* **An `hls::task` that writes before it reads counts during reset.**  A free-running FFT task
  that emits anything before its first read will produce phantom outputs; `#pragma HLS reset` is
  the fix.
* **Write-then-blocking-read on two streams lands in one state and deadlocks.**  Use a `read_nb`
  loop if the task ever needs a request/response shape.
* **Boundary `TLAST` comes from `ap_axis`**, not from a `framed_word` — the framed form is
  internal only.
* **`StreamIF.depth` is a physical depth for internal channels only.**  At a boundary port Vitis
  ignores it, silently.

**Gate:** `check(VitisFft)` passes all four codegen gates, and a csynth of the generated top
succeeds with the same II you already measured in `verifyFFT1024`.  Mark it `@pytest.mark.vitis`.

---

## S3 — the RTL rung

**Goal:** the generated design, driven through real RTL, is bit-exact against the pysim.

This is the stage that turns "the model matches a C simulation" into "the model matches the
hardware".  You have most of the input data already: `verifyFFT1024/results/output_cosim.txt` is a
cosim output, and I confirmed it matches a native rebuild of the DUT on all 8192 samples.

Write an XSI gate on the BFM pattern used by the `rf_*` designs.  Two hard requirements:

* **Raise `WANT_XSI_GATES`** in `tests/conftest.py` (currently **127**) by however many gates you
  add.  `-m xsi` fails if a gate *skips*, which is the whole point — a silently skipped RTL gate
  is indistinguishable from a passing one.
* **XSI discards `$display`/`$error`.**  Any condition you want to assert has to be read out of
  the VCD, and paired with a deliberately-dirty run that proves the assertion can fail.

**Gate:** an exact cycle count, recorded in the test, plus bit-exactness against the pysim.

---

## S4 — L2, which is cheaper than it looks

**Goal:** the memory-mapped form of the same block.

`tps/xf_dsp/L2/include/hw/vitis_fft/fixed/vitis_fft/fft_kernel.hpp` is a pure AXI-MM wrapper over
the **identical** L1 core.  Its `readLines`/`writeLines` only bit-slice an `ap_uint<512>`
super-sample into `t_R` complex samples and back — **no arithmetic at all**.

So there is no second model to build and no second bit-exactness claim to make.  L2 is S1's module
plus:

* `m_axi` endpoints in and out, and an `n_frames` parameter;
* a 512-bit pack/unpack layer.

**Use the schema machinery for the packing.**  `WideType`-style N-in-512 layout is exactly what
`DataList`/`DataArray` and the generated `<stem>_array_utils.h` serializers exist for, and hand-rolled
`.range()` packing is a recurring source of bugs here that hide at width 1.  `RfdcSampWord`'s
dense-14-in-64 work is the precedent to copy.

**Gate:** the L2 module's output equals the L1 module's output for the same input, and a csynth
succeeds.  Because L2 adds no arithmetic, any difference is a packing bug — which makes this a
sharp gate, so do not weaken it to a tolerance.

---

## S5 — the same arc for GEMV

Identical structure, with two differences worth planning around.

**The interface is a wide word, not a port group.**  `gemv` streams
`hls::stream<WideType<t_DataType, t_ParEntries>>`, so `ParEntries` is a word layout, where the
FFT's `R` was a port count.  Same `DataList` machinery, different shape.

**`ParEntries` changes the answer.**  It is not a throughput knob layered over fixed arithmetic —
it sets the reduction's beat width, and your goldens sweep it for that reason.  It must be a
declared parameter that feeds both `run_iter()` and the template args, and a gate should assert
that two different `ParEntries` give *different* bits, so the parameter cannot go inert.

**Also decide what to do about the `ap_fixed` defect.**  You found that shipped `gemv` destroys the
fractional bits of every `ap_fixed` instantiation (`dotHelper.hpp`, `p_res.write(l_res)` narrowing
by numeric conversion), and you model both behaviours.  An `HwModule` has to pick one as its
default, and the honest default is **as-shipped**, because that is what the hardware does — with
the patched behaviour available behind an explicit flag and a docstring that says why the flag
exists.  A model that silently produces the *correct* answer where the hardware produces a wrong
one is not a twin.

**Report both defects upstream to AMD**, independently of this plan.  The `ap_fixed` narrowing and
the `axpy` FMA contraction are real findings and worth an issue each.

---

## S6 — cycles

Bits are done; timing is not modelled at all.  The block *is* pipelined, so the obvious worry is
that a single end-to-end number cannot represent it.  That worry is right, but the fix is smaller
than modelling every stage.

### What the hardware actually is

Two facts from the vendor source, and they point the same way:

* **The boundary is frame-buffered.**  `fftStreamingKernel`
  (`hls_ssr_fft_streaming_kernel.hpp:44`) is `convertSuperStreamToArray` → `fft<>` on a full
  `T_in[R][N/R]` array → `convertArrayToSuperStream`, three processes under `#pragma HLS DATAFLOW`.
  The core consumes a whole frame before it emits one.  There is no smooth sample-by-sample flow
  where sample *k* leaves at *k + L*.
* **Inside, the stages are a chain.**  `FFTStageClassS2S` (`hls_ssr_fft.hpp:425`) recursively
  chains `S = log_R(L)` stages through `hls::stream`s pragma'd to `depth = 8` — but between them sit
  the data commutors and transposers, which reorder *across* the frame.  So the depth-8 FIFOs are
  not the inter-stage latency; the reordering is.

So what is observable at the ports is: **frame in → latency → frame out, with frames overlapping at
some initiation interval.**  Per-sample internal events would model a flow that the boundary does
not expose.

### The real modelling error is conflating two numbers, not omitting stages

A pipeline exists precisely to **decouple latency from throughput**.  What a system model needs is
both:

* **latency** — first input to first output, roughly `S ×` the commutor reordering cost;
* **II** — how often a new frame can start, roughly `N/R`.

A model carrying one number ("the transform costs T") is wrong the moment the FFT sits in a chain
with anything else, which is the only reason to build it.  A model carrying **both** is right for
every feed-forward use, and gets there without any per-stage machinery.  So: your end-to-end
equivalent, on condition that it is two numbers rather than one.

### Why not one `hls::task` per stage

It is the most faithful picture, and it has a recorded failure mode in this repo: an **un-paced
free-running N-stage pipeline deadlocks at `done = N+1`**, and the fix is a per-job token.  Model
`S` stages as `S` free-running pysim processes and you will spend the stage debugging your model's
concurrency instead of measuring the hardware's timing.  Related and also recorded: back-pressure
paces the **rate**, not how far ahead a free-running producer runs, so adding FIFO depth between
modelled stages does not buy the behaviour it looks like it should.

Escalate to per-stage only when a question actually demands it — the FFT inside a **feedback loop**,
or a DSE that varies stage count or shares resources between stages.  Neither is on the path here.

### Two numbers need two processes

A single sequential `run_iter` **cannot** express them.  Its loop is read-frame → delay →
write-frame, so frame *k+1* is not accepted until frame *k* has been written: back-to-back jobs
serialize and II collapses into latency.  That is wrong exactly where it matters — a host issuing
frames back to back, which is the normal case.

So the pysim needs **two processes and a bounded queue between them**:

| | | records |
|---|---|---|
| **intake** — stays `run_iter` | accept a frame, compute the bits **once**, push `(frame, t_ready)` | the **II** firing |
| **emit** — an extra process | pop, wait until `t_ready`, write the frame out | the **latency** |

Keeping intake as `run_iter` matters: `_run_iter_forever` is what populates `firing_records` and
drives `timed_delay`, so the calibration path keeps working unchanged.  The bits are computed once,
at intake — the emit side only releases them.

The framework already supports this and there is a precedent to copy: `Rfdc.run_proc`
(`waveflow/hw/rfdc.py:503`) runs its DAC path as its own process and its ADC path inline —

```python
def run_proc(self):
    self.process(self._emit_proc())      # SimObj.process — simobj.py:159
    yield from super().run_proc()        # the _run_iter_forever loop; run_iter == intake
```

and `SimObj.transaction_queue(capacity)` (`simobj.py:195`) is the SimPy `Store` to put between them.

**The queue capacity is the model's third number, and it is not cosmetic.**  It bounds how many
frames are in flight — roughly `ceil(latency / II)` — and it is what makes back-pressure correct
when the downstream stalls.  Leave it unbounded and the module will happily accept frames forever
while its consumer is blocked, which no hardware does.

This is also what keeps the design on the right side of the recorded deadlock law.  That law is
about *un-paced* free-running chains; a **finite** queue plus explicit ready-times is the paced
form, and the bound is the pacing.  So: finite and deliberate, never `float("inf")`.

**Two pysim processes against one C++ task is not a divergence.**  `kernel_task()` is the
realization hook — the generator takes the descriptor and never extracts `run_iter`
(`composite_gen.py:1030`) — so the pysim's process structure is free.  The two processes are the
pysim expressing what Vitis implements with `#pragma HLS DATAFLOW` inside a single call.

### Where the numbers come from

`FreeRunMod.timed_delay(features)` (`hw_freerun.py:234`) predicts a firing's delay from an attached
model and records the firing for calibration, returning `0.0` when uncalibrated — so
`yield self.timeout(self.timed_delay({…}))` is safe to write unconditionally.

Seed both numbers from what you already produce: `waveflow/utils/csynthparse.py` pulls
`PipelineII` and `Latency` per module out of `csynth.xml`.  Then let the S3 XSI gate falsify them —
and make that gate **two frames back to back**, not one, because a single-frame gate cannot tell a
correct II from a serialized one.  That is the whole failure this section exists to prevent.

**Acceptance criterion:** a predicted **latency and II** with a measured error bar, not a
plausible-looking formula.  And note that a counter in a BFM is not the wire — a previous arc here
read 191 where the VCD said 192, because a counter and `TVALID && TREADY` are different things.

---

## The documentation track — one page per stage, and a worked example that grows

Docs are **not** a final stage.  Each stage lands its own page plus whatever the example can do by
then, so the explanation arrives while the work is fresh and can be reviewed before the next stage
builds on it.

### Where it goes

One name across all four trees:

```
waveflow/vitis_l1/        the models + HwModules
tests/vitis_l1/           the conformance apparatus
docs/guide/vitis_l1/      the explanation — what the block is, how it is wrapped, why
examples/vitis_fft/       the worked example (snake_case, like every sibling)
docs/examples/vitis_fft/  the example's walkthrough
```

`examples/` is an installed package (`pyproject.toml`, `packages.find`) using PEP 420 namespace
packages — **no `__init__.py`** — so `from examples.vitis_fft... import ...` resolves from any
working directory after `pip install -e`.

### What lands when

| stage | guide page | example can do |
|---|---|---|
| S1 | `index.md` — what the SSR FFT is, the R-wide port group, which parameters bind when | instantiate `VitisFft`, drive one frame, compare against the golden |
| S2 | `synthesis.md` — the `kernel_task` override, why the body is copied not extracted | add the csynth rung |
| S3 | extend `synthesis.md` with the RTL rung and its cycle count | — |
| S4 | `l2.md` — the memory-mapped form, and why it needs no second model | second design in the same graph |
| S5 | `gemv.md` | — |
| S6 | `timing.md` — latency vs II, and why the pysim needs two processes | **two frames back to back**, comparing predicted vs measured |

The S6 example is the one worth designing for now: "send data, get output, compare values **and**
timing" only becomes a real check at two frames, because one frame cannot tell a correct II from a
serialized one.

The frequency-domain matched filter comes after S6 as a second example — it composes two FFTs and a
multiply, so it exercises exactly the latency/II decoupling S6 builds, and it is the first thing
that would expose a wrong II at the system level.

### How this becomes supervision rather than prose

Two existing gates make a page falsifiable, and both should be used from S1:

* **`snippets: run`** in a page's front matter (`tests/docs/test_doc_snippets.py`) concatenates
  every ```` ```python ```` fence on the page into one script, runs it, and requires the following
  ```` ```text ```` fences to match stdout.  A line of exactly `...` means "skip ahead".  So the
  walkthrough in `docs/examples/vitis_fft/` is *executed*, not just written — if `VitisFft`'s API
  moves, the page fails.
* **`tests/docs/test_documented_numbers.py`** recomputes numbers a page quotes against the source
  of truth.  Every cycle count, II and latency in `timing.md` belongs here.  This is the one that
  matters most for S6: it makes "the doc says II = N/R" a claim CI can refute.

Without these, a stage's page is a promise; with them it is a gate.  A reviewer then reads the
prose for *judgement* and lets the harness check the *facts* — which is the only way review scales
past the first two stages.

### Conventions

Front matter follows `docs/examples/rf_shot_loopback/index.md`: `title`, `parent`, `nav_order`,
`has_children`, `audience`, `summary`.  Child pages carry `parent: <the parent's TITLE string>` plus
`grand_parent: Examples`.

**The trap:** Just the Docs binds `parent:` by **title string**, not by path, so two pages sharing a
title silently attach to the wrong parent.  Titles must be unique site-wide; there is a test for it.

Reference a flow's steps **by name and link**, never as "Step 3" — numbering goes stale the first
time a step is inserted, and the reader cannot tell which one you meant.

---

## Scope, stated so it does not drift

| | covered | **not** covered |
|---|---|---|
| FFT | fixed point, `R=4`, `L=4^S`, NATURAL, FORWARD, TRN | radix 2/8/16; forked sizes 32/128/512; float; inverse |
| GEMV | L1 `dot_tree` (float/double), `dot_dsp` (int), `ap_fixed` | L2 `krnl_gemv` (multi-channel DDR, needs XRT) |

The FFT's `S6` — other radices and the forked architecture — stays open and is **not** a
prerequisite for any stage here.  Build the `HwModule` for what is proved, and let the module
refuse parameters the model does not cover rather than silently approximating them.  A loud refusal
is a feature; this is the one place where being unhelpful is correct.

---

## Known defects carried over

* **`tests/vitis_l1/gemv/cpp/gen_input_f64.py` is not host-reproducible.**  It scales by
  `10.0 ** rng.integers(...)`, and libm `pow` differs in the last bit between glibc and mingw, so
  ~81 of 3077 values move by 1–2 ULP across hosts.  The golden is insensitive (those are
  subdominant terms in sums spanning 1e-12…1e12) and every gate passes either way, but a
  regeneration elsewhere shows the data file as changed.  Worth fixing with exact powers
  (`np.ldexp`, or a table) so regeneration is deterministic everywhere.
* **`tests/vitis_l1/` has 18 ruff findings** (mostly `E702`, semicolons in `verify.py`).  Left as
  they were — `tests/hw/` has 26, so this is the existing baseline, and `CLAUDE.md` lints
  `waveflow/` only.  `waveflow/vitis_l1/` is clean.
* **One FFT gate skips by design** — `test_twiddle.py:110`, "L=16 does not discriminate the two
  orders; revisit at a larger L".  Now that `L=1024` goldens exist, that revisit is cheap and would
  close an honest hole.
