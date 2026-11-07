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
- **D**: picked from witnesses 1 and 6.

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

### D11 Instruction budget

A 32-bit field `budget` in the job header: instructions issued (scalar and vector). Exhausted -> `Status`
error `BUDGET`, fatal (the command-response rule). 0 = no budget.

## Stage 1: HLS witnesses

*(not started)*

## Stage 2: ISA, assembler, interpreter

*(not started)*

## Stage 3: applications on the interpreter

*(not started)*
