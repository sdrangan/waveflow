# How to model Vitis' DATAFLOW in Waveflow

## Background

Vitis has very limited ability to model different concurrency paradigms. In contrast, Waveflow has
essentially arbitrary concurrency with processes pending on channels. One way to reconcile the two is
to recognize that HwComponent classes fall into a few categories:

- **Custom Vitis kernel classes** — the HwComponent synthesizes as a single Vitis function, either
  free-running or `ap_ctrl`-started. Every class we have written so far is of this form.
- **Pre-made Vivado blocks** — Vivado IP with pre-defined interfaces, or thin wrappers around them,
  that the user does not author.
- **Block graphs** (need a better name) — a collection of custom Vitis kernels, pre-made Vivado
  blocks, and/or nested block graphs with connectivity. These can only be simulated in Vivado (RTL
  sim, slow; no C-sim). See [[project-vivado-ipi-system-flow]].

## What Vitis DATAFLOW actually is

Vitis DATAFLOW is **not** a general concurrency paradigm (as we first hoped). It is **a set of
sequential sub-functions that Vitis pipelines across the iterations of a bounded loop.** The local
arrays between the sub-functions become ping-pong (PIPO) buffers: while iteration `j` runs the
compute stage, iteration `j+1` can run the load stage into the *other* buffer.

This was validated end-to-end on the 1-D interleaver (`Y[i] = X[P[i]]`, a load/gather/store kernel);
the hand-written sandbox at `examples/interleaver/sandbox/il_1d/` is the **golden reference** for the
generated form. Measured (Vitis 2025.1 cosim, `N=256`, `NJOBS=4`):

| MEM_DW | LW | period    | regime       |
| ------ | -- | --------- | ------------ |
| 32     | 1  | 540 ≈ 2n | load-bound   |
| 64     | 2  | 286 ≈ n  | gather floor |

with the ping-pong overlap emerging (load(j+1) ∥ store(j)) — throughput is `max` over the stages, not
their sum.

### The pipelining is real but fragile — it fails silently

Getting the overlap took real debugging; per-stage csynth reported `II=1` everywhere while the jobs
were actually serialized, and the failure was only visible in the cosim VCD (per-job burst windows →
period). The canonical-form discipline that keeps the overlap:

1. **Ping-pong ⇒ a bounded loop.** The buffers must be locals declared inside a bounded `for` loop
   with `#pragma HLS DATAFLOW` in its body. A free-running `while(!done)` cannot PIPO a shared array.
2. **No stage may both *read* and *write* a shared memory port.** A store stage that does even a dead
   read-modify-write *read* on the shared `mem` bundle makes Vitis assume `store(j)` might alias
   `load(j+1)` and serialize them — killing the overlap. Neither `restrict` nor
   `#pragma HLS dependence inter false` fixed it on one interface; making the store **pure-write**
   did. So the canonical form is: **load reads, store writes, compute touches no external memory.**
3. **Vectorized serializers must be pipelined and statically bounded.** A runtime-bounded peel loop
   synthesizes a 2^30 worst-case trip that poisons the whole DATAFLOW interval; an unpipelined
   group-walk stalls the wide read.

Groundwork already landed for rule 2/3 (see `waveflow/hw/arrayutils.py`): `write_array_slice` is now
**pure-write by default** (whole-word writes, tail lane clobbered — safe under the word-granular
MemMgr contract), with `write_array_slice_rmw` as the rare read-modify-write variant; the LW>1 read
is peeled + pipelined. This is what lets a load/compute/store DATAFLOW keep its ping-pong on a single
memory port. Validated: interleaver cosim (2n→n, above) + 12/12 arrayutils slice Vitis cases.

## The Python abstraction: `VitisDataflow` / `DataflowStep`

The design is deliberately **very constrained** so it maps to Vitis DATAFLOW without surprises. A
DATAFLOW region is declared as an ordered list of steps:

```python
class Interleaver(HwComponent):
    ...

    def __post_init__(self):
        """Create the dataflow region."""
        load_step  = DataflowStep(self.load,    inputs=['cmd'], outputs=['X', 'P'],
                                  if_dir={'gmem': 'R'})
        comp_step  = DataflowStep(self.compute, inputs=['X', 'P'], outputs=['Y'])
        store_step = DataflowStep(self.store,   inputs=['Y'],
                                  if_dir={'gmem': 'W'})
        self.dataflow = VitisDataflow([load_step, comp_step, store_step])
```

Each `DataflowStep` wraps a process generator with declared edges. The region is modeled in pysim as
`N` processes connected by `simpy.Store` queues (output of step `n` → input of step `n+1`), and
invoked per-job from any other process generator:

