---
title: SIMD Core Instruction Set
parent: SIMD Core
nav_order: 1
summary: "The SIMD core's instructions: RV32IM for control, the RVV 1.0 subset it implements (Zve32x, LMUL 1, SEW 16 and 32), the wf. complex and queue extensions with their encodings and exact arithmetic, the CSRs, the implementation choices RVV leaves open (tail and mask undisturbed), and what is absent. Standard instructions keep their RISC-V meaning and encoding; GNU as assembles them with -march=rv32im_zicsr_zve32x."
---

# SIMD Core Instruction Set

A program is RISC-V assembly. Every standard instruction keeps its RISC-V meaning and its 32-bit
encoding (`waveflow/simdcore/isa.py`, checked against GNU as); the extensions carry the `wf.` prefix.
**An instruction not listed here is illegal**: the job ends with status `ILLEGAL`. Nothing is
redefined.

GNU as takes the standard subset with `-march=rv32im_zicsr_zve32x -mabi=ilp32`; the `wf.` instructions
go through `.insn` (`waveflow.simdcore.asm.to_gnu` writes them so). Waveflow's own assembler
(`waveflow.simdcore.asm.assemble`) takes all of it, labels and the usual pseudo-instructions included.

## Scalar: RV32IM

| Group | Instructions |
|---|---|
| integer | `lui auipc addi slti sltiu xori ori andi slli srli srai add sub sll slt sltu xor srl sra or and` |
| multiply, divide | `mul mulh mulhu div divu rem remu` (`div` is iterative, about 34 cycles) |
| memory | `lw sw lh lhu sh` (aligned; outside data memory: status `ADDR`) |
| control | `beq bne blt bge bltu bgeu jal jalr`; `ecall` ends the job |
| CSR | `csrrw csrrs csrrc csrrwi csrrsi csrrci` (`csrr`, `csrw`, `csrwi`, `csrs`, `csrc` as pseudo-instructions) |

Pseudo-instructions: `nop li mv not neg j jr ret beqz bnez blez bgez bltz bgtz bgt ble bgtu bleu`.
No `lb`/`sb`, no `fence`, no compressed instructions, no interrupts, no privileged state.

## Vector: the RVV 1.0 subset

**Configuration.** `vsetvli`, `vsetivli`, `vsetvl` with `SEW` 16 or 32 and `LMUL` `m1`; `ta`/`tu` and
`ma`/`mu` are all accepted. `vl = min(AVL, VLMAX)`, `VLMAX = VLEN / SEW` (`VLEN` a build parameter,
2048 bits by default: 64 `e32` or 128 `e16` elements). Another `SEW` or `LMUL` sets `vill`, and the
next vector instruction is illegal.

