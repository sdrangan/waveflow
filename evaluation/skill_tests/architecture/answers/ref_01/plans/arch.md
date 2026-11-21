# arch.md -- the MMSE MIMO detector: how

Status: three alternatives, priced in `eval.md`. **Checkpoint 2** comes after `eval.md`: you pick.

Every alternative shares the split and the apply path; they differ in how `W` is computed.

## What every alternative shares

**HW / SW split.** In hardware: everything at the resource-element rate (the apply `W y`, 23.5 G
CMAC/s) and at the filter-group rate (computing `W`, 546 k a second). In software, on the A53 as a
`SwFunction` on a `Processor`: the per-slot control -- which users are on which PRBs, one command per
filter group to the hardware, the error reports. It is 0.2 % of a core (`spec.md`); anything faster
would not need a processor, anything slower is not this design.

**The apply path: library block `SystolicEngine`** (`dsp/systolic/systolic_engine.md`, a kernel:
streams only). `x_hat = W Y` is a matrix product, `m = U` rows, `K = B = 32`, `n` resource elements:
one command per filter group, `keep_a` (one `W` for the group's 7 outputs of 24 columns). Four
engines, each taking every fourth PRB (`eval.md`: one engine is input-bound at 0.73 of the rate
needed). A built kernel would redo what the engine does; no reason to.

**A distributor and a collector** (built: no library block routes a PRB's samples to the engine that
owns its filter): a leaf kernel that reads each PRB's `Y` from the FFT's stream and the command, and
writes them to engine `prb mod 4`; and one that merges the four outputs back into PRB order for the
demapper. Both a word a cycle, no arithmetic.

## Alternative A -- `W` by conjugate gradient on the vector unit

| block | kind | library | job |
|---|---|---|---|
| `gram_cg_mm` | kernel | `SystolicEngine` (centre, 128-bit) | `G = H^H H` (`MUL_AH`, 4 panels), and each CG iteration's `S = G P` |
| `cg` | kernel | `VecUnit` running the CG program (`examples/vecunit_cg`) | 5 iterations of multi-RHS CG on `G W = H^H`, `sigma2` added to the diagonal |
| `apply[0..3]` | kernel | `SystolicEngine` (Kmax 32, Pmax 1, keep_a) | `x_hat = W Y` |
| `dist`, `coll` | kernels | built | route `Y` and `W` to engines; merge outputs |
| control | software | `Processor` + `SwFunction` | per-slot commands |

The device: all kernels, with no adaptor on the data path (streams from the FFT, streams to the
demapper). The commands from the A53 reach `dist` through a bus view (a `QueueIn`,
`systems/devices.md`), the statuses back through a `QueueOut`.

## Alternative B -- `W` by a direct (Cholesky) solve, built

| block | kind | library | job |
|---|---|---|---|
| `gram` | kernel | `SystolicEngine` (centre, 128-bit) | `G = H^H H + sigma2 I` |
| `chol_solve` | kernel | **built: no library block does a direct solve** (`systems/library.md`, *What no block covers*) | Cholesky `G = L L^H`, then `L Z = H^H`, `L^H W = Z`, 32 right-hand sides |
| `apply[0..3]`, `dist`, `coll`, control | as A | | |

`chol_solve` is the only block whose cost no model covers: `U` square roots, `U(U+1)/2` divides,
and the triangular solves (2,048 CMAC a PRB-slot) at a lane loop of `U` lanes.

## Alternative C -- no explicit `W`: solve per resource element

`z = H^H y` per RE (the same 256 CMAC an RE as applying `W`), then two triangular solves per RE with
the PRB's Cholesky factor (64 CMAC an RE more). It saves forming `W` (12 CMAC an RE when amortized
over 168 REs) and costs 64: more work on the rate-critical path, and the triangular solves are serial
per RE. Kept only to show it is priced; `eval.md` rejects it on the count alone.

## Interfaces (A and B)

Command-response throughout (`systems/command_response.md`): every command a `tx_id`, every result
headed by a response echoing it, one status per command on its own stream.

| connection | kind | carries | width | rate it must sustain |
|---|---|---|---|---|
| A53 → `dist` | bus view `QueueIn` | `SlotCmd {tx_id, prb, U, users}` per filter group | 64-bit | 546 k/s (0.002 words/cycle) |
| estimator → `gram` (via `dist`) | stream | `H` (`B × U` complex), `sigma2` | 128-bit | 0.14 words/cycle |
| FFT → `dist` | stream | `Y`, PRB by PRB, `B × 168` complex | 128-bit, 4 samples a word | **2.94 words/cycle**: one stream cannot carry it -- four, one per engine, or the FFT delivers four streams (open question) |
| `dist` → `apply[j]` | stream | `EngineCmd`, `W` (`U × B`), 7 blocks of `Y` | 128-bit | 0.73 words/cycle each |
| solve → `dist` | stream | `W` (`U × B`) per filter group, with its `tx_id` | 128-bit | 0.14 words/cycle |
| `apply[j]` → `coll` | stream | `EngineResp` + `x_hat` block | 128-bit | 0.18 words/cycle each |
| `coll` → demapper | stream | `x_hat` per PRB, `U × 168` | 128-bit | 0.73 words/cycle |
| statuses → A53 | bus view `QueueOut` | `Status {tx_id, code}` per filter group | 64-bit | 546 k/s |

## Open questions

1. Can the FFT deliver `Y` as four streams (one per engine), or does `dist` take one 512-bit stream?
2. Alternative B depends on a csynth probe (`eval.md`); A on the channel granularity (`spec.md` 1).
3. Should the solve run ahead by one slot (latency, but no stall) or in step with the apply?
