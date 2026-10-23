# Plan: a general vector unit (RVV-style, decoupled), with CG as its first program

**Status:** In progress: Stages 0-5 done 2026-10-22 (drafted 2026-10-20 with the user).
**Complexity:** L3. A new package (`waveflow/vecunit/`): an ISA, an assembler, a bit-exact interpreter,
a pysim module of four tasks, their HLS bodies, a harness and a calibration. Plus two examples that
program the unit: `examples/vecunit_cg/` (CG) and `examples/vecunit_mf/` (a multi-antenna matched
filter, for a second student). Nothing irreversible: `CgVectorCore` / `CgVectorUnit` stay as they are.
**Plan file:** `plans/vecunit_new.md` · **Lessons:** `plans/vecunit_new_lessons.md` (create it at the
first lesson).
**Replaces:** the untracked pysilicon-era stub `examples/vecunit/vecunit.py` (it imports modules
that no longer exist and has never run). Delete it when Stage 2 starts.

> **For whoever executes this plan:** read *Rules for the doer* first. Paths are repo-relative. The
> plan separates what is known (with its evidence) from what is suspected. Never promote a suspicion
> to a fact without the measurement that settles it.

## Rules for the doer

1. Work on a branch `vecunit` from `main`, in a separate worktree if another agent shares the
   checkout. In a worktree, set `PYTHONPATH` to the worktree root, because the venv's editable install
   maps to the main checkout.
2. One stage at a time. A stage is done when its gate passes by the stated command; paste the command
   and its result into the stage's record in this file. **Pause for review at the end of every stage
   and on any surprise.**
3. Root-cause failures from the error, the logs and the traces. Test at most two hypotheses, then stop
   and report.
4. **A measured number names its tool version** (Vitis 2024.1 and 2025.1 do not reproduce each
   other). Never mix versions in one table without the version on each row. Count cycles and events on
   the development laptop; take wall-clock times only on the lab machine.
5. **Pre-register before measuring** wherever a fit is scored (Stage 7): commit the fit and held-out
   configurations before any run, and score the held-out set once.
6. HLS bodies follow `docs/guide/vectorization/hls/loop_optimization.md`: a straight-line loop per
   message, and never pack or unpack a word by hand (use the generated `<elem>_array_utils` / `wf_lanes`
   routines).
7. Protocol: `docs/guide/systems/command_response.md` (a `tx_id` per job, a `Status` per job on its
   own stream; an error is fatal). Module logic is stream-only (`docs/guide/patterns/stream_only.md`).
8. Before building a tool the plan did not foresee, search the KB (`waveflow kb search ...`) and
   propose it first.
9. **The vector unit knows nothing about CG.** No CG name, format, constant or special case goes into
   `waveflow/vecunit/`. If the CG program needs something the ISA lacks, add a *general* instruction,
   record why in the decisions section, and add a non-CG test that uses it.
10. Never merge or push unless asked. One PR, many commits.

## Goal

Generalize the CG vector core (`waveflow/linalg/cg_vector.py`, body `waveflow/build/cg_vector_task.h`)
into a **programmable vector unit** that a computer-architecture reader recognizes at once. Then run CG
on it as a *program*, bit-exact with the existing golden (`waveflow/linalg/cg.py`) and close to the
current core's cycle count. Run a second, unrelated application on the same block, a multi-antenna
matched filter, so that one block is reused rather than a new kernel being written per application.

**Success criteria:**

1. **Generality.** The unit's package contains no application code. Two unrelated applications run on
   the same unit, each as a program: CG (iterative, state-heavy, short vectors) and the matched filter
   (streaming, I/O-bound, long vectors). Between them they cover every instruction form in the ISA.
2. **Exactness.** CG on the unit equals `cg.cg_solve` bit for bit: the interpreter first, then pysim,
   then RTL.
3. **Cost of programmability, measured.** Cycles a job and resources against `CgVectorCore` at the
   same configuration (`Kmax 8, Nmax 32, L 4`), same tool version, same machine. Target: cycles within
   about 10% at the same `L`. Resources are reported, not targeted. The overhead is a result for the
   paper. For the matched filter, which is I/O-bound, the measure is the frame time against its input
   floor (input words per frame at one word a cycle).

## The framing (what the paper will say)

The paper targets a computer-architecture venue, so we do **not** claim a new kind of processing unit.
The claim is:

> A vector unit implementing an RVV-style subset (register groups, element-wise fixed-point
> arithmetic, VL), executed **member-per-lane**, extended with **per-member reductions and scalars**,
> **statically chained instruction groups**, and **per-register fixed-point formats**. It is organized
> as a **decoupled access/execute** machine: load, execute and store run as three programs connected by
> architectural queues of double-buffered blocks.

Every piece of that has prior art (references at the end). The paper's contribution is not the unit
itself: it is that the unit, its bit-exact model, its timed model, its RTL and its calibrated cost model
all come from one Python specification, so its design space can be explored cheaply and trusted. The
unit is the case study, and a reviewer should recognize it at a glance.

### The vocabulary, mapped

| CompArch term | Here | In the current CG core |
|---|---|---|
| vector register `v_j`, length VL | a column of VL ≤ `VLMAX` elements | `pr[*][g][l]`, one column of `P` |
| register group (RVV LMUL) | `L` aligned registers named together; every vector instruction names groups | the `L` columns of a column group `g` |
| lanes | one lane per group member: member `l` runs on lane `l` | the `l` loop (`UNROLL`) |
| VL / `vsetvl` | `wf.setvl k` | `k` (rows) |
| strip-mining | the program walks the column groups | the `g` loop |
| chaining (Cray-1) | a chained group of instructions runs as one pass: one pipelined loop over `i` at II=1 | `PASS1`, `PASS2`, `PASS3` |
| decoupled access/execute (Smith) | load / execute / store tasks; queue reads stall when empty | `CgVectorLoad` / `CgVectorCore` / `CgVectorStore` |
| architectural queue | a stream-of-blocks channel (ping-pong) | `b_blk`, `s_blk`, `p_blk`, `x_blk` |

**Why member-per-lane is legal RVV.** RVV defines element-wise operations on a register group without
fixing which element executes when or on which lane. Running "member `l` on lane `l`, elements over
time" is a microarchitecture for the same semantics. Only the operations that are **not** element-wise
need extensions: reductions and scalar operands that differ per member.

**Why queues, not VTA-style tokens.** VTA synchronizes load/compute/store through explicit dependency
tokens because its buffers are shared, addressed SRAMs. Waveflow tasks share no addressable memory,
only FIFO-ordered blocks (the decision recorded in `plans/systolic_characterization.md`, Part B,
"ordered, banked stream-of-blocks"). So a queue block's lock *is* the token: execute stalls on an empty
input queue and on a full output queue, which is Smith's architectural queues. No token FIFOs are
needed.

## Architecture

