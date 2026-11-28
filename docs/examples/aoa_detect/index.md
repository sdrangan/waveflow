---
title: Emitter detection and AoA
parent: Examples
nav_order: 13.5
has_children: true
example_dir: examples/aoa_detect
summary: "Under construction. A multi-antenna receiver watches C channels for emitters that come and go. Each channel is in one of two modes: searching (an energy gate, then a spatial covariance and a scan over directions on a shared systolic unit) or tracking (a cheap check that the emitter is still there). Every channel's block arrives at the same instant, so the controller dispatches several jobs at once onto shared hardware, and the mix of expensive and cheap jobs follows the emitters. The result is a detection delay, which only a timed simulation gives. The problem is meant for exploring agentic design-space exploration with Waveflow."
---
# Emitter detection and AoA

**Status: under construction.** These pages state the problem, the algorithm, the architecture, the workload and the
parameters.
Sizes, formats and several policies are open, and nothing here is built.

A receiver with $n_{rx}$ antennas watches $C$ frequency channels. In each channel an emitter appears
from time to time, stays at a fixed angle of arrival (AoA) for a while, then stops. The receiver must
**detect each emitter, estimate its AoA and SNR, and notice when it stops**, with as little delay as
possible.

Each channel runs a small state machine on blocks of $B$ samples:

- **Searching:** a cheap energy gate on each block; if it fires, the block's spatial covariance (a
  Gram matrix) and a scan over candidate directions, both on a shared systolic unit.
- **Tracking:** a cheap check that the energy, and if needed the power in the emitter's direction,
  is still there.

All $C$ channels deliver a block at the same instant, so the controller dispatches several jobs at
once. A search costs several times a tracking check, and how many channels are searching follows the
emitters and the gate's false alarms. So the work arrives in bursts, on hardware and memory the
channels share, and the **delay from an emitter's onset to its detection** depends on queueing and
contention. That makes it a test of design-space exploration with a calibrated simulator (pysim)
against RTL simulation alone.

## Pages

| Page | What it covers |
|---|---|
| [The problem](problem.md) | The signal model, the emitters' on/off process, what is measured, and what can be designed. |
| [The algorithm](algo.md) | The per-channel search / track state machine, each stage's maths and cost, and its thresholds. |
| [The architecture](arch.md) | The capture and power detector, the ring of blocks in a dual-port BRAM, the hardware controller, and the pools of systolic and vector units, with a diagram. |
| [The workload](workload.md) | How a test drives the receiver: emitters placed on the ground in a sector with free-space path loss, switched on and off by a Gilbert–Elliott process with bursts, the samples and their truth log, and the scenarios. |
| [The parameters](param.md) | Every parameter with a proposed value and whether it is explored: algorithm, number formats, memory and queues, credits, units, policies, and the load they imply. |

The first sketch of the idea is `examples/aoa_detect/idea.md`.
