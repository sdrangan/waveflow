# Records: the SIMD core (`plans/simdcore.md`)

One section a stage: the decisions, the commands that gate it and their results. Numbers name their tool
version.

> **How these were made.** The user asked (2026-11-07) for as much progress as possible while away, so
> Stage 0's records below are **proposals the doer acted on**, not approvals. Each says what it rests on
> and what would reverse it. Everything built on them is reversible; the ones marked **REVIEW** are the
> calls the user should look at first on return.

## Stage 0: decisions

### The arithmetic (before the records)

**Matched filter**, `examples/vecunit_mf` `steady`: a frame of 256 samples at `nrx` 8 antennas, chunks of
64 samples. `Y[i, j] = conj(X[i]) · R[i, j]`: 2,048 complex products a frame. Input: `X` 32 words and `R`
256 words a chunk (64-bit words, two complex samples a word) = **1,152 words a frame, the input floor
at one word a cycle**. `VecUnit` (2024.1): 1,408 cycles (1.22x); its execute task binds.

On this core, with I/O register windows (D10) and the loader writing `R` column-major (antenna `j` in
register `vR+j`), a chunk is 8 instructions `wf.vcmulc.vv vY+j, vX, vR+j` of `VL` 64, one complex
multiply each:

| `L` (complex lanes, `cplx4`) | DSP | compute a frame (`8·4·64/L`) | + overhead (4 chunks x (`D` + ~16 issue)) | bound |
|---|---|---|---|---|
| 1 | 4 | 2,048 | ~2,150 | compute, 1.87x floor |
| **2** | **8** | **1,024** | **~1,130** | **the loader: ~1.0x floor** |
| 4 | 16 | 512 | ~620 | the loader |

`L` = 2 meets the floor; `L` = 4 idles half its lanes. Without D10's windows (plain `vle` / `vse` in the
issue path, three instructions an antenna) `L` = 2 takes ~3,100 cycles (2.7x floor) and `L` = 6 would be
needed: the windows are worth a factor of three on a streaming kernel.

**CG**, `examples/vecunit_cg` `steady`: a job is 8 independent problems of `n` 32 (complex), 3
iterations. Per iteration and problem, CG's vector work is 5 complex elementwise instructions and 2
reductions (`ps = Re<P, S>`, `X += αP`, `R -= αS`, `rz' = |R|²`, `P = βP + R`) and 2 divides (`α`, `β`).
One problem per instruction (`VL` = 32, standard RVV; `T = 32/L`):

| `L` | vector cycles a job (`3 · 8 · 7 · 32/L`) | + divides, 48 a job | + reduction drains | estimate | `VecUnit` (2025.1) |
|---|---|---|---|---|---|
| 2 | 2,688 | 0 to 1,632 (overlap dependent, D6) | ~300 | **3,000-4,600** | 3,447 |
| 4 | 1,344 | same | ~300 | **1,700-3,300** | |

The divides decide CG: 48 iterative 34-cycle divides are 1,632 cycles if nothing hides them. With
kind-ordered programs (every problem's dot, then every divide, ...; as `VecUnit`'s CG program) and a
decoupled scalar unit (D2), the divides of one kind overlap the vector work of the next. D6 keeps a
pipelined divider as the fallback.

### D1 ISA and encoding

- **List:** the plan's ISA section, trimmed: no widening ops in v1 (D3), `vlseg2` / `vsseg2` kept,
  `vluxei` kept (LUT region).
- **Encoding:** real RISC-V encodings (checked: the GNU as of the Vitis 2025.1 install,
  `C:\Xilinx\2025.1\gnu\riscv\nt\riscv64-unknown-elf\bin\riscv64-unknown-elf-as.exe`, binutils 2.39,
  assembles `-march=rv32im_zicsr_zve32x` and `.insn`).