```
            ┌──────── load program ────────┐   ┌─ execute program ─┐   ┌──── store program ────┐
 in ports ─▶│ VecLoad: stream → block       │──▶│ VecExec:          │──▶│ VecStore: block →     │─▶ out ports
            │ (unpack with the lane utils)  │ q │ register file,    │ q │ stream (header+payload)│
            └───────────────────────────────┘ in│ scalar file,      │out└───────────────────────┘
                                                │ chained passes    │
                                                └───────────────────┘──▶ status (tx_id, Status)
```

All four modules are `FreeRunMod` tasks inside a composite `VecUnit(FreeRunMod)`, wired like
`CgVectorUnit` (`StreamIF` for streams, `StreamOfBlocksIF` for queues). Each of load, execute and store
takes its own **program stream**: three programs, as the user asked. A single stream split by a
dispatcher is the VTA alternative, but it can deadlock: the dispatcher blocks on one task's full
instruction FIFO while another task waits for an instruction queued behind it. D3 records the choice.

### Architectural state (all sizes are build-time `HwParam`s)

- **Vector register file:** `NG` groups × `L` members × `VLMAX` complex elements. **Each group has its
  own fixed-point format**, fixed at build time (an extension; see the ISA). Physically, one memory per
  (group, lane) as the starting point: the student's core partitions the same way. Banking groups to
  save muxes is D2.
- **Scalar file:** `NS` per-member scalars `s_j[l]`, each with its own format.
- **Queues:** `NQI` input and `NQO` output queues. A queue block holds one `Kmax × Nmax` matrix in the
  row-major lane-group layout of `waveflow/linalg/lanes.py` (the systolic engine's and the CG core's
  layout), with the queue's own format. Depth 2 (ping-pong).
- **No architectural accumulators.** A reduction is exact inside its pass and quantizes once into its
  scalar destination, exactly where `cg.py` quantizes.

### Formats

`cg.py` is bit-exact because every product and sum is exact until a register is written, and each
register rounds to its own format with its own rounding and saturation. The unit keeps that rule:
operands are converted on read (a constant shift and sign extension per source register, which is
wiring), the slot computes exactly in the union format, and the result is **quantized to the
destination's format** on write. With few distinct formats `F` (CG: 5 vector, 4 scalar), that means `F`
constant quantizers per slot and lane, followed by a mux on the destination's format id. D4 decides
this, and measures it in Stage 1.

**The RVV analogue:** widening `vwmacc` (exact accumulation) and `vnclip` (rounding and saturating
narrowing, under `vxrm`). Stage 2 must state how `fixputils.quantize`'s rounding and overflow modes
correspond to `vnclip`'s, and name every difference. That statement goes into the paper as is.

## The ISA (provisional; D1 fixes it)

The notation follows the RISC-V V 1.0 assembly syntax. Standard mnemonics keep their RVV meaning,
applied to groups. Extensions use the `wf.` prefix, following RISC-V's custom-extension convention
(vendor prefixes such as SiFive's `sf.` and T-Head's `th.`); call the extension `Xwfvec`. Semantics are
per member `l < L` and element `i < VL`; `q_d` quantizes to the format of `d`.

### Execute program

| Instruction | Semantics | Standard or extension |
|---|---|---|
| `wf.setvl k` | `VL = k` (≤ `VLMAX`) | `vsetvli`, without SEW/LMUL (fixed at build) |
| `vmv.v.i vd, 0` | `vd[l][i] = 0` | RVV |
| `vmv.v.v vd, vs` | `vd = q_vd(vs)` | RVV, plus format conversion |
| `vadd.vv / vsub.vv vd, vs1, vs2` | `vd = q_vd(vs1 ± vs2)` | RVV |
| `vmul.vv vd, vs1, vs2` | `vd = q_vd(vs1 · vs2)`, complex | RVV with complex elements (ext.) |
| `wf.vmulc.vv vd, vs1, vs2` | `vd = q_vd(conj(vs1) · vs2)` | **conjugate operand** (see *Operand forms*) |
| `wf.vmul.vb vd, vs1, vs2` | `vd[l][i] = q_vd(vs1[0][i] · vs2[l][i])` | **member broadcast** (see *Operand forms*) |
| `vmacc.vv vd, vs1, vs2` | `vd = q_vd(vd + vs1 · vs2)` | RVV |
| `wf.vmacc.vs vd, s, vs2` | `vd = q_vd(vd + s[l] · vs2)` | `vmacc.vx` with a **per-member scalar** |
| `wf.vnmsac.vs vd, s, vs2` | `vd = q_vd(vd − s[l] · vs2)` | `vnmsac.vx`, per-member scalar |
| `wf.vmadd.vs vd, s, vs2` | `vd = q_vd(vd · s[l] + vs2)` | `vmadd.vx`, per-member scalar |
| `wf.vdotr sd, vs1, vs2` | `sd[l] = q_sd(Σ_i Re(conj(vs1[l][i]) · vs2[l][i]))`, exact sum | **per-member reduction** (cf. `vredsum` + `vwmacc`) |
| `wf.sdivz sd, sa, sb` | `sd[l] = sb[l] == 0 ? 0 : q_sd(w(sa[l]) / sb[l])` | scalar; the zero guard and the widening `w` are `fixputils.div`'s |
| `wf.smv sd, sa` | `sd = q_sd(sa)` | scalar move |
| `wf.vlse vd, q, g` | `vd = q_vd(column group g of q's head block)`: a strided read, stride `n/L` lane groups | `vlse` from a queue (DAE queue read) |
| `wf.vle1 vd, q` | `vd[0] = q_vd(the single column of q's head block)`: member 0 only | load of a vector to be broadcast (the `.vb` source) |
| `wf.qpop q` | release `q`'s head block (the next `vlse` waits for the next block) | DAE |
| `wf.vsse q, vs, g` | write `vs` to column group `g` of `q`'s tail block | `vsse` to a queue |
| `wf.qpush q, k, n` | commit `q`'s tail block (`k × n`) | DAE |

**Operand forms.** These are the two forms the matched filter needs, which CG does not exercise. Both
are general, and both apply to every multiplying instruction (`vmul`, `vmacc`, `vnmsac`, `vmadd`), not
only to the rows shown:

- **Conjugate** (`c` after the mnemonic: `wf.vmulc`, `wf.vmaccc`, ...): `conj(vs1)` in place of `vs1`.
  It costs a negation of the imaginary part, folded into the multiplier. `wf.vdotr` already
  conjugates its first operand.
- **Member broadcast** (`.vb` in place of `.vv`): `vs1`'s member 0 serves every lane. It is the dual of
  the per-member scalar: `s[l]` is constant over `i` and varies over `l`, while a broadcast vector
  varies over `i` and is constant over `l`. Use it when one reference vector meets every column: a
  matched filter, beamforming weights per subcarrier, an equalizer. Without it, a program must copy the
  vector into all `L` members, which costs `L×` the registers and the load bandwidth. Hardware: a
  fan-out of member 0's read port to all lanes, with no mux.

