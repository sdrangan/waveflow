---
title: Elaboration
parent: Module Code Generation
nav_order: 2.5
audience: python
applies_to: [HwModule]
api: [elaborate, ElabContext, assert_param_pure, structure_signature, ParamPurityError]
summary: "elaborate(cls, params) is the single sim-free entry that builds an HwModule purely to read its structure — endpoints, sub-modules, interfaces, boundary. The contract is that structure is a pure function of the HwParam/HwConst values, so codegen is keyed by (class, param-set) rather than by any instance; assert_param_pure enforces it by elaborating twice and comparing a name-agnostic signature. Not to be confused with xelab RTL elaboration, which is the same idea one stage later and one language down."
---

# Elaboration

## Concept

Code generation needs a module's **structure** — its endpoints, its sub-modules, the interfaces wiring
them, its boundary. It does not need a running simulation, and it must not depend on one.

`elaborate` is the single entry that provides exactly that:

```python
from waveflow.build.elaborate import elaborate
from examples.fir_block.fir_block import FirBlock

top = elaborate(FirBlock, {"mem_dwidth": 64, "ntap": 32, "samp_w": 16,
                           "samp_i": 2, "unroll_lane": True}, name="fir_block")
```

It constructs the class through its **normal `__init__`** with the given
[`HwParam` / `HwConst`](../flows/parametrization.md) overrides applied — no immutability bypass, no
special path. What differs from an ordinary instantiation is the context it is built in.

## `ElabContext` — a simulation that never runs

An `HwModule` is a `SimObj` and expects a `sim=`. Elaboration supplies an `ElabContext`: a subclass of
`Simulation` that provides exactly what construction needs from the lifecycle — `add_obj` and a SimPy
`env` for the resources endpoints allocate — and nothing else. Calling `run_sim()` on one raises.

The `env` is created **lazily**, so a module that declares its structure without touching the sim
allocates nothing at all. That is not a micro-optimization; it is the invariant made visible. Because
`ElabContext` *is a* `Simulation`, construction behaves byte-for-byte as it did back when codegen sites
wrote `cls(name="_codegen", sim=Simulation())` by hand — which is what this replaced.

## The contract

> A module's structure is a **pure function of its `HwParam` / `HwConst` parameters**. `name`, `sim`,
> and runtime data are elaboration *context* and must not affect it.

When that holds, code generation is:

```text
elaborate(class, param-set)  →  structure  →  C++
```

one output per param-set, **instance-independent**. Any real instance built with those parameters
matches the generated C++ by construction — which is the property that lets a build DAG generate code
from a class it never simulates, and lets [resource attribution](../resource/composite.md) read a
report back against a module graph rebuilt from the same parameters.

It is also what makes *variants* well defined: [`hwgen`](./templating.md) emits one kernel per
param-set by elaborating the class once per override dict.

{: .note }
> [`DataSchema`](../schema/python/fields.md) gets this for free — its structure *is* class attributes
> plus classmethods, readable with no instantiation at all. `HwModule` builds structure
> **imperatively** in `__post_init__`, so what a schema has by construction, a module has only by
> contract. Hence the gate below.

## The purity gate

The contract is enforced, not merely documented. On the first elaboration of a given
`(class, param-set)`, `assert_param_pure`:

1. builds the module **twice**, with *different* names, in *fresh* contexts;
2. reduces each to a `structure_signature` — a canonical token tree that excludes identity, names, the
   sim, SimPy resources and back-references;
3. raises `ParamPurityError` if they differ, naming the **first differing attribute path**.

```text
ParamPurityError: MyBlock structure is not a pure function of its parameters {'nlane': 4}:
two elaborations produced different structure.  Structure (endpoints / sub-components /
interfaces / boundary) must depend only on HwParam/HwConst parameters — not on identity,
global counters, time, randomness, or external mutable state.
  First difference at <root>.sub_comps[2].depth
```

The verdict is cached per `(class, param-set)`, so the gate costs one extra elaboration the first time
and nothing afterwards. Pass `check_purity=False` to skip it where a caller has already verified.

What it catches is anything that leaks non-parameter state into structure: a module-level counter used
to size a buffer, `time`, `random`, or a mutable global read at construction.

### What the signature deliberately ignores

| excluded | why |
|---|---|
| `name`, `sim`, `parent`, back-references | identity and context, not structure |
| SimPy objects | per-build identity; only the type tag is kept |
| `processes`, `action_history`, firing records | runtime scaffolding |
| `_resource_model`, `_timing_model` | see below |

That last row is load-bearing rather than tidy-minded. A module's calibration key is a **digest of this
signature**, so if attaching a [resource model](../resource_model/) changed the signature, the key
would move and every store lookup would miss — and `add_rm` needs the key in order to choose the model
it is attaching. A model is a statement about how hardware was *measured*, not about what it *is*.

## Where it is used

| caller | what it elaborates for |
|---|---|
| `hwgen` , `hwcodegen` | the structure the C++ emitter walks; one kernel per param-set |
| `codegen_check` | answering [`check(source, target)`](./index.md) without an instance |
| `InspectSynthStep` | the module graph a [synthesis report](../resource/composite.md) is attributed against |
| `module_key` | the identity a [calibration record](../calib/modules.md) is filed under |
| `trace_steps` | the two rungs that cost nothing because they are pure elaboration |

## Not to be confused with `xelab` RTL elaboration

Both are called elaboration, and that is not a coincidence — but they run at different stages, on
different languages, and produce different things.

**Elaboration in EDA generally** is the phase after source is parsed and before it is used: the tool
walks the design hierarchy from the top down, creates an instance for every instantiation, resolves
each instance's parameter values, binds instance names to their definitions, connects ports, and
allocates storage for nets. The output is a fully-resolved instance tree. The parameterized
*description* becomes a concrete *design*.

**`xelab`** is Vivado's implementation of that for simulation. After `xvlog` (or `xvhdl`) compiles RTL
sources into a library, `xelab <top>` elaborates and links them into a simulation **snapshot** under
`xsim.dir/`. Waveflow's [XSI rung](../build/xsi.md) runs `xelab -dll`, which emits `xsimk.dll` — a
loadable simulator the C++ BFM drives cycle by cycle.

|  | Waveflow `elaborate` | `xelab` |
|---|---|---|
| Input | an `HwModule` class + `HwParam` values | compiled Verilog / VHDL |
| Resolves | Python parameters into a module graph | HDL parameters into an instance tree |
| Output | a structural stand-in, read then discarded | a runnable simulation snapshot |
| Stage | before C++ is generated | after C-synthesis has produced RTL |
| Cost | microseconds, no toolchain | seconds to minutes, needs Vivado |

Both happen in a full Waveflow build, in that order: Python elaboration produces the C++ that Vitis
turns into RTL, and `xelab` then elaborates *that* RTL so the testbench can drive it. Same idea, two
stages apart.

## See also

- [Module structure](./structure.md) — what "structure" means for codegen: one kernel, endpoints as
  arguments.
- [Parameterization](../flows/parametrization.md) — `HwParam` vs `HwConst`, the inputs elaboration is
  a pure function of.
- [Composite kernels](../resource/composite.md) — elaboration used to read a synthesis report back
  against the design that produced it.
- [XSI Build Rung](../build/xsi.md) — where `xvlog` / `xelab` / the BFM fit together.
