---
title: Module keys and the record store
parent: Model calibration
nav_order: 7
has_children: false
audience: python
api: [module_key, identify, walk_modules, ModuleIdentity, ModuleStore, Record, Provenance]
summary: "Every measurement — a cycle count from cosim, a DSP count from csynth — is a fact about one module in one configuration, and has to be filed where a different design can find it again. The address is the module's STRUCTURE, not its parameter dict, which makes the projection from system parameters onto per-module subsets mechanical rather than hand-declared. Records share one envelope {key, target, source, cost_seconds, payload, provenance} for both timing and resources, and a read verifies provenance rather than trusting the directory name."
---

# Module keys and the record store

A [component residual](../timing_model/component_residual.md) is filed under a component *name* — a string like
`mem_r_stream_framed_task`. That works when the thing being calibrated is one reusable framework
kernel with one shape. It stops working as soon as the same module exists in many configurations and
you want to know which one a number belongs to.

This page covers the addressing layer that replaces the name: a **content-addressed module key**, and
the **record store** that files measurements under it.

{: .note }
> This machinery is not timing-specific. The same keys and the same record envelope carry
> [resource measurements](../resource_model/resources.md); only the *source* of a number differs.

## The key is the structure, not the parameters

```python
from waveflow.calib.module_key import module_key
from examples.fir_block.fir_block import FirCompute

module_key(FirCompute, {"ntap": 32, "samp_w": 16, "unroll_lane": False})
# 'fir_compute-4d3d359e'
```

The readable prefix is for grepping a directory listing; correctness rests entirely on the digest,
which is a SHA-256 over the module's
[structure signature](../comp_codegen/structure.md) — the same canonical, name-agnostic fingerprint
[`elaborate`](../flows/parametrization.md) already uses to check that structure is a pure function of
its parameters.

Keying on structure rather than on a parameter dict buys two things that a parameter tuple cannot.

### The parameter projection becomes mechanical

A system of `N` modules draws its parameters from overlapping subsets: module *i* is a function of
only some of them. Nothing has to declare those subsets — elaborate the top, walk it, and each leaf's
signature already reflects exactly the parameters that reached it:

```python
from waveflow.build.elaborate import elaborate
from waveflow.calib.module_key import walk_modules
from examples.fir_block.fir_block import FirBlock

top = elaborate(FirBlock, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                           "samp_i": 2, "unroll_lane": False}, name="fir_block")
for path, comp, ident in walk_modules(top):
    print(f"{path:32s} {ident.key:26s} {ident.params}")
```

```text
fir_block                        fir_block-d8242bfe         {'mem_dwidth': 32, 'ntap': 32, 'samp_i': 2, 'samp_w': 16, 'unroll_lane': 0}
fir_block.fir_block_rx           fir_cmd_rx-93cb7e21        {'mem_dwidth': 32, 'samp_w': 16}
fir_block.fir_block_memr         mem_r_stream-04919c18      {'inband': 1, 'max_xfer_len': 8, 'mem_awidth': 32, 'mem_dwidth': 32}
fir_block.fir_block_compute      fir_compute-4d3d359e       {'mem_dwidth': 32, 'ntap': 32, 'samp_i': 2, 'samp_w': 16, 'unroll_lane': 0}
fir_block.fir_block_memw         mem_w_stream-cc38eace      {'inband': 1, 'max_fwd_words': 8, 'max_xfer_len': 8, ...}
```

`fir_cmd_rx` sees only `{mem_dwidth, samp_w}`. Both mem-streams see *neither* `ntap` nor `samp_w`.
So a sweep over `ntap × samp_w` re-keys the compute at every point and the memory modules **not at
all** — a 4×4 grid costs sixteen `fir_compute` measurements and **one** `mem_r_stream`. That reuse is
the point of keying this way, and it costs nothing to obtain.

### A realization knob gets its own model, by construction

`unroll_lane` is not a feature to regress against — flipping it is a *different circuit*, with a
different multiplier count and a different loop structure. A parameter-tuple key would invite fitting
*across* it. A structure key forks:

```python
module_key(FirCompute, {"ntap": 32, "samp_w": 16, "unroll_lane": False})  # fir_compute-5ac2b952
module_key(FirCompute, {"ntap": 32, "samp_w": 16, "unroll_lane": True})   # fir_compute-d2418e88
```

## Three things the key refuses to do

A key that is subtly wrong is worse than no key, because every lookup misses and every miss looks like
new work rather than a bug. Three failure modes are therefore errors rather than surprises.

