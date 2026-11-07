---
title: The design context
parent: Agentic DSE
nav_order: 2
audience: python
api: [DseAdaptor, LtbfCgMemAdaptor]
summary: "How a design is described for DSE. A subclass of DseAdaptor defines the design's context: its parameter space and constraints, its prose, how to build its system at a design point, and its golden model. It also implements what runs its own build flow: each part's identity at a point (the service's cache keys), pysim, synthesis per part, csynth and the RTL run. The base class derives the rest: defaults and legality; the architecture (the sub-top tree and its interconnects, the same at every point, and checked to be); and which parts each parameter rebuilds. Each method, what it returns and who uses it, with the LTBF system as the example. Once the class is defined, the generator renders DSE.md, the file passed to the agent: what it contains, where each section comes from, and the rules that keep it honest."
---

# The design context

The base class and the LTBF system's adaptor are in
[`examples/dse_cg`](../../../examples/dse_cg/README.md), written through Phase 3 of
[`plans/dse_cg.md`](../../../plans/dse_cg.md); the replay backend and the experiment's runs are later
steps.

A design is described for DSE by one Python class, a subclass of **`DseAdaptor`**
([`adaptor.py`](../../../examples/dse_cg/adaptor.py)). The class defines the design's **context**:
what its parameters are, which values are legal, what the system is and how it is built, and how to
evaluate it. Everything that DSE needs from a design comes through this class: the file that
describes the design to the agent, and the [service](service.md) that evaluates the agent's choices.

Why a class rather than a config file: a config file restates facts that already live in the design's
Python, and a restatement drifts. Someone widens a parameter's range in the code and forgets the file,
and the agent searches the old range. A class can import those facts instead of copying them, and can
derive what nobody should have to write down, such as the parts of the hardware and which parameters
touch each. This follows Waveflow's rule that Python is the single source of truth for hardware.

## The class

You subclass `DseAdaptor` and implement what only your design can know:

```python
from examples.dse_cg.adaptor import DseAdaptor

class LtbfCgMemAdaptor(DseAdaptor):
    name = "ltbf_cg_mem"
    platform = "xczu48dr_250mhz_vitis2025_1"

    def space(self): ...                       # parameters, constraints, workloads
    def context(self): ...                     # the prose that describes the design
    def build_system(self, params): ...        # the HwSystem at a design point
    def golden(self, params, workload): ...    # the untimed bit-exact simulation
```

The LTBF system's is [`ltbf_cg_mem.py`](../../../examples/dse_cg/ltbf_cg_mem.py). The members fall in
three groups:

