# Plan: a minimal integer SIMD core with an RVV-subset ISA (`waveflow/simdcore/`)

**Status:** drafted 2026-11-07 with the user; revised the same day after review (execution model, cost
model, fused complex lanes, decoupled load / store, decision rule). Stage 0 (decisions) not started.
**Complexity:** L3. A new package (`waveflow/simdcore/`): an ISA that is a strict subset of RV32IM +
RVV 1.0 (`Zve32x`) plus a few `wf.` extensions, an assembler, a bit-exact interpreter, a pysim module,
the generated HLS, calibration, and programs. Nothing irreversible: `waveflow/vecunit/` (`VecUnit`) and
`waveflow/linalg/` (`CgVectorUnit`) stay as they are. `VecUnit` stays frozen in the scope of
`plans/vecunit_future.md`; whether this core replaces it is decided by the rule pre-registered in D9,
on Stage 5's numbers.
**Plan file:** `plans/simdcore.md` · **Lessons:** `plans/simdcore_lessons.md` (create it at the first
lesson). Read `plans/vecunit_new_lessons.md` first: most of its lessons carry over.

> **For whoever executes this plan:** read *Rules for the doer* first. Paths are repo-relative. The
> plan separates what is decided, what is proposed (Stage 0 settles it), and what is suspected. Never
> promote a suspicion to a fact without the measurement that settles it.

## Why (the argument, in short)

**`VecUnit`'s cost is in its mixing, not its compute.** Its calibrated resource model
(`docs/guide/dsp/vecunit/timing.md`, Vitis HLS 2024.1) has LUT terms `S²·L` (the operand and forwarding
multiplexers of the chained slots), `L·NW·QW` (the per-register fixed-point formats and their
quantizers) and `n_mul·L` (the multipliers), and an FF term `S²·L²`. At `L` 4, `S` 3, keeping the
multiplier in slot 0 only still costs 68.8k LUT against 82.6k (2025.1) with every slot multiplying: two
thirds of the multipliers are about 17 % of the LUTs. The fixed-function `CgVectorUnit` is 21,464 LUT
(2025.1) and only 4 % slower on CG (3,581 against 3,447 cycles a job, 2025.1).

**`VecUnit` buys speed the glue does not need.** Chaining `S` instructions in one pass gives about
`N/L` cycles for `N` elements -- `S` times the throughput of issuing them one at a time -- at `S²·L`
cost. Elementwise glue does a few operations a sample and is bounded by the rate its input arrives;
the extra speed idles.

**The bet: a plain SIMD core.** A scalar control unit plus `L` identical lanes that never talk to each
other except through one reduction tree, issuing one vector instruction at a time over a long vector:

| | `VecUnit` (chained, spatial) | this core (temporal) |
|---|---|---|
| cycles, a program of `S` vector instructions on `N` elements | `ceil(S / S_slots) · N/L` + pass overheads | **`S · N/L + D`** (`D` the lane pipeline depth, paid once) |
| resources | `S²·L` LUT, `S²·L²` FF, + formats | **`O(L)`** |
| at matched throughput (this core with `S·L` lanes) | `S²·L` | `S·L` |
| buffer memory | queues of blocks | `O(N)` a live vector (ping-pong for I/O) |

The `+ D` is paid once only when each instruction occupies the lanes for at least `D` cycles
(`N/L >= D`); a shorter vector pays up to `S · D` (the worst case, D2).

FFTs, FIRs and systolic matrix work stay in their own specialized PEs (`waveflow/linalg/`, the FFT and
FIR blocks); this core does the **elementwise glue** between them: complex and real elementwise
multiply / add, scaling by shifts, piecewise functions, clipping, maxima and sums.

And the ISA should be one an **agent already knows**: RISC-V scalar plus the RVV vector extension, so a
coding agent can write programs from its prior knowledge and the docs, and standard tools (GNU as,
LLVM MC, Spike) work on the standard subset.

**The real competitor for fixed, high-rate glue is a generated HLS kernel per step**, not `VecUnit`.
This core earns its place where glue changes often, where several low-rate steps share one block, or
where an agent writes the program. Stage 5 measures one generated kernel (the matched filter) beside
both units so the claim is checked, not assumed.

## Decided with the user (2026-11-07)

1. **Integer only, with shifts.** No per-register fixed-point formats, no quantizer per format. Scaling
   is explicit in the program (`vsra`, `vssra`, `vsmul`, `vnclip`, the rounding shift of the `wf.`
   complex ops), as in RVV fixed-point code.
2. **No cross-lane mixing in the ALU.** The only cross-lane structure is one reduction tree (sum, max,
   min, and the mask reductions) into the scalar unit. FFT / FIR / systolic work is out of scope.
3. **Conditionals by predication, not divergent branches.** Masks live in `v0` (RVV); compare
   instructions write masks, `vmerge` / masked ops select. Piecewise functions compute their pieces and
   select. All branches are **uniform** and run in the scalar unit. No SIMT reconvergence stack, no
   per-lane PC (deferred; see *Later*).