| Group | Instructions (`.vv .vx .vi` where RVV has them) |
|---|---|
| add, logic | `vadd vsub vrsub vand vor vxor vmin vminu vmax vmaxu`; `vneg.v vnot.v` pseudo |
| shifts | `vsll vsrl vsra` |
| multiply | `vmul vmulh vmulhu`; `vmacc vnmsac vmadd vnmsub` (note RVV's order: `vmacc.vv vd, vs1, vs2`) |
| fixed point | `vsadd vsaddu vssub vssubu vsmul vssra vssrl` (rounding by `vxrm`, saturation sets `vxsat`) |
| compare -> mask | `vmseq vmsne vmslt vmsltu vmsle vmsleu`; `vmsgt vmsgtu` (`.vx .vi` only) |
| select, move | `vmerge.vvm/.vxm/.vim` (`vd = v0 ? vs1 : vs2`), `vmv.v.v/.v.x/.v.i` |
| mask logic | `vmand.mm vmor.mm vmnand.mm`; `vmnot.m vmmv.m` pseudo; `vcpop.m vfirst.m` |
| reductions | `vredsum vredmax vredmaxu vredmin vredminu` (`.vs`: `vd[0] = op(vs1[0], vs2[*])`) |
| scalar moves | `vmv.x.s vmv.s.x` |
| memory | `vle16/32.v vse16/32.v`; build options: `vlse vsse vlseg2 vsseg2` (`strided`), `vluxei16/32.v` (`lut`) |
| divide (build option `vdiv`) | `vdiv vdivu vrem vremu` (`.vv .vx`) |

**Masks are one element each, not packed bits.** Element `i`'s mask is **bit 0 of element `i`** of
the mask register (RVV 0.7's layout at `MLEN = SEW`, not RVV 1.0's packed bits), so the lane that holds
an element holds its mask. A compare or mask instruction writes each active element whole, 0 or 1. A
trailing `v0.t` masks an instruction by `v0`'s elements. A mask written at one `SEW` and read at the
other is unspecified.

**Choices RVV leaves open.** Tail elements (`i >= vl`) and masked-off elements are **left undisturbed**
(legal under `ta`/`ma` too). `vstart` is always 0.

**Memory.** Element width `EEW` at most `SEW` (a smaller `EEW` is a fractional `EMUL`, legal). Every
access aligned and inside data memory, else status `ADDR`; a `vle` / `vse` base address must also be a
multiple of `4 · L` bytes (element `i` is in lane `i mod L`'s bank). Scalar `lh lhu lw sh sw` are a
build option (`scalar_mem`). `vluxei` (indexed) may only read the **LUT
region**, the top `4 · lut_entries` bytes of data memory: the one per-lane address in the machine, for
tables. Strided and indexed accesses that hit one memory bank twice in a group cost the extra cycles.

**Absent.** Widening and narrowing (`vwmul`, `vnclip`, ...), `LMUL` other than 1, floating point,
slides and gathers (`vslide*`, `vrgather`), `vid`, segment counts above 2, ordered indexed accesses.

## The `wf.` complex instructions

At `SEW` 32 an element can be a **complex 16-bit pair**: `re` in bits 15:0, `im` in bits 31:16. The
complex instructions read and write elements so (another `SEW` is illegal). Encoding: `custom-0`
(`0x0B`) in the OP-V field layout `funct6 vm vs2 vs1 funct3 vd`, `funct3` `000` for `.vv`, `100` for
`.vx` (`rs1` read as a complex pair).

| `funct6` | Instruction | Each element |
|---|---|---|
| 0 | `wf.vcmul.vv vd, vs2, vs1` / `.vx vd, vs2, rs1` | `vd = rs(vs2 · vs1)` |
| 1 | `wf.vcmulc.vv vd, vs2, vs1` | `vd = rs(conj(vs2) · vs1)` |
| 2 | `wf.vcmacc.vv vd, vs2, vs1` / `.vx` | `vd = sat16(vd + rs(vs2 · vs1))` |
| 3 | `wf.vcmadd.vv vd, vs2, vs1` / `.vx` | `vd = sat16(rs(vd · vs1) + vs2)` |
| 4 | `wf.vcdotr.vv vd, vs2, vs1` | `vd = rs32(Re(conj(vs2) · vs1))`, a 32-bit real |
| 5 | `wf.vcconj.v vd, vs2` | `vd = conj(vs2)` (`-(-32768)` saturates) |
| 6 | `wf.vcdotra.vv vd, vs2, vs1` | `vd = sat32(vd + rs32(Re(conj(vs2) · vs1)))` |

`wf.vcabs2.v vd, vs` is the pseudo-instruction `wf.vcdotr.vv vd, vs, vs`: `|z|²`.

The products are exact (`re · re − im · im` before any rounding). Then, per part:

- `rs(p) = sat16(roundoff(p, csh))`, `rs32(p) = sat32(roundoff(p, rsh))`;
- `roundoff` is RVV's: a right shift rounded by `vxrm` (0 round half up, 1 to even, 2 down, 3 to odd);
- `csh` and `rsh` are fields of the CSR **`wfsh`** (`0x800`): `csh` bits 4:0 (reset 15: Q15),
  `rsh` bits 12:8 (reset 0);
- saturation sets `vxsat`.

A real scalar `a` is the complex `.vx` operand `a + 0j`: `rs1 = a & 0xFFFF` for `a >= -32768`.

## The `wf.` queue instructions

Encoding `custom-1` (`0x2B`), I-type, the window index in `imm`:

| Instruction | Meaning |
|---|---|
| `wf.qpop q` | input window `q` becomes the next block from its loader: column `j` of the `k x n` block in register `base + j`, row `i` its element `i`; rows `>= k` zero |
| `wf.qpush q` | output window `q`'s rows `0 .. vl-1` go to its storer as a block; with two copies, the registers are unspecified until written again; with one, they keep their values |

Windows are build parameters (`Window(name, base, n, direction, sew, port, copies)`): which
registers, which stream port, one or two copies. See [Programming](programming.md#windows).

## CSRs

| CSR | Address | Access |
|---|---|---|
| `vstart` | `0x008` | 0 only |
| `vxsat`, `vxrm`, `vcsr` | `0x009`, `0x00A`, `0x00F` | read / write |
| `cycle` | `0xC00` | read: the job's cycle count (the cycle model's) |
| `vl`, `vtype`, `vlenb` | `0xC20`-`0xC22` | read only |
| `wfsh` | `0x800` | read / write: the `wf.` shifts |

## The job

A job is a program, up to eight arguments in `a0 .. a7`, an instruction budget, and the blocks its
`wf.qpop`s take. It ends at `ecall`. Errors are fatal: `ILLEGAL`, `ADDR`, `BUDGET` (the budget spent),
`PC` (a fetch outside the program); the core answers that job's status and takes no more.