```python
def run_proc(self):
    while True:
        cmd = yield from self.s_in.get(self.Cmd)
        yield self.dataflow.put(cmd)          # push cmd into the first queue; backpressure paces jobs
```

```python
class VitisDataflow(SimObj):
    def __post_init__(self):
        nsteps = len(self.steps)
        self.q = [simpy.Store(self.env, capacity=self.pipo_depth) for _ in range(nsteps)]
        for i in range(nsteps):
            self.process(self.step_proc(i))

    def step_proc(self, i):
        while True:
            x = yield self.q[i].get()
            y = yield from self.steps[i].run(x)          # step's own timing model applies here
            if i != len(self.steps) - 1:                 # last step has no output queue
                yield self.q[i + 1].put(y)

    def put(self, input0):
        yield self.q[0].put(input0)
```

### `inputs`/`outputs` vs `if_dir` — two different roles

Keep these distinct; both describe a step's edges but at different scopes:

- **`inputs` / `outputs`** — the **internal** dataflow channels *between steps* (`X`, `P`, `Y`). Each
  gets a `simpy.Store` in pysim and becomes a loop-local PIPO array (or `hls::stream`) in codegen.
- **`if_dir`** — the **external** interface endpoints the step touches at the region *boundary*
  (`gmem`, or an external AXI-Stream). These become the kernel's `m_axi`/`axis` ports and are subject
  to the shared-port canonical rule (rule 2 above).

### Direction as capability (not just annotation)

`if_dir` does not merely annotate an endpoint — it hands the step a **restricted view** that
structurally lacks the wrong methods, so misuse fails fast in pysim instead of diverging silently
from Vitis.

- Tag endpoint methods **once**, on the `InterfaceEndpoint` subclass where they are defined, with
  `@port_read` / `@port_write` (e.g. `MemMaster.read_slice`/`read_lane` are reads,
  `write_slice` is a write; a master FIFO's `put` is a write, a slave's `get` is a read). The tags
  are generic — every endpoint type participates, not just memory.
- `if_dir={'gmem': 'R'}` builds a **read proxy** exposing only `@port_read` methods; `'W'` a write
  proxy; `'RW'` the full endpoint (a **loud escape hatch** — allowed outside dataflow, or for a step
  you knowingly accept will serialize). If `load` calls `write_slice`, pysim raises
  `AttributeError: ReadPort('gmem') has no method 'write_slice'` **at the call site during the
  sandbox run**, long before Vitis.
- The same declaration drives a **second, static** guarantee: a read-only bound endpoint makes
  codegen emit `const ap_uint<W>*`, so a stray write is a C++ compile error on **every** path
  (data-dependent or not). pysim proxy = early local feedback; `const` = static guarantee; together
  they are airtight.

The payoff: `load`→read-proxy and `store`→write-proxy means **no single step can both read and write
the shared bundle** — which *is* the canonical-form rule (rule 2). The abstraction makes the
serializing bug unexpressible; the constraint is the feature.

Direction is a property of the **step's binding** to the port, not the port itself: the same
`MemMaster` may be used read-write by a monolithic (non-dataflow) kernel. `if_dir` is therefore
optional for endpoints whose direction is already fixed by type (master/slave streams) and
**mandatory (or defaulted-with-a-warning) for R/W-capable endpoints** (memory masters) — forgetting
it there is exactly the silent-serialization trap.

### The overlap is the queue depth (fit-free timing)

The `simpy.Store` capacity **is** the PIPO depth. `capacity=2` lets the producer run one job ahead —
reproducing the measured ping-pong overlap — while `capacity=1` models no overlap. So the region's
LT throughput (`max` over stages) emerges from the queue depths with **zero end-to-end fitting**.
This dovetails with two-level calibration ([[project-two-level-calibration]]): each step carries its
own timing model — bus-per-word (platform) for the load/store steps, calibrated compute for the
middle step — and the region throughput falls out of the graph. A per-edge **channel type**
(PIPO whole-buffer vs FIFO stream) selects the completion semantics: PIPO → the consumer waits for
the producer's *whole* buffer; stream → overlap at element granularity.

## Codegen mapping

**The DATAFLOW pragma must be in the loop body (measured), but the command read can be fused into
it.** Sandbox de-risk (interleaver, MEM_DW=64, `NJOBS=4`; `interleaver_c{2,3}.cpp`):