4. **Familiar ISA.** Standard mnemonics keep their RV32IM / RVV 1.0 meaning exactly; extensions carry
   the `wf.` prefix. Anything we do not implement is *absent* (illegal instruction), never redefined.
5. **Temporal execution, one instruction over the whole strip (the cost model above).** The outer loop
   is over instructions, the inner over elements: `cycles ~ S·N/L + D`, resources `O(L)`, no forwarding
   network. Lower throughput than `VecUnit` is accepted for the lower cost. *(Revised from "elements
   outer, instructions inner": without bypass muxes that runs at `S·D·N/L`.)*
6. **Load and store run in parallel with compute.** The loader, the core and the storer are separate
   tasks on ping-pong blocks (stream-of-blocks or T2P BRAM); vector memory traffic does not take issue
   slots from compute. D10 decides how programs name the blocks.
7. **Complex lanes, fused complex ops in v1.** *(Revised: was "the `vlseg2` idiom first, fusion only if
   Stage 3 shows the multiply count binds".)* A lane is a complex lane: four 16-bit multipliers, a
   conjugate flag, a rounding shift. `wf.vcmul` / `wf.vcmacc` / `wf.vcabs2` make a complex step one
   instruction, so `S` counts the steps of the math. Fusion does not change the multiplier-limited
   throughput (the matched filter costs about `8N/M` cycles on `M` multipliers either way); it buys 4x
   fewer instructions, fewer temporaries and programs that read like the math. A real-only build stays
   a configuration so Stage 5 measures the trade (D7). **Optimize for complex-heavy glue now**; the
   lane type is a build option (`lane="cplx4"` default, `lane="real2"` for real-heavy glue; see
   *Throughput and the lane*).
8. **Application-free.** As with `VecUnit` (rule 9 of `plans/vecunit_new.md`): no application name,
   constant or special case goes into `waveflow/simdcore/`.

## Goal and success criteria

Build the core, run the same programs `VecUnit` runs, and measure the cost of each.

1. **Exactness.** Every program bit-exact against its NumPy golden: interpreter, then pysim, then RTL.
   The goldens are **new**, written in this core's integer arithmetic; the `VecUnit` goldens (per-register
   formats) are not bit-comparable. The standard subset of the interpreter also matches **Spike**
   instruction for instruction (Stage 2).
2. **Accuracy against the float reference**, so a cheaper core cannot win by being less exact: output
   SNR for the matched filter, residual and iterations to converge for CG, each beside `VecUnit`'s on the
   same inputs.
3. **Cost, measured against `VecUnit`, `CgVectorUnit` and a generated matched-filter kernel**, same tool
   version (2025.1), same part (`xczu48dr`, 4 ns). D9 fixes the metric (matched throughput, or
   LUT x cycles) and the pairings, and pre-registers before any csynth:
   - LUT targets: well under `VecUnit`'s 82.6k, stretch near `CgVectorUnit`'s 21.5k;
   - cycles a job for the matched filter within 1.3x of its input floor (if Stage 0's arithmetic shows
     the target reachable at the D9 lane count);
   - cycles a job for CG, against `VecUnit`'s 3,447 and `CgVectorUnit`'s 3,581;
   - **the decision rule**: e.g. at matched throughput on the matched filter and CG, LUT at most X % of
     `VecUnit`'s within Y % of its cycles makes this core the default and opens a plan to retire
     `VecUnit`.
4. **The cost model holds.** Measured cycles match `S·N/L + D` (plus the D2 terms for short vectors and
   scalar overhead) on the toy kernels, and LUT grows linearly in `L` and does not grow with program
   length.
5. **Agent-programmable.** An agent given only `docs/guide/dsp/simdcore/` and the RVV spec writes the
   kernels from their NumPy description, and they assemble and pass on the first or second try. Scored
   once, on a pre-registered set of kernels it has not seen, with the agent's model and version recorded
   (Stage 7).
6. **Calibrated.** Timing and resource models, pre-registered fit and held-out sets, as for `VecUnit`.

## Architecture (proposed; Stage 0 decides each `D`)

```
   stream in ---> loader ---> [ block A | block B ]   (ping-pong, D10)
                                     |
            program                  v
   +--------------------+   +---------------------------------------------+
   | instruction memory |   |  data memory: N banks, word-interleaved;    |
   +---------+----------+   |  vector registers, LUT region, scratch      |
             |              +---------------------------------------------+
             v                 ^ operands / results            |
   +-------------------+       |                               v
   |   scalar unit     |  vl,  +---------------------------------------------+
   |  RV32IM subset    | ----> |  L complex lanes: 4 mul, add/sub, shift,     |
   |  PC, loops, addrs | issue |  compare -> v0 mask, merge, min/max         |
   +---------+---------+       +-----------------------+---------------------+
             ^  vmv.x.s / reductions                   |
             +--------------- reduction tree <---------+
                                     |
   stream out <--- storer <--- [ block A | block B ]   (ping-pong, D10)
```

### Execution model

```
for each strip of VL elements:            # strip-mining (vsetvli); one strip if VL covers the block
    for each instruction s in 1..S:        # outer: instructions, issued in order
        for t in 0..VL/L-1:                 # inner: pipelined at II 1
            for lane in 0..L-1 (unrolled):
                vd.elem[t·L + lane] = op_s(vs1.elem[...], vs2.elem[...])
```

Each vector instruction occupies all `L` lanes for `VL/L` cycles. When `VL/L >= D`, the first element of
instruction `s` is written back before instruction `s+1` reads it, so dependent instructions issue back
to back with no forwarding and no interlock -- the classic vector-machine answer to hazards (Cray-1),
and the RVV programming model agents expect. When `VL/L < D` the core stalls the difference (counted,
not hidden; D2).

**The HLS risk.** HLS sees the vector register file as a loop-carried dependence across instructions and
will either flush the pipeline at each instruction's end (`D` lost an instruction) or raise the II. The
proposed body flattens the instruction and element loops into one pipelined loop with
`#pragma HLS dependence variable=<vregs> inter false`, safe only under `VL/L >= D` (or with the stall
counter of D2). Stage 1 witness 6 settles this before anything else is built on it.

### Blocks

- **Scalar unit.** In-order RV32IM subset: integer ALU, branches, `jal`, loads/stores to DM, the CSRs
  below. It owns all control flow and every address. Whether scalar instructions overlap a running
  vector instruction (a small vector issue queue, as Ara and Spatz do) or wait for it is D2; the strip
  loop's bookkeeping is otherwise a per-strip overhead in the cycle model.
- **Lanes.** `L` identical complex lanes (build `HwParam`). Per lane: four 16-bit multipliers (one
  DSP each), add / sub, shifts with rounding, compare to mask, merge, min / max, logic, the conjugate and
  rounding shift of the `wf.` complex ops. No lane reads another lane's elements. How the lane runs real
  data (one 32-bit or two 16-bit elements a cycle) is D3.
- **Vector length.** `VLEN` is a power of two fixed per build; `VLMAX = VLEN / SEW` (so it halves from
  `e16` to `e32`). D2 and D10 fix `VLEN` against `L` and `D`.
- **Data memory.** One local scratchpad, `L` banks, word-interleaved so a unit-stride access of `L`
  elements hits each bank once. Strided accesses (`vlse` / `vsse`) and the LUT region's indexed reads
  can conflict; D5 charges or forbids conflicts.
- **Load / store (decided, item 6).** The loader fills one block while the core computes on the other
  and the storer drains the previous one. D10 decides whether the blocks *are* vector registers or are
  DM regions moved by decoupled `vle` / `vse`.
- **Complex layout (D4).** Proposed: a complex element is one 32-bit word, `re` in the low 16 bits and
  `im` in the high, and complex data runs at `SEW` = 32 (`e32`). `vle32.v` loads complex data; the
  `wf.` complex ops read each `e32` element as a complex pair; `|z|²` comes back as a 32-bit real in the
  same element width. `vlseg2e16` / `vsseg2e16` remain for splitting re / im into two `e16` registers.
- **Reduction tree.** One `log2 L` tree, shared by `vredsum`, `vredmax(u)`, `vredmin(u)`, `vcpop.m`,
  `vfirst.m`. Each lane first reduces its own elements; the tree combines lanes. Argmax is the idiom
  `vredmax` -> `vmseq.vx` -> `vfirst.m`.
- **Coefficient tables.** `vluxei<sew>` (indexed load) is legal only into a small LUT region, replicated
  per lane or banked (D5). This is the one per-lane address in the machine, for piecewise-polynomial
  approximations (sqrt, 1/x, atan, exp).
- **Protocol.** `docs/guide/systems/command_response.md`, as `VecUnit`: a job header (`tx_id`, program
  id or program, region sizes, an instruction budget), input bursts, output bursts each headed by a
  `Resp`, a `Status` per job on its own stream. An error -- illegal instruction, out-of-range address,
  **the instruction budget exhausted** (D11; an agent-written program that never ends must not hang an
  RTL gate) -- is fatal.

### Throughput and the lane

Count `M` 16-bit multipliers (one DSP each). The multiplier bound, MACs a cycle:

| Operation (16-bit) | Multiplies a MAC | Bound |
|---|---|---|
| real x real | 1 | `M` |
| real x complex | 2 | `M/2` |
| complex x complex | 4 (3 with the DSP pre-adder) | `M/4` (`M/3`) |

A lane also has an **operand bandwidth**: two 32-bit operand words a cycle. A complex multiply does 4
multiplies on 64 operand bits, a real one 1 on 32 -- half as many a bit -- so no fixed lane keeps its
multipliers busy on all three. The two lane types (a build option, `lane`):

| MACs a cycle at `M` multipliers | `lane="cplx4"` (default): 4 mul a 32-bit word, `M/4` lanes | `lane="real2"`: 2 mul a 32-bit word, `M/2` lanes |
|---|---|---|
| complex x complex | **`M/4`** (4 of 4 busy) | **`M/4`** (2 cycles an element) |
| real x complex | `M/4` (2 of 4 busy) | **`M/2`** |
| real x real, two `e16` packed | `M/2` (2 of 4 busy) | **`M`** |

`real2` reaches the multiplier bound on all three but needs twice the lanes -- twice the register
bandwidth, DM banks and lane control -- for the same complex throughput, and its complex ops take two
cycles an element (the cost model's `T` doubles for them). For complex-heavy glue `cplx4` is cheaper
in everything but DSPs, so it is the default; Stage 3's instruction mix and Stage 5's numbers say
whether `real2` earns a place.

**Accumulators** are a third, wider operand. A complex MAC keeps its sum in the DSPs' 48-bit
accumulators (four cascaded DSPs, no extra LUT) where the instruction allows (`wf.vcdotr` into a
reduction); a MAC into a register (`wf.vcmacc`, `vmacc`) reads `vd` as a third operand, which the
register file must supply (D2, witness 1). The 3-multiply complex product (DSP pre-adder) is a D7
option: one bit of growth on the pre-sums and a different rounding the golden must follow.

## The ISA (provisional; D1 fixes it)

**Profile.** RV32IM + **`Zve32x`** (the RVV 1.0 embedded, integer-only subset, `ELEN` 32), trimmed as
below, so the toolchains take `-march=rv32im_zve32x` and Spike `--isa=rv32im_zve32x --varch=vlen:<VLEN>,elen:32`.

**Scalar (RV32IM subset):** `lui auipc addi slti sltiu xori ori andi slli srli srai add sub sll slt sltu
xor srl sra or and lw sw lh lhu sh beq bne blt bge bltu bgeu jal jalr`, `mul mulh mulhu` (`div divu rem
remu` per D6), `csrrw csrrs csrrwi csrrsi` (so the assembler's `csrr` / `csrw` / `csrwi` pseudo-ops work:
`vxrm` must be written and `vxsat` cleared) on the CSRs `vl vtype vlenb vxrm vxsat vstart cycle`, and
`ecall` as "job done". No `fence`, no interrupts, no privileged state.

**Configuration:** `vsetvli` / `vsetivli` / `vsetvl` with `SEW` in the build's set (D3), `LMUL` = 1,
`ta, ma` accepted. Agents write `vsetvli t0, a0, e16, m1, ta, ma` by reflex; it must just work.
Tail and masked-off elements are left **undisturbed** (legal under `ta, ma`, and it matches Spike
without a special case).

**Widening and `LMUL`.** RVV widening ops (`vwmul`, `vwmacc`) write `2·SEW` results to a register group
with `EMUL = 2·LMUL`, a register pair at `LMUL` 1. Either D3 admits register-pair destinations for the
widening ops only, or the set leaves them out and 32-bit accumulation runs at `e32`. "`LMUL` = 1 only"
and `vwmul` cannot both stand unqualified.

**Vector memory:** `vle<sew>.v vse<sew>.v vlse<sew>.v vsse<sew>.v vlseg2e<sew>.v vsseg2e<sew>.v`,
`vluxei<sew>.v` (LUT region only), masked forms with `v0.t`. What they cost in issue slots depends on
D10.

**Vector integer:** `vadd vsub vrsub vmul vmulh vmulhu vmacc vnmsac vmadd vnmsub vsll vsrl vsra vand vor
vxor vmin vminu vmax vmaxu` (`.vv .vx .vi` forms where RVV has them), with the assembler's pseudo-ops
`vneg` (`vrsub.vx` with `x0`) and `vnot` (`vxor.vi -1`); fixed-point `vsadd vssub vsmul vssra vssrl
vnclip vnclipu` (with `vxrm`); widening / narrowing per the paragraph above.

**Masks and select:** `vmseq vmsne vmslt vmsltu vmsle vmsleu` (`.vv .vx .vi` where RVV has them) and
`vmsgt vmsgtu` (`.vx .vi` only, as in RVV), `vmerge.vvm / .vxm / .vim`, `vmv.v.v / .v.x / .v.i`,
`vmand.mm vmor.mm vmnand.mm` (`vmnot.m` is the pseudo-op on `vmnand`), `vcpop.m vfirst.m`, masked
execution `v0.t`.

**Reductions and scalar moves:** `vredsum.vs vredmax.vs vredmaxu.vs vredmin.vs vredminu.vs`,
`vmv.x.s vmv.s.x`.

**Complex extensions (`wf.`, on in v1, item 7).** At `e32`, each element a complex 16-bit pair (D4):

| Instruction | Semantics (each element) |
|---|---|
| `wf.vcmul.vv vd, vs2, vs1` | `vd = round_shift(vs2 · vs1)` |
| `wf.vcmulc.vv vd, vs2, vs1` | `vd = round_shift(conj(vs2) · vs1)` |
| `wf.vcmacc.vv vd, vs2, vs1` | `vd = sat(vd + round_shift(vs2 · vs1))` |
| `wf.vcabs2.v vd, vs2` | `vd = round_shift(re² + im²)`, a 32-bit real |
| `wf.vcconj.v vd, vs2` | `vd = conj(vs2)` |

The rounding shift comes from a CSR or the instruction (D1); rounding follows `vxrm`, saturation sets
`vxsat`, as for `vsmul`. Each costs nothing when the build turns it off (rule 7).

**Encoding (D1).** Proposed: the **real RISC-V encodings** (32-bit, standard opcodes; `wf.` ops in
`custom-0..3` through `.insn`), so GNU as and LLVM MC assemble programs, Spike runs the standard subset
as a second golden, and objdump disassembles. Instruction words travel two to a 64-bit stream word.
This reverses `VecUnit`'s "no bit-level RVV encoding compatibility" (its *Out of scope*) on purpose:
the tools are the reason for the ISA.

**Python layer.** `waveflow/simdcore/asm.py` offers one constructor per mnemonic, named as the mnemonic
(`v.vmacc_vv(7, 3, 4)`), and a text assembler for GNU syntax, so the same program is either Python or a
`.S` file. Both produce the same words; a test checks them against GNU as where it is installed.

### What programs look like

**The matched filter, `z = conj(x) · y` and `zsq = |z|²`: `S` = 2.** With D10's proposal (blocks are
registers: `v0` = x, `v1` = y in, `v2` = z, `v3` = zsq out), the whole program for one block is:

```asm
    vsetvli     t0, a0, e32, m1, ta, ma   # a0 = complex samples in the block
    wf.vcmulc.vv v2, v0, v1               # z   = conj(x) * y   (Q15, rounded, saturated)
    wf.vcabs2.v  v3, v2                   # zsq = |z|^2, 32-bit
    ecall
```

Cycles a block: `2 · N/L + D`. Each instruction runs `L` complex samples a cycle; the program delivers
`L/2` samples a cycle. With `M = 4L` multipliers that is `8N/M` cycles against a floor of `6N/M` (6
multiplies a sample; `wf.vcabs2` leaves two of a lane's four multipliers idle). To keep up with an input
of `r` complex samples a cycle, `L = 2r`.

**The same `z` in plain RVV, for comparison** (strip-mined, re / im split, `vsmul` Q15):

```asm
loop:
    vsetvli    t0, a0, e16, m1, ta, ma
    vlseg2e16.v v2, (a1)          # v2 = re(x), v3 = im(x)
    vlseg2e16.v v4, (a2)          # v4 = re(y), v5 = im(y)
    vsmul.vv   v6, v2, v4         # xr*yr
    vsmul.vv   v8, v3, v5         # xi*yi
    vsadd.vv   v6, v6, v8         # re(conj(x) y)
    vsmul.vv   v7, v2, v5         # xr*yi
    vsmul.vv   v8, v3, v4         # xi*yr
    vssub.vv   v7, v7, v8         # im(conj(x) y)
    vsseg2e16.v v6, (a3)
    sub  a0, a0, t0
    slli t1, t0, 2                # 4 bytes per complex element
    add  a1, a1, t1
    add  a2, a2, t1
    add  a3, a3, t1
    bnez a0, loop
    ecall
```

Six compute instructions, three memory instructions and six scalar ones per strip, and a different
rounding (each product rounded before the sum) -- the golden must follow whichever the program does.

**Piecewise function and a running maximum:**

```asm
    vmslt.vx   v0, v8, t1         # mask = x < t1
    vmerge.vvm v9, v10, v11, v0   # y = mask ? p1(x) : p0(x)   (v11 = p1, v10 = p0)
    vredmax.vs v12, v8, v12       # v12[0] = max(v12[0], max_i x[i])
```

## Applications (live in `examples/`, not in the core)

The same set as `VecUnit`, so the comparison is like for like, plus the kernels this core is for:

1. **Toy kernels** (`examples/simdcore/`): `cmul`, `gain`, `poly` (Horner), clip / saturate, `|z|^2`,
   piecewise-linear and LUT-polynomial functions, argmax, sum. They double as the cost-model check
   (criterion 4): programs of `S` = 1 .. 8 on the same `N`.
2. **The matched filter** (`examples/vecunit_mf/`'s computation, re-programmed with a new integer golden,
   plus `|z|²`): long vectors, streaming, I/O-bound -- the best case for this core.
3. **CG** (`examples/vecunit_cg/`'s computation, re-programmed, integer, new golden): short vectors, dot
   products through the reduction tree, divides in the scalar unit (D6). The hardest case: it may sit
   in the `VL/L < D` regime. Whether CG maps one problem across lanes (standard RVV) or one problem per
   lane (as `VecUnit` does) is a program choice, recorded in Stage 3.

## Rules for the doer

1. Work on the plan's branch `simdcore-plan` (PR #308: one PR, many commits), in a separate worktree
   if another agent shares the checkout (set `PYTHONPATH` to the worktree root). Records -- decisions,
   gate commands and their results -- go in `plans/simdcore_records.md`.
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
   one: a minimal core growing an instruction at a time into a full processor -- or into `VecUnit`.
   **No forwarding network and no second issue slot for compute** without a new decision with the user:
   they are exactly the `S²·L` this core exists to avoid.
8. **RVV semantics are the spec.** Where the interpreter and the RVV 1.0 spec disagree on a standard
   instruction, the interpreter is wrong. Where RVV leaves behaviour to the implementation, pick one,
   document it, and test it (tail and mask: undisturbed).
9. Before building a tool the plan did not foresee, search the KB (`waveflow kb search ...`) and propose
   it first.
10. Never merge or push unless asked. One PR, many commits.

## Stages

### Stage 0: decisions (a page of records, reviewed before any code)

Write a record for each, with the options, the proposal and why; the user approves before Stage 1.
Where a record's value depends on a Stage 1 witness (marked *range*), Stage 0 records the range and the
rule that picks the value; Stage 1 picks it.

Before the records, **the arithmetic**: for the matched filter and CG, instructions a sample (compute,
memory, scalar), multiplies a sample, the input floor in cycles, and the cycles `S·N/L + D` predicts at
the candidate `L`. D9's targets are set from it, not before it.

- **D1 ISA and encoding.** The exact instruction list (above, trimmed or extended), real RISC-V
  encodings or not, the CSR set, and where the `wf.` complex ops take their rounding shift.
- **D2 Hazards, vector length and issue** (*range*). `VLEN`, `L`, and the lane depth `D` they must
  cover; what happens when `VL/L < D` (stall cycles, counted); whether scalar instructions overlap a
  running vector instruction (vector issue queue) or wait. The interleaved-warp alternative is recorded
  and rejected or kept.
- **D3 Element widths and the lane.** The `SEW` set (16 and 32 proposed; complex at `e32`).
  Widening: register-pair destinations or none (see *Widening and `LMUL`*). The lane type (`cplx4`
  default, `real2` option; *Throughput and the lane*). Whether a `cplx4` lane runs real `e16` data two
  elements a cycle (two of its four multipliers) or one. Whether a real `e32` multiply is built from
  the lane's four 16-bit multipliers. `plans/vecunit_new_lessons.md` lesson 8: width follows the
  spread, so keep the set small.
- **D4 Complex layout** in DM (packed 32-bit word, `re` low, proposed) and its bank mapping.
- **D5 LUT region and conflicts**: size, replicated per lane or banked; strided and indexed conflicts
  charged or forbidden.
- **D6 Divide**: a scalar `div` (multi-cycle, one per job step), none, or a shared lane divider.
- **D7 Fused complex ops**: on in v1 (decided, item 7); which of the table's ops; the real-only
  configuration Stage 5 measures against.
- **D8 Program delivery**: per job, or resident with a program id in the job header.
- **D9 The comparison and the decision rule** (success criterion 3): the metric (matched throughput, or
  LUT x cycles), the pairings with `VecUnit`, `CgVectorUnit` and a generated matched-filter kernel, the
  pre-registered targets, and the rule that makes this core the default.
- **D10 Blocks and vector memory** (decided: load / store in parallel with compute; this record decides
  the form). Options:
  - **(a) Blocks are vector registers** (proposed). `VLEN` covers a block; named registers (e.g.
    `v0`-`v1` in, `v2`-`v3` out) are ping-pong banks the loader and storer swap between firings, as
    `VecUnit`'s queues of blocks. No `vle` / `vse` in the common case, so `S` counts compute only; the
    strip loop disappears for a block that fits. Costs: register storage `O(N)` a register (decide how
    many of the 32 architectural registers are full length), and departing from RVV's memory model for
    the I/O registers. Check Spike's `VLEN` limit against the block size.
  - **(b) Decoupled `vle` / `vse`.** Standard RVV: a load / store unit beside the lanes (as Ara and
    Spatz), a scoreboard a register, the loader and storer filling DM regions. Short registers (`O(L)`),
    standard programs, but a second issue path and DM ports for both units.
- **D11 Instruction budget**: the job header field, its unit (instructions or cycles), and the error
  `Status`.

### Stage 1: HLS witnesses (facts before design)

Small kernels in `plans/witness/simdcore/`, each csynth'd at 2025.1, `xczu48dr`, 4 ns, the RTL run in
xsim where the question is behaviour:

1. One complex lane's datapath (`wf.vcmul` with conjugate and rounding shift, add, compare, merge) at
   II 1: LUT, FF, DSP, depth `D`.
2. `L` = 2, 4, 8, 16 lanes: does LUT scale linearly (no hidden mixing)?
3. The reduction tree at `L` = 2..16.
4. The banked DM with unit-stride and `seg2` access, the LUT region's indexed reads, and D10's ping-pong
   blocks (loader, core and storer overlapping, at RTL).
5. The scalar unit's decode and issue loop: its II (lesson 9 of `vecunit_new_lessons.md`: the decode
   loop set `VecUnit`'s timing). The model needs it at or below `VL/L`.
6. **The flattened instruction x element loop** with the register-file dependence declared false:
   II 1 across instruction boundaries, no flush, results correct at RTL when `VL/L >= D` and the stall
   path correct when it is not. If this fails, stop and report: the cost model rests on it.
7. **`VecUnit` at `S` = 1** (one format set), csynth only: separates `VecUnit`'s chaining cost from its
   format cost within one build. `S` = 1 lies outside its fitted resource model, so it is measured.

**Gate:** a record table with every number and its tool version; the *range* values of D2 picked from
witnesses 1, 5 and 6; any witness above its D9 budget is a surprise (rule 2).

### Stage 2: ISA, assembler, interpreter

`waveflow/simdcore/{isa,config,asm,model}.py`. The interpreter is the golden: bit-exact, every
instruction tested against a plain-Python reference written from the RVV spec text (not from the
interpreter), and the standard subset against **Spike** on random programs where Spike is installed
(skip loudly otherwise; a marker `-m spike`). The interpreter counts cycles by the cost model (D2) as
well as instructions.

**Gate:** `pytest tests/simdcore -m "not vitis and not xsi"`; `pytest -m spike` where available.

### Stage 3: the applications as programs, on the interpreter

Toy kernels, the matched filter, CG (`examples/simdcore*/`), each bit-exact against its new NumPy golden
and scored for accuracy against the float reference (criterion 2). Count instructions and lane-cycles
per element, the cycles the model predicts, and how CG was mapped and which regime (`VL/L` against `D`)
it runs in. Run the matched filter on the real-only configuration too (D7's evidence).

**Gate:** the examples' fast tests; the counts and accuracy in the record.

### Stage 4: the pysim module and its harness

`SimdCore` (`HwModule`, free-running) with its loader, core and storer as separate tasks on ping-pong
blocks; the harness on the command-response protocol; bit-exact against the interpreter on random
programs and the three applications; the instruction budget's error path tested.

**Gate:** fast tests.

### Stage 5: HLS bodies, RTL

Generated HLS (`waveflow/simdcore/hls.py` -> `waveflow/build/simdcore_core.h`), csynth at the D9
configurations, XSI gates bit-exact against the interpreter (judge free-running bodies at RTL, not
cosim: `vecunit_new_lessons.md` lesson 2). A generated HLS kernel for the matched filter, built the
usual way, beside them.

**Gate:** `pytest -m vitis tests/simdcore`, `pytest -m xsi tests/simdcore`; the D9 table filled: LUT,
FF, DSP, BRAM, cycles a job, accuracy, against `VecUnit`, `CgVectorUnit` and the generated kernel;
measured cycles against `S·N/L + D` (criterion 4); the D9 decision rule applied and its outcome
recorded.

### Stage 6: calibration

Timing and resource models, pre-registered fit and held-out sets spread over configurations (lessons 9,
15, 16). Scored once.

### Stage 7: docs and the agent test

`docs/guide/dsp/simdcore/`: theory (why a plain SIMD core; the cost model against chaining),
architecture, ISA (the list and every deviation from RVV), programming (strip-mining, complex, masks,
reductions, LUTs, the I/O registers), interface, timing, hardware. Then the agent test (criterion 5): a
pre-registered set of kernels described in NumPy, an agent with only the docs, programs scored on the
interpreter. Record the pass rate and every failure mode (which RVV habits broke).

### Stage 8 (optional, to confirm with the user): a NumPy-style front end

A small tracer from NumPy-like Python (`a * b` on complex arrays, `np.conj`, `np.abs(z)**2`, `np.where`,
`np.maximum`, `np.clip`, `.sum()`, `.argmax()`) to programs. Not a compiler: straight-line elementwise
expressions, strip-mined loops, nothing else.

## Out of scope

- FFT, FIR, systolic and any other cross-lane data movement beyond the reduction tree.
- Chaining, operand forwarding, more than one compute instruction in flight per lane (rule 7).
- Floating point, `LMUL` > 1 (beyond D3's widening destinations), general gather / scatter, caches,
  DRAM addressing, interrupts, privilege.
- Divergent control flow (per-lane PCs, a reconvergence stack, `split` / `join`). Predication covers
  the target kernels; see *Later*.
- Retiring `VecUnit` (a later plan, opened by D9's rule on Stage 5's numbers).

## Later (each a build option, each waiting for an application)

- Divergent branches as AMD GCN does them: an `exec` mask plus scalar save / restore, branch on
  all-zero; structured `If / Else / While` in the Python layer emits them.
- Interleaved warps for latency hiding behind memory, if short vectors bind (D2).
- Packed real pairs on the complex lane's multipliers, if D3 left them out.
- A shuffle or slide instruction (`vslideup`, `vrgather` restricted) if an application needs it.

## References

| Topic | Reference |
|---|---|
| the ISA | RISC-V "V" Vector Extension, version 1.0 (incl. the `Zve32x` embedded subset); RISC-V Unprivileged ISA (RV32I, M) |
| reference simulator | Spike, the RISC-V ISA simulator (riscv-software-src/riscv-isa-sim) |
| RVV implementations | M. Cavalcante et al., "Ara," *IEEE TVLSI*, 2020; M. Cavalcante et al., "Spatz," ICCAD 2022 -- Spatz (small integer RVV, few lanes) is the nearest published design: take its area a lane as a sanity check for D9 |
| open SIMT GPUs (contrast) | B. Tine et al., "Vortex," MICRO 2021; Ventus (THU), ICCD 2024 / *IEEE TVLSI* 2025 |
| exec-mask predication | AMD GCN / RDNA ISA manuals (`EXEC`, `s_and_saveexec`) |
| vector chaining, temporal vectors | R. M. Russell, "The CRAY-1 computer system," *CACM*, 1978 |
| `VecUnit`'s cost model | `docs/guide/dsp/vecunit/timing.md` (*Resources*); `plans/vecunit_future.md` (2025.1 numbers) |

Check each against OpenAlex before the docs cite it (as `plans/vecunit_new.md` did).

## Log

| Date | Stage | Event | Where |
|---|---|---|---|
| 2026-11-07 | -- | Drafted with the user: integer-only SIMD core, predication not divergence, RVV-subset ISA for agent familiarity, FFT / FIR / systolic left to their own PEs | this file |
| 2026-11-07 | -- | Revised after review: temporal execution and the cost model (`S·N/L + D`, `O(L)`) against `VecUnit`'s `S²·L`; load / store parallel to compute (D10); fused complex lanes in v1; `Zve32x`; new goldens and an accuracy criterion; D9 decision rule; witnesses 6 (flattened loop) and 7 (`VecUnit` at `S` = 1); instruction budget (D11) | this file |
| 2026-11-07 | 0 | Throughput bounds and the lane option (`cplx4` default, `real2`); Stage 0 records written as proposals while the user is away (D10 revised to I/O register windows) | `plans/simdcore_records.md` |
| 2026-11-07 | 1 | Witnesses W1-W7 (2025.1): flattened loop II 1, D 9, cosim bit-exact, one fill; short-vector hazard confirmed at RTL; ~3.7k LUT a lane, linear; scalar issue II 3; `VecUnit` S 1 = 37.4k LUT (second D9 reference). Model re-run: MF 1.11x floor, CG 4,125 / 2,412 at `L` 2 / 4 | `plans/witness/simdcore/`, records Stage 1 |
| 2026-11-07 | 2 | ISA, assembler, interpreter, cycle model; every encoding matches GNU as; every instruction matches a spec-text reference. Spike deferred (none here) | records, Stage 2 |
| 2026-11-07 | 3 | Toy kernels; matched filter bit-exact with `vecunit_mf`'s golden, 1.11x floor at `L` 2 (model); CG bit-exact with its new golden, 4,124 / 2,284 cycles at `L` 2 / 4 (model), residual 0.028 vs float 0.027. Added `vdiv` (option), `wf.vcdotra`, `wf.vcmadd.vv`, one-copy output windows | records, Stage 3 |
| 2026-11-07 | 4 | `SimdCore` in pysim (loaders, core, storers on block queues; command-response words); equals the interpreter on MF and closed-loop CG; MF steady 1,160 cycles a frame at `L` 2 (1.007x floor) | records, Stage 4 |
| 2026-11-07 | 5 (prep) | D12 element-aligned masks, D13 hardware rules (optional `lut` / `strided` / `scalar_mem`, `vle`/`vse` bank-aligned, no window on `v0`, input windows read-only); Stage 5 architecture proposed; the agent test's goldens and scorer written, reference solutions pass all 8 | records D12, D13; `tests/simdcore/agent_test/` |
| 2026-11-07 | 5 | Generated HLS core (scalar, vector, divider tasks): csim 27/27 and XSI (MF, CG with vdiv) bit-exact. MF L 2: 1,168 cycles a frame (1.01x input floor), 36.5k LUT; CG L 2: 5,657 cycles, 49.0k LUT; CG L 4: 76.8k LUT. Vector loop II 1 at 5.31 ns (not closed at 4). **D9 rule not met** (CG); no VecUnit retirement | records, Stage 5 |