- **`wf.` ops** use the OP-V field layout (`funct6 vm vs2 vs1 funct3 vd`) under **`custom-0`** (`0x0B`),
  `funct3` `000` for `.vv`, `100` for `.vx`:

  | `funct6` | mnemonic | each element (complex = an `e32` element, `re` low 16 bits) |
  |---|---|---|
  | 0 | `wf.vcmul.vv/.vx vd, vs2, vs1/rs1` | `vd = rs(vs2 · vs1)` |
  | 1 | `wf.vcmulc.vv vd, vs2, vs1` | `vd = rs(conj(vs2) · vs1)` |
  | 2 | `wf.vcmacc.vv/.vx vd, vs2, vs1/rs1` | `vd = sat(vd + rs(vs2 · vs1))` |
  | 3 | `wf.vcmadd.vx vd, vs2, rs1` | `vd = sat(rs(vd · rs1) + vs2)` |
  | 4 | `wf.vcdotr.vv vd, vs2, vs1` | `vd = rs32(Re(conj(vs2) · vs1))`, a 32-bit real (`wf.vcabs2 vd, vs` is the pseudo-op with `vs2 = vs1`) |
  | 5 | `wf.vcconj.v vd, vs2` | `vd = conj(vs2)` |
  | 6 | `wf.vcdotra.vv vd, vs2, vs1` | `vd = sat32(vd + rs32(Re(conj(vs2) · vs1)))` (added in Stage 3 for CG's dot products) |

  `wf.vcmadd` also got its `.vv` form in Stage 3 (CG's `P = R + βP` in place).

  `rs(p)` = `sat16(round(p >> csh))` per part, `rs32(p)` = `sat32(round(p >> rsh))`; rounding by
  `vxrm`, saturation sets `vxsat`. The shifts are a **custom CSR `wfsh`** (`0x800`, user read/write):
  `csh` bits 4:0 (reset 15, Q15), `rsh` bits 12:8 (reset 0). Any `.vx` complex scalar is `rs1` read as
  a complex pair (a real `α` is `α + 0j`).
- **Queue ops** for D10, I-type under **`custom-1`** (`0x2B`): `wf.qpop q` (`funct3` 0, `imm` = `q`) waits
  for and takes the next input block of window `q`; `wf.qpush q` (`funct3` 1) hands window `q`'s block to
  the storer.
- **CSRs:** `vl vtype vlenb vxrm vxsat vstart cycle wfsh`.

Would reverse: an agent-test failure mode (Stage 7) showing the `wf.` operand order or the shift CSR
confuses agents.

### D2 Hazards, vector length and issue

- **Temporal vectors** (the plan's model); interleaved warps rejected for v1 (no latency to hide: memory
  is local and the I/O is decoupled).
- **`VLEN` 2048 bits** (64 `e32`, 128 `e16` elements), *range* 1024-4096: a chunk of the matched filter
  fits one register. `T = VL/L`: 32 at `L` 2 for a full `e32` vector.
- **Short vectors:** when `VL/L < D`, the next dependent instruction waits `D - VL/L` cycles (counted
  by the interpreter's cycle model and by the hardware).
- **Scalar unit decoupled** (**REVIEW**): the scalar unit and the vector unit are two `hls::task`s; the
  scalar unit decodes, resolves scalar operands and pushes vector instructions into a queue, running
  ahead. A scalar read of a vector result (`vmv.x.s`, `vcpop.m`, `vfirst.m`) waits for it on a return
  stream. Rests on: CG's divides overlapping vector work (the arithmetic above). Reverse if witness 5
  shows the two-task form costs more than it hides. Note the request-response law
  (`reference-hls-request-response-deadlock`): the scalar task reads the return stream only after it
  has pushed the producing instruction, and the return stream's depth covers every result in flight.
- **D = 9**: picked from witness 6 (Stage 1).

### D3 Element widths and the lane

- **`SEW` ∈ {16, 32}.** Complex data at `e32`; real 16-bit at `e16`; 32-bit real (sums, `|z|²`) at `e32`.
- **No widening ops in v1** (`vwmul`, `vwmacc`, `vnsra`, `vnclip` narrowing from `2·SEW` need `EMUL` 2):
  32-bit accumulation runs at `e32`, and `vnclip`'s job is done by `vssra` + `vmin` / `vmax` or by the
  `wf.` ops' saturating shift. Reverse: an application whose golden needs a 16x16 -> 32 real product
  stream.
- **Lane `cplx4`** (default, the user's call: complex-heavy for now); `real2` a build option kept in the
  config and the interpreter's cycle model, built in Stage 5 only if Stage 3's mix says so.
- **`e16` real on `cplx4`: two elements a lane-cycle** (two of its four multipliers), the lane word
  being 32 bits.
- **`e32` real multiply** (`vmul` / `vmulh` / `vsmul` at `e32`): one a lane-cycle from the four 16-bit
  multipliers, *if* witness 1 shows it costs under 15 % of the lane; else absent (illegal) at `e32`.

### D4 Complex layout

A complex element is one 32-bit word, `re` bits 15:0, `im` bits 31:16 (little-endian: `re` first in
memory, so `vlseg2e16` splits it). Element `i` of a register lives in lane `i mod L`; DM is
word-interleaved over `L` banks, bank = word address mod `L`.

### D5 LUT region and conflicts

- **LUT region:** 256 entries of 32 bits, replicated per lane (a LUTRAM or BRAM18 a lane), written by
  `vse` to its address range and read by `vluxei`. Size a build parameter.
- **Strided / indexed DM conflicts are charged, not forbidden:** a group of `L` accesses takes as many
  cycles as its busiest bank (the interpreter counts them). Programs avoid them; the matched filter
  does, through the loader's column-major windows.

### D6 Divide

`div divu rem remu` in the scalar unit, iterative (one quotient bit a cycle, ~34 cycles), blocking the
scalar unit only. With D2's decoupling the vector queue keeps running. Fallback (if CG's Stage 3 count
shows divides bind): a pipelined divider, II 1, a build option.

**Revised in Stage 3:** CG's `alpha` and `beta` are vectors of `N` (a register a row), so the scalar
divide would cost 32 x 34 cycles a step. Added the standard **`vdiv` / `vdivu` / `vrem` / `vremu`** as
a build option (`SimdCoreConfig.vdiv`, off by default): a pipelined divider a lane, occupancy `VL/L`,
read-to-write `vdiv_latency` (36, a guess for witness 1 to replace). CG builds it; nothing else does.

### D7 Fused complex ops

On in v1 (decided, item 7): the D1 table. The real-only configuration (`cplx_ops=False`: the `wf.`
complex ops illegal) is built in Stage 5 for the trade. The 3-multiply product: off; recorded as a
later option.

### D8 Program delivery

**Per job**, as `VecUnit`: the job header, then the program words (two to a 64-bit stream word), into
instruction memory. Resident programs (a program id) later, if a job's program load shows in Stage 5.

### D9 The comparison and the decision rule (pre-registered 2026-11-07, before any csynth)

Configurations: `cplx4`, `VLEN` 2048, 16-bit complex, 2025.1, `xczu48dr`, 4 ns.

| | `L` | cycles target | LUT target (stretch) |
|---|---|---|---|
| matched filter `steady` (a frame) | 2 | <= 1.15x floor (1,325) | <= 20k (12k) |
| CG `steady` (a job) | 2 | <= 1.5x `VecUnit` (5,170) | <= 25k (15k) |
| CG `steady` (a job) | 4 | <= 1.0x `VecUnit` (3,447) | <= 30k (21.5k = `CgVectorUnit`) |

Accuracy (criterion 2): matched-filter output SNR within 1 dB of `VecUnit`'s; CG residual after 3
iterations within 2x of `VecUnit`'s, on the same inputs.

**Decision rule (REVIEW):** this core becomes the default programmable unit, and a plan to retire
`VecUnit` opens, if on **both** the matched filter and CG, at the smallest `L` whose cycles are within
10 % of `VecUnit`'s, its LUT is at most **50 %** of `VecUnit`'s and the accuracy targets hold. The
generated matched-filter kernel is reported beside them, not part of the rule.

Revising a target after Stage 1 is allowed only with the reason logged (the witnesses are components;
the targets are the whole unit).

### D10 Blocks and vector memory (**REVIEW**: the doer revised its own earlier proposal)

**I/O register windows.** A build declares input and output **windows**: runs of consecutive vector
registers (e.g. `X` = `v1`, `R` = `v8`-`v15`, `Y` = `v16`-`v23`). Each window has two physical copies
(ping-pong): the loader fills the next copy of an input window from a 2-D block (`k x n`, row-major on
the stream; column `j` lands in register `base + j`) while the program runs on the current one;
`wf.qpop q` swaps copies; `wf.qpush q` hands an output window's copy to the storer, which writes it
row-major. Window copies are their own memories, so the loader and storer never take a register-file
port the program needs.

- Rests on: the arithmetic above (a factor of three on the matched filter against `vle` / `vse` in the
  issue path) and on keeping one issue slot (rule 7). The 2-D column mapping is `VecUnit`'s column
  groups.
- `vle` / `vse` / `vlse` / `vsse` / `vluxei` remain, in the issue path, for DM scratch and the LUT region.
- **Spike** sees standard RVV: the harness wraps a program with `vle` preloads of the windows and `vse`
  dumps, and `wf.qpop` / `wf.qpush` as no-ops, for the instruction-for-instruction comparison.
- Option (b), decoupled `vle` / `vse` with a load/store unit and a register scoreboard, stays recorded:
  standard programs, but the memory instructions still move every element twice (loader -> DM -> register),
  so it needs about twice the DM bandwidth of (a) for a streaming kernel.

**Settled in Stages 2-3:** after `wf.qpush` a two-copy window's registers are unspecified until
written (the interpreter fills them with `0xA5` bytes so a misuse shows); a write waits until the
storer has drained the copy it lands in. An **output window may have one copy** (`Window.copies = 1`):
`wf.qpush` keeps the values and a write waits until the storer has drained them -- CG keeps `P` in its
output window this way. Windows on one port share its loader or storer (one 64-bit word a cycle). The
closed loop (`on_empty`, CG's `S = A P`) is timed as a static testbench: the block is on the port as
soon as the port is free, as `examples/vecunit_cg`'s RTL bench.

### D11 Instruction budget

A 32-bit field `budget` in the job header: instructions issued (scalar and vector). Exhausted -> `Status`
error `BUDGET`, fatal (the command-response rule). 0 = no budget.

## Stage 1: HLS witnesses

**Done 2026-11-07** (`plans/witness/simdcore/`, its README has every row and the raw outputs). Vitis HLS
2025.1, `xczu48dr`, 4 ns, csynth estimates unless marked cosim.

| Witness | Result | Decides |
|---|---|---|
| W6 flattened instruction x element loop, L 2 | **II 1, depth 9**, cosim bit-exact on a 12-instruction dependent chain; latency = one fill, no flush between instructions | the core's shape (Stage 5) |
| W6 at `VL/L` = 2 | csim passes, **cosim fails** | the short-vector stall (D2) is required hardware |
| W6 without the dependence pragma | II 2, 6.5 ns est. | the pragma is required |
| W2 L 2 / 4 / 8 / 16 | 8,744 / 15,804 / 29,548 / 59,861 LUT; 4 DSP a lane | linear in L (~3.7k a lane) |
| W1 one lane | 3,889 LUT; fixed shifts 2,960; + e32 multiply 4,235 (+8.9 %) | D3: keep the e32 multiply |
| W3 reduction | ~270 LUT a lane, depth 3 + 1 a tree level | |
| W5 scalar + vector `hls::task`s | two jobs, no deadlock; issue loop **II 3** at 4.05 ns (II 1: 6.8 ns) | D2 decoupling holds; `scalar_ii` = 3 |
| W4 window ping-pong | inconclusive: HLS split each window 3 ways (24 BRAM18) | Stage 5 builds windows port-per-copy |
| W7 `VecUnit` L 4, CG layout | S 3: 81,862; S 1: 42,522; S 1 one format: **37,386** LUT | D9 second reference |

**Picked:**

- **D = 9** (`SimdCoreConfig.depth`, was 8). **`scalar_ii` = 3** (new field: the scalar unit issues an
  instruction every 3 cycles, a vector instruction included, since it decodes them). `vdiv_latency` 36
  stays a guess (not witnessed; CG only).
- **The `wf.` shifts stay run-time** for now (24 % of a lane). If Stage 5's L 2 total misses 20k LUT,
  the first cut is `csh` / `rsh` fixed per build (`-1.9k` at L 2).

**Re-run on the interpreter with D 9 and `scalar_ii` 3** (was D 8, `scalar_ii` 1):

| | L 1 | L 2 | L 4 | L 8 |
|---|---|---|---|---|
| matched filter, cycles a frame (x floor) | 2,186 (1.90) | **1,282 (1.11)** | 1,250 (1.09) | 1,234 (1.07) |
| CG, cycles a job (3 iterations) | 7,945 | **4,125** | **2,412** | 2,000 |

Both D9 cycle targets still hold (matched filter <= 1.15x at L 2; CG <= 5,170 at L 2 and <= 3,447 at
L 4). CG at L 4 has 227 cycles of dependence stall (`VL/L` = 8 < D = 9) and at L 8, 775: L 8 buys
little for CG.

**D9 revised (logged, rule 2):** add the un-chained `VecUnit` (`S` 1, one vector and one scalar format,
**37,386 LUT** at L 4) as a **second reference** beside the 82k build. Reason: an un-chained `VecUnit`
runs at the same `S·N/L` as this core, so it is the like-for-like cost baseline; W7 shows chaining is
48 % of `VecUnit`'s LUT and the formats 6 %. The decision rule's 50 % is unchanged and still measured
against the 82k build; the comparison with the S 1 build is **reported** beside it, since it is what
says whether the ISA, rather than dropping chaining, saves anything. Expectation from the witnesses:
this core at L 4 ~23k + loader / storer / control, i.e. tens of percent under S 1, not 3x.

## Stage 2: ISA, assembler, interpreter

**Done 2026-11-07** (`waveflow/simdcore/{config,isa,asm,model}.py`, `tests/simdcore/`).

- `isa.py`: one table (`SPECS`, 170 mnemonics) drives encode, decode and the assembler; the real RV32IM /
  Zicsr / RVV 1.0 encodings; `wf.` complex ops under `custom-0` in the OP-V layout, queue ops under
  `custom-1` (D1).
- `asm.py`: GNU-syntax text with labels and the pseudo-ops (`li` expands as GNU's does); the Python form
  (`Program`, one method per mnemonic) builds the same text; `to_gnu` renders `wf.` ops as `.insn`.
- `model.py`: the interpreter (tail and mask undisturbed; `vill` on an unsupported `vtype`; EEW <= SEW
  vector memory, fractional EMUL legal; LUT-region-only `vluxei`; windows; fatal error codes) and the D2
  cycle model (`Timing`).
- `tests/simdcore/_ref.py`: a plain-Python reference written from the RVV spec text (per element,
  Python integers, nothing from the interpreter); `test_model.py` checks every elementwise, compare,
  merge / move, reduction, mask, scalar-move and complex instruction against it on random registers,
  `vl`, masks, `vxrm` and shifts, plus scalar RV32IM, memory, `vsetvli`, windows, errors and the cycle
  model (`S·N/L + D` within 3 cycles for `L` 1-4, `S` 1-8; the short-vector stall exactly
  `(S-1)(D - VL/L)`).
- **Encodings checked against GNU as** (`test_asm.py`): three random corpora of every mnemonic (and
  `li`'s expansions) assemble to GNU as's words. GNU as here: Vitis 2025.1's
  `riscv64-unknown-elf-as` (binutils 2.39), `-march=rv32im_zicsr_zve32x`.

One correction found by the tests: RVV allows `EEW < SEW` vector loads and stores (fractional `EMUL`,
e.g. `vlseg2e16.v` at `e32`); the first interpreter made them illegal.

**Gate:**

```
../pysilicon-venv/Scripts/python.exe -m pytest tests/simdcore -q -rs
129 passed
```

**Spike: not run.** This laptop has no Spike and no WSL; the `-m spike` comparison (plan Stage 2) is
deferred to a Linux machine -- the doer did not write a harness it could not run. On Linux: build Spike,
run each Stage 3 program with `--isa=rv32imv_zicsr --varch=vlen:<VLEN>,elen:32` (windows preloaded by
`vle`, as D10 says) and compare register state at `ecall`.

## Stage 3: applications on the interpreter

**Done 2026-11-07** (`examples/simdcore/`, `examples/simdcore_mf/`, `examples/simdcore_cg/`; tests in
`tests/examples/test_simdcore_*.py`). All cycle numbers below are **the interpreter's D2 cycle model
(`depth` 8, `vdiv_latency` 36, guesses until Stage 1's witnesses), not measurements.**

### Toy kernels

`cmul gain abs2 clip poly pwl lut argmax sum` (`examples/simdcore/toy.py`), each against a NumPy golden
written from the math, at `N` 1-128 and `L` 1-8. `lut` is a 64-segment piecewise-linear sin from the
LUT region (`vluxei16`); `argmax` the `vredmax` -> `vmseq` -> `vfirst` idiom.

### Matched filter

`examples/simdcore_mf/mf_program.py`: `wf.vcmulc` per antenna with `csh` 14, `vxrm` rdn reproduces
`examples/vecunit_mf`'s golden **bit for bit** (its formats: one floor rounding of the exact product)
-- so its accuracy equals the vector unit's by construction. One frame = one job; `S` = 1 an antenna,
`nrx + 2` vector instructions a chunk.

Four back-to-back frames (256 samples x 8 antennas, one job), per frame, against the input floor of
1,152 words (`python -m examples.simdcore_mf.mf_program`):

| `L` (`cplx4`) | DSP | cycles a frame | x floor | lanes busy |
|---|---|---|---|---|
| 1 | 4 | 2,186 | 1.90 | 2,048 (compute-bound) |
| **2** | **8** | **1,282** | **1.11** | 1,024 (load-bound) |
| 4 | 16 | 1,250 | 1.09 | 512 |
| 8 | 32 | 1,234 | 1.07 | 256 |

The vector unit (2024.1 RTL): 1,408 cycles a frame (1.22x), 53 DSP at `L` 4. D9's target (`L` 2,
<= 1.15x) holds as a prediction. A single frame alone takes 1,672 at `L` 2: the first chunk's load and
the last chunk's store are not hidden within one job.

**D7's evidence (computed from the model, not run).** Without the `wf.` complex ops, `conj(x)·r` is 4
`vsmul` and 2 `vsadd`/`vssub` at `e16` on split `re`/`im` registers (two elements a lane-cycle): 3N/L
lane-cycles against N/L -- three times the compute -- and each product rounded before the sum, so not
bit-exact with the golden (that needs the widening ops D3 left out). At `L` 2 the real-only program
would be compute-bound (~3,100 cycles a frame); it would need `L` 6 to reach the floor.

### CG

`examples/simdcore_cg/cg_program.py`. A register is a row (`VL = N` = 32 columns); a column's dot
product is `K` = 8 accumulating `wf.vcdotra`; `alpha`, `beta` are vectors by `vdiv`, guarded
(`vmseq` + `vmerge`) and saturated to 16 bits. `P` lives in the one-copy output window, `R` in
registers, `X` in DM (`vle` / `vse`, 16 a iteration: 32 registers hold `IN`, `P`, `R` and the
temporaries, not `X` too). The integer golden (`cg_golden`) follows the program step by step; the
interpreter matches it bit for bit, closed loop through `S = A P`, for `K` 3-8, `N` 13-64, 1-6
iterations, `L` 1-8, and a zero column (both guards).

The steady shape (8 x 32, 3 iterations; 248 vector and 71 scalar instructions a job):

| `L` | DSP (+ `L` dividers) | cycles a job | lanes busy | `VecUnit` (2025.1 RTL) |
|---|---|---|---|---|
| 2 | 8 | **4,124** | 3,840 | |
| 4 | 16 | **2,284** | 1,920 | 3,447 at 53 DSP |

D9's targets (`L` 2 <= 5,170; `L` 4 <= 3,447) hold as predictions. At `L` 4, `VL/L` = 8 = `D`: the
dependence stalls start there (100 cycles); at `L` 8 the short vectors would bind.

**Accuracy (criterion 2):** mean relative residual `‖B − A X‖ / ‖B‖` on the float problem after 3
iterations, 8 problems (`accuracy()`):

| float CG | this core (16-bit vectors) | `VecUnit` centre build (12-bit) |
|---|---|---|
| 0.0266 | **0.0282** | 0.0639 |

A first version with the centre build's scalar precision (`ps` frac 10, `alpha` frac 7, `beta` frac 9)
gave 0.098 -- worse than the vector unit despite 16-bit vectors; widening the scalars into the room 32
and 16 bits leave (`ps`, `rz` frac 14, `alpha` frac 11, `beta` frac 13) fixed it. The comparison is not
like for like (the vector unit's 12-bit centre formats were chosen for its cost); what it shows is that
integer shifts lose nothing against per-register formats here. **Headroom:** `rz << 11` must fit 31
bits, so `rz < 64` (`|R|²` summed); `vsll` wraps rather than saturates -- the program's responsibility,
recorded, not guarded.

### What Stage 3 changed

- ISA: `vdiv` family (a build option, D6), `wf.vcdotra.vv`, `wf.vcmadd.vv` (D1) -- each for CG.
- Windows: one-copy output windows; the port-shared loader and storer; `on_empty` for closed loops (D10).

**Gate:**

```
../pysilicon-venv/Scripts/python.exe -m pytest tests/simdcore tests/examples/test_simdcore_toy.py tests/examples/test_simdcore_mf.py tests/examples/test_simdcore_cg.py -q
195 passed
```

## Stage 4: the pysim module and its harness

**Done 2026-11-07** (`waveflow/simdcore/unit.py`, `tests/simdcore/test_unit.py`).

- `SimdCore` (composite): a `SimdLoad` an input port, `SimdExec`, a `SimdStore` an output port, joined by
  stream-of-blocks queues (depth 2): one a input window (the loader routes a block by its header's
  window), one an output port (blocks in push order, tagged with their window).
- The protocol: a job's program burst on `s_prog` (header `tx_id ninstr nargs budget`, `a0..`
  arguments, instructions two a 64-bit word); an input block per burst (header `window k n`, elements
  row-major two a word); per `wf.qpush` one burst on its port (a response word `tx_id window k n`, then
  the elements); a status word per job on `m_status` (`tx_id status steps`). An error is fatal.
- `SimdExec` drives the interpreter an instruction at a time (`Core.begin / peek / step / result`, a
  refactor of `run`, same results): at a `wf.qpop` it waits on the queue and hands the block's arrival
  cycle to the cycle model (`Timing.external`); at a `wf.qpush` it waits until the model's push cycle
  and commits the block. So pysim's time is the D2 model's, with the real queues' back-pressure.
- An application subclasses `SimdCore` and gives `jobs(kind)` (`Job`: program, arguments, budget, input
  blocks); `workload` and `golden` come from the interpreter (`job_bursts`, `golden_bursts`), input
  blocks on each port in the order the interpreter popped them. Data memory is not loaded over the
  protocol (a program fills it from a window), so the `lut` toy kernel is interpreter-only for now.

**pysim's steady interval, the matched filter** (5 back-to-back frames of 256 x 8, the cycle model's
`depth` 8): `L` 1: 2,081 cycles a frame (compute-bound, 2,048 lane-cycles); `L` 2: **1,160** (1.007x
the 1,152-word floor) -- the loader runs into the next job while this one's tail computes, which the
single-job interpreter run (1,282) cannot show.

**Gate:**

```
../pysilicon-venv/Scripts/python.exe -m pytest tests/simdcore tests/examples/test_simdcore_toy.py tests/examples/test_simdcore_mf.py tests/examples/test_simdcore_cg.py -q
201 passed
```

The pysim tests: the matched filter (`nrx` 8 at `L` 2, `nrx` 3 at `L` 4; three jobs, a short last
chunk) and CG (two closed-loop solves, static `S` stimulus) equal the interpreter burst for burst; a
fatal `ADDR` in the second of three jobs stops the unit after its status; the steady intervals above.
