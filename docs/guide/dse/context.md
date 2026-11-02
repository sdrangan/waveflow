---
title: The design context
parent: Agentic DSE
nav_order: 2
audience: python
api: [DseAdaptor, CgDetectorAdaptor]
summary: "How a design is described for DSE. A subclass of DseAdaptor defines the design's context: its parameter space and constraints, its prose, how to build its system at a design point, and its golden model. The base class derives the rest: defaults and legality; the architecture (the sub-top tree and its interconnects, the same at every point, and checked to be); each part's identity at a point and its synthesis; and which parts each parameter rebuilds. Each method, what it returns and who uses it, with the CG detector as the example. Once the class is defined, the generator renders DSE.md, the file passed to the agent: what it contains, where each section comes from, and the rules that keep it honest."
---

# The design context

**Under construction.** The base class and the CG adaptor are in
[`examples/dse_cg`](../../../examples/dse_cg/README.md); most methods are not written yet.

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

class CgDetectorAdaptor(DseAdaptor):
    name = "cg_detector"
    platform = "xczu48dr_250mhz_vitis2025_1"

    def space(self): ...                       # parameters, constraints, workloads
    def context(self): ...                     # the prose that describes the design
    def build_system(self, params): ...        # the HwSystem at a design point
    def golden(self, params, workload): ...    # the untimed bit-exact simulation
```

The CG detector's is [`cg_detector.py`](../../../examples/dse_cg/cg_detector.py). The members fall in
three groups:

| | member | what it gives |
|---|---|---|
| **implement** | `name`, `platform` | the design's name; the calibration platform its models were fitted on |
| | `space()` | parameters, constraints, workloads, as data |
| | `context()` | the prose that describes the design |
| | `build_system(params)` | the `HwSystem` at a design point |
| | `golden(params, workload)` | the untimed bit-exact simulation |
| **override if needed** | `pysim(params, workload)` | the timed simulation with calibrated models |
| | `synth(node, params)` | the synthesis of one part of the architecture |
| | `rtlsim(params, workload)` | the RTL simulation of the DUT |
| | `hls_estimate(kernel, params)` | the HLS compiler's estimates for one kernel |
| **provided** | `defaults()`, `fill()`, `legal()` | defaults and legality |
| | `architecture()` | the parts of the DUT and how they connect, the same at every point |
| | `identities(params)`, `rebuilds()` | what each part would synthesize at a point; which parts each parameter rebuilds |

The split follows what can be derived. `defaults`, `fill` and `legal` follow from `space()`. The
rest follow from `build_system`, because a Waveflow system already knows how to simulate itself, how
its DUT is cut into parts, and what each part would synthesize. You override one only when your
design has a better way.

The rest of this page goes through the methods in that order, then shows how the class becomes the
file the agent reads.

## Methods you implement

### `name` and `platform`

Two class attributes. `name` identifies the design to the service and in the agent's files.
`platform` names the calibration platform whose timing and resource models `pysim` uses (here the
`xczu48dr` at 250 MHz, Vitis 2025.1, see [Model calibration](../calib/index.md)).

### `space()`: the parameters, constraints and workloads

`space()` returns plain data: every parameter with its kind, legal values, default and meaning; the
constraints; and the workloads. The CG detector's imports the knobs already defined in
[`examples/mimo_cg/hw/space.py`](../../../examples/mimo_cg/hw/space.py) rather than copying them:

```python
def space(self) -> dict:
    design = [
        ("K", S.K_VALUES, 4, "users solved for: the CG system is K x K"),
        ("R", S.R_VALUES, 4, "systolic array rows"),
        ("W", SPACE_W, 12, "word width of the fixed-point formats, bits"),
        ...
    ]
    return {
        "parameters": [
            {"name": n, "kind": "design", "values": list(v), "default": d, "description": t}
            for n, v, d, t in design
        ] + [
            {"name": "nit", "kind": "workload", "values": list(range(1, 9)), "default": 4,
             "description": "CG iterations, a field of each command (at most K)"},
            ...
        ],
        "constraints": [
            {"rule": "K % R == 0", "why": "the array's rows tile K"},
            {"rule": f"R * C <= {S.MAX_PES}", "why": "the largest array the device holds"},
            ...
        ],
        "workloads": [
            {"name": "steady", "fixes": {"nit": 3}, "valid_for": ["cycles_per_solve"]},
            {"name": "snr_sweep", "fixes": {"snr_db": [0, 5, 10, 15, 20]},
             "valid_for": ["mse_db", "ber"]},
        ],
    }
