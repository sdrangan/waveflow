---
title: Writing a BFM model
parent: Custom Hooks
nav_order: 5
audience: hls
api: [bfm_model, XsiSimObj, BfmModel, BFM_DUALS, DynParam]
summary: "The other pre-written realization. Where kernel_task() hands over an hls::task body for a module INSIDE the cut, bfm_model() hands over a cycle-exact C++ model for one OUTSIDE it. Covers when a new model is warranted (usually never — reuse AxisMaster/AxisSlave/AxiMm*Slave), the five XsiSimObj phases and why sample/update are split, the DynParam config contract and its falsy-value trap, and the conformance obligation that check() cannot discharge."
---

# Writing a BFM model

A module can be realized in more than one way, and **which realization applies is a property of the
build, not of the class**. There are two pre-written realizations, and they are exactly symmetric:

| hook | hands over | realized as | used when the module is |
|---|---|---|---|
| [`kernel_task()`](./writing.md) | a hand-written `hls::task` body | a task inside the generated top | **inside** the cut |
| `bfm_model()` | a hand-written cycle model | an `XsiSimObj` beside the top | **outside** the cut |

Both say *"here is my pre-written artifact"*. [Writing a hook](./writing.md) covers the first; this
page covers the second. A module may declare either, both, or neither — and a module with neither is
a **simulation-only node** (an RF channel, a golden reference), which is a finding from `check`, not
something the class has to declare about itself.

The target is `xsi_bfm_model`, and it is asked per **module**:

```python
>>> check(StreamDriver, "xsi_bfm_model")
(True, None)
>>> check(MemCopy, "xsi_bfm_model")
(False, "MemCopy declares no bfm_model() hook, so it has no pre-written cycle model to place beside
         a top. ...")
```

## When you need a new model — usually you do not

Five models already exist in
[`waveflow/build/xsi/xsi_bfm.h`](../../../waveflow/build/xsi/xsi_bfm.h), and they cover every port a
generated kernel can expose. Before writing a sixth, check which axis is actually varying:

