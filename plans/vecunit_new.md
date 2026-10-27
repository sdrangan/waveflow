# Plan: a general vector unit (RVV-style, decoupled), with CG as its first program

**Status:** Stages 0-7 done 2026-10-22; open: the Stage 6 csynth item, and the timing model's worst held-out score (Stage 7). Stage 8 (simulation speed, measurement only) added and done 2026-10-24; Stage 9 (measurement parity with the systolic example) added 2026-10-24; 9a-9d done, 9e and 9f wait for D7 and D8, 9g for the user; Stage 10 (faster untimed and pysim runs, no result changed) added and done 2026-10-24 (drafted 2026-10-20 with the user).
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

#### Record: Stage 6 (2026-10-22) -- fused runs; Vitis HLS / Vivado xsim 2024.1

**What was built** (in-order issue, no ISA change, no new task). The execute task's decode gathers a
**run**: consecutive passes while none reads a group or a scalar an earlier pass of the run writes
(bit masks; a pass that would is held over to start the next run), up to `max_run` (8). The run's
passes go into small descriptor memories, and **one pipelined loop over all their elements** (II 1,
depth 24) executes them: the pipeline fills once a run instead of once a pass. Consecutive
independent scalar instructions (`max_srun`, 8) run as one pipelined loop (II 2) on pipelined
dividers. Fusion changes no result: a later pass of a run writes a group only after an earlier pass
read it (`VL` plus the depth later). The rule is one definition, `model.exec_items`, which pysim's
timing uses and the HLS decode mirrors (an error while gathering runs the run gathered so far, then
stops: the job still stops exactly at the bad instruction; a program burst's framing error stops
it with `BAD_LENGTH` after the prefix that streamed, in pysim as at RTL: `model.program_prefix`).
CG's program gains `order="kind"` (the default): every group's first pass, every `alpha`, every
second pass, every `beta`, every third pass -- 11 runs of 8 passes and 5 runs of 8 divides a job,
where `order="group"` gives 46 pass runs and 40 scalar runs.

**Measured** (RTL bit-exact against pysim on CG `"test"` and `"steady"` and the matched filter's
`"steady"`; csynth est. 3.10 ns, Fmax 322 MHz):

| | Stage 5 | Stage 6 | `CgVectorUnit` |
|---|---|---|---|
| CG, a steady `8 x 32` job of three iterations | 7,295 | **3,359** | 3,581 |
| matched filter, a frame (input floor 1,152) | 1,448 | **1,408** (1.22x) | -- |
| unit LUT / FF / DSP / BRAM (CG configuration) | 59.6k / 26.8k / 50 / 28 | 82.1k / 71.7k / 53 / 36 | 21.6k / 11.9k / 52 / 20 |

**The cycle target is met: CG on the unit runs 6 % faster than `CgVectorUnit`**, because the program
groups the eight column groups' divides into pipelined runs, which the fixed core's loop order
serializes. The price is resources: the execute task is 71.4k LUT, 69.0k FF -- the fused loop 32.3k
/ 27.5k (48 DSP), the scalar run's four pipelined dividers 13.2k / **32.6k FF** (one a lane at II 2),
the run decode 14.8k LUT. Fewer, shared dividers would trade those flip-flops for divide time: a DSE
knob, not taken here.

**Where the CG job's 3,359 cycles go now** (233 instruction words; 88 passes in 11 runs, 704
elements; 5 divide runs of 8; 9 control): about 20 cycles of decode a pass -- some 1,760 cycles, the
largest share. A decode stage that runs ahead of execution (its own task, feeding run descriptors)
would hide most of it; recorded as the next optimization, not built.