**Chaining.** An instruction with the `.c` suffix chains into the next. A chained group of at most `S`
instructions (`S` = slots, a build parameter, 3 for CG) runs as **one pass**: one pipelined loop over
`i < VL` at II=1, in which a later slot sees an earlier slot's result for the same element, **after its
quantization** (that is what keeps chaining bit-exact). Two slots of one pass may not write the same
register. A reduction delivers at the end of the pass. Paper phrasing: *statically chained instruction
groups, executed as one pipelined pass* (cf. Cray-1 chaining, Snitch's FREP hardware loop).

**Not in v1:** masks (`v0.t`), strided or indexed access to DRAM, branches. No branches is the same
choice VTA makes: the program builder unrolls loops, and a CG job of `nit ≤ 8` iterations is a few
hundred instructions.

### Load and store programs

| Instruction | Semantics |
|---|---|
| `wf.ld q, port, k, n` | read one `k × n` matrix from input stream `port` (framed, `q`'s format), unpack it with the lane utilities, commit it as the next block of input queue `q` |
| `wf.st port, q, hdr` | take the next block of output queue `q`, write a reply header (from `hdr`: `tx_id`, tag) and the payload on output stream `port` |

Each of the three programs starts with a `Job(tx_id)` word. The execute task emits one
`Status(tx_id)` per job on its own stream.

### Encoding

One 64-bit word per instruction, as a `DataUnion` (`waveflow/hw/dataunion.py`) of instruction formats.
The Python assembler (`waveflow/vecunit/asm.py`) builds three programs from instruction objects,
checks them (aligned groups, `VL ≤ VLMAX`, the chaining rules, slot capabilities, queue balance
between programs), and serializes them. Encode the opcodes in RISC-V custom-opcode style so the
paper's encoding table looks familiar. Bit compatibility with RVV is **not** a goal.

## CG as a program (lives in `examples/vecunit_cg/`, not in the unit)

`examples/vecunit_cg/cg_program.py` builds a `VecUnitConfig` from a `CgFormats`: group formats for
`X, R, P, S` per column group, scalar formats for `ps, rz, alpha, beta`, queue formats `B, S` (in) and
`P, X` (out). It emits the three programs for a job `(nit, k, n)`. Register allocation per column group
`g` (`ng = n/L` groups): `X_g, R_g, P_g, S_g`; scalars `ps_g, al_g, be_g, rz_g` and `rz2_g`.

```
# execute program, one job
wf.setvl k
# start, per g: X = 0, R = q_R(B), P = q_P(R), rz = q_rz(Σ|R|²), P0 out
for g:  vmv.v.i X_g, 0
for g:  wf.vlse.c R_g, qB, g ; vmv.v.v.c P_g, R_g ; wf.vdotr rz_g, R_g, R_g
        wf.qpop qB
for g:  wf.vsse qP, P_g, g
        wf.qpush qP, k, n
# iterations it = 1..nit
for g:
    wf.vlse.c      S_g, qS, g          # pass 1: S in, ps = q_ps(Σ Re(conj P · S))
    wf.vdotr       ps_g, P_g, S_g
    wf.sdivz       al_g, rz_g, ps_g    # alpha (0 if ps == 0)
    wf.vmacc.vs.c  X_g, al_g, P_g      # pass 2: X = q_X(X + P·alpha)
    wf.vnmsac.vs.c R_g, al_g, S_g      #         R = q_R(R − S·alpha)
    wf.vdotr       rz2_g, R_g, R_g     #         rz' = q_rz(Σ|R|²) on the new R (chained)
    wf.sdivz       be_g, rz2_g, rz_g   # beta (0 if rz == 0)
    wf.vmadd.vs.c  P_g, be_g, R_g      # pass 3: P = q_P(P·beta + R)
    wf.vsse        qP, P_g, g          #         (after the last iteration: vsse qX, X_g, g)
wf.qpop qS ; wf.qpush qP (or qX), k, n
```

Instead of `wf.smv rz, rz2`, the builder swaps the names `rz_g` and `rz2_g` every iteration (register
renaming in the program, not in hardware). The order above is `cg.vec_step`'s, steps 2–9. It gives
the same three passes per group and iteration as `cg_vector_task.h`, and the same two divides.

The load program is `wf.ld qB` then `nit × wf.ld qS`. The store program is `nit × wf.st` from `qP`,
then one from `qX`. These mirror `CgVectorLoad` and `CgVectorStore`, with the CG knowledge moved out of
the HLS into the program builder. `examples/vecunit_cg/` also holds the harness that plays the systolic
array's part (`S = cg.mm_step(A, P)` on each `P` the unit emits), so the example runs a whole CG solve
against `cg.cg_solve`.

## The matched filter as a program (lives in `examples/vecunit_mf/`, not in the unit)

A multi-antenna matched filter in the frequency domain:
`Y[i, j] = conj(X[i]) · R[i, j]`, for `i` over the samples (subcarriers) of a frame and `j < nrx` over
the receive antennas.

| Matched filter | Unit |
|---|---|
| antenna `j` | group member, one lane per antenna; `nrx > L` gives `nrx/L` groups |
| sample `i` | element; a chunk of `VL ≤ VLMAX` samples at a time, strip-mined over the frame |
| `R` (samples × antennas) | an input-queue block, `k` = chunk, `n` = `nrx`: the row-major lane-group layout CG uses for `B` and `S` |
| `X` (samples) | a single-column input-queue block, loaded into member 0 with `wf.vle1` |
| `Y` | an output-queue block, same layout as `R` |

```
# execute program, per chunk of k samples
wf.setvl k
wf.vle1 X0, qX ; wf.qpop qX                    # the reference, member 0 only, reused for every group
for g:  wf.vlse.c R_g, qR, g                   # one chained pass per group:
        wf.vmulc.vb.c Y_g, X0, R_g             #   Y = conj(X) · R, X broadcast to all lanes
        wf.vsse qY, Y_g, g                     #   straight to the output queue
wf.qpop qR ; wf.qpush qY, k, nrx
```

The load program alternates `wf.ld qX` and `wf.ld qR` per chunk. The store program is one `wf.st` per
chunk. `X0` is loaded once per chunk and reused by every group; that, and the reuse of the unit itself,
is the point of the example.

**What it exercises that CG does not:** the conjugate and broadcast operand forms, long vectors
(`VLMAX` 64–256 against CG's 8, so the same RTL template with other `HwParam`s), and a streaming
program whose rate is set by I/O, not by compute. **Expectation, to be measured:** the frame time
sits near the input floor. The gap is the cost of programmability for a streaming kernel; compare it
with a dedicated kernel if one is written.

**Its golden** is a few lines of NumPy on stored integers, `fixputils.quantize(conj(X)[:, None] * R)`
to `Y`'s format, in `examples/vecunit_mf/mf_golden.py`. The example also states the instruction count
a frame (about `1 + 3·nrx/L` per chunk), so that `VLMAX` is chosen large enough that instruction
issue never binds.

## Stages

### Stage 0: decisions (a page of records, reviewed before any code)

Write each as a record in this file: the choice, the alternatives, and the evidence or the reason.

- **D1, the ISA.** Confirm or amend the table above, including the conjugate and broadcast operand
  forms (which instructions take them, and how they encode). Decide real vs complex elements:
  complex-only, or a build parameter. Decide the slot count `S` and **which ops each slot supports** (a
  build parameter and a DSE knob: a slot that cannot multiply costs no DSP).
- **D2, the register file.** One memory per (group, lane), as the CG core does, against `NBANK` banks
  per lane with a bank-conflict rule checked by the assembler (the standard banked-VRF scheme, as in
  Ara). Count the read and write ports a pass needs: CG's pass 2 reads `X, P, R, S` and writes `X, R`.
  Each memory serves one read and one write a cycle (T2P).
- **D3, program delivery.** Three program streams (recommended), against one stream and a dispatcher.
  Record the dispatcher's deadlock case if you reject it.
- **D4, formats.** Per-group formats with per-slot constant quantizers and a destination mux
  (recommended), against one shared format per register class. State the `vnclip` correspondence
  (above).
- **D5, queue blocks.** Whole-matrix blocks with `vlse`/`vsse` by column group (recommended: it matches
  the systolic engine's and the CG core's layout, so the units connect without a reorder), against
  per-group blocks. Also decide the **single-column block** for `wf.vle1`: the lane-group layout assumes
  `L | n` (`cg_vector.cmd_status`), which a `k × 1` vector breaks. Options: a vector queue with its own
  layout (values packed along `i`), or a `k × L` block of which only column 0 is meaningful. Record the
  load bandwidth each costs.
- **D6, one firing = one job.** The register file is local to the execute firing, and a job's
  instruction loop runs inside the firing. Carrying state across firings with `static` ran csynth out
  of memory in the systolic engine (6.7 GB, `plans/systolic_characterization.md`, Part B record), and so
  did a loop over commands in one firing (7.0 GB). Stage 1 checks whether an instruction loop over a
  small register file escapes that.

**Gate:** the records written and approved. **Pause.**

#### Record: Stage 0 decisions (2026-10-22, branch `vecunit`)

Machine: Linux, Vitis HLS / Vivado **2024.1** (the version every `CgVectorCore` number on
`docs/guide/dsp/cg_vector.md` was measured with, so Stage 5's comparison needs no re-measurement of
the core's published figures; the systolic engine's numbers are 2025.1 and are not reused).

**Facts found while writing these (each read in the code):**

- **A `DataUnion` is at least two words.** Its payload starts on a fresh word after the header
  (`waveflow/hw/dataunion.py:588-589`, docstring `:413-416`), so "one 64-bit word per instruction, as a
  `DataUnion`" cannot hold as the class is written. A `DataList` packs fields LSB-first and moves a
  field that does not fit to the next word, never splitting one (`waveflow/hw/dataschema.py:1508-1540`),
  so a `DataList` of fields totalling 64 bits is exactly one word.
- **The CG core's register file is already banked.** `xr`, `rr`, `pr` (`waveflow/build/cg_vector_task.h:86-96`)
  are one memory per (array, lane), with the column group `g` in the *address*. In the plan's terms that
  is four banks per lane (`X`, `R`, `P`, and `S` read from the block), a group's bank fixed by its
  class: not one memory per (group, lane).
- **The divides dominate an iteration today**, in the calibrated model: at the centre configuration a
  `STEP` costs the core 48 + 280 + 48 + **765** = 1,141 cycles, the last term the divides (`groups_div`,
  `waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1/components/cg_vector_unit/params.json`):
  about 48 cycles a divide, not the uncalibrated `_DIV_LATENCY = 40`. Stage 6's hypothesis stands.
- **Vitis 2024.1's `stream_of_blocks` has explicit acquire and release** (`hls::read_buf` /
  `hls::write_buf`, `acquire()` / `release()`, `/tools/Xilinx/Vitis_HLS/2024.1/include/hls_streamofblocks.h:179-225`)
  beside the scoped `read_lock` / `write_lock`. Whether they synthesize under run-time control (a
  `wf.qpop` decoded from an instruction) is not known: Stage 1 adds it as witness 5.
- **Bit-exactness with union formats.** Every exact sum and product has the same value whatever
  exact format holds it, and `fixputils.quantize` depends only on the value and the target
  (`waveflow/utils/fixputils.py:135-158`), so a slot may compute in one union format and still equal
  the interpreter, which computes in each register's own format. **Division is the exception:**
  `fixputils.div` truncates toward zero at the *dividend's* fraction bits (`:262-293`), so the
  hardware divides at the dividend's own fraction bits (a constant shift per scalar format, then a
  mux), not at the union's.

**D1, the ISA.** *Proposed:* the table above, with these changes.
- **Encoding: one 64-bit `DataList`**, not a `DataUnion` (two words): RISC-V-style fields, LSB first:
  `opcode` 7 (a custom opcode per class: vector, scalar, queue, control), `funct` 6, `chain` 1,
  `conj` 1, `bcast` 1, `rd` 8, `rs1` 8, `rs2` 8, `imm1` 12, `imm2` 12. The opcode and `funct` say
  what the other fields mean, as in RISC-V. Each program is one framed burst: a `VecJob(tx_id,
  ninstr)` word, then `ninstr` instruction words.
- **Elements: complex only** (both applications are complex). Scalars are real (a reduction yields
  `Re(...)`; `alpha`, `beta` are real). Real elements: a later build parameter.
- **Slots: `S = 3`, uniform**: every slot can do every vector operation (a complex multiplier per
  lane, the reduction adder, a queue read or write). A capability mask per slot is a build parameter
  from the start (a DSE knob), default all. 3 slots × 4 lanes × 4 multipliers = 48 DSP at the centre,
  about the CG core's 49.
- **`wf.vle1` dropped** if D5 takes option (a): a `k × 1` vector is then an ordinary block, read with
  `wf.vlse vd, q, 0`, and `.vb` reads member 0.
- **Rules:** a slot that reads a register an earlier slot of its pass wrote gets the forwarded value
  after its quantization; a reduction's result is not readable inside its own pass; scalar operands are
  read once at the pass's start; two slots of a pass may not write one register.

**D2, the register file.** *Proposed:* `NBANK` banks per lane, a group's bank `= group mod NBANK`,
address `(group div NBANK)·VLMAX + i`; the assembler refuses a pass that reads two different groups
of one bank, or writes two groups of one bank (each bank memory serves one read and one write a cycle,
T2P). The alternative, one memory per (group, lane), needs an `NG:1` mux per operand. Every slot
reads its operands at the same element `i`, so a group read once serves every slot that names it.
CG's pass 2 reads `X, P, R, S` and writes `X, R`: four banks, conflict-free when `X_g, R_g, P_g, S_g`
are groups `4g + 0..3`, which is the CG core's layout. Stage 1 witness 1 measures both forms (mux cost
at `NG` 8, 16, 32) before this is final.