| | member | what it gives |
|---|---|---|
| **implement** | `name`, `platform` | the design's name; the calibration platform its models were fitted on |
| | `space()` | parameters, constraints, workloads, as data |
| | `context()` | the prose that describes the design |
| | `build_system(params)` | the `HwSystem` at a design point |
| | `golden(params, workload)` | the untimed bit-exact simulation |
| | `BUILT`, `MODELLED`, `TOP` | which parts are built and which modelled, each described; the system top's name |
| **implement for the tools** | `identities(params)`, `hls_identities(params)`, `sim_identity(params, workload)` | what each part, each HLS kernel and an RTL run would be at a point: the service's cache keys |
| | `pysim(params, workload)` | the timed simulation with calibrated models |
| | `synth(node, params)` | the synthesis of one part of the architecture |
| | `rtlsim(params, workload)` | the RTL simulation of the DUT |
| | `hls_estimate(kernel, params)` | the HLS compiler's estimates for one kernel |
| **provided** | `defaults()`, `fill()`, `legal()`, `legal_with()` | defaults and legality |
| | `spec(params)`, `architecture_at(params)`, `architecture()` | the parts of the DUT and how they connect, the same at every point |
| | `node_of(name)` | the part a sub-top or instance belongs to (override to group a memory's banks) |
| | `rebuilds()` | which parts each parameter rebuilds |

The split follows what can be derived. `defaults`, `fill` and `legal` follow from `space()`. The
architecture and `rebuilds` follow from `build_system` and the identities, because a Waveflow system
already knows how its DUT is cut into parts. The tool methods and the identities are the design's,
because they drive its own build flow (for the LTBF system, the example's `hw/build.py`): what a
part's identity hashes is what that flow would compile.

The rest of this page goes through the methods in that order, then shows how the class becomes the
file the agent reads.

## Methods you implement

### `name` and `platform`

Two class attributes. `name` identifies the design to the service and in the agent's files.
`platform` names the calibration platform whose timing and resource models `pysim` uses (here the
`xczu48dr` at 250 MHz, Vitis 2025.1, see [Model calibration](../calib/index.md)).

### `space()`: the parameters, constraints and workloads

`space()` returns plain data: every parameter with its kind, legal values, default and meaning; the
constraints; and the workloads. The LTBF system's restates one table, `GRID`, so the resizing rule
of the plan edits that table and nothing else:

```python
GRID = {  # name: (kind, values, default, the parts a new value rebuilds, description)
    "R": ("design", (2, 4, 8), 4, ("sys",), "systolic array rows"),
    "W": ("design", (12, 16), 16, ("sys", "vec"), "operand width of the fixed-point formats, bits"),
    "banks": ("design", (1, 2), 1, ("xbar",), "Shared Mem's banks, each on its own crossbar port"),
    "mem_rate": ("design", (2, 1), 2, (), "Shared Mem's cycles per read word"),
    "t": ("workload", (2, 3), 3, (), "CG iterations of the long-term update"),
    ...
}

def space(self) -> dict:
    return {
        "parameters": [{"name": n, "kind": k, "values": list(v), "default": d, "description": t}
                       for n, (k, v, d, _parts, t) in GRID.items()],
        "constraints": [{"rule": f"(R, C) in {list(SHAPES)}",
                         "why": "the systolic shapes built and calibrated at c 8, Mmax 16"}],
        "workloads": [
            {"name": "long_term", "fixes": {"drop": 1, "sector": 1}, "valid_for": ["t_upd_ms"]},
            {"name": "accuracy", "fixes": {"drops": "11-100 (held out from the profiling)"},
             "valid_for": ["median_loss_db", "p90_loss_db"]},
        ],
    }
```

The table's fourth column is the plan's claim; `rebuilds()` computes the same from the parts'
content, and a test checks that they agree.

- A **design** parameter changes the hardware; a **workload** parameter changes only what the
  hardware is asked to do ([The DSE problem](problem.md#parameters)).
- A **constraint** is a Python expression over the parameter names, with a sentence saying why. It
  may mix design and workload parameters.
- A **workload** fixes the workload parameters for one kind of measurement and names the metrics it
  is valid for.

`space()` is the one method everything else reads: the agent's `dselib.space()` returns it, the
service checks points against it, and the generator turns it into the parameters section of the
agent's file.

### `context()`: the prose

`context()` returns the few things only a person can write:

```python
def context(self) -> dict:
    return {
        "title": "LTBF receiver on a shared-memory accelerator",
        "description": "An uplink receiver for a 64-antenna base station ...",
        "data_flow": "The long-term update of one sector (the `long_term` workload) is a job graph "
                     "the processor runs ... the processor writes each job into the engine's Job "
                     "FIFO and learns of its end through the Resp hub's interrupt ...",
        "docs": [("the systolic engine", "docs/guide/dsp/systolic/systolic_engine_mem.md"), ...],
        "workloads": {"long_term": "One sector's long-term update ... Its time is T_upd.", ...},
        "metrics": [
            {"name": "t_upd_ms", "unit": "ms", "from": ["pysim", "rtlsim"],
             "description": "one sector's long-term update, at 250 MHz"},
            ...
        ],
        "modelled": [
            {"part": "Shared Mem", "pysim": "a memory model: `banks`, `mem_rate`",
             "rtl": "the framework's memory RTL, simulated (one leaf per `mem_rate`)",
             "resources": "block RAM by formula (Vivado's mapping of the span); no LUT, FF, DSP"},
            ...
        ],
    }
```

`modelled` lists the parts that are simulated but never synthesized (for the LTBF system, Shared Mem
and the Ingest DMA): their model in pysim and at RTL, and how their resources are counted, so the
agent knows what a synthesized total includes.

`data_flow` is the one part of the architecture that is written by hand: how work moves through the
parts, in what order, and who tells whom when something is done. The parts and their connections
are derived (`architecture()`, below); the story of a job through them is not.

Keep it short and factual: what the system does and how work moves through it, as you would explain
it to a new engineer. Do not put advice here ("try small arrays first"). Advice belongs in the
[skill](skill.md), and in the experiment it must be the same for both arms. Do not put the task here
either: the objective is the prompt.

### `build_system(params)`: the system at a design point

`build_system` returns the [`HwSystem`](../systems/rtl.md#the-system) for one design point: the
simulation, the DUT and its testbench, built with those parameter values. It is the bridge from the
DSE problem to the rest of Waveflow, and it is what lets the base class derive so much: the system
can be simulated in pysim, its DUT can be cut into parts, and each part has an identity.

For the LTBF system it is `build(inputs(SysParams(...)))` of
[`hw/system.py`](../../../examples/ltbf_cg_mem_dse/hw/system.py) at the point. `params` arrives
filled: every design parameter present, defaults included.

### `golden(params, workload)`: the untimed bit-exact simulation

`golden` runs the design's bit-exact model with no timing: the systems team's simulation. It returns
the accuracy metrics for the workload, the body of `dselib.golden`:

```python
{"status": "ok", "workload": {"name": "accuracy", "t": 3, "target": "-6_14"},
 "median_loss_db": 1.537, "p90_loss_db": ..., "ues": 1969, "drops": 90, "seconds": ...}
```

It has no default because no general rule produces it: the bit-exact model is the design's own. For
the LTBF system it is the fixed-point chain of
[`examples/ltbf_cg_mem_dse/ltbf_fixed_chain.py`](../../../examples/ltbf_cg_mem_dse/ltbf_fixed_chain.py),
with the design's formats, scored against exact floating-point LTBF over the 90 held-out drops at
the workload's `t` and `target`. The per-user losses are computed once per (`W`, `form`, `target`)
and cached, so a call is a lookup.

## Methods that run the tools

These drive the design's own build flow, so the design implements them. The LTBF system's run the
example's flow (`hw/build.py`'s `variant_dag`) in a build root per HLS configuration.

### `identities`, `hls_identities`, `sim_identity`: the cache keys

`identities(params)` gives each built part's **identity** at a design point: a hash of what would be
synthesized -- the sub-top's Verilog and its HLS tops' generated sources with every header they
include; for the crossbar, the top with every sub-top a black box and the crossbar's configuration.
It is cheap: it builds the Python system and generates the HLS, and synthesizes nothing. Only the
service uses it: a part whose identity the ledger already holds costs nothing, so two design points
that share a part share its synthesis. The agent never sees an identity.

`hls_identities(params)` is the same per HLS kernel, the key of `hls_estimate`; it is finer than
`identities` because one part may hold several kernels. `sim_identity(params, workload)` is the key of
`rtlsim`, in two halves: `build` (every part's identity, the modelled ones included: what the RTL is
elaborated from) and `run` (the build, the design parameters that rebuild nothing, the workload). A new
`build` pays elaboration and simulation; a new `run` on an elaborated build pays simulation alone.

### `pysim(params, workload)`: the timed simulation

`pysim` returns everything `golden` does, plus cycles, per-block activity and resources, each
approximate number with a confidence level. The LTBF system's runs `build_system`'s system in pysim
on the workload, checks it bit for bit against the fixed-point golden, and prices resources with the
calibrated models (`estimate_system`, each engine by its own resource model), plus Shared Mem's
block RAM by formula. The units' models were fitted to csynth's estimate, and the parts' glue, the hub
and the crossbar have none, so a system-level integration term
([`integration.py`](../../../examples/dse_cg/integration.py)) maps each part's estimate to Vivado's
synthesis: one form a part and resource, fitted on the six points of plan step 2c and nowhere else
(the points the grid's pre-registration excludes from scoring). `pysim` reports the integrated total;
the units' sum stays beside it (`total_units`).

Its study also has hand-fitted models: per-part resource and
per-job cycle models ([`hw/models.py`](../../../examples/ltbf_cg_mem_dse/hw/models.py)) and a
job-level simulator ([`hw/jobsim.py`](../../../examples/ltbf_cg_mem_dse/hw/jobsim.py)). They are kept
as a comparison, not used as `pysim`: the experiment has to measure Waveflow's generic pysim, not a
model written for one design. Override `pysim` only when a design cannot be simulated as a system.

### `synth(node, params)`: one part's synthesis

`synth` synthesizes one part of the architecture, with everything under it, out of context, and
returns its resources, its timing and a per-instance utilization report. The LTBF system's uses
Waveflow's per-part route: `system_ooc.synth_sub_ooc` for a device's sub-top, and `synth_top_ooc` for
the crossbar node (the top with every sub-top a black box: the crossbar IP and the glue), each cached
by content, on the `xczu48dr` at 4 ns.

Only the service's live backend calls it, and only for a part the ledger does not hold yet
([The service](service.md)). A design point's resources are the sum of its parts, each synthesized on
its own. That sum differs a little from one synthesis of the whole DUT, which can optimize across the
boundaries between parts; the per-part sum is what a DSE builds and pays for, so it is the measure.

### `rtlsim(params, workload)`: the RTL simulation

`rtlsim` simulates the whole DUT at RTL on a workload and returns the measured cycles, a check that
the RTL is bit-exact with pysim (every Job FIFO and Ctrl write, every Resp record per source, every
data burst, pysim's end within 5%), the per-block activity, and the activity table as an artifact.
The LTBF system's runs Waveflow's system flow on `build_system`'s system through XSI
([Simulating at RTL](../systems/rtl.md)), which is incremental: a new workload on a design already
elaborated pays for simulation alone. Like `synth`, only the service's live backend calls it.

### `hls_estimate(kernel, params)`: the HLS compiler's estimates

`hls_estimate` C-synthesizes one HLS kernel (an HLS top of the DUT) and returns the compiler's
report: estimated resources, II, and latency where the compiler can compute one (`None` for a
free-running kernel whose latency depends on its streams). Only HLS kernels have one; the
hand-written adaptors and the vendor crossbar do not. The LTBF system's runs the system flow's
`csynth` step for that top, the same step that runs before every synthesis, so measuring the ground truth
records these estimates as a by-product.

It is in both arms of the experiment: it is the quick look a traditional hardware team already has.

## Methods provided

### `defaults()`, `fill(params)`, `legal(params, workload)`

| method | what it does |
|---|---|
| `defaults()` | every parameter's default, from `space()` |
| `fill(params)` | a design point with its missing parameters set to their defaults; a name that is not a parameter is an error, so a typo cannot silently evaluate the default |
| `legal(params, workload)` | the first constraint a point breaks, or `None`; parameters not given are taken at their defaults, so a rule that mixes design and workload parameters is checked like any other |
| `legal_with(point)` | the point completed to a legal design that keeps its values, the other parameters tried from their defaults outward (`R` 2 alone leaves the systolic shapes, so `C` moves to 4) |

### `architecture()`: the parts and how they connect

The DUT is not synthesized as one piece. It is cut into parts
([Devices: the top's sub-tops](../build/xsi_system.md#devices)):

- a **device** for each kernel, a *sub-top*: the kernel with the views, adaptor and bus writers that
  belong to it;
- the **memory**, another sub-top (one a bank), if it is inside the DUT -- in the LTBF system it is,
  but modelled: simulated, never synthesized;
- the **crossbar** between them, vendor IP in the system top rather than a sub-top, but a part of
  its own all the same.

`architecture()` returns that tree, each part with its kind and the modules it holds, and the
**interconnects** between the parts: the crossbar's masters and slaves, the stream links that cross
from one part to another, the interrupt lines, and what crosses the DUT boundary. It walks
`spec(params)`, the `SystemTopSpec` of `build_system`'s system, at the defaults; `node_of` names the
part each sub-top belongs to, so Shared Mem's banks are one part. Nothing about it is written by hand
except the data-flow prose of `context()`.

In parametric DSE the architecture is **the same at every point**: the same parts, connected the same
way, with only the sizes inside them changing (`architecture_at(params)` reports them: the memory's
banks, the crossbar's ports). So `architecture()` is computed once, and checked: the tree is built at
the defaults and at each parameter's extreme values, and if the shape differs, the adaptor is
refused. A "parameter" that adds or removes a part is an architectural choice, outside
this kind of DSE.

### `rebuilds()`

`rebuilds()` compares the identities at the default parameters and with one parameter moved: the parts
whose identity changed are the ones that parameter rebuilds. A parameter that reaches no part (a
modelled memory's speed, a scheduler setting) rebuilds nothing and costs only an RTL run. It is how `DSE.md` can say which parts
each parameter touches, and nobody writes that table, so it cannot be wrong.

| | what | depends on the point? | who sees it |
|---|---|---|---|
| `architecture()` | the parts and their connections | no | the agent, in `DSE.md` |
| `rebuilds()` | which parts each parameter touches | no | the agent, in `DSE.md` |
| `identities(params)` | what each part would synthesize | yes | the service only |
| `hls_identities(params)` | what each HLS kernel would compile | yes | the service only |
| `sim_identity(params, workload)` | what an RTL run would elaborate and play | yes | the service only |

## Generating `DSE.md`

Once the class is defined, one call generates the key file, **`DSE.md`**, which is what the agent is
given about the design:

```python
from pathlib import Path
from examples.dse_cg import brief
from examples.dse_cg.ltbf_cg_mem import LtbfCgMemAdaptor

brief.write_context(LtbfCgMemAdaptor(), Path("examples/dse_cg/DSE.md"))
```

The agent reads `DSE.md` because the [skill](skill.md) tells it to: the first argument of
`/dse examples/dse_cg/DSE.md <objective>` is this file. The skill holds the method, the same for every
design; `DSE.md` holds everything specific to this one; the prompt holds the task.

### What it contains

The generator fills the template [`DSE.md.tmpl`](../../../examples/dse_cg/DSE.md.tmpl) from the class:

| section of `DSE.md` | what the agent learns | from |
|---|---|---|
| title, design, platform | which design, on which calibration platform, what it does | `name`, `platform`, `context()` |
| **Architecture**: data flow | how work moves through the parts | `context()` |
| **Architecture**: the tree | the parts, what each holds, which parameters each depends on | `architecture()`, `rebuilds()` |
| **Architecture**: interconnects | how the parts are connected | `architecture()` |
| **Architecture**: modules | each module, grouped by part: its class, what it does, links to its docs and source | `architecture()`; docs found by the `api:` list in each guide page's front matter |
| **Parameters** | name, kind, legal values, default, meaning, which parts a new value rebuilds | `space()`, `rebuilds()` |
| constraints | which combinations are legal, and why | `space()` |
| **Workloads** | each workload, what it fixes, which metrics it is valid for | `space()` |
| **Metrics** | each metric, its unit, which evaluations give it | `context()` |
| **Costs** | the tool time of each evaluation, and how many jobs run at once | recorded: the lab machine's measured times, stored with the replay table |

Everything comes from the class except the costs, which are measured. Nothing is a number someone
typed into the file.

For the LTBF system, the architecture section renders to this (abridged; the committed
[`DSE.md`](../../../examples/dse_cg/DSE.md) has it whole):

```markdown
## Architecture

The architecture is fixed: every design point has the same parts, connected the same way. Only the
sizes inside the parts change.

The long-term update of one sector (the `long_term` workload) is a job graph the processor runs ...

- **hub** (device, built): the Resp hub; depends on no parameter.
- **ing** (device, modelled): the Ingest DMA; depends on no parameter.
- **mem** (memory, modelled): Shared Mem, one sub-top a bank; depends on no parameter.
- **sys** (device, built): the systolic engine's device (SysWrap); depends on `R`, `C`, `W`, `form`.
- **vec** (device, built): the vector unit's device (VecWrap); depends on `W`, `L`.
- **xbar** (crossbar, built): the AXI crossbar and the top's glue; depends on `banks`.

How the parts are connected:

| node | kind | what |
|---|---|---|
| `host` | crossbar master | bus (outside the DUT) |
| `sys` | crossbar master | m_in |
| ... | | |
| `sys` -> `hub` | stream | resp_sys |
| `hub` | interrupt to the host | hub_qresp |
```

A design parameter rebuilds only the parts it reaches: a new `L` rebuilds `vec` alone, a new `W`
rebuilds `sys` and `vec`, a new `banks` the crossbar alone, and a new `mem_rate` or `cg_window` no
part at all (a new RTL run on the same parts).

### For an experiment run

`write_context` writes the file a user's agent reads, with links into the repository. For an
experiment run, `brief.build_sandbox` renders the same template into the run's sandbox, with links into
the sandbox's copies of the docs and source. The template's `arm:waveflow` blocks hold the lines about
`pysim`; the traditional arm's `DSE.md` is rendered without them and is otherwise the same file.

### Rules that keep it honest

- **Never edit `DSE.md` by hand.** Change the class or the design and regenerate. A test
  (`tests/examples/test_dse_cg_brief.py`) checks that the committed file matches a fresh rendering.
- **No objective, no budget.** The objective is the prompt and the budget is the service's. One
  `DSE.md` serves every task on the design.
- **No advice.** Facts about the design only; the method is in the skill.
- **Every number has a source.** An unfilled `{{block}}` in the template is an error, not an empty
  section.