| what differs about your peer | the answer |
|---|---|
| the **data** it presents or expects | not a model — a [burst bundle](../build/bfm.md). Point an existing model at a different file. |
| a **knob** on otherwise identical behavior | not a model — a [`DynParam`](../flows/parametrization.md#dynparam) field, see below. |
| the **protocol behavior** itself | a new model. |

Only the last one earns a class. A peer that never backpressures and counts underruns behaves
differently on the wire from an `AxisSlave`; a peer that plays different words does not.

## Which models exist, and against what

The testbench never picks freely: it must present the **dual** of the DUT port it faces — the
opposite role on the same protocol. That pairing is one table,
[`BFM_DUALS`](../build/bfm.md#bfm-duals), and a new model has to name the row it
fills.

Two entries in that table are **holes**, and they bound what this hook can do today: nothing
implements AXI4-Lite (so a `HostActivated` DUT cannot be driven at RTL at all), and nothing masters
an `m_axi` bus *into* a DUT (in this flow the kernel is always the master).

## The five phases, and why `sample` and `update` are split

Every model derives from `XsiSimObj`, the C++ mirror of Python's [`SimObj`](../sim/). All five phases
default to no-ops, so a model implements only what it needs:

| phase | when | what belongs here |
|---|---|---|
| `pre_sim()` | before reset | load vectors from a bundle, seed memory |
| `sample()` | clock **low** | read the DUT's outputs; latch whether a beat happens |
| `update()` | after the rising edge | apply the beat, advance the FSM, count the cycle |
| `drive()` | end of cycle | present the values the DUT will see next cycle |
| `post_sim()` | after the run | dump results to a bundle, collect metrics |

**The split between `sample` and `update` is not stylistic.** A synchronous transfer is *decided* from
values that were stable before the edge and *applied* after it. `sample()` reads `TVALID` while the
clock is low and latches `beat_ = valid && ready`; `update()` acts on that latch. Collapsing them —
reading `TVALID` and pushing the word in one step — silently changes *which cycle* a transfer is seen
on, which does not break the run: it shifts every measured cycle count by one and produces a design
that looks a little faster or slower than it is.

For the same reason `drive()` writes only values the model already holds. A `drive()` that reads a DUT
output and answers it in the same call has built a combinational loop across the clock boundary.

## The config contract {#config-contract}

Anything the model's behavior needs that *varies per instance* crosses as a
[`DynParam`](../flows/parametrization.md#dynparam): a field on the Python module, emitted into the
generated harness as a member assignment.

```python
class StreamDriver(HwModule):
    in_bundle: DynParam[str] = ""        # Python side
```
```cpp
class AxisMaster : public XsiSimObj {
public:
    std::string in_bundle;               // C++ side — the SAME name
    void pre_sim() override { if (!in_bundle.empty()) /* load it */; }
};
```
```cpp
s_cmd.in_bundle = "vectors/s_cmd";       // what the harness emits
```

Two obligations, because the assignment is emitted **blind**:

- **The C++ member must exist and must mean the same thing.** A missing member is a compile error, so
  that half is safe. A member that exists but means something subtly different is not — nothing
  compares the two definitions.
- **The falsy-value trap.** `discover_dyn_params` skips any field whose value is falsy, so `0`,
  `0.0`, `False` and `""` emit **nothing** and the C++ default silently wins. A knob whose meaningful
  value is `0` or `False` cannot be expressed as a `DynParam` today — invert it (`skip_reset` rather
  than `do_reset`) so the interesting value is truthy, or give the C++ member the same default and
  document that they must agree.

## The conformance obligation {#conformance}

`check(mod, "xsi_bfm_model")` is **resolved, not derived** — and the difference matters more here
than anywhere else in the codebase.

The [`composite_kernel`](../comp_codegen/) verdict runs the real extractor, so it answers with rules
nobody restated. This one performs four lookups: the hook is declared, the named class exists in the
header, its `ports` cover every crossing endpoint, and each endpoint has a dual. That is the complete
list. `(True, None)` means **"resolvable"**; it does not mean "correct".

Nothing checks that your C++ model behaves like the Python module it stands for, and nothing static
can — one is a SimPy process, the other a cycle-level FSM. So the obligation is yours, and it is
discharged the same way every other equivalence claim in this project is:

> **A new model needs a byte-identical vector gate.** Drive the same on-disk burst bundles through
> the pysim module and through the C++ model, and assert the output bundles are identical bytes. Not
> "close", not "the test passed" — identical.

That is why the scenario lives in files rather than in C++ (see
[BFM testbenches](../build/bfm.md)): one bundle can drive both
backends, so there is a comparison to make at all.

Note that pysim and XSI are expected to disagree on **timing** — the pysim model is loosely-timed and
the BFM is cycle-exact. The gate is on the data, not the cycle counts.

## A worked example: `AxisSlave`

The simplest real model in the library. It answers an AXI-Stream output: always ready, keeps every
word, and timestamps each one.

```cpp
class AxisSlave : public XsiSimObj {
public:
    AxisSlave(Dut& d, const std::string& prefix) : d_(d) {
        P_data  = d.port((prefix + "_TDATA").c_str());     // bind by RTL port prefix...
        P_valid = d.port((prefix + "_TVALID").c_str());    // ...which <top>_ports.h supplies,
        P_ready = d.port((prefix + "_TREADY").c_str());    // generated from the kernel's own spec
    }

    void sample() override {                    // clock LOW: decide, do not act
        valid_ = d_.get1(P_valid);
        data_  = d_.getW(P_data);
        beat_  = (valid_ && h_ready_);          // the beat is latched here...
    }

    void update() override {                    // after the edge: act on what was decided
        ++cycle_;
        if (beat_) { words_.push_back(data_); beat_cycles_.push_back(cycle_); }
    }

    void drive() override { d_.put1(P_ready, h_ready_); }   // only a value we already hold

    std::string out_bundle;                     // the DynParam's C++ half
    void post_sim() override {
        if (!out_bundle.empty()) BurstBundle::write_capture(out_bundle, words_, beat_cycles_);
    }
    // ...
};
```

Three things to copy from it:

- **The constructor takes a port *prefix*, not port names.** `<top>_ports.h` is generated from the
  same `TopSpec` that emits the kernel's interface pragmas, so a model and the kernel it drives
  cannot disagree about what a port is called.
- **The model counts its own cycles.** `update()` is called exactly once per cycle, so an internal
  counter *is* the cycle number — no clock reference and no change to the uniform phase API. This is
  what lets the run loop carry no measurement logic at all.
- **The sink reports when work completed; the loop only decides when to stop looking.** Conflating
  those is how three of four hand-written testbenches once reported a drain tail as if it were the
  design's latency.

Then declare it from Python:

```python
def bfm_model(self):
    from waveflow.build.composite_gen import BfmModel
    return BfmModel("AxisSlave", ports=("stream_ep",))
```

`ports` are **attribute names**, in the C++ constructor's order — that order is a fact about the C++
and nothing else records it. Each is validated against the module's `add_endpoint` registry at
elaboration time, so a renamed port is an error where you can see it rather than deep inside the walk.

## See also

- [Writing a hook](./writing.md) — the peer hook, for a module realized *inside* the cut.
- [BFM testbenches](../build/bfm.md) — the model library, the lifecycle, and the dual table.
- [XSI testbench in HLS](../comp_codegen/xsi_tb.md) — how a whole testbench graph is resolved.
- [Hardware modules](../flows/modules.md) — kind, hooks and cut as three separate axes.