**D3, program delivery.** *Proposed:* three program streams, one per task. **Rejected:** one stream
and a dispatcher. The deadlock: the dispatcher forwards in program order; the load task stops
because its output queue is full (execute has not popped); its instruction FIFO fills; the
dispatcher blocks writing the next load instruction, while the execute instruction that would pop
the queue sits behind it in the single stream. Every task waits on another, unless each instruction
FIFO holds a whole program.

**D4, formats.** *Proposed:* a format per register group, per scalar and per queue, fixed at build
time. Operands convert on read to a union format (one per class: vector, scalar), a constant shift
and sign extension per source format then a mux on the source's format id. A slot computes exactly in
the union format, and quantizes to its destination with one constant quantizer per destination format
and a mux on the destination's format id. Divides take the dividend's own fraction bits (above). One
shared format per register class is the rejected alternative (it cannot reproduce `cg.py`, whose
registers have five formats).

*The `vnclip` correspondence* (from the RISC-V V 1.0 specification, §§ 3.8 and 12.5): `vnclip`
narrows a `2·SEW` source to `SEW` bits by a right shift, rounds under `vxrm`, and saturates (setting
`vxsat`). `fixputils.quantize` matches it as follows:
- `AP_RND` (round half up) is `vxrm = rnu`, formula for formula: both add the bit just below the cut
  (`floor + ((s >> (dF-1)) & 1)`, `waveflow/utils/fixputils.py:154`).