| form                                                                       | period @dw64 | overlap       |
| -------------------------------------------------------------------------- | ------------ | ------------- |
| **C1** `#pragma HLS DATAFLOW` in the loop body, separate READ loop | 286 ≈ n     | overlaps      |
| **C2** region as a called `il_job()`                               | 813          | `[0,0,0,0]` |
| **C3** command read fused *inside* the DATAFLOW loop body          | 287 ≈ n     | overlaps      |
| **C4** command read *before* the pragma (pragma not first stmt)    | 287 ≈ n     | overlaps      |
| **C5** `while(1)` + sentinel `break` (data-dependent exit)       | 814          | `[0,0,0,0]` |
| **C6** sentinel `break` in a **parse** loop, then counted `for`  | 286 ≈ n     | overlaps      |

Findings: **(1)** C2 (region as a called function) is bit-exact but ~2.8× slower — Vitis runs each
call to completion, so the cross-job ping-pong is lost. The region cannot be a standalone
`dataflow(cmd)` function; the pragma + inlined stages must live in the loop body. **(2)** C3/C4 show
the per-job command read (`s_in.read()` → n + byte addresses) can sit *inside* the DATAFLOW loop
body — before or after the pragma (it is scope-level) — with the overlap intact; no separate parse
loop needed. **(3)** C5 shows a data-dependent early exit (`while(1)` + `break`) compiles bit-exact
(no csynth complaint) but **serializes** — the ping-pong is lost. The overlap needs a **counted
`for (j < nj)` with one clean exit**; the runtime bound `nj` is fine (C1/C3/C4 all use it), it is the
mid-body `break` that defeats iteration pipelining. **(4)** C6 validates the fix: the same sentinel
protocol with the `break` in a **parse** loop (which only counts jobs into bounded arrays, no
dataflow work) followed by a counted DATAFLOW `for` recovers the full overlap (286, identical to C1).

### Host contract: send the count, don't buffer the batch

The **fundamental law**: m_axi + DATAFLOW overlap ⟹ a counted loop ⟹ the kernel needs `nj`. That
gives three host contracts, and the default is the streaming one:

| host knows…                          | form                         | first-job latency        | overlap |
|--------------------------------------|------------------------------|--------------------------|---------|
| **`nj` upfront** (batch dispatch)    | **count header** (C3/C4)     | starts on `cmd0`         | ✓       |
| only a **sentinel** (`nj` unknown)   | parse-then-count (C6)        | after full batch (barrier) | ✓     |
| unbounded, `nj` never known          | outer chunk loop             | per-chunk                | ✓ / seams |

**Default = count header (C3/C4).** The kernel reads only the count, then reads each job's command
*inside* the counted loop, so job 0 starts as soon as `[nj][cmd0]` arrives — the host can still be
streaming later commands, and the FIFO decouples them. No buffer-all barrier.

```cpp
unsigned nj = s_in.read();                 // read only the COUNT (one word)
for (j = 0; j < nj; ++j) {
    #pragma HLS DATAFLOW
    <read cmd[j] from s_in>                 // per-job read, inside the loop (C3/C4-validated)
    load(cmd, ...); compute(...); store(...);
}
```

The **sentinel** form (C6, `PARSE: while(1){ ...; if (sentinel) break; nj++ }` then the counted
`for`) is a *fallback* only when `nj` cannot be known before the batch: it buffers the whole command
batch into a bounded `MAX_JOBS` array first, so the first job waits for the full batch (barrier). A
truly unbounded stream needs an outer chunk loop (overlap within a chunk, a drain seam between
chunks). So prefer the count header; reach for the sentinel/chunking only when forced.

`dataflow.put(cmd)` is the pragma + inlined stages; it is *not* a C++ function call. The pysim
`put()`-into-queues model is unaffected — only the C++ shape is constrained.

| Python                                 | Generated Vitis                                                            |
| -------------------------------------- | -------------------------------------------------------------------------- |
| the`for/while: … dataflow.put(cmd)` | `for (j…) { #pragma HLS DATAFLOW; <read cmd>; <inlined steps> }`        |
| `cmd = s_in.get(Cmd)` in that loop   | `s_in.read()` fused inside the loop body (C3), before the stages         |
| internal`outputs`/`inputs` edge    | loop-local PIPO array (or`hls::stream` if channel type = stream)         |
| `DataflowStep.proc`                  | a sub-function called**inside the loop body** (not a wrapper region) |
| `if_dir={'gmem':'R'}`                | `m_axi` port, `const ap_uint<W>*`; reads via `read_array_slice`      |
| `if_dir={'gmem':'W'}`                | `m_axi` port; writes via `write_array_slice` (pure-write)              |