**Open (rule 3: two hypotheses tested, stopped):** the generic RTL gate's configuration
(`rtl_config`: five vector formats mixing round/truncate and saturate/wrap) drives csynth of the fused
body past 12 GB (killed; 24.7 GB on the first try). H1 -- its small arrays (8 groups, 24-group
blocks): rejected, the same formats at CG's sizes also exceed 12 GB. H2 -- fusion: with `max_run =
max_srun = 1` the same configuration synthesizes in 65 s at 1.27 GB. So fusion and this format mix
together; CG's formats (three vector, all round and saturate) and the matched filter's synthesize in
under 2 minutes at about 1.3 GB. The generic RTL gate runs with fusion off until this is resolved.

**Revisited after Stage 7 (2026-10-22; still open, rule 3: two more hypotheses, stopped).** H3 -- the
wrap-around formats: rejected; the same configuration with every format saturating, fused, was killed
at 12.0 GB after 501 s. H4 -- the quantizer's per-format wrap `switch`, a real multi-width mux here
(vector formats 12 and 14 bits wide; CG's and the matched filter's are one width each): rejected;
emitting wrap cases only for wrapping formats (bit-identical) left the all-saturating configuration
with no wrap case at all, and it was killed at 12.1 GB after 491 s. The change was reverted (it would
have altered the RTL Stage 7 calibrated). What still separates this set from CG's: the union width
(`WV` 14 against 12, from the 14-bit format) and four distinct vector formats against three; a
rounding/truncation mix alone is not it (the matched filter has one and builds). Each is one csynth
of under 10 minutes to the watchdog.

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

#### Record: Stage 7 (2026-10-22) -- Vitis HLS / Vivado xsim 2024.1, xczu48dr, 4 ns, this machine

**Pre-registered** (rule 5) in commit `83687710`, before any calibration run:
`waveflow/vecunit/calib.py` names the timing fit set (on the CG configuration: 12 random jobs over
every instruction form, 8 to 32 passes, blocks of at most 8 columns, and 8 CG solves of varied `k`,
`n`, `nit`), the held-out timing scenarios (6 fresh random jobs and 2 solves; CG `"steady"`; the
matched filter's `"steady"`), the resource fit configurations (10: `L` 2-8, `NG` 8-64, `S` 2-3,
`"all"` or `"mul1"` capabilities, CG or matched-filter formats), 3 held-out configurations, and the
targets (3 % mean, 6 % worst). The calibration departs from the plan's `add_calib_steps` as agreed
with the user at the end of Stage 6: a linear job model fitted by its own code.

**Built.** `calib.py`: the RTL job intervals (status to status), pysim's intervals, the fit, the
score-once guard, the csynth-only resource measurement, the resource fit and score; the fits are
filed on the packaged platform (`waveflow/calib/platforms/xczu48dr_250mhz_vitis2024_1/models/
vecunit_timing/` and `vecunit_resources/`), and pysim reads the timing at import. `unit.py`:
`VecTiming` linear in `job_features` (decode, run, elem, srun, sop, divide, control, instr, job),
charged per item so pysim's per-item times sum to the job model (a test). **A fix found by the
pre-registered `mul1` rows' design:** the slot capability mask -- a DSE knob since Stage 0 -- gated
only the decoder's checks; every slot still had its multiplier. The fused loop now tests
compile-time masks `MULS` / `DOTS` (`hls.py`): an all-capable unit is unchanged (CG `"steady"` at RTL:
the same status cycles, 3361 / 6720 / 10079 / 13438, and the same csynth estimate, 82,063 LUT / 71,661
FF / 53 DSP / 36 BRAM), and `mul1` drops from 53 DSP to 21. Docs: `docs/guide/dsp/vecunit.md` (ISA,
chaining and pass rules, formats rule, architecture, where timing is measured, the scores, limits;
its example runs under the docs tests), `docs/examples/vecunit_cg/index.md`,
`docs/examples/vecunit_mf/index.md` (the input floor); `CLAUDE.md` lists `waveflow/vecunit/`; the
docs TOC pin gains the two examples. A Stage 5 omission the full suite caught: the two build steps
were named in no page (`tests/docs/test_step_docs.py`).

**Timing** (RTL every run bit-exact against pysim). Fit on 19 job intervals:

| `decode` | `run` | `elem` | `srun` | `sop` | `divide` | `control` | `instr` | `job` |
|---|---|---|---|---|---|---|---|---|
| 15.9 | 66.1 | 0.99 | 68.7 | 4.26 | 1.23 | 1.96 | 0 | 0 |

In-sample: the model 0.14 % mean / 0.38 % worst, pysim charged from it 0.16 % / 0.45 %.
**Held out, scored once:**

| Scenario | Intervals | Mean | Worst |
|---|---|---|---|
| CG configuration, fresh random jobs and solves | 7 | 0.30 % | 0.64 % |
| CG `"steady"` | 3 | 0.02 % | 0.02 % |
| matched filter `"steady"` | 3 | 7.5 % | 7.5 % |
| all | 13 | **1.9 %** | **7.5 %** |

The mean target holds; the worst does not, on the matched filter (pysim 1,513 cycles a frame, RTL
1,408). Root cause (one hypothesis, confirmed from csynth's log): the execute task's decode loop
`RUNG` is configuration-dependent -- II 13, depth 23 in the CG build; II 12, depth 14 in the matched
filter's (`DELIVER` depth 7 against 6) -- and every fit job ran on the CG build. A chunk's 3 decoded
passes and 2 runs give 3 + 18 + 2 = 23 of the 26 cycles a chunk pysim overcharges. Not refitted (the
held-out set is scored once). **Next:** take the per-pass and per-run costs from each build's loop
schedule and fit only the rest, on jobs from several configurations, with a fresh held-out set.

**Resources** (csynth only, the whole unit; the sweep ran three builds at a time under a 9 GB / 40
min watchdog; the largest, `L 8, NG 32, S 3`, took 37 min). DSP and BRAM are structural and exact on
all 13 configurations: `4 · n_mul · L + 5`, and the memories as they bind (the register file one
simple dual-port BRAM18 a bank and lane, the scalar file a true dual-port one a lane with 18-bit
ports, each queue's two blocks). LUT and FF are linear in terms chosen from csynth's breakdown by loop
(the dividers, one a lane, `L` and `L·NW·QW`; the fused loop, `S²·L`, and for FF `S²·L²`; the
multipliers, `n_mul·L`), each basis the lowest leave-one-out error of the few tried on the fit set;
the signs are free because two format sets fix a lane's width terms at two levels. Fit in-sample: LUT
1.3 % mean / 3.5 % worst, FF 0.3 % / 0.7 %. **Held out, scored once:**

| Held out (`L`, `NG`, `S`, slots, formats) | LUT | FF | DSP | BRAM |
|---|---|---|---|---|
| 8, 16, 2, all, CG | 104,024 (3.9 %) | 108,575 (1.1 %) | 69 | 72 |
| 4, 8, 3, `mul1`, matched filter | 68,810 (2.3 %) | 39,654 (2.8 %) | 21 | 32 |
| 2, 16, 3, all, CG | 51,621 (1.0 %) | 34,482 (0.5 %) | 29 | 20 |
| mean / worst | **2.4 % / 3.9 %** | **1.4 % / 2.8 %** | exact | exact |

Both within the targets. Read from the breakdown: `NG` hardly matters (81.5k to 83.3k LUT from 16 to
64 groups); the scalar dividers are exactly proportional to `L` (32.6k FF at `L` 4 in the CG build,
10.3k in the matched filter's); the run loop's FF has a knee (8.5k, 27.5k, 47.0k at `L` 2, 4, 8).

**Not done:** revisit the open Stage 6 item (fusion with a wide mixed-mode format set past 12 GB in
csynth); the timing model's schedule-derived form (above).

**Gate:** held-out scores reported (above). `pytest -m "not vitis and not xsi and not gem5"`: 4,965 passed,
40 skipped, 5 failed, none from this branch -- `test_markov_figures` (2) and
`test_vitis_fft::test_generate_writes_the_build_tree` fail on `main` too (checked in a worktree of
`main`: a figure-status file the repo does not track, the Vitis 2025.1 DSP headers absent here);
`test_xsi_workspace_copies[rt_sounder, vitis_fft]` compares untracked, gitignored workspace copies of
`run.bat` dated 2026-10-12/13 against the framework's, which `main` changed on 10-14. Fixed on the way:
the XSI gate floor (238 -> 241, Stage 5's three gates), the build steps no page named, the platform
manifest (`tests/linalg/test_no_example_imports.py`). `pytest -m xsi tests/vecunit/test_unit_xsi.py`:
3 passed in 557 s (the generic configuration fusion-off, CG, the matched filter, on the gated HLS).

### Stage 8: simulation speed -- CG and the matched filter, untimed, pysim and RTL across sizes

(Added 2026-10-24 with the user.) The twin of `docs/evaluation/sim_speed/systolic.md` for the unit's
two applications: the same bit-exact work run several ways, timed across problem sizes, on one build
per application. **Measurement only.** Nothing in the pysim, the interpreter, the harness or the HLS
is optimized in this stage; the user will optimize later. Where the time goes is recorded so that
work can start from it.

**The ways, on the same operands** (the cost model of `docs/evaluation/sim_speed/index.md`: pysim
time = the untimed model's time + events × a per-event cost; here the execute task calls the
interpreter's `Machine`, so the interpreter is the untimed term):

| way | CG | matched filter |
|---|---|---|
| untimed, the application's golden | `cg.cg_solve` on the whole `B`, every column at once | `mf_golden` on the whole frame |
| untimed, the unit's interpreter | `run_cg` per job (closed loop: `mm_step` on each `P` the unit emits) | `run_mf` |
| pysim | `CgVecUnit` in its harness: building the harness (the stimulus) and the run | `MfVecUnit` likewise |
| RTL | the csynth'd unit under XSI through the system flow (`waveflow.vecunit.build.build_dag`), untraced; csynth and the XSI build reported apart | the same |

**Sizes: one build each,** the one Stages 5-7 measured and calibrated, so the cycle counts can be
checked against those records. The problem grows on fixed hardware, as the systolic page's does:
- **CG:** `CgVecUnit` at the centre (`Kmax 8, Nmax 32, L 4`, the `CENTRE` formats, 64-bit words).
  An `8 × n` right-hand side, `n` = 32, 128, 512, 2,048, `nit` 3, one `A`: `n / 32` jobs, each a
  column block of `8 × 32`. The columns are independent CG problems, so this is how a wider `B` runs on
  this unit; `k` cannot pass `Kmax` without a new build. Workload `"rhs_<n>"`; a width that is not a
  multiple of `Nmax` is refused.
- **Matched filter:** `MfVecUnit` as built (`VLMAX 64, nrx_max 8, L 4`). One frame of `nsamp` = 256,
  1,024, 4,096, 16,384 samples at 8 antennas (4 to 256 chunks). Workload `"frame_<nsamp>"`.

Expected RTL lengths from Stages 6-7: about 3,360 cycles a CG job (3.4k to 215k cycles) and 1,408 per
256 samples (1.4k to 90k), the systolic page's range.

**How it is counted** (as `examples/systolic/systolic_runtime.py`): the untimed ways and the pysim are
the best of `--reps` runs (11), the RTL one run a size. The pysim's time includes building the harness;
the build and the run are also recorded apart, with the SimPy events and the simulated cycles. The
harness has no host to end the RTL run, so XSI simulates a fixed bound, `1.2 × pysim cycles + 2,000`;
the table charges it `simulate × done ÷ bound`, where `done` is the cycle the last output word left.
The raw times go in `runtime.json`. **Every run is checked:** the interpreter against the golden
(CG: every `X` against `cg_solve` on the whole `B`), pysim's every output burst against the
interpreter's after the clock stops, the RTL by the flow's `compare`. Per way, a fit
`setup + jobs × per job` (CG) or `setup + chunks × per chunk` (matched filter) over the sizes. A
cProfile of the largest pysim run is split into the interpreter, the word conversion, and SimPy with
the framework. It is recorded and not acted on.

**Code:**
- `waveflow/vecunit/runtime.py`: the application-free part. A unit's pysim timed and checked, its
  RTL timed through `build_dag`, the bound, the fit, the profile split. It takes a unit and a
  workload kind (rule 9).
- `examples/vecunit_cg/cg_runtime.py` → `examples/vecunit_cg/runtime.json`, and
  `examples/vecunit_mf/mf_runtime.py` → `examples/vecunit_mf/runtime.json`: each holds its sizes, its
  untimed ways, the command line and the results, as the systolic script does.
- The workloads `"rhs_<n>"` (`cg_unit.py`) and `"frame_<nsamp>"` (`mf_unit.py`), with tests: pysim
  exact at a small size, the CG `X` equal to `cg_solve` on the whole `B`, a bad size refused, the
  runtime helpers on the smallest size.
- Docs: `docs/evaluation/sim_speed/vecunit_cg.md` and `vecunit_mf.md` (the set-up, the command,
  results across sizes, one-time costs, insights, where the time goes, future work), and their rows
  and links on `docs/evaluation/sim_speed/index.md`.

**Gate:** both scripts run with `--rtl`, every run exact in pysim and at RTL; both `runtime.json`
committed; both pages written from them, the tool version (Vitis 2024.1) and the machine on each
table; `pytest tests/vecunit tests/examples/test_vecunit_cg.py tests/examples/test_vecunit_mf.py
tests/docs -q` passes. **Pause.**

#### Record: Stage 8 (2026-10-24) -- Vitis HLS / Vivado xsim 2024.1, xczu48dr, 4 ns

Machine: a Linux workstation in the lab (Intel Core i7-7700K, 4 cores / 8 threads, 32 GB, Python
3.12.3), otherwise idle; untimed and pysim the best of 11 runs, the RTL one run a size.

**Built.** `waveflow/vecunit/runtime.py` (pysim timed and checked against the golden bursts, the RTL
timed through `build_dag` with the XSI phases and the last output's cycle, the bound, the fit, the
cProfile split, the record kept across runs); `examples/vecunit_cg/cg_runtime.py` and
`examples/vecunit_mf/mf_runtime.py` with their `runtime.json`; the workloads `"rhs_<n>"` and
`"frame_<nsamp>"` (`MfVecUnit.frame` now makes a frame's operands); 7 tests
(`tests/vecunit/test_runtime.py`, one each in the two example test files);
`docs/evaluation/sim_speed/vecunit_cg.md` and `vecunit_mf.md`, two rows and two links on the section's
index, and a link from each example page. Nothing was optimized.

**Gate:** `python -m examples.vecunit_cg.cg_runtime --rtl --root <r>` (12 min 51 s) and
`python -m examples.vecunit_mf.mf_runtime --rtl --root <fresh r>` (5 min 33 s), each then `--profile`:
every run exact (the interpreter against the golden, pysim against the interpreter, the RTL by
`compare`). `pytest -m "not vitis and not xsi and not gem5" tests/vecunit tests/examples/test_vecunit_cg.py
tests/examples/test_vecunit_mf.py tests/docs tests/linalg/test_no_example_imports.py` -> 322 passed.

| CG, `8 × n` (jobs) | RTL cycles | golden | interpreter | pysim (build + run) | RTL under XSI | RTL ÷ pysim |
|---|---|---|---|---|---|---|
| 32 (1) | 3,491 | 0.98 ms | 12.9 ms | 81 ms (66 + 15) | 2.7 s | 34× |
| 2,048 (64) | 215,108 | 4.2 ms | 847 ms | 5.0 s (4.1 + 0.9) | 250 s | 50× |

| matched filter, `nsamp × 8` (chunks) | RTL cycles | golden | interpreter | pysim (build + run) | RTL under XSI | RTL ÷ pysim |
|---|---|---|---|---|---|---|
| 256 (4) | 1,825 | 0.07 ms | 2.0 ms | 8.7 ms (4.3 + 4.4) | 0.46 s | 53× |
| 16,384 (256) | 90,277 | 2.4 ms | 128 ms | 274 ms (70 + 204) | 40 s | 148× |

One-time: csynth 98 s (CG) and 89 s (matched filter); the XSI build 93 s and 85 s. The other sizes and
the fits are on the pages.

**Found** (recorded, not acted on):
- **CG's harness build is 82 % of its pysim time:** the stimulus's closed-loop capture
  (`cg_unit.cg_job`) runs the interpreter in its default element-major mode, 4.5× the instruction-major
  mode pysim's execute task uses.
- **pysim's run costs 1.06-1.16× the interpreter for CG, 1.6-2.2× for the matched filter**: a chunk is
  a small block, and SimPy with the framework is 39 % of the matched filter's run (7.5 % of CG's).
- **CG pysim's first job starts 233 cycles late** (its status at 3,594 against 3,361); every later
  interval is the RTL's to a cycle. The Stage 7 fit used intervals only, so a run's start-up was never
  calibrated.
- **The matched filter's pysim over-charges a chunk by 11 %** (389 cycles against 351), Stage 7's open
  item, so a 256-chunk frame reads 10.6 % long.
- XSI ran the CG build at 860-1,290 cycles a second and the matched filter's at 2,240-3,940 (csynth:
  82.1k / 74.5k LUT, 71.7k / 48.3k FF).

### Stage 9: measurement parity with the systolic example

(Added 2026-10-24 with the user.) The systolic example (`examples/systolic/`, `docs/examples/systolic/`)
measures more than simulation speed. This stage gives the two vector programs every measurement of
it that applies to them. Each sub-stage has its own gate; they run in the order below, because the
later ones read the traces of 9b and the catalogue of 9c. Everything is measured with Vitis HLS /
Vivado xsim **2024.1** on the Stage 8 machine. The systolic engine's published numbers are 2025.1 and
are not reused: 9f re-measures what it needs.

**Not taken, with the reason:**
- The word-conversion benchmark (`systolic_runtime.py --conversion`). `lanes.to_words` / `from_words`
  are shared code, already measured on `docs/evaluation/sim_speed/systolic.md`, and conversion is 1-6 %
  of the unit's pysim run (Stage 8).
- The resource sweep (`systolic_sweep.py`). Stage 7 already has one, over 13 configurations.

#### 9a. Word width (small)

`CgVecUnit` and `MfVecUnit` take `word_bits` (64 or 128) through to `cg_config` / `mf_config`, which
already accept it. The harnesses and the Stage 8 runtime scripts gain `--word-bits`. A complex element
is 32 bits (16-bit lanes), so a 128-bit word holds 4. At `L 4` a lane group is then exactly one word,
and the loader and the store stay on their word-a-cycle path. At `L 2` a lane group is half a word, and
they fall to a value a cycle (`unit._slow_path`, `vecunit_core.h`). Name this wherever a 128-bit run
with `L 2` is reported.

**Gate:** pysim exact at 128 bits on `"test"` and `"steady"` for both units;
`python -m examples.vecunit_{cg,mf}.harness --through compare --word-bits 128` exact at RTL; one XSI
gate per example at 128 bits in `tests/vecunit/test_unit_xsi.py`.

#### 9b. Traces: which stage binds (medium)

As `docs/examples/systolic/timing.md#finding-the-stage-that-binds`. First check that `--trace` binds
the unit's channels by name through `waveflow.utils.trace.load_trace`:
- each queue's `block_events` (the queues use the undocumented explicit acquire and release,
  `EXPLICIT_ACQUIRE_RELEASE`, under `hls::task`);