- `AP_TRN` (floor) is `vxrm = rdn`.
- `AP_SAT` is `vnclip`'s signed clip.
- `AP_WRAP` has no clipping instruction; with `AP_TRN` it is the narrowing shift `vnsra`, which
  floors and wraps. `AP_RND` with `AP_WRAP` is no single RVV instruction.

Differences to name in the paper: `vnclip` narrows only from `2·SEW` to `SEW`, where `quantize` goes
from any width to any `W ≤ 64`; its shift is a run-time operand, here the two formats fix it; it
shifts only right, while `quantize` also shifts left (exactly) when the target keeps more fraction
bits; it saturates at `SEW` bits, `AP_SAT` at `W`; and RVV's `rne` and `rod` have no `fixputils` mode.

**D5, queue blocks.** *Proposed:* whole-matrix blocks in the row-major lane-group layout of
`waveflow/linalg/lanes.py`, read by `vlse` and written by `vsse` per column group. For the
single-column block (the matched filter's `X`), three options:
- **(a) relax `L | n`** (proposed): a row holds `ceil(n/L)` lane groups, the last one zero-padded.
  A `k × 1` vector is an ordinary block whose group 0 holds the value in lane 0; `wf.vlse vd, q, 0`
  reads it, and `.vb` uses member 0. The wire carries `k` values (`k/2` words at 64 bits); the
  loader writes a lane group a cycle, so `k` cycles. It also lets `nrx` not divide by `L`.
- (b) a vector queue with its own layout (values packed along `i`, `L` a group): `k/2` words on
  the wire, `k/L` cycles to load, but a second block layout and a `wf.vle1` that reads it.
- (c) a `k × L` block with only column 0 meaningful, as sent: `L×` the input words (`kL/2`).

**D6, one firing = one job.** *Proposed as in the plan:* the register and scalar files are locals of
the execute firing, and the job's instruction loop runs inside it. Stage 1 witness 1 checks that an
instruction loop over a small register file escapes the 6.7 GB / 7.0 GB csynth failures.

**Protocol (rule 7):** each program's `VecJob(tx_id, ninstr)` word; the execute task writes one
`Status(tx_id, code)` per job on `m_status` (codes from `waveflow/linalg/message.py`: `BAD_OP` for an
unknown instruction, `BAD_DIMS` for a register, `VL`, `k` or `n` out of range); an error is fatal.
`wf.st` heads each output with `VecResp(tx_id, tag, k, n)`. The data bursts on the input ports carry
no header: the load program gives each one's size.

**Approved (user, 2026-10-22):** D1 as proposed (one 64-bit `DataList` word; complex elements; `S = 3`
uniform slots with a capability-mask parameter); D5 option (a), relax `L | n` and drop `wf.vle1`;
D3, D4, D6 as proposed; D2 provisional until witness 1. The user also asked to go on through the
stages without the per-stage pause, stopping only for a decision that is theirs, a surprise that
changes the design, or a failure not root-caused in two hypotheses.

### Stage 1: HLS witnesses (facts before design)

Small standalone csynth experiments, built under a short path such as `C:\wfsc\vec*` (long paths make
csynth fail silently on Windows):

1. An execute task with a local register file `[NG][L][VLMAX]` partitioned per D2, an instruction loop,
   and a pass loop with run-time operand selection. Report II, csynth memory, DSP/LUT, and the mux cost
   as `NG` grows (8, 16, 32).
2. `#pragma HLS DEPENDENCE variable=reg inter false` on the pass loop: II=1 without it or not.
   Element-wise passes make it safe; check that cosim or XSI agrees.
3. `S = 3` chained slots with forwarding after quantization, at II=1, at 4 ns.
4. The quantizer cost of D4 for `F` = 5 formats.
5. (Added at Stage 0.) Queue blocks acquired and released by decoded instructions:
   `hls::read_buf` / `hls::write_buf` `acquire()` / `release()` inside the instruction loop, under
   run-time control. If csynth refuses it, the lock scopes must follow the program's structure.

**Gate:** each witness's numbers recorded (tool version on each). **Pause**; the decisions may change.

#### Record: Stage 1 witnesses (2026-10-22) -- Vitis HLS / Vivado xsim 2024.1

Sources, scripts and the full table: `plans/witness/vecunit/` (`README.md`). The prototype is most of
the execute task: an instruction loop, a three-slot chained pass with run-time operand selection, a
banked register file, per-format conversions and quantizers, the guarded divide. One complex
multiplier per slot and lane serves every form (the scalar forms and the reduction reuse it): **48
DSP** at the centre, about the CG core's 49.

Gate command: `./plans/witness/vecunit/run.sh <out>`, `COSIM=1 ./run.sh <out> w2_cosim`, and
`./run_rtl5.sh <out> w5 w5b_cosim w5c_cosim w5d_cosim` -- every variant synthesized, II and resources
in the README's table; `w2_cosim` PASS; `w5*` `VERDICT_PASS` at RTL.

| witness | result (all 2024.1, xczu48dr, 4 ns) | decision |
|---|---|---|
| 1, register file | banked (4 banks): **50.9k LUT at 8, 16 and 32 groups**; a memory a group: 57k, 70k, 106k LUT, 32-128 BRAM, up to 1.69 GB | **D2: banked**, confirmed |
| 2, `DEPENDENCE` | needed: without it II 2; with it II 1 and **C/RTL co-simulation PASS** (RTL equals C on a CG-like program) | the pragma goes in the body |
| 3, chaining | `S = 3`, forwarding after quantization: **II 1 at 2.913 ns**, pass depth **22-24 cycles** | as planned |
| 4, quantizers | five formats cost 12.9k LUT over one; **one shared quantizer** (shift, round bit, bounds and wrap muxed on the format id) gives the same bits for **44.5k** (against 50.9k) | **D4 amended**: the shared quantizer |
| 5, run-time queue locks | `hls::read_buf` / `write_buf` (`EXPLICIT_ACQUIRE_RELEASE`, undocumented) under run-time control: csynth II 1, **correct at RTL** (xsim, direct testbench: 256/256, 2/2) | **D5 stands** |
| D6 | csynth peak memory 0.71-1.69 GB in every variant | one firing = one job, confirmed |

**Found:**
- **Vitis co-simulation cannot judge a free-running `hls::task` design with stream-of-blocks
  channels** (2024.1): its C test-bench phase runs the synthesized model and fails even with the
  documented scoped locks, while the RTL itself is correct. Free-running bodies are judged at RTL
  (xsim, XSI), as the repo already does.
- **A risk for success criterion 3 (cycles within about 10 %).** A pass costs about `VL + 22` cycles
  plus its decode, so at CG's `VL = 8` the fill and drain are nearly three times the elements. A
  first estimate per CG step: 8 groups × (3 passes × ~32 + 2 divides × ~48) ≈ 1,540 cycles against
  the core's calibrated 1,141, about +35 %, unless the passes overlap. Stage 5 measures it; Stage 6
  (overlap) is the plan's answer, and this makes it likely needed rather than optional.
- The undocumented lock API is a risk to re-check with every tool version.

### Stage 2: ISA, assembler, interpreter

`waveflow/vecunit/`:
- `isa.py`: the instruction `DataUnion` and its fields.
- `config.py`: `VecUnitConfig` (L, VLMAX, Kmax/Nmax of queue blocks, groups and their formats, scalars
  and their formats, queues and their formats, slots and their capabilities, `g_div`, `word_bits`,
  `lane_bits`).
- `asm.py`: the assembler and checker.
- `model.py`: the **bit-exact interpreter** of the three programs on stored integers, with
  `fixputils`. This is the golden of the unit.

Tests in `tests/vecunit/`: every instruction and every operand form (conjugate, broadcast,
per-member scalar) against a NumPy reference; **random programs** (random chained groups, formats, VL)
against an unchained execution of the same instructions. Chaining must not change a single bit.

**Gate:** `pytest tests/vecunit` passes. **Pause.**

#### Record: Stage 2 (2026-10-22)

The stub `examples/vecunit/vecunit.py` was not in this checkout: nothing to delete.

- `waveflow/vecunit/isa.py`: `VecInstr` (a 64-bit `DataList`, fields as D1), `VecJob`, `VecResp`,
  `VecStatus`; `Instr` (encode, decode, the `.c` chain, RVV-syntax disassembly); a constructor per
  instruction. `wf.vle1` is gone (D5 (a)); `wf.ld` / `wf.st` are queue-class functs of the load and
  store programs; `wf.qpop` / `wf.qpush` take or commit a block that was not yet taken, so the lock
  rule is "acquire at first use, release at pop / push".
- `config.py`: `VecUnitConfig` -- a format per group, scalar and queue; `L`, `VLMAX`, `Kmax`, `Nmax`,
  `S`, `NB`, slot capabilities (`Cap`), `g_div`, ports, word and lane widths; the union formats.
- `model.py`: the interpreter on stored integers with `fixputils`, each value in its own format; a
  pass element-major (`mode="pass"`, as the hardware) or instruction-major (`"serial"`); blocks in the
  hardware's lane-group layout (`ceil(n/L)` groups a row, spare lanes zero); the checks the execute
  task makes (`instr_status`, `pass_status`: an error is fatal, the job stops at the instruction).
- `asm.py`: `check` / `assemble` -- the hardware's checks, plus what the hardware would get wrong
  silently (unwritten registers and lane groups, a stride that is not the block's, queue imbalance).

Tests (`tests/vecunit/`, 87): every vector instruction and operand form, `wf.vdotr`, `wf.sdivz` (the
zero guard) and `wf.smv` against an **independent exact-rational reference** (`_ref.py`, no
`fixputils`), with random formats (both rounding and both overflow modes), random `VL`, in both modes;
**150 random programs** (over 300 chained instructions) run element-major and instruction-major, every
register, scalar and output block equal; forwarding after quantization; every status case and pass
rule; slot capabilities; the fatal stop; blocks with `L` not dividing `n`; the assembler's rules.

Gate: `pytest tests/vecunit -q` -> 87 passed; `ruff check waveflow/vecunit tests/vecunit` clean;
`mypy waveflow/vecunit` clean for the package.

### Stage 3: the two applications as programs, on the interpreter

**3a, the matched filter first** (the simpler program, so it shakes out the program builder and the
queue layouts before CG): `examples/vecunit_mf/mf_program.py` and a test. Random `X` and `R`; `nrx` at,
below and above `L` (one and several groups); frames that are a multiple of the chunk and frames that
are not (a short last chunk, `VL < VLMAX`). The interpreter must equal `mf_golden.py` bit for bit.

**3b, CG.**

`examples/vecunit_cg/cg_program.py` (the builder above) and a test: for random `A` (Hermitian positive
definite), `B`, formats from the `mimo_cg` study, `nit` from 1 to 8, `k` and `n` at and below the
maxima. The interpreter plus the `mm_step` harness must equal `cg.cg_solve` bit for bit, including the
zero guards (a column that freezes; `ps = 0`).

**Gate:** both applications bit-exact on all of it. **Pause.**

#### Record: Stage 3 (2026-10-22)

The interpreter now runs the three programs as the tasks would, each in its own order: the load
program when the execute program reads an empty queue, the store program after every push, and a
harness callback (`on_starve`) supplies a burst a port has run out of -- so the CG harness computes
each `S` from the `P` the unit has just emitted, a closed loop.

- **3a, the matched filter** (`examples/vecunit_mf/`: `mf_golden.py`, `mf_program.py`): a unit of four
  groups (`X`, `R`, `Y`, a spare), `X` read once a chunk into member 0 from a `k x 1` block, one
  chained pass a column group (`wf.vlse.c`, `wf.vmulc.vb.c`, `wf.vsse`). `5 + 3·ceil(nrx/L)`
  instructions a chunk. `tests/examples/test_vecunit_mf.py` (26): `nrx` 3, 4, 7, 8 at `L = 4`; frames
  of 32 (two chunks of 16), 37 (a short last chunk) and 5 (one short chunk); both pass modes;
  saturation; **all bit-exact against `mf_golden`.**
- **3b, CG** (`examples/vecunit_cg/cg_program.py`): the builder above, with `rz` / `rz2` renamed every
  iteration and, by default, the last iteration's dead work dropped (its second pass writes `X`
  straight out; no third pass). `tests/examples/test_vecunit_cg.py` (104): random Hermitian positive
  definite `A`; the `mimo_cg` study's formats `W12g8`, `W10g0`, `W16g4` and its saturation stress
  set; `nit` 1, 2, 3, 5, 8; `(k, n)` = (8, 32), (8, 13), (5, 4), (3, 32), (1, 1); element-major
  passes; the program that keeps the dead work; a zero column (both guards fire every iteration) and
  a converged `1 x 1` column that freezes. **Every emitted `P` and the final `X` equal `cg.cg_solve`'s
  bit for bit.**

Gate: `pytest tests/vecunit tests/examples/test_vecunit_mf.py tests/examples/test_vecunit_cg.py -q`
-> all passed.

### Stage 4: the pysim module and its harness

`waveflow/vecunit/unit.py`: `VecLoad`, `VecExec`, `VecStore` and the composite `VecUnit`. The execute
task's `run_iter` runs the interpreter's semantics per instruction and charges a rough time per pass
(`overhead + VL + depth`) and per divide (`latency`). Rough constants until Stage 7 calibrates them.
Use a stream-only unit harness (`docs/guide/flows/units_harness.md`, `harness(module)`, timed phases,
`workload("test")`) that drives the three programs and the input matrices, and checks the outputs
against the interpreter.

**Gate:** pysim bit-exact against the interpreter on `workload("test")`, both applications included.
**Pause.**

#### Record: Stage 4 (2026-10-22)

- `waveflow/vecunit/unit.py`: `VecLoad`, `VecExec`, `VecStore` (free-running tasks, each with its own
  framed program stream) and the composite `VecUnit`, a stream-of-blocks channel per queue (depth 2).
  Boundary: `s_lprog`, `s_in<p>`, `s_xprog`, `m_status`, `s_sprog`, `s_out<p>`. The execute task runs
  the interpreter's `Machine` per instruction, takes each block at its first `wf.vlse` / `wf.vsse`
  and gives it back at `wf.qpop` / `wf.qpush`, and writes one `VecStatus` per job; an error halts it.
  The configuration is a build field, so the harness rebuilds the unit as it is.
- `waveflow/vecunit/workload.py`: jobs `(tx_id, Programs, inputs)` as harness stimulus and as golden
  bursts (the interpreter's); `random_jobs` -- every instruction form, valid by the assembler.
- Applications supply scenarios by subclassing: `examples/vecunit_cg/cg_unit.py` (`CgVecUnit`: the
  configuration from a `CgFormats`; each job's `S` inputs captured from a closed-loop interpreter run)
  and `examples/vecunit_mf/mf_unit.py` (`MfVecUnit`).
- **Deviation:** time is charged per instruction from rough constants (`VecTiming`: a pass `S + 4 +
  VL + 22`, a divide 45), not yet through `TimedPhase`; Stage 7 calibrates them.

Tests: `tests/vecunit/test_unit.py` -- two configurations (4 lanes, 3 slots, 64-bit words; and 2
lanes, 2 slots with a capability mask, 3 input queues, 2 input and 2 output ports, 32-bit words),
random jobs, every output burst and status equal to the interpreter's; a fatal error (status
`BAD_DIMS`, what was pushed before it stored, the next job never answered). `tests/examples`: the CG
unit and the matched-filter unit on `"test"` and `"steady"`, bit-exact.

Rough pysim times (uncalibrated constants; for orientation only): CG `"steady"` (four `8 x 32`
solves of three iterations) 20,306 cycles, about 5,080 a job, against the CG core's calibrated
4,735 a job with its message I/O (`START` 475 + 3 × `STEP` 1,420, Vitis 2024.1); the matched filter
`"steady"` at `VLMAX = 16` (four frames of 64 samples, 8 antennas) 2,305 cycles against an input floor
of 1,152 words.

Gate: `pytest tests/vecunit tests/examples/test_vecunit_mf.py tests/examples/test_vecunit_cg.py` ->
all passed (225).

### Stage 5: HLS bodies, RTL

`waveflow/build/vecunit_{load,exec,store}_task.h` and the composite's generation, following
`cg_vector*_task.h` and the systolic engine bodies. csynth at 4 ns (`xczu48dr`; if the license refuses
it, use the stand-in part and say so). XSI bit-exact on `workload("test")` and on both applications.
The matched filter runs at its own build configuration (long `VLMAX`, few groups) of the same RTL
template.

**Measure, against `CgVectorCore` at the same configuration and tool version:** cycles a job (trace the
execute task's passes), DSP, LUT, FF, BRAM. Report the overhead of programmability per resource.
**For the matched filter:** cycles a frame against the input floor, and which task binds (the trace
of the load task's input port against the execute task's passes).

**Gate:** RTL bit-exact; the comparison table recorded. **Pause.**

#### Record: Stage 5 (2026-10-22) -- Vitis HLS / Vivado xsim 2024.1, xczu48dr, 4 ns, this machine

**Built.** The three task bodies are generated per configuration (`waveflow/vecunit/hls.py` ->
`wf_vecunit_<id>.h`: a `wf_vec_cfg_<id>` struct with the union and exact types, the format tables,
the shared quantizers and the dividend-format divide; `vecunit_load/exec/store_<id>`) on the
configuration-independent `waveflow/build/vecunit_core.h` (instruction and pass checks, slot decode,
operand resolution, matrix load and store between a framed burst and a block's padded rows).
`waveflow/vecunit/build.py` (the headers, a codegen step, `build_dag`: a unit's harness through the
system flow), `report.py` (cycles and resources of a run). Framework: `render_tcl(defines=...)`, empty
by default, for `-DEXPLICIT_ACQUIRE_RELEASE` (`hls_task.h` includes the stream-of-blocks header before
a body could define it). Every queue block holds `WV`-bit values (the widest vector format).

**Gate:** every RTL run bit-exact against pysim (`compare.json`: no mismatch), pysim equal to the
interpreter -- the generic unit on random jobs over every instruction form (`rtl_config`), the CG unit
on `"test"` (four solves, a zero column among them) and `"steady"`, the matched filter on `"test"` and
`"steady"`. csynth meets 4 ns: est. 2.914 ns (Fmax 343 MHz), the pass loop at II 1, depth 22.
Commands: `python -m examples.vecunit_cg.harness --root <r> --through compare [--workload steady]`,
the same for `examples.vecunit_mf`, `pytest -m xsi tests/vecunit/test_unit_xsi.py`.

**Measured against `CgVectorUnit`** (its rx, load, core and store; the same steady workload: four
`8 x 32` solves of three iterations, the same formats `W12g8`; the same tool and machine):

| | cycles a job | LUT | FF | DSP | BRAM |
|---|---|---|---|---|---|
| `CgVectorUnit` (four tasks) | **3,581** | 21,596 | 11,876 | 52 | 20 (channels) |
| ... its core, `cg_vector_task` | | 15,109 | 9,458 | 49 | 0 |
| vector unit, CG program | **7,295** (2.04x) | 59,585 | 26,847 | 50 | 28 |
| ... its execute task | | 48,933 | 24,207 | 48 | 16 |

CgVectorUnit: replies 1,129-1,131 cycles apart per `STEP`; the unit's numbers are csynth estimates.
**The 10 % cycle target is missed by Stage 5** (as Stage 1 warned); resources are 2.8x LUT, 2.3x FF,
0.96x DSP.

**The matched filter** (`VLMAX 64`, 8 antennas, four frames of four chunks): a frame every **1,448
cycles against an input floor of 1,152 words (1.26x)**; the input accepted 4,608 words in 5,628
cycles. The execute task binds (about 362 cycles a chunk against the loader's about 320).

**Where the cycles go** (csynth's schedule, the RTL run):
- the guarded divide serialized the four lane dividers -- a branch per lane, as `cg_vector_task.h`
  warns: 177 cycles a divide instead of 45. Fixed (divide by a safe divisor, then select): the first
  CG job fell from 12,413 to 7,297 cycles;
- a pass costs about `VL + 22 + 31`: its elements, the pipeline's fill and drain, and about 31 cycles
  of decode (gather, checks, operand resolution, reduction delivery); a CG job is 88 passes and 40
  divides (about 45 cycles each, the lanes side by side).

**Found:** a configuration whose formats spread widely (`centre()` in the tests: integer bits 1 to
14) builds a wide union datapath -- 28 x 21-bit multiplier inputs, 52-bit results -- and its csynth ran
past 50 minutes without finishing; the CG and matched-filter configurations take about a minute. The
union's width, not only the number of formats, is a cost of D4; the generic RTL gate uses a moderate
spread (`rtl_config`).

### Stage 6: overlap (optional; measure first)

Two optimizations the decoupling makes possible. Each is in-order issue with a scoreboard, no ISA
change:
- **scalar/vector overlap:** a group's two divides (about 40 cycles each, `_DIV_LATENCY` in
  `cg_vector.py`, uncalibrated) overlap the next group's passes. Column groups are independent CG
  problems, so this is legal. Hypothesis to measure: at `k = 8` the divides dominate an iteration.
- **contexts:** two jobs' register sets resident at once, so the unit computes on one while the
  systolic array works on the other. This needs the system flow, so it is a separate plan.

**Gate:** each measured against Stage 5's numbers, kept only if it pays. **Pause.**

### Stage 7: calibration, resource model, docs

Pre-register the configurations (rule 5). Calibrate the pass and divide costs in the harness
(`add_calib_steps`). Fit the resource model over `L`, `NG`, `S`, slot capabilities and formats. Score
the held-out set once.

Docs:
- `docs/guide/dsp/vecunit.md`: the ISA table, the chaining rule, the formats rule, the architecture,
  where timing is measured, the scores.
- `docs/examples/vecunit_cg/index.md`: CG as a program.
- `docs/examples/vecunit_mf/index.md`: the matched filter as a program, and the I/O floor.

Add `waveflow/vecunit/` to the subsystem list in `CLAUDE.md`.

**Gate:** held-out scores reported; `pytest -m "not vitis and not xsi and not gem5"` and the unit's XSI
gates pass. **Pause.**

## Out of scope

- Moving `examples/mimo_cg` onto this unit, or onto the systolic engine (a separate plan).
- The CG system with the systolic engine and contexts (Stage 6, second item).
- Masks, DRAM addressing, branches, bit-level RVV encoding compatibility.

## References (checked against OpenAlex, 2026-10-20)

| Topic | Reference |
|---|---|
| vector chaining | R. M. Russell, "The CRAY-1 computer system," *CACM*, 1978. doi:10.1145/359327.359336 |
| decoupled access/execute | J. E. Smith, "Decoupled access/execute computer architectures," ISCA 1982. doi:10.1145/1067649.801719 |
| decoupled vector | R. Espasa, M. Valero, "Decoupled vector architectures," HPCA 1996. doi:10.1109/hpca.1996.501193 |
| load/compute/store accelerator ISA | T. Moreau et al., "A Hardware–Software Blueprint for Flexible Deep Learning Specialization" (VTA), *IEEE Micro*, 2019. doi:10.1109/mm.2019.2928962 |
| decoupled, single stream | H. Genc et al., "Gemmini," DAC 2021. doi:10.1109/dac18074.2021.9586216 |
| stream-dataflow | T. Nowatzki et al., "Stream-Dataflow Acceleration," ISCA 2017. doi:10.1145/3079856.3080255 |
| N-buffered metapipelines | D. Koeplinger et al., "Spatial," PLDI 2018. doi:10.1145/3192366.3192379 |
| RVV implementation, banked VRF | M. Cavalcante et al., "Ara," *IEEE TVLSI*, 2020. doi:10.1109/tvlsi.2019.2950087 |
| hardware loop (FREP) | F. Zaruba et al., "Snitch," *IEEE TC*, 2021. doi:10.1109/tc.2020.3027900 |
| stream registers | F. Schuiki et al., "Stream Semantic Registers," *IEEE TC*, 2021. doi:10.1109/tc.2020.2987314 |
| the ISA | RISC-V "V" Vector Extension, version 1.0 (specification; not indexed by OpenAlex) |

## Log

| Date | Stage | Event | Where |
|---|---|---|---|
| 2026-10-20 | -- | Drafted with the user: RVV-style framing, DAE organization, CG as an example program | this file |
| 2026-10-20 | -- | Added the multi-antenna matched filter as the second application (a second student): conjugate and member-broadcast operand forms, `wf.vle1`, single-column blocks in D5, Stage 3a | this file |
| 2026-10-22 | 0 | Decisions D1-D6 written and approved: a 64-bit `DataList` instruction word (a `DataUnion` is two), relax `L \| n` (no `wf.vle1`), uniform slots; witness 5 added (run-time queue locks) | Stage 0 record |
| 2026-10-22 | 1 | Witnesses (2024.1): banked RF flat at 50.9k LUT; `DEPENDENCE` needed, cosim PASS; II 1 at 2.913 ns, pass depth 22-24; shared quantizer 44.5k; run-time SoB locks correct at RTL (cosim cannot judge them). D4 amended; pass depth a risk to the 10 % target | Stage 1 record, `plans/witness/vecunit/` |
| 2026-10-22 | 2 | `waveflow/vecunit/` (isa, config, asm, model) and 87 tests: every instruction against an exact-rational reference, 150 random programs chained = serial | Stage 2 record |
| 2026-10-22 | 3 | The matched filter (26 cases) and CG (104 cases: four format sets, `nit` 1-8, the zero guards) as programs, bit-exact on the interpreter with a closed-loop systolic harness | Stage 3 record |
| 2026-10-22 | 4 | pysim: `VecLoad` / `VecExec` / `VecStore` / `VecUnit`, the harness, random and application workloads; bit-exact against the interpreter on two generic configurations, CG and the matched filter; a fatal error | Stage 4 record |
| 2026-10-22 | 5 | Generated HLS bodies; RTL bit-exact (generic, CG, matched filter); CG 7,295 cycles a job against CgVectorUnit's 3,581 (2.04x, Vitis 2024.1), 59.6k LUT against 21.6k; the matched filter 1.26x its input floor | Stage 5 record |
