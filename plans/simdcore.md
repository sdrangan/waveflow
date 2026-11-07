# Plan: a minimal integer SIMD core with an RVV-subset ISA (`waveflow/simdcore/`)

**Status:** drafted 2026-11-07 with the user; Stage 0 (decisions) not started.
**Complexity:** L3. A new package (`waveflow/simdcore/`): an ISA that is a strict subset of RV32I + RVV 1.0
plus a few `wf.` extensions, an assembler, a bit-exact interpreter, a pysim module, the generated HLS,
calibration, and programs. Nothing irreversible: `waveflow/vecunit/` (`VecUnit`) and
`waveflow/linalg/` (`CgVectorUnit`) stay as they are. If this core wins on cost and is as easy to
program, a later plan decides whether `VecUnit` is retired.
**Plan file:** `plans/simdcore.md` · **Lessons:** `plans/simdcore_lessons.md` (create it at the first
lesson). Read `plans/vecunit_new_lessons.md` first: most of its lessons carry over.

> **For whoever executes this plan:** read *Rules for the doer* first. Paths are repo-relative. The
> plan separates what is decided, what is proposed (Stage 0 settles it), and what is suspected. Never
> promote a suspicion to a fact without the measurement that settles it.

## Why (the argument, in short)

`VecUnit` (82.6k LUT at `L` 4, 2025.1) pays for generality *inside the datapath*: every operation in
every lane in every chained slot, per-register fixed-point formats and their quantizers, banked
register groups and the routing between them. The fixed-function `CgVectorUnit` is 21.5k LUT. The
cost of programmability is mostly that mixing, not the instruction stream.

The bet: a **plain SIMD core** -- a scalar control unit plus `N` identical integer lanes that never
talk to each other except through one reduction tree -- is much cheaper, and still covers what the
programmable unit is for. FFTs, FIRs and systolic matrix work stay in their own specialized PEs
(`waveflow/linalg/`, the FFT and FIR blocks); this core does the **elementwise glue** between them:
complex and real elementwise multiply / add, scaling by shifts, piecewise functions, clipping,
maxima and sums.

And the ISA should be one an **agent already knows**: RISC-V scalar plus the RVV vector extension, so a
coding agent can write programs from its prior knowledge and the docs, and standard tools (GNU as,
LLVM MC, Spike) work on the standard subset.

## Decided with the user (2026-11-07)

1. **Integer only, with shifts.** No per-register fixed-point formats, no quantizer per format. Scaling
   is explicit in the program (`vsra`, `vssra`, `vsmul`, `vnclip`), as in RVV fixed-point code.
2. **No cross-lane mixing in the ALU.** The only cross-lane structure is one reduction tree (sum, max,
   min, and the mask reductions) into the scalar unit. FFT / FIR / systolic work is out of scope.
3. **Conditionals by predication, not divergent branches.** Masks live in `v0` (RVV); compare
   instructions write masks, `vmerge` / masked ops select. Piecewise functions compute their pieces and
   select. All branches are **uniform** and run in the scalar unit. No SIMT reconvergence stack, no
   per-lane PC (deferred; see *Later*).
4. **Familiar ISA.** Standard mnemonics keep their RV32I / RVV 1.0 meaning exactly; extensions carry
   the `wf.` prefix. Anything we do not implement is *absent* (illegal instruction), never redefined.
5. **Complex data by the standard RVV idiom first:** `vlseg2e<sew>` / `vsseg2e<sew>` de-interleave
   re/im into two registers; complex multiply is four multiplies. A fused `wf.vcmul` / `wf.vcmacc` is a
   build option, added only if Stage 3 shows the multiply count binds.
6. **Application-free.** As with `VecUnit` (rule 9 of `plans/vecunit_new.md`): no application name,
   constant or special case goes into `waveflow/simdcore/`.

## Goal and success criteria

Build the core, run the same programs `VecUnit` runs, and measure the cost of each.

1. **Exactness.** Every program bit-exact against its NumPy golden: interpreter, then pysim, then RTL.
   The standard subset of the interpreter also matches **Spike** instruction for instruction (Stage 2).