```

- A **design** parameter changes the hardware; a **workload** parameter changes only what the
  hardware is asked to do ([The DSE problem](problem.md#parameters)).
- A **constraint** is a Python expression over the parameter names, with a sentence saying why. It
  may mix design and workload parameters, as `nit <= K` does.
- A **workload** fixes the workload parameters for one kind of measurement and names the metrics it
  is valid for. (The CG detector's workloads are not written yet; the two above show the shape.)

`space()` is the one method everything else reads: the agent's `dselib.space()` returns it, the
service checks points against it, and the generator turns it into the parameters section of the
agent's file.

### `context()`: the prose

`context()` returns the few things only a person can write:

```python
def context(self) -> dict:
    return {
        "title": "CG detector",
        "description": "A conjugate-gradient solver for MIMO detection: ...",
        "data_flow": {
            "text": "The host writes a job descriptor; the systolic array forms the Gram matrix, "
                    "then passes the descriptor to the vector unit, which runs the CG iterations ...",
            "docs": ["docs/examples/mimo_cg/index.md"],
        },
        "metrics": [
            {"name": "mse_db", "unit": "dB", "description": "error against the floating-point answer"},
            {"name": "cycles_per_solve", "unit": "cycles", "description": "throughput on `steady`"},
            ...
        ],
    }
```

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

For the CG detector it builds the detector at `HwConfig(**params)`. `params` arrives filled: every
design parameter present, defaults included.

### `golden(params, workload)`: the untimed bit-exact simulation

`golden` runs the design's bit-exact model with no timing: the systems team's simulation. It returns
the accuracy metrics for the workload, the body of `dselib.golden`:

```python
{"mse_db": {"0": -8.1, "5": -13.0, "10": -21.4, ...}, "ber": {...}}
```

It has no default because no general rule produces it: the bit-exact model is the design's own. For
the CG detector it is the fixed-point CG of
[`examples/mimo_cg/mimo_cg_fixed.py`](../../../examples/mimo_cg/mimo_cg_fixed.py) run over the
workload's channels.

## Methods with a default

### `pysim(params, workload)`: the timed simulation

`pysim` returns everything `golden` does, plus cycles, per-block activity and resources, each
approximate number with a confidence level. The default runs `build_system`'s system in pysim on the
workload and prices resources with the calibrated models (`estimate_system`).

The CG detector uses the default. Its study also has an analytic model,
[`examples/mimo_cg/hw/models.py`](../../../examples/mimo_cg/hw/models.py), fitted by hand before the
system flow existed. It is kept as a comparison, not used as `pysim`: the experiment has to measure
Waveflow's generic pysim, not a model written for one design. Override `pysim` only when a design
cannot be simulated as a system.

### `synth(node, params)`: one part's synthesis

`synth` synthesizes one part of the architecture, with everything under it, out of context, and
returns its resources, its timing and a per-instance utilization report. The default uses Waveflow's
per-device route (`synth_route="per_device"`) for a sub-top and builds the crossbar IP for the
crossbar.

Only the service's live backend calls it, and only for a part the ledger does not hold yet
([The service](service.md)). A design point's resources are the sum of its parts, each synthesized on
its own. That sum differs a little from one synthesis of the whole DUT, which can optimize across the
boundaries between parts; the per-part sum is what a DSE builds and pays for, so it is the measure.

### `rtlsim(params, workload)`: the RTL simulation

`rtlsim` simulates the whole DUT at RTL on a workload and returns the measured cycles, a check that
the RTL is bit-exact with pysim, the per-block activity, and the activity table as an artifact. The
default runs Waveflow's system flow on `build_system`'s system ([Simulating at RTL](../systems/rtl.md)),
which is incremental: a new workload on a design already elaborated pays for simulation alone. Like
`synth`, only the service's live backend calls it.

### `hls_estimate(kernel, params)`: the HLS compiler's estimates

`hls_estimate` C-synthesizes one HLS kernel (an HLS top of the DUT) and returns the compiler's
report: estimated resources, II, and latency where the compiler can compute one (`None` for a
free-running kernel whose latency depends on its streams). Only HLS kernels have one; the
hand-written adaptors and the vendor crossbar do not. The default runs the system flow's `csynth`
step for that top, the same step that runs before every synthesis, so measuring the ground truth
records these estimates as a by-product.

It is in both arms of the experiment: it is the quick look a traditional hardware team already has.
`hls_identities(params)`, a provided method, gives each kernel's identity, so the service compiles a
kernel once per set of sizes. It is finer than `identities` because one part may hold several
kernels.

## Methods provided

### `defaults()`, `fill(params)`, `legal(params, workload)`

| method | what it does |
|---|---|
| `defaults()` | every parameter's default, from `space()` |
| `fill(params)` | a design point with its missing parameters set to their defaults; a name that is not a parameter is an error, so a typo cannot silently evaluate the default |
| `legal(params, workload)` | the first constraint a point breaks, or `None`; parameters not given are taken at their defaults, so a rule like `nit <= K` is checked like any other |

### `architecture()`: the parts and how they connect

The DUT is not synthesized as one piece. It is cut into parts
([Devices: the top's sub-tops](../build/xsi_system.md#devices)):

- a **device** for each kernel, a *sub-top*: the kernel with the views, adaptor and bus writers that
  belong to it;
- the **memory**, another sub-top, if it is inside the DUT;
- the **crossbar** between them, vendor IP in the system top rather than a sub-top, but a part of
  its own all the same.

`architecture()` returns that tree, each part with its kind and the modules it holds, and the
**interconnects** between the parts: the crossbar's masters and slaves, the stream links that cross
from one part to another, the interrupt lines, and what crosses the DUT boundary. It walks
`build_system`'s system at the defaults; nothing about it is written by hand except the data-flow
prose of `context()`.

In parametric DSE the architecture is **the same at every point**: the same parts, connected the same
way, with only the sizes inside them changing. So `architecture()` is computed once, and checked: the
tree is built at the defaults and at each parameter's extreme values, and if the shape differs, the
adaptor is refused. A "parameter" that adds or removes a part is an architectural choice, outside
this kind of DSE.

### `identities(params)` and `rebuilds()`

`identities(params)` gives each part's **identity** at a design point: a hash of what would be
synthesized. It is cheap, because it builds the Python system and synthesizes nothing. Only the
service uses it: a part whose identity the ledger already holds costs nothing, so two design points
that share a part share its synthesis. The agent never sees an identity.

`rebuilds()` compares the identities at the default parameters and with one parameter moved: the parts
whose identity changed are the ones that parameter rebuilds. It is how `DSE.md` can say which parts
each parameter touches, and nobody writes that table, so it cannot be wrong.

| | what | depends on the point? | who sees it |
|---|---|---|---|
| `architecture()` | the parts and their connections | no | the agent, in `DSE.md` |
| `rebuilds()` | which parts each parameter touches | no | the agent, in `DSE.md` |
| `identities(params)` | what each part would synthesize | yes | the service only |
| `hls_identities(params)` | what each HLS kernel would compile | yes | the service only |

## Generating `DSE.md`

Once the class is defined, one call generates the key file, **`DSE.md`**, which is what the agent is
given about the design:

```python
from pathlib import Path
from examples.dse_cg import brief
from examples.dse_cg.cg_detector import CgDetectorAdaptor