- the inputs' accepted beats (`vectors/s_in0_acc`) and the program streams' beats.

If the manifest cannot bind explicit acquire and release, that is a framework change: search the KB
and propose it first (rule 8). Then `waveflow/vecunit/report.py` gains a trace report: per queue, each
block's held and idle cycles; per job, the execute task's time waiting on an input block, waiting on
an output block, and busy; and the task that binds, with that evidence.

**Gate:** the report on CG `"steady"` and the matched filter's `"steady"` at RTL, traced. It names the
binding task, and agrees with or corrects Stage 5's reading (the matched filter's execute task at
about 352 cycles a chunk against the loader's about 320). A test runs the report on a small traced
run.

#### 9c. The RTL catalogue against the bounds (medium; about 20 RTL runs)

`examples/vecunit_cg/cg_measure.py` and `examples/vecunit_mf/mf_measure.py`, each writing a
`measured.json` like `examples/systolic/systolic_measure.py`. Per run: the configuration, word width,
workload and tool; whether it is exact; each job's status cycle and the output intervals; pysim's
cycles; csynth's resources; and whether csynth's estimated clock meets 4 ns.
- **The bounds, per job:** the input words at a word a cycle; the output words, headers included; and
  the execute task's own floor from its timing features (decode, elements, scalar runs, divides).
  **Before each run, write in the script which bound should bind.** The record says where the
  prediction held.