| Refusal | Why |
|---|---|
| **Impure structure** — `ParamPurityError` | Raised by `elaborate`'s existing gate. If structure is not a function of the parameters, there is no well-defined key to compute. |
| **An embedded object address** — `UnstableSignatureError` | A signature that reaches an object with the default `repr` embeds a memory address, so the digest would change every run. The digest is SHA-256 over a canonical serialization, never `hash()` (which is randomized per process for strings). |
| **Unbound ports** — `UnboundModuleError` | A stream endpoint's `queue_size` is `None` until [`Interface.bind`](../interface/stream.md) supplies the channel depth, so the structure is not yet determined. |

That last one deserves its own note, because it looks like pedantry and is not.

{: .warning }
> **FIFO depth is physical.** It costs resources and shapes backpressure, so it belongs in the key —
> and an unbound module is simultaneously un-keyable, un-synthesizable (codegen refuses `depth=None`),
> and un-faithful in pysim (an unbound endpoint simulates with *unbounded* capacity, the condition
> under which backpressure silently disappears). Keying one anyway would file records under an address
> no real composite ever produces.
>
> The corollary matters when you build a standalone harness: bind its streams at **the same depths the
> composite uses**, deliberately. Different depths are genuinely different hardware, and the records
> will correctly refuse to join.

A module's *boundary* ports are exempt — they face outside the design, so their depth is the enclosing
context's to set, not this design's.

## The record store

Measurements are filed as a single envelope, the same one for every quantity:

```python
{"key": "fir_compute-4d3d359e", "target": "resource", "source": "hls_estimate",
 "cost_seconds": 19.5, "payload": {"lut": 1900, "ff": 2100, "dsp": 8},
 "provenance": {"signature": "4d3d359e…", "part": "xc7z020clg484-1",
                "period_ns": 10.0, "tool": "vitis_hls 2025.1"}}
```

They live beside the existing per-component fits, under the same [platform](../platform/identity.md), so the
platform's part/clock identity and its mismatch gate cover them without a second notion of target:

```text
<platform_dir>/
    platform.json                          # part + clk — the single source
    components/<name>/params.json          # a component residual (timing)
    modules/<key>/module.json              # the ModuleIdentity this key resolves to
    modules/<key>/resource/records.jsonl   # its resource measurements
    modules/<key>/timing/records.jsonl     # its timing measurements
```

```python
from waveflow.calib.record_store import ModuleStore

store = ModuleStore(platform.dir)
store.keys()                                          # every module measured
rec = store.best(key, "resource", identity=ident)     # strongest evidence available
store.coverage("resource")                            # {key: {source: count}}
store.total_cost_seconds()                            # what this library cost to build
```

### Three fields that are not bookkeeping

**`source`** names the *fidelity tier* — `hls_estimate` / `vivado_synth` / `vivado_impl` for
resources, `pysim` / `cosim` / `xsi` for timing. Carrying it from the first record means upgrading an
estimate to a post-implementation number later is a **data addition**, not a schema migration.
`best()` uses it to return the strongest evidence without the caller knowing what exists.

**`cost_seconds`** is what an exploration budget is spent from, and what makes a claim like "K
syntheses sufficed for N design points" auditable *from the library itself* rather than from a
notebook. It is recorded, never modelled — [`BuildResult.elapsed_seconds`](../build/index.md) times
every step, and the number is copied from the run that actually happened.

**`provenance`** is what makes a cached entry safe to reuse: the full structure digest, the part, the
clock period, and the tool.

### A read verifies; it does not trust

```python
store.read(key, "resource", identity=ident)   # raises StaleRecordError on a digest mismatch
```

{: .warning }
> A stale cache entry that reports success is worse than no cache. This tree has already been bitten
> by it once — a stale `rtl_fir_block.f` beside a cached `xsimk.dll` makes an XSI run go green while
> proving nothing. In a store shared across designs *and* parameter points, that failure mode goes
> from occasional to constant.
>
> So the short key is a digest *prefix*, but `module.json` holds the **full** digest, and every read
> compares. A record from different hardware raises `StaleRecordError`; two identities under one key
> raise `KeyCollisionError` rather than pooling their measurements into one nonsense model.

Pass `verify=False` only to inspect a store you already know is stale.

## Publishing

The store rides the same two-tier [work → publish flow](../platform/workflow.md) as every other fit: sweeps
write an untracked work directory freely, and `publish_calib` promotes into the tracked library with a
coverage-regression guard — for modules, a count of records, so a thin re-sweep cannot clobber a
library that cost more syntheses to build.

Both halves under `modules/` are published: the identity, because it is what makes a later read
verifiable, and the records, because a synthesis is expensive enough that losing one to a work-dir
wipe is real cost. Unlike a firing tree there is no churny raw tier here to leave behind.

## See also

- [Resource measurements](../resource_model/resources.md) — where resource records come from.
- [Platforms](../platform/identity.md) — the part/clock identity these records are keyed under.
- [The calibration workflow](../platform/workflow.md) — the work → publish split.