2. **Cost, measured against `VecUnit` and `CgVectorUnit`** at a comparable configuration (Stage 0 fixes
   the pairing, e.g. `N` = 4 lanes of complex 16-bit against `VecUnit`'s `L` 4), same tool version
   (2025.1), same part (`xczu48dr`, 4 ns). Pre-registered targets (Stage 0 commits them before any
   csynth): LUT well under `VecUnit`'s 82.6k, with a stretch target near `CgVectorUnit`'s 21.5k; cycles
   a job for the matched filter within 1.3x of its input floor.
3. **Agent-programmable.** An agent given only `docs/guide/dsp/simdcore/` and the RVV spec writes the
   Stage 3 kernels from their NumPy description, and they assemble and pass on the first or second
   try. Scored once, on a pre-registered set of kernels it has not seen (Stage 7).
4. **Calibrated.** Timing and resource models, pre-registered fit and held-out sets, as for `VecUnit`.

## Architecture (proposed; Stage 0 decides each `D`)

```
            job hdr + program         input bursts                    output bursts + Resp
                  |                       |                                  ^
                  v                       v                                  |
   +--------------------+       +------------------+              +------------------+
   | instruction memory |       |  loader (DMA in) |              | storer (DMA out) |
   +---------+----------+       +--------+---------+              +--------+---------+
             |                           v                                 ^
             v                  +---------------------------------------------+
   +-------------------+        |  data memory (DM), N banks, word-interleaved |
   |   scalar unit     |        +---------------------------------------------+
   |  RV32I(+M) subset |  vl,       ^ unit-stride / seg2 / LUT-indexed  |
   |  PC, loops, addrs |  scalars   |                                   v
   +---------+---------+  ------->  +---------------------------------------------+
             | issue                |  N lanes: vreg slice, int ALU, mul, shift, |
             +--------------------> |  compare -> v0 mask, merge, min/max        |
                                    +-----------------------+---------------------+
             ^  vmv.x.s / reductions                        |
             +-------------------- reduction tree <---------+
```

- **Scalar unit.** In-order RV32I subset: integer ALU, branches, `jal`, loads/stores to DM, a few CSRs
  (`vl`, `vtype`, `vxrm`, `vxsat`, `cycle`). `M` (multiply; divide optional, D6). It owns all control
  flow and every address.
- **Lanes.** `N` identical integer lanes (build `HwParam`). Each holds its slice of the 32 vector
  registers. Per lane: add / sub, multiply (high and low halves), shifts with rounding, compare to mask,
  merge, min / max, logic. No lane reads another lane's registers.
- **Vector length (D2).** Proposed: `VLMAX = N * T`, each vector instruction occupying a lane for `T`
  cycles (element `i` on lane `i mod N`). With `T` >= the lane pipeline's depth, a dependent instruction
  issued next finds its operands written without forwarding or interlocks -- the classic vector-machine
  answer to hazards, and the RVV programming model agents expect (`vsetvli` strip-mining). The
  alternative is interleaved warps (a barrel); both remove the bypass network, D2 picks one.
- **Data memory.** One local scratchpad, `N` banks, word-interleaved so a unit-stride access of `N`
  elements hits each bank once. The loader and storer move bursts between the job's streams and DM
  regions; programs see plain addresses (`vle16.v v1, (a1)`), not stream pops.
- **Complex layout (D4).** Proposed: a complex element is one `2*SEW` word, `re` low and `im` high, so
  `vlseg2e16.v v2, (a1)` reads `N` consecutive words (conflict-free) and splits them into `v2` (re) and
  `v3` (im).
- **Reduction tree.** One `log2 N` tree, shared by `vredsum`, `vredmax(u)`, `vredmin(u)`, `vcpop.m`,
  `vfirst.m`. Each lane first reduces its own `T` elements; the tree combines lanes. Argmax is the idiom
  `vredmax` -> `vmseq.vx` -> `vfirst.m`.
- **Coefficient tables.** `vluxei<sew>` (indexed load) is legal only into a small LUT region, replicated
  per lane or banked (D5). This is the one per-lane address in the machine, for piecewise-polynomial
  approximations (sqrt, 1/x, atan, exp).
- **Protocol.** `docs/guide/systems/command_response.md`, as `VecUnit`: a job header (`tx_id`, program
  id or program, region sizes), input bursts, output bursts each headed by a `Resp`, a `Status` per job
  on its own stream; an error (illegal instruction, out-of-range address) is fatal.

## The ISA (provisional; D1 fixes it)

**Scalar (RV32I subset):** `lui auipc addi slti sltiu xori ori andi slli srli srai add sub sll slt sltu
xor srl sra or and lw sw lh lhu sh beq bne blt bge bltu bgeu jal jalr`, `mul mulh mulhu` (`div divu rem
remu` per D6), `csrr` for the CSRs above, and `ecall` as "job done". No `fence`, no interrupts, no
privileged state.

**Configuration:** `vsetvli` / `vsetivli` with `SEW` in the build's set (D3), `LMUL` = 1 only (`m1`),
`ta, ma` only. Agents write `vsetvli t0, a0, e16, m1, ta, ma` by reflex; it must just work.

**Vector memory:** `vle<sew>.v vse<sew>.v vlse<sew>.v vsse<sew>.v vlseg2e<sew>.v vsseg2e<sew>.v`,
`vluxei<sew>.v` (LUT region only), masked forms with `v0.t`.

**Vector integer:** `vadd vsub vrsub vneg vmul vmulh vmulhu vmacc vnmsac vmadd vnmsub vsll vsrl vsra
vand vor vxor vnot vmin vminu vmax vmaxu` (`.vv .vx .vi` forms where RVV has them); fixed-point
`vsadd vssub vsmul vssra vssrl vnclip vnclipu` (with `vxrm`); widening/narrowing only as D3 needs
(`vwmul`, `vnsra`).

**Masks and select:** `vmseq vmsne vmslt vmsltu vmsle vmsleu vmsgt vmsgtu`, `vmerge.vvm / .vxm / .vim`,
`vmv.v.v / .v.x / .v.i`, `vmand.mm vmor.mm vmnot.m`, `vcpop.m vfirst.m`, masked execution `v0.t`.

**Reductions and scalar moves:** `vredsum.vs vredmax.vs vredmaxu.vs vredmin.vs vredminu.vs`,
`vmv.x.s vmv.s.x`.

**Extensions (`wf.`, build options that cost nothing when off):** `wf.vcmul.vv vd, vs1, vs2` and
`wf.vcmacc.vv` on register pairs (`vd:vd+1` = re:im), with `wf.vcconj`; only if D7 says so.

**Encoding (D1).** Proposed: the **real RISC-V encodings** (32-bit, standard opcodes; `wf.` ops in
`custom-0..3` through `.insn`), so GNU as and LLVM MC assemble programs, Spike runs the standard subset
as a second golden, and objdump disassembles. Instruction words travel two to a 64-bit stream word.
This reverses `VecUnit`'s "no bit-level RVV encoding compatibility" (its *Out of scope*) on purpose:
the tools are the reason for the ISA.

**Python layer.** `waveflow/simdcore/asm.py` offers one constructor per mnemonic, named as the mnemonic
(`v.vmacc_vv(7, 3, 4)`), and a text assembler for GNU syntax, so the same program is either Python or a
`.S` file. Both produce the same words; a test checks them against GNU as where it is installed.

### What programs look like

Complex elementwise multiply, Q15 in 16-bit, strip-mined:

```asm
loop:
    vsetvli    t0, a0, e16, m1, ta, ma
    vlseg2e16.v v2, (a1)          # v2 = re(a), v3 = im(a)
    vlseg2e16.v v4, (a2)          # v4 = re(b), v5 = im(b)
    vsmul.vv   v6, v2, v4         # ar*br  (Q15, rounded, saturated)
    vsmul.vv   v8, v3, v5         # ai*bi
    vssub.vv   v6, v6, v8         # re
    vsmul.vv   v7, v2, v5         # ar*bi
    vsmul.vv   v8, v3, v4         # ai*br
    vsadd.vv   v7, v7, v8         # im
    vsseg2e16.v v6, (a3)
    sub  a0, a0, t0
    slli t1, t0, 2                # 4 bytes per complex element
    add  a1, a1, t1
    add  a2, a2, t1
    add  a3, a3, t1
    bnez a0, loop
    ecall
```

Piecewise function and a running maximum:

```asm
    vmslt.vx   v0, v8, t1         # mask = x < t1
    vmerge.vvm v9, v10, v11, v0   # y = mask ? p1(x) : p0(x)
    vredmax.vs v12, v8, v12       # v12[0] = max(v12[0], max_i x[i])
```

## Applications (live in `examples/`, not in the core)

The same set as `VecUnit`, so the comparison is like for like, plus the kernels this core is for:

1. **Toy kernels** (`examples/simdcore/`): `cmul`, `gain`, `poly` (Horner), clip / saturate, `|z|^2`,
   piecewise-linear and LUT-polynomial functions, argmax, sum.
2. **The matched filter** (`examples/vecunit_mf/`'s golden, re-programmed): long vectors, streaming,
   I/O-bound -- the best case for this core.
3. **CG** (`examples/vecunit_cg/`'s golden, re-programmed, integer): short vectors, dot products through
   the reduction tree, divides in the scalar unit (D6). The hardest case; it says what this core costs
   on work it was not designed around. Whether CG maps one problem across lanes (standard RVV) or one
   problem per lane (as `VecUnit` does) is a program choice, recorded in Stage 3.

## Rules for the doer

1. Work on a branch `simdcore` from `main`, in a separate worktree if another agent shares the
   checkout (set `PYTHONPATH` to the worktree root).
2. One stage at a time. A stage is done when its gate passes by the stated command; paste the command
   and its result into the stage's record. **Pause for review at the end of every stage and on any
   surprise.**
3. Root-cause failures from the error, the logs and the traces. Test at most two hypotheses, then stop
   and report.
4. **A measured number names its tool version** (2025.1 for everything in this plan). Count cycles on
   the development laptop; take wall-clock times only on the lab machine.
5. **Pre-register before measuring** wherever a fit or a target is scored.
6. HLS bodies follow `docs/guide/vectorization/hls/loop_optimization.md`; never pack or unpack a word by
   hand.
7. **Scope guard** (`plans/vecunit_future.md`, 2026-10-27): every instruction beyond D1's list is a build
   option that costs nothing when off, and waits for an application that needs it. The risk is the same
   one: a minimal core growing an instruction at a time into a full processor.
8. **RVV semantics are the spec.** Where the interpreter and the RVV 1.0 spec disagree on a standard
   instruction, the interpreter is wrong. Where RVV leaves behaviour to the implementation (tail and
   mask agnostic values), pick one, document it, and test it.
9. Before building a tool the plan did not foresee, search the KB (`waveflow kb search ...`) and propose
   it first.
10. Never merge or push unless asked. One PR, many commits.

## Stages

### Stage 0: decisions (a page of records, reviewed before any code)

Write a record for each, with the options, the proposal and why; the user approves before Stage 1.

- **D1 ISA and encoding.** The exact instruction list (above, trimmed or extended), real RISC-V
  encodings or not, and the CSR set.
- **D2 Hazards and vector length.** `VLMAX = N * T` temporal vectors (proposed) or interleaved warps;
  the values of `N` and `T`; what happens when `vl < N * depth` (stall cycles, counted, not hidden).
- **D3 Element widths.** One `SEW` per build (16 proposed, with 32-bit accumulation by `vwmul` /
  `vwmacc` and `vnclip` back) or several. `plans/vecunit_new_lessons.md` lesson 8: width follows the
  spread, so keep the set small.
- **D4 Complex layout** in DM (packed `2*SEW` word, proposed) and its bank mapping.
- **D5 LUT region**: size, replicated per lane or banked, conflicts charged or forbidden.
- **D6 Divide**: a scalar `div` (multi-cycle, one per job step), none, or a shared lane divider.
- **D7 Fused complex multiply**: off in v1; the measurement in Stage 3 that would turn it on.
- **D8 Program delivery**: per job, or resident with a program id in the job header.
- **D9 The comparison pairing and the pre-registered targets** (success criterion 2).

### Stage 1: HLS witnesses (facts before design)

Small kernels in `plans/witness/simdcore/`, each csynth'd at 2025.1, `xczu48dr`, 4 ns, the RTL run in
xsim where the question is behaviour:

1. One lane's datapath (add, `vsmul`, shift, compare, merge) at II 1: LUT, FF, DSP, depth.
2. `N` = 4, 8, 16 lanes: does LUT scale linearly (no hidden mixing)?
3. The reduction tree at `N` = 4..16.
4. The banked DM with unit-stride and `seg2` access, and the LUT region's indexed reads.
5. The scalar unit's decode and issue loop: its II (lesson 9 of `vecunit_new_lessons.md`: the decode
   loop set `VecUnit`'s timing).

**Gate:** a record table with every number and its tool version; any witness above its D9 budget is a
surprise (rule 2).

### Stage 2: ISA, assembler, interpreter

`waveflow/simdcore/{isa,config,asm,model}.py`. The interpreter is the golden: bit-exact, every
instruction tested against a plain-Python reference written from the RVV spec text (not from the
interpreter), and the standard subset against **Spike** on random programs where Spike is installed
(skip loudly otherwise; a marker `-m spike`).

**Gate:** `pytest tests/simdcore -m "not vitis and not xsi"`; `pytest -m spike` where available.

### Stage 3: the applications as programs, on the interpreter

Toy kernels, the matched filter, CG (`examples/simdcore*/`), each bit-exact against its NumPy golden.
Count instructions and lane-cycles per element; record whether the multiply count binds (D7's
evidence) and how CG was mapped.

**Gate:** the examples' fast tests; the instruction counts in the record.

### Stage 4: the pysim module and its harness

`SimdCore` (`HwModule`, free-running) with its loader, core and storer; the harness on the command-response
protocol; bit-exact against the interpreter on random programs and the three applications.

**Gate:** fast tests.

### Stage 5: HLS bodies, RTL

Generated HLS (`waveflow/simdcore/hls.py` -> `waveflow/build/simdcore_core.h`), csynth at the D9
configurations, XSI gates bit-exact against the interpreter (judge free-running bodies at RTL, not
cosim: `vecunit_new_lessons.md` lesson 2).

**Gate:** `pytest -m vitis tests/simdcore`, `pytest -m xsi tests/simdcore`; the D9 table filled: LUT,
FF, DSP, BRAM, cycles a job, against `VecUnit` and `CgVectorUnit`.

### Stage 6: calibration

Timing and resource models, pre-registered fit and held-out sets spread over configurations (lessons 9,
15, 16). Scored once.

### Stage 7: docs and the agent test

`docs/guide/dsp/simdcore/`: theory (why a plain SIMD core), architecture, ISA (the list and every
deviation from RVV), programming (strip-mining, complex, masks, reductions, LUTs), interface, timing,
hardware. Then the agent test (success criterion 3): a pre-registered set of kernels described in NumPy,
an agent with only the docs, programs scored on the interpreter. Record the pass rate and every failure
mode (which RVV habits broke).

### Stage 8 (optional, to confirm with the user): a NumPy-style front end

A small tracer from NumPy-like Python (`a * b` on complex arrays, `np.where`, `np.maximum`, `np.clip`,
`.sum()`, `.argmax()`) to programs. Not a compiler: straight-line elementwise expressions, strip-mined
loops, nothing else.

## Out of scope

- FFT, FIR, systolic and any other cross-lane data movement beyond the reduction tree.
- Floating point, `LMUL` > 1, general gather / scatter, caches, DRAM addressing, interrupts, privilege.
- Divergent control flow (per-lane PCs, a reconvergence stack, `split` / `join`). Predication covers
  the target kernels; see *Later*.
- Retiring `VecUnit` (a later decision on Stage 5's numbers).

## Later (each a build option, each waiting for an application)

- Divergent branches as AMD GCN does them: an `exec` mask plus scalar save / restore, branch on
  all-zero; structured `If / Else / While` in the Python layer emits them.
- Interleaved warps for latency hiding behind memory, if D2 chose temporal vectors and memory binds.
- Fused complex multiply (D7) and packed real pairs on its multipliers.
- A shuffle or slide instruction (`vslideup`, `vrgather` restricted) if an application needs it.

## References

| Topic | Reference |
|---|---|
| the ISA | RISC-V "V" Vector Extension, version 1.0; RISC-V Unprivileged ISA (RV32I, M) |
| reference simulator | Spike, the RISC-V ISA simulator (riscv-software-src/riscv-isa-sim) |
| RVV implementations | M. Cavalcante et al., "Ara," *IEEE TVLSI*, 2020; M. Cavalcante et al., "Spatz," ICCAD 2022 |
| open SIMT GPUs (contrast) | B. Tine et al., "Vortex," MICRO 2021; Ventus (THU), ICCD 2024 / *IEEE TVLSI* 2025 |
| exec-mask predication | AMD GCN / RDNA ISA manuals (`EXEC`, `s_and_saveexec`) |
| vector chaining, temporal vectors | R. M. Russell, "The CRAY-1 computer system," *CACM*, 1978 |

Check each against OpenAlex before the docs cite it (as `plans/vecunit_new.md` did).

## Log

| Date | Stage | Event | Where |
|---|---|---|---|
| 2026-11-07 | -- | Drafted with the user: integer-only SIMD core, predication not divergence, RVV-subset ISA for agent familiarity, FFT / FIR / systolic left to their own PEs | this file |
