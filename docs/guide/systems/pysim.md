---
title: Simulating in Python
parent: Flow Overview
nav_order: 4
audience: python
api: [Simulation, SimObj, FreeRunMod, run_iter, harness, StreamDriver, StreamSink, SwHost, Processor]
summary: "The second step of the flow: the DUT in a Simulation. A Simulation is a discrete-event simulation of any SimObjs -- the design's modules and anything else the question needs: a host program, a wireless channel, a traffic source, a processor model. A free-running module's behavior is its run_iter, one firing per message, with its timing declared as timed phases. The cut: any module can be the DUT of its own harness, harness(Module, workload=...), with a generated stand-in at each port -- so each module is simulated alone, against the golden, before the whole. Then the composite (each stage alone, then together) and the system with its host. Reading results off the objects."
---

# Simulating in Python

The first simulation of a design is **pysim**: a discrete-event simulation in Python. It is fast, its
timing is a model, and it can hold anything -- which is what makes it the place a design is explored
and evaluated as part of a larger system.

## A `Simulation` holds anything

A [`Simulation`](../sim/index.md) runs a collection of [`SimObj`](../sim/simobj.md)s: every object is
constructed on it, wired by interfaces, and `run_sim()` drives them all -- each one's `pre_sim`, its
processes, then its `post_sim` ([Running a simulation](../sim/running.md)).

```python
sim = Simulation()
kernel = PolyKernel(name="poly_kernel", sim=sim, clk=clk)     # constructing it registers it
...                                                            # the objects around it, the interfaces
sim.run_sim(until=host.done)                                   # until an event, or until nothing is left
```

The hardware's modules are `SimObj`s, and so is everything else a question needs beside them:

- **a host program** -- a [`SwHost`](../build/sw_threads.md), whose threads issue commands and read
  results over the bus;
- **a processor** running software beside the hardware -- a calibrated [`Processor`](../cpu/index.md);
- **the world** -- a wireless channel between a transmitter and the DUT, a sensor, a traffic source:
  any model written as a `SimObj` with ports.

None of these has to be hardware, or has to become hardware. That is the difference from the next step:
an [RTL simulation](rtl.md) can hold only what has an RTL twin.

## A module's behavior: one firing

A free-running module's behavior is its `run_iter`: **one firing**, the work on one message -- for the
polynomial, read a command, head its results, run the samples, report
([Defining the kernel in Python and HLS](hwmodule.md)). In pysim it is what runs:

- the reads and writes are the stream ports' -- messages by their schemas, arrays by their element type;
- the work is the golden's function, so pysim's results are the golden's by construction;
- the **timing** is declared, not hand-tuned: [timed phases](../timing_model/timed_phases.md), spans
  between events at the module's own ports, calibrated later against the RTL
  ([Timing and resources](calibration.md));
- a fatal error waits on an event nothing triggers: the module stops, as the hardware does.

## The cut: a module in its own harness {#the-cut}

Any module can be simulated **alone**. `harness` makes it the DUT of its own system, with a stand-in at
each port generated from the port's type:

```python
sysm = harness(PolyKernel, workload="test")
sysm.run()
```

| the module's port | stand-in |
|---|---|
| a stream input | a `StreamDriver`, playing the module's scenario |
| a stream output | a `StreamSink`, recording every burst |
| a credit stream it produces into, or consumes | a `CreditSink`, or a `CreditSource` |
| a memory's slave port (`memory_harness`) | `MmScript`, a scripted AXI-MM master |

The scenario is the module's own: `workload(kind)` returns `{input port: bursts}`, built from the
golden's intents, so a harness needs no testbench code:

```python
def workload(self, kind: str = "test") -> dict:
    cmds = {"test": default_cmds, "calib": calib_cmds}[kind]()
    return {"s_in": stimulus(cmds)}
```

After the run, each sink holds what it recorded -- `sysm.stand_ins["m_out"].words`, one array per
burst -- to check against the golden ([Functional verification](verification.md)). Every stand-in has an
RTL twin, so the same harness runs at RTL in the next step. A stand-in can also be made hostile -- a
sink that stalls, credit held back -- to show the module survives more than a polite testbench
([Developing module by module](../flows/units.md)).

**Develop module by module**: each module in its harness, against the golden, before the whole is
assembled. A whole that fails is then a fault in how the parts are joined, not in the parts.

## Steps up

**A pipeline: [block_eq](../../examples/block_eq/system_flow.md).** Each stage is a module with framed
ports, so each runs in its own harness, driven by the messages the golden says its neighbour sends --
the gain stage alone, the apply stage alone -- and then the composite, `harness(BlockEq)`, its stages
joined by their internal streams. Three harnesses, one golden.

**A system with a host: [markov](../../examples/markov/pysim.md).** The modules, a memory and a
`SwHost` on one simulation, wired directly or over a crossbar, run until the host is done:

```python
sysm = markov_system(default_jobs(4, 300), link="mm")      # the kernels' devices, the memory, the host
results = sysm.run()                                        # {job: {"x", "ones", "n", "t"}}
```

Each kernel was first run alone in its harness -- the generator against a credit sink, the chain's core
against a credit source -- and the memory in `memory_harness`.

Next: [Simulating at RTL](rtl.md) -- the same objects, the DUT as hardware.