- **CG workloads** chosen so that a different bound binds: `steady` (`8 × 32`, `nit` 3); short
  vectors (`k` 2, so passes are short and the divides' share rises); I/O-heavy (`nit` 1); long
  (`nit` 8).
- **Matched-filter workloads:** `steady` at `nrx` 8, 4 (one column group) and 1, where `X`'s
  single-column block costs the most.
- **Both word widths (9a), and configurations other than the two builds** (the systolic example's
  half-size engine): CG at `L 2`, `L 8` and `S 2`; the matched filter at `VLMAX` 16 and 256. Each is a
  csynth and an XSI build of about three minutes. The script lists the runs; a run already in
  `measured.json` is skipped unless forced.

**Gate:** both `measured.json` committed (force-added past the `*.json` ignore), every run exact,
`meets_clock` checked on each, and a test like `test_measured_runs_are_exact_and_meet_the_clock`.
Pages `docs/examples/vecunit_cg/timing.md` and `docs/examples/vecunit_mf/timing.md` hold the bounds
table and what binds, as `docs/examples/systolic/timing.md` does.

#### 9d. Calibration by phase and by configuration (large; rule 5)

Stage 7's recorded next step, and the cause of both timing gaps Stage 8 found: the first job's 233
cycles of start-up, and the matched filter's 11 % a chunk. In place of the one global linear job
model:
- time each phase from the traces (9b): a decoded pass, a run's fill and drain, an element, a scalar
  run, a divide, a job's start-up, the loader's and the store's per word;
- take the per-pass and per-run costs from each build's own csynth loop schedule (II and depth), and
  fit only the rest (lesson 9);
- file the fit per configuration identity, as the systolic core's `engine_core-*` records are filed
  per word width, so pysim loads the one that matches its build.

**A decision record first:** whether `add_calib_steps` / `timing_phases` can express a phase per
decoded item. Stage 7 departed from them; say why the new form does or does not return to them.

**Pre-register before any run:** a fit set on the CG and matched-filter builds and at least two of
9c's other configurations, and a fresh held-out set. Score once against Stage 7's targets (3 % mean,
6 % worst), now including the matched filter.

**Gate:** held-out scores reported; Stage 8's scripts re-run (pysim only), and their pages' pysim
cycle columns updated.

#### 9e. Behind memory: `VecUnitMem` (large; D7 first)

A wrapper that stands to the unit as `SystolicEngineMem` stands to the engine:
- a job descriptor carrying base addresses;
- a feeder that reads the input matrices from memory through one `m_axi` port (`hls::burst_maxi`,
  each request issued ahead of its data) and streams them to the unit's inputs;
- a writer that writes the unit's outputs back;
- a done per job.

It sits outside the ISA, and the unit still sees streams, so *Out of scope*'s "DRAM addressing"
(meaning in the ISA) stands.

**D7 decides:**
- whether the programs come from memory or stay on streams;
- one memory port or two;
- whether the wrapper splits a wide CG `B` into column-block jobs from one descriptor (the split
  Stage 8's testbench does today);
- the matched filter's `X` and `R` layout in memory.

**Pause for approval.** Then the pysim, the HLS (the framework's memory feeder and writer), the RTL,
and a harness with its one memory (`harness()` puts one behind the `m_axi` ports).

**Measure,** as `docs/examples/systolic/timing.md#the-wrapper`:
- the done cycles against the input floor;
- the read port's busy share (traced, 9b);
- Stage 8's sizes again, through memory: the runtime scripts gain the memory path, as
  `systolic_runtime.py --sizes` runs `SystolicEngineMem`.

**Gate:** RTL exact, and the measurements recorded.

##### Record: D7, proposed (2026-10-24) -- awaiting the user's approval

Read first: `waveflow/linalg/systolic_engine_mem.py`. A job descriptor goes to a sequencer, a feeder owns
the read port and streams a tile in one firing, and a writer turns `Resp | C` into memory writes with
the framework's `MemWStream`. Its pointer-FIFO rule: the `m_axi` reader and writer of one top fire
equally often.

- **Programs stay on streams** (proposed). The unit and its three program ports stay as they are, and
  the wrapper moves only data. The alternative, programs read from memory through descriptor
  addresses, is how a processor-attached accelerator would take them, but it adds a third reader and
  a program format in memory. Revisit it with 9f's host.
- **One read port and one write port** (proposed), as `SystolicEngineMem`: a feeder `VecMemFeed` reads
  each `wf.ld`'s matrix, and a writer `VecMemWrite` writes each `wf.st`'s (`VecResp` then payload)
  through `MemWStream`.
- **One descriptor a job, no splitting** (proposed): `VecMemJob(tx_id, the input bursts' bases, the
  output bursts' bases)`. The host issues `n / 32` descriptors for a wide CG `B`, as Stage 8's
  testbench issues jobs. Splitting inside the wrapper needs the program to repeat per block, which
  is a program format question for later.
- **Layout: row-major**, the bursts the streams carry today. Each matrix is one contiguous run, so
  the feeder makes a burst a matrix. The matched filter's `X` and `R` per chunk sit side by side.
- **The pointer-FIFO rule:** a feeder firing once a `wf.ld` and a writer once a `wf.st` would fire
  equally often for CG (`nit + 1` each a job) but not for the matched filter (two loads, one store a
  chunk). So each fires once a job: the feeder reads all of a job's loads in one firing, as
  `EngineFeeder` reads a tile's panels, and the writer writes all of its stores.

#### 9f. CG with the systolic engine in the loop (large; D8 first)

Today every `S = A·P` is precomputed into the stimulus, so the 3,359 cycles a job (Stage 6) leave out
the multiply's latency and the round trip. This sub-stage builds the closed loop, one job at a time.
**Contexts** (two jobs resident at once) stay out of scope.

The loop: a stream-fed `SystolicEngine` in CG's formats (`a = A`, `b = P`, `c = S`, the plain
product), and the CG unit, joined by an adapter:
- each `P` the unit emits (`VecResp` and payload) becomes an engine command with `P` as its `B`
  operand;
- each engine result (`Resp` and `C`) becomes the headerless `S` burst on the unit's input.

D5 chose the unit's block layout to be the engine's, so no reorder should be needed; check it.

**D8 decides:**
- the adapter: an HLS task in the composite, or the system's host;
- `A`: re-sent with every product (64 values), or held in the engine;
- the engine's configuration (its `Kmax`, `Nmax` against CG's `k`, `n`).

**Pause for approval.**

**Gate:** RTL exact against `cg.cg_solve` on four solves. Report:
- cycles a job with the real multiply, against Stage 6's 3,359 with `S` precomputed;
- the iteration's critical path from the trace (9b): the unit's third pass, `P` out, the engine, `S`
  in, the next first pass;
- the unit's idle share while it waits for `S`;
- csynth of the engine at 2024.1, recorded beside its 2025.1 figures, never mixed with them.

##### Record: D8, proposed (2026-10-24) -- awaiting the user's approval

Facts found (read in the code): the stream-fed `SystolicEngine` already has `keep_a`, one `A` for `nb`
products, which its docstring calls "the CG case". Its centre formats are CG's (`a` 12/3, `b` 12/4,
`c` 12/5), and its model is `waveflow.linalg.matmul.matmul`, which `cg.mm_step` calls, so the loop is
bit-exact by construction. Its sizes (`Mmax 8, Kmax 8, Nmax 32`) are CG's job. `VecUnitConfig`
already has several ports a side.