brief.write_context(CgDetectorAdaptor(), Path("examples/dse_cg/DSE.md"))
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

For the descriptor-ring CG system (V2 in the plan), the architecture section would render to something
like this:

```markdown
## Architecture

The architecture is fixed: every design point has the same parts, connected the same way. Only the
sizes inside the parts change.

The host writes a job descriptor to memory and notifies the systolic unit. The systolic unit forms
the Gram matrix, writes it back, and passes the descriptor to the vector unit, which runs the CG
iterations ...

dut
├── systolic   device     SystolicUnit, its descriptor front end, its views   depends on R, C, cmul, W, mem_dw
├── vec        device     VecUnit, its descriptor front end, its views        depends on L, W, g_s, mem_dw
└── xbar       crossbar   AXI crossbar                                        depends on mem_dw

How the parts are connected:

| from | to | over | carries |
|---|---|---|---|
| host | systolic, vec | xbar, AXI-Lite | job descriptors, notifications |
| systolic, vec | memory | xbar, AXI4 | matrices and vectors |
| systolic | vec | xbar, credit stream | the descriptor after each half iteration |
| vec | host | interrupt | job done |
```

The CG detector of V1 is synthesized as one top, so its tree has one part, `detector`, and every
design parameter rebuilds it.

### For an experiment run

`write_context` writes the file a user's agent reads, with links into the repository. For an
experiment run, `brief.build_sandbox` renders the same template into the run's sandbox, with links into
the sandbox's copies of the docs and source. The template's `arm:waveflow` blocks hold the lines about
`pysim`; the traditional arm's `DSE.md` is rendered without them and is otherwise the same file.

### Rules that keep it honest

- **Never edit `DSE.md` by hand.** Change the class or the design and regenerate. A test will check
  that the committed file matches a fresh rendering.
- **No objective, no budget.** The objective is the prompt and the budget is the service's. One
  `DSE.md` serves every task on the design.
- **No advice.** Facts about the design only; the method is in the skill.
- **Every number has a source.** An unfilled `{{block}}` in the template is an error, not an empty
  section.
