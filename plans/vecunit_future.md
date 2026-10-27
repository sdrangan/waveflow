# Vector unit: planned changes, batched

Changes to `waveflow/vecunit/` collected so they can be made together: the slow gates (csynth, the
XSI RTL runs, on Vitis / Vivado 2025.1 on this machine) then run once for the whole batch rather
than once a change. Each item says what changes, why, where, and which tests it needs:
**fast** (`pytest -m "not vitis and not xsi and not gem5"`), **csynth** (`-m vitis`), or **XSI**
(`-m xsi`, `tests/vecunit/test_unit_xsi.py` and the examples' gates).

Status: a first batch implemented on branch `vecunit-batch` (2026-10-27, local, not pushed); see
*Batch status* below.

## Batch status (2026-10-27)

| Item | State | Where |
|---|---|---|
| F1 queue names | done: `qi<n>` / `qo<n>`; ports stay `p<n>` (`in` / `out` was not confirmed) | commit 654615d8 |
| F2 scalar extensions | done: interpreter, assembler, pysim, HLS; exact-rational tests; toy `poly` | d489b934, ddf2f966 |
| F3 real lanes | not started: its decisions are open; to become its own staged plan | -- |
| F4 result shift | done, decision 5 (the override bit) as proposed; `rs_max` default 15; CG and the matched filter pinned at `rs_max = 0` (their calibrated builds) | d489b934, ddf2f966 |
| F5 long vectors | not started: its first steps are measurements | -- |
| F6 one program | done: `asm.split` | d489b934 |
| F7 dependent fusion | done, `fuse_raw_vl` a configuration constant (default 0, off); an RTL gate at 64 checks csynth's depth is below it | d489b934, ddf2f966 |
| R1, R2 load/store errors | **not done**: needs a decision -- how the load and store tasks report (below) | -- |
| R3 deadlock | done: `asm.schedule_problems`, exact (checked against every interleaving of small jobs) | d489b934 |
| R4, R5, R6 | done | d489b934 |
| R7 held-out score | partly: the guide says the Stage 7 scores predate the Stage 9d fixes; the re-score is open | ddf2f966 |
| R8 docs | done | ddf2f966 |
| PR #300 | queue `funct` 4 reserved (`isa.QOp`, `vecunit_core.h`) | d489b934, ddf2f966 |

**R1 / R2, the decision they need.** A load or store program error, and a burst whose length is
not its `wf.ld`'s (R1), must be fatal *and reported*, but only the execute task owns a status stream,
and it may be blocked on the very queue the halted loader would have filled. Options: (a) a status
stream from each of the load and store tasks (`m_lstatus`, `m_sstatus`: two more boundary ports, the
harness and golden extended); (b) the loader commits an error-marked block that the execute task
reads as a fatal status (a flag beside each block; the store side has no such path back). (a) is the
simpler contract and works for both; (b) keeps one status stream. Either changes the boundary and
needs XSI.

## Cost accounting

Every item carries a **Resources** line: its expected LUT, FF, DSP and BRAM cost, estimated before it
is built, so the batch is weighed as a whole; after the batch, csynth's numbers are checked against
the estimates. Estimates use the calibrated resource model
(`waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1/models/vecunit_resources/params.json`, Vitis
HLS 2024.1, `xczu48dr`) where its terms apply, and say so where they do not (a new structure, or a
configuration outside the fitted range: `L` 2 to 8, `NG` 8 to 64, `S` 2 and 3, the CG and
matched-filter format sets).

**The baseline:** the CG unit (`examples/vecunit_cg`: `L` = 4, `S` = 3, every slot multiplying,
`NG` 32), by the model's terms:

| Term | What it is | LUT |
|---|---|---|
| intercept | the fixed logic: decode, load and store, control | 23,301 |
| `S²·L` (783 each) | the fused loop's operand and forwarding multiplexers | 28,175 |
| `L·NW·QW` (2.20 each; `NW` 39, `QW` 44) | the scalar dividers, one a lane | 15,075 |
| `n_mul·L` (929 each) | the multipliers' surrounding logic | 11,150 |
| `L` (829 each) | the rest that grows with the lanes | 3,315 |
| **total** | | **≈ 81,000** (82k measured) |

DSP `= 4 · n_mul · L + 5` = 53 (exact); BRAM18 36 (exact). The device has about 4,272 DSPs and 425k
LUTs: the multipliers are about 1 % of it, the LUTs about 19 %. **LUTs are the budget**: the
forwarding multiplexers and the dividers above all.

## Planned (from reading the docs)

### F1. Queue and port names in the assembly text say which side they are on

**What.** The disassembler prints input queues as `qi<n>` and output queues as `qo<n>`, in place of
`q<n>` for both: `wf.vlse v0, qi0, 0, 1`, `wf.qpop qi0`, `wf.vsse qo0, v1, 0, 1`, `wf.qpush qo0`,
`wf.ld qi0, ...`, `wf.st ..., qo0, ...`. Proposed with it, to confirm: ports the same way, `in<n>`
and `out<n>` in place of `p<n>` (`wf.ld qi0, in0, 16, 4`, `wf.st out0, qo0, 0, 16, 4`), matching the
unit's `s_in<n>` / `s_out<n>`.

**Why.** Today `q0` is `qi0` in `wf.ld` / `wf.vlse` / `wf.qpop` and `qo0` in `wf.st` / `wf.vsse` /
`wf.qpush`, and `p0` is input port 0 in `wf.ld` and output port 0 in `wf.st`: the instruction decides,
so a listing such as `double`'s has two different `q0`s and two different `p0`s. The architecture
figure names them `qi0`, `qo0`, `s_in<p>`, `s_out<p>`; the listings should use the same names.

**Where.** `waveflow/vecunit/isa.py::disassemble` only: the encoding, the interpreter, pysim and the
HLS are unchanged (nothing parses assembly text back). The text it prints appears in
`tests/vecunit/test_isa.py` and `tests/vecunit/test_asm.py` (expected strings), in the assembler's
messages, and in the listings of `docs/guide/dsp/vecunit/` (theory, program examples, programming),
`docs/examples/vecunit/` and `docs/examples/vecunit_mf/`, and in docstrings of
`examples/vecunit_mf/mf_program.py` and `examples/vecunit_cg/cg_program.py`.

**Resources.** None: text only.

**Tests.** Fast only.

### F2. Scalar extensions: constants in, and vector-with-scalar arithmetic

**What.** Five instructions, so the scalar file is an input path as well as an internal one:

| Proposed | Does | RVV analogue |
|---|---|---|
| `wf.sli sd, imm` | `sd[l] = imm` for every lane: a constant in the program. `imm1:imm2` give 24 bits, read as the stored integer of `sd`'s format | `li` on the scalar core, feeding `.vx` |
| `wf.vmv.s.v sd, vs` | `sd[l] = Re(vs[l][0])`: element 0 of each member into its lane's scalar (per-lane constants from data); delivered at the end of its pass, like a reduction | `vmv.x.s` |
| `wf.vmul.vs vd, s, vs2` | `vd = q(s[l] · vs2)` | `vmul.vx` |
| `wf.vadd.vs vd, s, vs2` | `vd = q(vs2 + s[l])`, the scalar added to the real part | `vadd.vx` |
| `wf.vsub.vs vd, s, vs2` | `vd = q(vs2 − s[l])` | `vsub.vx` |

**Why.** Today no instruction loads a scalar: scalars come only from `wf.vdotr`, `wf.sdivz` and
`wf.smv`, and are used only by the in-place multiply-adds `wf.vmacc.vs`, `wf.vnmsac.vs`,
`wf.vmadd.vs`. That suits CG, whose scalars are computed, but any program with constants -- a
polynomial, a DC offset, a gain, a threshold -- must send each constant as a `VLMAX`-row block (each
value `VLMAX` times, since a `wf.vlse` stride is at least 1), or bring it in with a `VL` = 1 reduction
against a ones vector and expand it into a vector with `wf.vmacc.vs` on a zeroed register.
`vmv.v.i` takes integers only.

**The test case.** `y[i] = a x[i]² + b x[i] + c` for real scalars `a`, `b`, `c` (Horner form). Today
(both written and run on the interpreter, 2026-10-26, max error under one LSB): a `16 × 12`
coefficient block of 192 values, or a `1 × 12` block through the reduction trick plus two setup
passes; three constant vector registers either way. With F2:

```
wf.sli s0, a ; wf.sli s1, b ; wf.sli s2, c      # once a job, no host data
per chunk:
wf.setvl k
wf.vlse.c    v0, qi0, 0, 1                     # pass 1: x
wf.vmul.vs.c v1, s0, v0                        #         a x
wf.vadd.vs   v2, s1, v1                        #         a x + b
vmul.vv.c    v3, v2, v0                        # pass 2: (a x + b) x
wf.vadd.vs.c v4, s2, v3                        #         + c
wf.vsse      qo0, v4, 0, 1
wf.qpop qi0 ; wf.qpush qo0
```

No coefficient block, no setup passes, three registers freed. The chunk still costs two dependent
passes: six operations need two passes at `S` = 3 whatever their operands. F2 fixes the plumbing,
not the speed. (A one-pass version needs `S` >= 6 or a four-operand form `vd = vs1·vs2 + s`, which
the 64-bit word can hold with the scalar index in `imm1`; a separate, later idea.)

**Cost.**

- **Encoding:** free. The scalar class uses 2 of its 64 `funct` codes, the vector class 12 of 64;
  the 24-bit immediate is `imm1:imm2`.
- **Datapath:** small. A multiplying slot already computes `vd ± s·vs2` (`wf.vmacc.vs`):
  `wf.vmul.vs` is that multiply-add with its addend forced to 0, and `wf.vadd.vs` / `wf.vsub.vs`
  with its vector factor forced to 1 (so they are `MUL`-slot instructions). `wf.sli` and
  `wf.vmv.s.v` write the scalar file, which reductions already write. New hardware: decode, and
  constant-0 / constant-1 muxes on the multiply-add. Caution: a scalar format with more integer or
  fraction bits than the vector formats widens the datapath (the union-width cost of
  `docs/guide/dsp/vecunit/architecture.md#formats`).
- **Software:** `isa.py` (encodings, constructors, text), `model.py` (semantics, `instr_status`,
  `cap_of`, `operands`, `pass_status`: `wf.vmv.s.v` delivers at the end of its pass), `asm.py`
  (checks), `unit.py` (timing features), `hls.py` and `waveflow/build/vecunit_core.h` (decode and
  datapath), `tests/vecunit/_ref.py` and `test_model.py` (every new instruction against the
  exact-rational reference), `test_asm.py`, the ISA docs, and a toy kernel for the polynomial.

**Resources (estimate, not in the model: a new structure).** About 1.5k LUT at the CG configuration,
2 % of the baseline: constant-0 and constant-1 selects on each multiplying slot's addend and factor
(about 90 LUT a slot a lane at the union width, 12 slot-lanes, ≈ 1.1k), the immediate's and
`wf.vmv.s.v`'s write paths into the scalar file (a lane each, small), and decode (a few hundred). No
DSP, no BRAM. FF: small (no new pipeline state).

**Tests.** Fast, csynth, XSI.

**Decisions.** It reopens the plan's D1 (the instruction set, approved at Stage 0): added to this
batch 2026-10-26. Open, proposed: scalars stay **real**; complex scalars (a complex gain or offset)
would double the scalar file and its datapath, a later option.

### F3. Real lanes: throughput in proportion to the multiplies

**What.** The unit's `L` lanes become **real**, each with one real multiplier (one DSP) per
multiplying slot. A complex value occupies a **lane pair**: its real part on lane `2i`, its imaginary
part on lane `2i+1`. Each multiplying instruction says how to treat its operands:

| Instruction | Real multiplies a sample | Samples a cycle on `L` real lanes | Multipliers busy |
|---|---|---|---|
| `vmul vd, vs1, vs2` (real × real) | 1 | `L` | all `L` |
| `vmultc vd, vs1, vs2` (complex × real) | 2 | `L/2` | all `L` |
| `vmulcc vd, vs1, vs2` (complex × complex) | 4 | `L/4` | all `L` |

(Names are placeholders; final mnemonics to follow the RVV and `wf.` conventions.)

**Why.** Today every lane is complex: four DSPs a lane a multiplying slot. Complex × complex uses
them all, complex × real only two, and real data must ride as complex values with a zero imaginary
part, using one in four. With real lanes every form keeps every multiplier busy. At the same DSP count
as today (`L` = 4 complex lanes = 16 DSPs a slot, against `L` = 16 real lanes): complex × complex 4
samples a cycle (as today), complex × real 8 (2×: CG's `alpha · P` updates), real × real 16 (4×).

**How each form maps.**

- **Real × real:** each lane multiplies its own two values.
- **Complex × real,** `(a + jb) r = ar + j br`: lane `2i` computes `a r`, lane `2i+1` computes
  `b r`, one multiply a lane. Both lanes of the pair need `r`: stored duplicated across the pair, or
  lane `2i+1` borrows lane `2i`'s (a 2:1 mux between neighbours).
- **Complex × complex,** `(a + jb)(c + jd) = (ac − bd) + j(ad + bc)`: four multiplies a pair, two a
  lane; each lane needs its partner's operands (neighbour-only muxes). Two ways to fit two multiplies
  a lane:
  - **(a) Two phases:** a pass with a complex × complex multiply takes two cycles an element (the run
    loop iterates over element and phase at II 1). One slot an instruction; a chained pass runs at its
    slowest slot's pace.
  - **(b) Two instructions,** as Arm's `FCMLA` (Armv8.3-A, SVE): a complex multiply-accumulate is two
    instructions, "rotation 0" and "rotation 90", each two of the four products. Every instruction is
    one multiply a lane an element at a fixed II 1, so the hardware stays uniform; a complex multiply
    costs two slots of a pass, or two passes.

  Leaning (b): fixed II, simpler decode and timing model, a known precedent. (a) keeps a complex
  multiply at one slot, which matters at `S` = 3.

**What else changes.**

- **Not the wire format:** a complex element on a port (real part low, imaginary high) is bit for bit
  two adjacent real values, so a complex `k × n` block is a real `k × 2n` block.
- **The register file:** 16 real lanes of 16 bits against 4 complex lanes of 32 is twice the width.
  All lanes of a group share one address, so they can be packed side by side in wide block RAMs; one
  RAM a lane would be four times the BRAM count.
- **Reductions:** a real dot product sums a lane; `Re(conj(a) b) = ac + bd` combines a lane pair. The
  scalar file stays a scalar a lane, or a pair for complex.
- **`conj` and broadcast:** `conj` negates the imaginary lane of a pair; broadcast becomes pair 0 to
  every pair (lane 0 to every lane for real data).
- **Columns a group:** `L` real columns or `L/2` complex ones; a block's lane group becomes `L` real
  values.
- **The `waveflow.linalg` lane helpers are complex-only** (`ComplexField`): a real-element path is
  needed (`DataArray` of `FixedField` exists).

**Cost.** A rebuild of the unit's core -- a "v2": the ISA and its encoding, the interpreter and its
exact-rational reference, the assembler's checks, pysim, the HLS datapath and decode (`hls.py`,
`waveflow/build/vecunit_core.h`), the CG and matched-filter programs (as complex × complex and
complex × real programs), the calibrated timing and resource models (refitted from new RTL runs),
every XSI gate, and most of `docs/guide/dsp/vecunit/`. The Stage 0-10 work of `plans/vecunit_new.md`
carries over as structure and method, not as code. CG gains less than the 2× suggests: a CG job at
`VL` = 8 is dominated by decode and pipeline fills, not by multiplies.

**Resources (estimate, outside the model: real lanes are a new structure and `L` = 16 is past the
fitted range).** At the same 16 DSPs a multiplying slot as the baseline (`L` = 16 real lanes against
4 complex):

| Term | Baseline | Real lanes | Why |
|---|---|---|---|
| DSP | 53 | 53 | one DSP a real lane a multiplying slot: the same multipliers |
| `S²·L` muxes | 28k | ≈ 56k | twice the data a slot: 16 lanes × 12 bits against 4 × 24 |
| dividers (`L·NW·QW`) | 15k | ≈ 60k | a divider a lane, and four times the lanes |
| multipliers' logic (`n_mul·L`) | 11k | ≈ 11k | the same multipliers, a quarter of the work a lane |
| intercept and `L` terms | 27k | ≈ 30-37k | the fixed logic unchanged (23k); the per-lane rest for 16 lanes, 13k at today's rate a lane, less at half the width |
| **LUT total** | **≈ 81k** | **≈ 160k** | about 2×, against 2× (complex × real) and 4× (real) throughput |

The dividers are the largest new cost and the easiest to cut: a divider a lane pair (complex
scalars), or a few shared ones (the "dividers' flip-flops" limit in `timing.md`), would bring them
back near 15k. FF grows the same way (the dividers' about 33k FF in the baseline would be about
130k). BRAM: twice the register-file width, packed wide (see above) about the same count.

**Tests.** Everything: fast, csynth, XSI, and new calibration runs.

**Decisions (open).**

1. Complex × complex: two phases (a) or two instructions (b)?
2. Is real data a first-class target? Mostly complex workloads (CG, the matched filter, MIMO) gain 2×
   on complex × real and 4× on real data; real-valued kernels (an FIR on real samples, power and
   magnitude, features) gain more.
3. Scope: this batch, or a v2 after the current PR merges with F1, F2 and the review fixes? Suggested:
   merge the current unit first, then plan the real-lane unit as its own staged plan in `plans/`, as
   `vecunit_new.md` was; F2's scalar instructions carry over to it.

### F4. A run-time result shift: `wf.setrs`

**What.** A **result shift** `RS` that scales every product by `2^−RS` before the write, set two ways:

- **in the instruction:** a shift field in each multiplying instruction (vector arithmetic leaves
  `imm1` / `imm2` unused): an override bit and 4 bits of shift (decision 5);
- **as a mode:** `wf.setrs k` sets a default `RS = k` that holds, like `VL`, until the next
  `wf.setrs`; an instruction whose override bit is clear uses it.

| Instruction | With `RS` |
|---|---|
| `vmul.vv vd, vs1, vs2` | `vd = q_vd(vs1 · vs2 · 2^−RS)` |
| `vmacc.vv vd, vs1, vs2` | `vd = q_vd(vd + vs1 · vs2 · 2^−RS)` |
| `wf.vmacc.vs`, `wf.vmadd.vs`, ... (and F2's `wf.vmul.vs`) | the product term scaled the same way |
| `wf.vdotr sd, vs1, vs2` | `sd = q_sd(Σ ... · 2^−RS)` |

The scaling is exact (a move of the binary point): still **one rounding**, at the write, in the
destination's mode, so bit-exactness and "chaining never changes a result" hold. The unit does not
track exponents: with `RS = 3` the stored values mean `2^3` times what their format says, and the
program or the host keeps that exponent, as in block floating point.

**Why.** Today a product's shift is fixed by the build-time formats: `vmul.vv v2, v0, v1` always
shifts by `frac(v0) + frac(v1) − frac(v2)`; another scaling means another build. A run-time shift
gives a per-job or per-frame gain, an FFT-style scaling schedule (as the Xilinx FFT core's run-time
`scale_sch`), and CG or a matched filter whose output range depends on the data. RVV's analogue is
its fixed-point instructions with a shift amount (`vssra`, `vnclip`).

**Later, not in F4:** a shift that varies **a lane, from a scalar** (RVV's `vssra.vx`) -- true block
floating point, each column's exponent from its own data -- with an instruction to find it (the
largest magnitude, or a leading-bit count).

**Cost.**

- **Encoding:** the per-instruction field is free; `wf.setrs` is one control instruction (the
  control class uses 3 of its 64 `funct` codes).
- **Fusion:** `wf.setrs` ends a run, as `wf.setvl` does; the per-instruction field does not.
- **Datapath:** each slot-lane quantizer's shift, today a select among the few shifts the formats
  need, becomes a barrel shifter over `0 .. RS_MAX`, and so do the round-bit and saturation logic
  after it. In the multiply-accumulates the scaled product's alignment with `vd` is a variable shift
  at the adder's input too. Possibly one more pipeline stage at 4 ns (latency, not II).
- **Software:** `isa.py`, `model.py` (exact; easy), `asm.py`, `unit.py` (one more control
  instruction in the timing features), `hls.py` and `vecunit_core.h` (the quantizer and the adders'
  alignment), the exact-rational reference tests, and the ISA docs.

**Resources (estimate, not in the model: a new structure).** At the CG configuration, 24 values a
pass are quantized (3 slots × 4 lanes × real and imaginary). A 4-stage barrel shifter with its
rounding and saturation is about 50-100 LUT a value: ≈ 2-3k LUT for the quantizers; the
multiply-accumulates' variable alignment about as much again: **≈ 4-6k LUT in all, 5-8 % of the
baseline**. Restricting `RS` to `vmul` and the reductions would take off about half. No DSP, no BRAM;
FF small (a stage at most).

**Measured (2026-10-27, Vitis HLS 2025.1, `xczu48dr`, 4 ns; the toy unit, `L` 4, `S` 3, `NG` 4,
csynth only).** `rs_max` 0 against 15: LUT 94,203 against 102,273 (**+8,070, +8.6 %**, above the
4-6k estimate), FF 70,315 against 72,300 (+1,985), DSP 77 and BRAM 24 both, the fused loop 24 deep
against 26 (+2 cycles of latency, II 1 both), 2.917 ns both. Both builds' decode loop runs at II 20
(the CG build's was 13 at 2024.1): not the shift's doing; to look into with the 2025.1 retest.

**Tests.** Fast, csynth, XSI.

**Decisions.**

1. Both the per-instruction field and the `wf.setrs` mode, the field overriding when non-zero:
   decided 2026-10-27.
2. `RS_MAX` a build parameter, default 15 (a 4-bit field): decided 2026-10-27.
3. `vadd.vv` / `vsub.vv` are not scaled (products are where the range grows): decided 2026-10-27.
4. Every multiplying instruction takes `RS`, the multiply-accumulates and reductions included (the
   full cost, ≈ 4-6k LUT): the accumulations are where the range grows (CG's `wf.vmacc.vs` /
   `wf.vnmsac.vs` updates, the dot products), and one rule ("every product is scaled by `2^−RS`") is
   simpler to state, check and generate code for. Decided 2026-10-27.
5. **Proposed:** the per-instruction field is an **override bit** plus the 4-bit shift (5 bits, still
   free in `imm1` / `imm2`), so "use the `wf.setrs` default" and "shift by 0" are distinct encodings.
   With a bare 4-bit field where zero means "the default", an instruction could not ask for no shift
   while a non-zero default is set.

### F5. Long vectors without paying the overhead a block

**What.** Make long vectors cheap. Today a pass costs about **82 cycles** beyond its elements (the
calibrated timing model, Vitis HLS 2024.1): about 16 to decode the pass, which does not overlap
execution, and about 66 a run -- of which the pipeline's fill and drain are only 22-24; the rest is
setup and teardown (entering and leaving the pipelined loop, the operand addresses, the queue
blocks' locks). The share of a pass's cycles that do work is `VL / (VL + 82)`: 16 % at `VL` 16, 44 % at
64, 76 % at 256, 93 % at 1024. Three routes, not exclusive:

1. **A large `VLMAX`.** Already a build parameter: nothing to implement. The register file is
   `NG × L × VLMAX` values of `2·WV` bits: at `NG` 32, `L` 4 (complex), `VLMAX` 1024, 32-bit values,
   about 4 Mbit -- 16 memories (`NB × L`) of 8,192 entries, 256 BRAM18 of the `xczu48dr`'s 2,160
   (about 12 %), or about 16 of its 80 UltraRAMs packed two lanes wide (20 %); plus queue blocks of
   `Kmax` 1024 rows, about 16 BRAM18 a queue. It helps only programs whose vectors are long (CG's are its
   `k` rows, 8 in the examples), every register pays the maximum length, and the resource model was
   fitted up to `VLMAX` 256.
2. **Runs that continue across blocks.** A strip-mined program cannot fuse across chunks today: each
   chunk's `wf.setvl` / `wf.qpop` / `wf.qpush` ends a run, and chunk 2 reads the registers chunk 1
   wrote. Two changes would let the chunks stream back to back with one pipeline fill:
   - **queue operations inside a run:** the run loop takes the next block when the current one is
     used up, without draining (the ping-pong, inside the pipeline);
   - **temporaries that skip the register file:** a chained pass that only forwards its intermediate
     values (`cmul`'s: queue → multiply → queue) need not write them, so it needs no `VLMAX` storage
     and its `VL` is bounded by the stream, not the registers. The idea of Snitch's stream semantic
     registers (Schuiki et al., *IEEE TC* 2021) and of stream-dataflow accelerators (Nowatzki et al.,
     ISCA 2017).

   Long-vector efficiency with small registers; a real change to the run loop and the pass rules.
3. **Cut the overhead itself.** Most of the 66 cycles a run is not pipeline depth. Part may be cheap
   to remove -- the next run's descriptors computed and its locks taken during the current run --
   and with the decoder that runs ahead (16 cycles a pass, in the ideas below) it helps **every**
   program, CG included, which routes 1 and 2 do not.

**First steps (measurements, no change).**

1. A toy unit at `VLMAX` 1024 through csynth: its BRAM, URAM and LUT. A configuration, not a change:
   with the 2025.1 retest.
2. The breakdown of the 66 cycles a run, from the CG build's csynth schedule (`report.loop_schedule`):
   removable setup favours route 3; real pipeline depth favours route 2.

Then decide on route 2: the general answer for streaming kernels (the matched filter), and the most
work.

**Resources.** Route 1: BRAM as above, LUT outside the model (`VLMAX` 1024 > 256), to measure. Route
2: not estimated until designed (the run loop's block switching, a no-write-back bit, the pass rules).
Route 3: none expected (scheduling, not new hardware).

**Tests.** Route 1: csynth. Routes 2 and 3: fast, csynth, XSI, and a timing recalibration.

**Decisions (open).** Which routes, after the two measurements.

### F6. One program, split into three in software

**What.** The programmer writes **one** program in the order it would run sequentially -- load,
compute, store -- and the assembler splits it into the three task programs, which the hardware
receives as today on three streams:

```
wf.ld     q0, p0, 16, 4        → load program
wf.setvl  16                   → execute
wf.vlse.c v0, q0, 0, 1         → execute
vadd.vv.c v1, v0, v0           → execute
wf.vsse   q0, v1, 0, 1         → execute
wf.qpop   q0                   → execute
wf.qpush  q0                   → execute
wf.st     p0, q0, 0, 16, 4     → store program
```

```python
def split(prog: list[Instr]) -> Programs:
    """One program, written in the order it would run sequentially, as the three tasks take it."""
    is_ld = lambda x: x.opcode == Opcode.QUEUE and x.funct == QOp.LD
    is_st = lambda x: x.opcode == Opcode.QUEUE and x.funct == QOp.ST
    return Programs(load=[x for x in prog if is_ld(x)],
                    execute=[x for x in prog if not (is_ld(x) or is_st(x))],
                    store=[x for x in prog if is_st(x)])
```

**Why.** Writing three programs by hand, and keeping their queue orders consistent, is the hard part
of programming the unit. The plan's D3 rejected a **hardware** dispatcher of one stream (it can
deadlock: the load task stalls on a full queue, its instruction FIFO fills, the dispatcher blocks on
the next load while the execute instruction that would free the queue waits behind it); that reason
stands, and F6 keeps the three streams. Splitting in **software** is easy here, unlike the general
access/execute partitioning problem (loops, branches, addresses): every instruction belongs to one
task by its opcode, programs are straight-line, and the queues are the synchronization -- a load's
only relation to execute is "the next block of this queue", a store's "the block after the next
`wf.qpush`", both already in program order. The decoupling then gives the overlap for free: the load
task runs ahead into the free block as far as the queue depth allows, with no load hoisted by hand.

**The deadlock check (the fix for R3).** The single program, run in order with two-block queues, is
a concrete schedule the three tasks can follow. If that sequential run never needs a third block in a
queue, the order is feasible; if it does (three loads into `qi0` before execute reads `qi1`), the
checker names the instruction. **Open:** prove, or test exhaustively on small programs, that a
feasible sequential order rules out deadlock of the split programs (each task's order is fixed and the
queues hold only counts, which suggests it does) before relying on it.

**Where.** `waveflow/vecunit/asm.py` (`split`, the sequential feasibility check), `tests/vecunit/test_asm.py`
(split round-trips; the deadlock cases R3 names), the programming docs, and the toy kernels and
examples' builders rewritten as single programs where that reads better. The three-program form stays.

**How it relates to F5 route 2.** Independent: route 2 is inside the execute task. They fit together:
a long streaming kernel is most naturally one program, "for each block: load, compute, store", and
route 2 would run that loop without a pipeline drain at each block.

**Not a compiler.** The splitter partitions; a compiler from a NumPy-like expression to a program would
also choose registers against bank conflicts, form chains up to `S`, and order work so it fuses -- today
the examples' hand-written builders, one an algorithm. A later, bigger step.

**Resources.** None: software only.

**Tests.** Fast only.

### F7. Fuse dependent passes when `VL` covers the pipeline's depth

**What.** Relax the fusion rule (`waveflow/vecunit/model.py::exec_items`). Today a run may not
contain a pass that reads a group or scalar an earlier pass of the run writes. Allow it for a
**vector group** when `VL >= D`, with `D` a build constant: the distance, in cycles, from a slot
reading an element to the pass writing it back (about 22-24 for three slots, Vitis HLS 2024.1;
csynth's schedule gives the exact figure a build).

**Why it is safe.** In a fused run, pass `p+1` reads element `i` about `VL` cycles after pass `p` read
it, and pass `p` writes element `i` about `D` cycles after reading it. With `VL >= D` the value is in
the register file before the next pass reads it: the dependence is honoured by time, not by a drain.
Vector machines call this chaining through the register file (tailgating). A scalar written by a
reduction is still delivered at the end of its pass, so a scalar read-after-write still ends a run.

**Why.** Dependent passes are common, and each costs its own run today (~66 cycles of the ~82 a pass):
- **FIR (PR #300):** every tap accumulates into one register, so a `T`-tap tile is `T` runs; fused,
  one run (still `T` decodes, see the decoder idea below);
- **the polynomial of F2:** its two passes;
- **CG:** the dependent steps within an iteration.

**Where.** `model.py` (`exec_items`: the rule, with `VL` and `D`; the interpreter's results are
unchanged, since fusion never changes a result), `asm.py` (nothing: legality is unchanged),
`unit.py` (pysim's timing: fewer runs), `hls.py` (the run loop's dependence: `DEPENDENCE ... false`
on `rf` today rests on the hazard masks forbidding a read-after-write in a run; with F7 it must rest
on `VL >= D` instead, so the decoder needs `D` and the run's `VL`), and `config.py` (`D`, or derived
from the build).

**Resources (estimate).** Small: a comparison of `VL` against `D` in the decoder's fusion check, no
datapath change. The risk is in correctness, not cost: `D` must be the true distance of the built
pipeline, checked at RTL (an XSI gate with `VL = D` and `VL = D − 1` on a dependent pair).

**Tests.** Fast, csynth, XSI, and a timing recalibration (the `run` feature counts change).

**Decisions (open).** Whether `D` is a configuration constant or read from the build's schedule; the
minimum `VL` it allows (below `D`, the run still breaks).

## Keeping PR #300 open (FIR on the vector unit)

PR #300 (`plans/vecunit_fir.md`, a draft plan, not started) proposes single-channel FIR through a
general window read, `wf.vwin vd, q, base, offset`: `vd[l][i] = q[base + offset + iL + l]`, lanes as
adjacent outputs, one tap a pass. Nothing in it needs building now; these keep it possible:

1. **Reserve queue `funct` 4 for a window read.** The queue class uses `funct` 0-3 of 64; F1-F7 must
   not spend 4. `imm1` (12 bits) holds the base, `imm2` (12 bits, signed) the offset. Record it in
   `isa.py`'s `QOp` with the batch (a comment, no behaviour).
2. **The queue-block layout already is its bank mapping.** A 1-D signal as `k` rows of `L` samples puts
   sample `j` in lane `j mod L` of row `floor(j/L)` -- PR #300's `bank(j)`, `row(j)`. Keep a block row
   "`L` consecutive samples" in every change. For **F3** (real lanes): a row is `L` real values, `L/2`
   complex samples in lane pairs, and window offsets count samples, so lane pairs for complex data.
3. **F2 and F4 are what it needs for coefficients and accumulation.** F2's `wf.sli` is the per-lane
   scalar "initialized identically across members" PR #300 asks for (no coefficient block or
   register); keep it broadcasting to every lane. F4's `RS` on `wf.vmacc.vs` (decided: every
   multiplying instruction) gives its "wide accumulate, narrow once" option (its D4).
4. **F7 is what makes it fast.** Its taps are dependent passes; without F7 a `T`-tap tile pays the
   per-run overhead `T` times.
5. **For its Stage 1 witness, later (proposing it means editing PR #300; not now):** PR #300 proposes
   cyclic `L`-way banked block storage. Today's block is one wide memory (`lg_t blk[QD]`, a lane group
   an entry). A window at an unaligned offset spans two consecutive rows, but consecutive elements
   advance exactly one row: reading one new row a cycle, holding the previous one in a register and
   rotating the pair by `offset mod L` gives a window a cycle at II 1 with no storage change -- the
   sliding-window register of streaming FIR hardware; the rotation (`log2 L` stages) is the new cost.
   A hypothesis for that witness to test, and an argument against changing the block storage now.

## From the review of PR #302 (fix before merge)

Each confirmed by reading the code on the PR branch unless marked otherwise.

| # | What | Where | Tests |
|---|---|---|---|
| R1 | The load task ignores TLAST: a burst longer or shorter than its `wf.ld` shifts every later load, with no error in RTL or pysim. Check the TLAST status and stop with an error status | `waveflow/build/vecunit_core.h::load_matrix` (`tl` never read), `waveflow/vecunit/unit.py::VecLoad` | fast, csynth, XSI |
| R2 | A load or store program error halts its task silently: the execute task then waits forever and no `VecStatus` is sent. Report it (the command-response protocol: an error is fatal and reported) | `unit.py` `VecLoad.run_iter`, `VecStore.run_iter`; the HLS bodies in `hls.py` | fast, csynth, XSI |
| R3 | The assembler accepts programs that deadlock the hardware: it balances each queue's loads and pops, but never checks their order across queues against the two-block depth (e.g. three `wf.ld` into `qi0` while execute reads `qi1` first). *Reported by the review, not yet reproduced.* Proposed fix: F6's sequential feasibility check | `waveflow/vecunit/asm.py::check`; `model.run_job` (its queues are unbounded) | fast |
| R4 | `wf.vsse` has no row check: the read side refuses `VL > k`, the write side does not, so a write past the block is accepted (out of bounds in HLS) | `asm.py::_check_execute` | fast |
| R5 | Narrow HLS fields with no configuration bound: the slot index is `ap_uint<2>` while `S` is unbounded (`S >= 5` writes from the wrong slot); queue pointers `ap_uint<12>` against `QD`; bank bases `ap_uint<16>` against `DEPTH`. Bound them in `VecUnitConfig` or `static_assert` in the generated header | `waveflow/vecunit/hls.py` (~383, 512, 620), `waveflow/vecunit/config.py` | fast, csynth |
| R6 | At 128-bit words the loader's slow path counts 64-bit halves, not words: `len(np.asarray(words).reshape(-1))` | `unit.py::VecLoad.run_iter` (~309); use the word count, as `VecStore` and `report.job_bounds` do | fast (timing only; bits unaffected) |
| R7 | The filed held-out timing score (1.9 % mean / 7.5 % worst) predates the Stage 9d pysim fixes; on current code it is 4.4 % / 18.8 %. `test_calib.py` reads the committed JSON back, so it cannot fail. Re-score on current code, update the test and the docs | `waveflow/vecunit/calib.py`, `tests/vecunit/test_calib.py:58`, `docs/guide/dsp/vecunit/timing.md` | fast (re-scores the recorded RTL intervals) |
| R8 | Docs contradict themselves: the matched filter's summary says it is I/O-bound (its timing table says execute binds); decode "about 20 cycles a pass, some 1,760 of 3,359" against the fit's 15.9 and `timing.md`'s 42 %; the 3,581-cycle `CgVectorUnit` baseline has no committed record | `docs/examples/vecunit_mf/index.md`, `docs/guide/dsp/vecunit/timing.md`, `docs/examples/vecunit_cg/index.md` | docs |

Should fix in the same batch:

- **The XSI gates check bits only.** The other units' gates also assert cycle counts. Assert the
  recorded job intervals (CG 3,359, the matched filter 1,408), at the tool version they are recorded
  with. XSI.
- **Nothing checks the committed `measured.json` / `runtime.json`** of `examples/vecunit_cg` and
  `examples/vecunit_mf`, nor the numbers the docs quote from them (`tests/examples/test_systolic.py`
  does this for the systolic example). Fast.
- **Hand-written copies.** `model.py` forks `fixputils`' quantize, add, multiply and divide for speed
  (fenced by `test_model_fast.py`); the execute body slices lane groups by hand instead of
  `wf_lanes::get` / `set`. Both correct today. Decide whether to fold the speed into `fixputils` and
  use the lane routines.

## The 2025.1 retest

The five vector-unit XSI gates (`WANT_XSI_GATES` 243) were last run on Vitis / Vivado 2024.1. The
execute task relies on `hls::read_buf` / `hls::write_buf` under `EXPLICIT_ACQUIRE_RELEASE`, checked
only against the 2024.1 header. Run them on 2025.1 after the batch, once; record every number with
its tool version. The toy unit (`examples/vecunit`) has no RTL gate yet: add one in the same run.

## Docs follow-ups (no unit change)

- ~~`docs/guide/dsp/vecunit/program_examples.md`: show only assembly; check each listing with a
  small test instead of Python fences on the page.~~ Done (2026-10-26): `tests/docs/test_vecunit_listings.py`.
  F1 changes the listings' text: regenerate them with it.
- Rewrite `isa.md`, `programming.md`, `hardware.md` and `timing.md` (today the old `vecunit.md`'s
  sections, split).

## Ideas, not planned

- **Build-time switches to drop unused features** (not this pass): once the batch's csynth numbers
  show what each feature costs, a `VecUnitConfig` parameter per expensive one -- the scalar
  extensions (F2), the complex pair forms (F3), the dividers, the broadcast and conjugate forms -- so
  a unit built for one application pays only for what its programs use. `slot_caps` is the existing
  model for this: a slot without `MUL` is built without a multiplier, and the assembler refuses a
  program that needs one.

- **A decoder that runs ahead of execution**, feeding run descriptors, so decoding a pass (about 16
  cycles, the largest share of a CG job) overlaps the datapath. In `timing.md`'s limits.