- **The CG unit on two ports a side** (proposed): `B` in on port 0 from the host, `S` in on port 1
  from the engine; `P` out on port 1 to the engine, `X` out on port 0 to the host. Nothing then
  routes by tag. Only the program builder changes (`cg_programs(..., ports=2)`), a configuration
  the unit already supports.
- **The link: two small HLS tasks in the composite** (proposed), not the host. `VecToEngine` takes
  the host's `EngineCmd` and `A` for a job, then forwards `nb` of the unit's `P` payloads as `B`,
  dropping each `VecResp`. `EngineToVec` drops each `Resp` and forwards `C` as the unit's `S`. A
  host in the loop would be the testbench, so the round trip would measure nothing real.
- **`A` held** (`keep_a = 1`, `nb = nit`), not re-sent: 64 values once a job.
- **The engine at its centre configuration, 64-bit words**, csynth'd at 2024.1, its figures beside
  the 2025.1 ones and never mixed with them.

#### 9g. Coarser pysim granularities (optional; to confirm with the user first)

As `SystolicEngine(pysim="flat")` and `SystolicEngineMem(pysim="tiles")`:
- `VecUnit(pysim="flat")`: one process, the three tasks passive, the same per-item timing;
- `"jobs"`: a job's arithmetic in one interpreter call, charged from `job_features` in one delay.