This is plausible precisely because each step is already a synthesizable function with a clearly
labeled region boundary. See [[project-fir-control-driven-codegen]] and
[[project-dataflow-composition-direction]].

## Implementation plan

The hand-written interleaver sandbox (`examples/interleaver/sandbox/il_1d/`) is the golden reference
throughout: every generated artifact is accepted only when it is **bit-exact in cosim** and matches
the sandbox's throughput (2n→n, ping-pong overlap visible in the VCD). Phases are ordered by
dependency; each has an explicit acceptance gate.

### Phase 1 — Interface direction as capability

**Goal:** direction-restricted endpoint views + `const` codegen, independent of dataflow.

- Add `@port_read` / `@port_write` method tags to the `InterfaceEndpoint` base; tag the concrete
  endpoints (FIFO/AXIS master + slave; memory master `read_slice`/`read_lane`/`write_slice`).
- `endpoint.as_dir('R'|'W'|'RW')` → capability proxy (read/write subset, or full).
- Codegen: a read-only bound endpoint emits a `const` pointer in the kernel signature.
  **Acceptance:** unit tests — a read proxy raises on any write call; a write proxy raises on any read
  call; the generated signature carries `const` for an `'R'` binding. No Vitis required.

### Phase 2 — `VitisDataflow` / `DataflowStep` pysim model

**Goal:** simulate a region as N processes + queues with fit-free overlap timing.

- `DataflowStep(proc, inputs, outputs, if_dir)`; `VitisDataflow(steps)` `SimObj` with a
  `simpy.Store` per internal edge (`capacity = pipo_depth`, default 2), `put()`, backpressure.
- Per-edge channel type (PIPO vs stream) → completion semantics; per-step timing model (reuse
  `waveflow/calib/`).
  **Acceptance:** pysim of the interleaver region reproduces the cosim throughput (period ≈ 540 @dw32,
  ≈ 286 @dw64) within tolerance, with the overlap **emerging from queue depth** (no end-to-end fit) —
  Gate against the sandbox cosim numbers.

### Phase 3 — Dataflow codegen

**Goal:** generate the canonical Vitis kernel from a `VitisDataflow`.

- Emit `for (j<nj) { #pragma HLS DATAFLOW; <steps> }`; internal edges → loop-local PIPO arrays /
  `hls::stream`; `if_dir` endpoints → `m_axi`/`axis` ports (`const` for `'R'`); stores via pure-write
  `write_array_slice`.
- Command/control plumbing via the existing control-driven extraction (`on_start`, `s_axilite`).
  **Acceptance:** the **generated** interleaver kernel cosims **bit-exact** and matches the
  hand-written sandbox throughput (2n→n, ping-pong overlap in the VCD). Structurally diff generated vs
  golden.

### Phase 4 — Graduate the interleaver as a Waveflow example

**Goal:** the full example, `shared_mem`/`hist` anatomy.

- `IlAccel(HwComponent)` + `IlCmd` schema (`MemAddr` byte addresses) + the `VitisDataflow` region +
  generated hook; `BuildDag` + `run_dag` CLI; LT sim consumer; MEM_DW sweep.
  **Acceptance:** example builds + cosims; LT sim matches cosim (Gate); MEM_DW sweep reproduces 2n→n;
  committed figure regenerable without Vitis ([[project-committed-figure-workflow]]).

### Phase 5 — Documentation (optional, gated on 4)

**Goal:** a guide page in the hardware-gen / dataflow arc.

- "Composing a load-compute-store DATAFLOW kernel": the canonical-form rules, the capability model,
  the interleaver walkthrough. Grounding for AI-assisted codegen ([[project-hook-authoring-docs]]).

## Open questions

- **Channel type inference.** Can PIPO-vs-stream be inferred (whole-array output ⇒ PIPO; element
  `put` in a loop ⇒ stream), or must each edge declare it?
- **Non-linear regions.** Vitis DATAFLOW allows a DAG (single-producer/single-consumer per channel),
  not just a line. Support fan-out/join, or restrict to linear for v1?
- **PIPO depth > 2.** Is `capacity` ever > 2 useful (deeper pipelining), and does Vitis honor it
  (`#pragma HLS STREAM depth=` / PIPO sizing)?
- **`if_dir` defaulting.** Exact rule for when direction is inferred vs required — and whether an
  R/W-capable endpoint bound without `if_dir` is an error or a warning-with-`'RW'`.
- **Compute step with no external memory** — confirm the middle step is forbidden any `if_dir` memory
  endpoint (it should only touch internal channels), matching rule 2.