Measure the SimPy events, the time, and the cycle error against `"tasks"` and the RTL on Stage 8's
sizes. **This is a pysim speed optimization,** which the user reserved for their own later work at
Stage 8. Do not start it until the user says whether it belongs here.

**Stage gate:** each sub-stage's gate, and the docs that change: `docs/guide/dsp/vecunit.md` (the
calibration, the limits), both example pages, and `docs/evaluation/sim_speed/vecunit_{cg,mf}.md`
after 9d and 9e. **Pause** at D7, at D8, and at the end.

#### Record: Stage 9 (2026-10-24) -- 9a to 9d done; 9e and 9f wait for D7 and D8; 9g not started

Vitis HLS / Vivado xsim **2024.1**, `xczu48dr` at 4 ns, the Stage 8 machine. Commits: `0cc4c90b`
(9a, 9b, the catalogue's tooling, the 9d pre-registration), then this one.

**9a, word width.** `CgVecUnit` / `MfVecUnit` take `word_bits`; `--word-bits` on the harnesses and the
runtime scripts. pysim exact at 128 bits; two new XSI gates exact at RTL (`pytest -m xsi
tests/vecunit/test_unit_xsi.py -k 128`: 2 passed, 510 s; the gate floor 241 -> 243). A 128-bit word is
two `uint64` columns: the tests and `runtime.outputs_match` now flatten both sides of a comparison.

**9b, traces.** No framework change was needed: under the explicit acquire and release the queues' lock
nets are where the manifest says (`report.load_unit_trace` finds the unit's scope by its tasks'
`ap_done`). `report.trace_report` reads per queue the held commits and the consumer's slack, per task
its firings, per input port its busy share, and `report.binding` names the binding task by who waits on
whom. The execute task's input waits cannot be read -- its fused loop enables every queue's memory in
every pass, so its "first access" can precede a commit -- so a negative slack is unknown, not a wait.
CG `steady` and the matched filter's `steady` are execute-bound, which agrees with Stage 5's reading;
the matched filter at `VLMAX 256` is load-bound.

**9c, the catalogue.** `cg_measure.py` (13 runs, 4 builds) and `mf_measure.py` (11 runs, 5 builds) ->
`measured.json` each: **every run exact, every build within 4 ns** in csynth's estimate.
`docs/examples/vecunit_{cg,mf}/timing.md` hold the tables. In brief: **CG is execute-bound in every
workload** (the execute task takes 4 to 20 times the loader's time), 128-bit words change nothing
(3,359 cycles), `L` 2 / 4 / 8 give 6,678 / 3,359 / 2,161 cycles a job, and decoding passes is 42 % of a
job (16 cycles a pass: `RUNG` II 13, `DELIVER` II 3). **The matched filter is execute-bound at short
chunks** (`VLMAX 16`: 2.89 times its input floor; 64: 1.22) **and load-bound at long ones** (256: 1.12)
and at `L 8` (1.15, one pass a chunk). Deviations: `S 2` runs neither program (both chain three
instructions), nor does the `mul1` capability mask, so the extra builds are `L 2`, `L 8`, `VLMAX` 16
and 256. All three campaign streams were meant to run in parallel; two never started (lesson 12,
relearned) and were restarted.

**9d, timing.** Two root-cause fixes, no fit: pysim's execute task starts a job at its program's first
word, not its last (CG's first job had been 233 cycles late -- its program's length); and it keeps that
lead through a queue block it takes at once (found on `steady_nit8`, whose first job was still 350
cycles late: `_caught_up` had moved the job's clock to the simulation's on every take). The first jobs
now land within 11 cycles on the `L 2` / `L 4` builds, 120 cycles late on `L 8` (not explained).
Stage 8's scripts re-run on this code: pysim's CG cycles within 0.07 % of the RTL's at every size.

The pre-registered fit by phase and configuration (each build's loop schedule from csynth; NNLS on the
rest; `calib.calibrate_timing2`). Its first run admitted two jobs whose interval the *previous* job's
store had set (the rule read only the job's own bounds, against the then-filed execute estimate); the
rule now compares the measured interval with the I/O that could have set it (the job's load and store,
the previous job's store), and the fit was re-run before any held-out run was scored: 61 jobs, the
model 2.05 % mean in-sample. **Held out, scored once** (14 scenarios on nine builds): **6.2 % mean,
22.1 % worst**, against targets of 3 % and 6 %. The Stage 7 timing on the same set (no fit, a
comparison): **4.4 % / 18.8 %** -- CG 0.02 to 0.85 % everywhere, the matched filter 7.5 to 13 %.
`calib.settle_timing2` kept the Stage 7 timing for pysim and filed the new model's record and both
scores beside it. Root cause of the loss: the matched filter's frames run close to the loader's bound,
so the rule excluded every one, and the coefficients (83 cycles a job, 11 a control instruction) came
from its random jobs. **Next:** the Stage 7 coefficients plus each build's schedule difference from the
CG build's -- no free parameter -- pre-registered, with a fresh held-out set.

**9e, 9f:** D7 and D8 written as proposals (above), awaiting the user. **9g:** not started (the user's
answer pending); Stage 10 covers the speed that changes no result.

Gate: `pytest -m "not vitis and not xsi and not gem5" tests/vecunit tests/examples/test_vecunit_cg.py
tests/examples/test_vecunit_mf.py tests/docs tests/mcp/test_knowledge_corpus.py
tests/linalg/test_no_example_imports.py` (below, before the commit); the catalogues as above.

### Stage 10: faster untimed and pysim runs, no result changed

(Added 2026-10-24 with the user.) Stage 8 measured how long the unit's two applications take to
simulate; this stage makes the unit's own Python faster -- the interpreter (the untimed model, and
pysim's arithmetic), the harness's stimulus and golden, and pysim's run -- **without changing any
result**. Scope (the user's): `waveflow/vecunit/` and `examples/vecunit_{cg,mf}/` only. Nothing in
`waveflow/utils/fixputils.py`, `waveflow/linalg/` (`lanes`, `cg.cg_solve`, the CG golden), the
interfaces or SimPy changes.

**"No result changed"** is the simulation-speed section's rule (outcomes, not just bits): every output
burst of every pysim run carries the same words *and* arrives at the same time; the interpreter gives
the same bits; the HLS is untouched, so the RTL needs no re-run.

**Found by profiling** (cProfile on Stage 8's largest runs, Stage 9's code):
- **The interpreter's time is `fixputils`' per-call generality, not arithmetic.** `Machine.vector`
  calls `fixputils.quantize`, `add`, `mult` on arrays of `L × VL` (32 values for CG): each call derives
  a result `Format` (a frozen dataclass of enums, built and hashed), checks widths, chooses dtypes and
  goes through numpy's `clip` wrapper. 49 % of CG's pysim run, 91 % with the rest of the interpreter.
- **`run_job` checks every instruction and pass on every job** (`instr_status`, `pass_status`): 23 %
  of the interpreter, though a solve's jobs share one program.
- **CG's harness build runs the closed-loop interpreter element-major** (`run_job`'s default, 4.5×
  the instruction-major mode, the same bits): 82 % of CG's pysim total. Its golden then runs the
  interpreter again on the static stimulus.
- **The assembler formats a disassembly for every pass** it checks (`asm._check_execute`), for a message
  only an error prints, and every job's identical program is checked and encoded again.
- **The execute task decodes and gathers each job's program again** (`program_prefix`, `exec_items`:
  20 % of CG's run), and **waits after every item**, a SimPy timeout each.

**Changes** (each holds the outcome; the tests below):
1. `model.py`: the interpreter's fixed-point kernels (`_quantize`, `_add`, `_sub`, `_mul`, `_div`) run
   `fixputils`' integer steps for signed int64 operands with each format pair's plan derived once and
   looked up by the formats' identities; a write quantizes real and imaginary parts in one call.
2. `model.py`: `checked_passes` -- a program's checks, cached in a dict a caller passes across its jobs
   (`run_job(checks=...)`); never a module-level cache, so a timing of independent runs stays cold.
3. `unit.py`: `VecExec` decodes and gathers each distinct program once per unit instance (its items and
   each run's queue blocks, in order), and **defers its waits** to the moments another task can see its
   clock -- a block taken, freed or committed, the status written (`VecExec.defer`).
4. `cg_unit.py`, `workload.py`: the closed-loop stimulus and the golden run the interpreter
   instruction-major; the CG unit keeps its closed-loop results as the golden
   (`VecUnit.remember_results`), and a unit builds each workload's jobs once (`scenario_jobs`).
5. `asm.py`: the checker formats its messages only for an error; `assemble(cache=...)` checks and
   encodes a program its caller's jobs share once.

**Not taken:** `mf_golden` (0.009 ms a chunk; rewriting it on the interpreter's kernels would tie the
golden to what it checks); the word conversion (`lanes`, out of scope, 1-6 % of a run); `cg.cg_solve`
(out of scope); coarser pysim granularities (Stage 9g: they change the timing's granularity, which is
a result).

**Gate:** (a) `tests/vecunit/test_model_fast.py`: the kernels against `fixputils` bit for bit on random
signed formats (both rounding and both overflow modes, the width guard's refusals, the divide's zero
guard and overflow case); deferral on and off giving every burst's words and time; the caches equal to
none; the kept golden equal to a recomputed one. (b) Every output burst of 17 pysim workloads (both
applications, the generic unit, four builds) the same words at the same times as Stage 9's code. (c)
`pytest -m "not vitis and not xsi and not gem5"` on the unit's and examples' tests. (d) Stage 8's
scripts re-run, before and after, on the same idle machine, best of 11; the pages report both.

#### Record: Stage 10 (2026-10-24) -- done

Built as listed above, in `waveflow/vecunit/` (`model.py`, `unit.py`, `asm.py`, `workload.py`) and
`examples/vecunit_{cg,mf}/` (`cg_unit.py`, `cg_program.py`, `mf_program.py`, `cg_runtime.py`); nothing
outside them.

**Gate.**
- (a) `tests/vecunit/test_model_fast.py`, 16 tests: the kernels against `fixputils` on 3,200 random
  format sets plus 2,000 divides (with zero divisors and the overflow case), refusals included; the
  identity table bounded; deferral on and off giving every burst's words and time on CG, the matched
  filter and the generic unit; the caches equal to none; the kept golden equal to a recomputed one.
- (b) A one-off cross-version check: every output burst of 17 pysim workloads (CG `test`, `steady`,
  `calib`, `holdout2`, `steady_k2`, `steady_n4`, `rhs_256`, at 128 bits and at `L 2`; the matched
  filter's `test`, `steady`, `calib`, `steady_nrx1`, `frame_1024`, at `VLMAX` 16 and 256; the generic
  unit's `test`) recorded -- its words' hash and its arrival time -- on Stage 9's final code (`f95a3f91`)
  and on this one: **724 bursts, 0 differences.**
- (c) `pytest -m "not vitis and not xsi and not gem5" tests/vecunit tests/examples/test_vecunit_cg.py
  tests/examples/test_vecunit_mf.py tests/docs tests/mcp/test_knowledge_corpus.py
  tests/linalg/test_no_example_imports.py`: below, before the commit.
- (d) Stage 8's scripts re-run on the idle machine, best of 11, the record before kept under
  `<key>_before_stage10` in each `runtime.json`; every simulated cycle count identical:

| | before | after | speed-up |
|---|---|---|---|
| CG pysim, 64 jobs (8 × 2048) | 4.98 s | 1.00 s | **5.0×** (3.1× at one job) |
| ... building the harness | 4.07 s | 0.53 s | 7.8× |
| ... the run | 0.91 s | 0.48 s | 1.9× |
| CG interpreter, 64 jobs | 853 ms | 474 ms | 1.8× (1.55× at one job) |
| the matched filter's pysim, 16,384 samples | 280 ms | 239 ms | 1.17× (1.08× at 256 samples) |
| the matched filter's interpreter | 127 ms | 89 ms | 1.43× |
| SimPy events: CG 64 jobs / the matched filter's 256 chunks | 7,509 / 9,242 | 6,678 / 8,733 | −11 % / −5.5 % |

The RTL now takes 101 to 250 times as long as CG's whole pysim (it was 33 to 50) and 56 to 169 times
the matched filter's (52 to 144). pysim's CG run takes 1.00 to 1.36 times the interpreter on the same
jobs. What is left: CG's interpreter (77 % of its run, 89 % of its harness build); the matched filter's
SimPy and framework (47 % of its run, outside the scope). Pages: `docs/evaluation/sim_speed/vecunit_cg.md`,
`vecunit_mf.md` (the tables are the current code's, with a before-and-after section), the section index.

## Out of scope

- Moving `examples/mimo_cg` onto this unit, or onto the systolic engine (a separate plan).
- Contexts: two jobs resident at once beside the systolic engine (Stage 6, second item). The closed
  loop without contexts, one job at a time, is Stage 9f.
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
| 2026-10-22 | 6 | Fused runs (in-order issue): CG 3,359 cycles a job against CgVectorUnit's 3,581 (0.94x, Vitis 2024.1), the matched filter 1.22x its floor; 82k LUT / 72k FF. Open: a mixed-mode format set with fusion exceeds 12 GB in csynth | Stage 6 record |
| 2026-10-22 | 7 | Pre-registered (`83687710`), then calibrated (Vitis 2024.1): timing held out 1.9 % mean, 7.5 % worst (the matched filter: the decode loop's schedule differs by configuration); resources held out LUT 2.4 % / 3.9 %, FF 1.4 % / 2.8 %, DSP and BRAM exact. The slot capability mask now reaches the datapath. Guide and two example pages | Stage 7 record |
| 2026-10-24 | 8 | Simulation speed, measurement only (Vitis 2024.1, a lab workstation): CG `8 × 32`..`8 × 2048` -- the RTL 34-50× the whole pysim, 180-280× its run; the matched filter 256..16,384 samples -- 53-148×; every run exact. CG's harness build is 82 % of its pysim (the closed-loop stimulus, element-major) | Stage 8 record, `docs/evaluation/sim_speed/vecunit_{cg,mf}.md` |
| 2026-10-24 | 9 | Added with the user: measurement parity with the systolic example -- 128-bit words, trace-based binding analysis, an RTL catalogue against the bounds over workloads and configurations, calibration by phase and configuration (pre-registered), a memory wrapper (D7), CG with the systolic engine in the loop (D8); coarser pysim granularities optional, to confirm | Stage 9 |
| 2026-10-24 | 9 | 9a-9d: 128-bit words (XSI gates 241 -> 243); trace reports (CG and the matched filter execute-bound, `VLMAX 256` load-bound); a 24-run RTL catalogue on nine builds, all exact; pysim's first-job start-up fixed at the root (CG within 0.07 %); the fit by phase and configuration lost on held-out (6.2 % / 22.1 % against the incumbent's 4.4 % / 18.8 %), the Stage 7 timing kept. D7, D8 proposed | Stage 9 record |
| 2026-10-24 | 10 | Faster untimed and pysim runs, no result changed (724 output bursts of 17 workloads identical, words and times): CG's pysim 3.1-5.0× faster, its interpreter 1.6-1.8×; the matched filter's interpreter 1.4×, its pysim 1.1-1.2× (half its run is SimPy and the framework). Fixed-point kernels without fixputils' per-call generality, programs checked once per caller, the stimulus instruction-major and kept as the golden, the execute task decoding once and waiting only where seen | Stage 10 record |
