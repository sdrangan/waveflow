---
title: Lifecycle
parent: Hardware Components
nav_order: 4
audience: python
applies_to: [HwComponent]
api: [pre_sim, run_proc, post_sim, on_start, sim_only]
summary: "The component lifecycle — pre_sim / run_proc / post_sim, plus on_start for regmap-launched kernels and @sim_only to exclude helpers from synthesis extraction."
---

# Lifecycle

## Concept

Waveflow simulation objects follow a standard lifecycle: `pre_sim`, `run_proc`, and `post_sim`. `HwComponent` participates in the same lifecycle through inheritance from `Component`/`SimObj`, while synthesis extraction targets selected methods (`run_proc` or `on_start`) depending on component structure.

For regmap-driven kernels, `on_start` is used as the invocation-style body triggered by host `ap_start`. Free-running simulation components usually implement `run_proc` as the long-running process body.

> The codegen side — which method is extracted as the kernel entry (`extract_kernel`'s
> selection policy) and how `@sim_only` excludes a helper — is in
> [Component Code Generation: Extractor](../comp_codegen/extractor.md).

## API

- [`SimObj.pre_sim`](../../../waveflow/simulation/simobj.py)
- [`SimObj.run_proc`](../../../waveflow/simulation/simobj.py)
- [`SimObj.post_sim`](../../../waveflow/simulation/simobj.py)
- [`extract_kernel`](../../../waveflow/build/hwcodegen.py) method-selection policy
- [`@sim_only`](../../../waveflow/hw/synth.py)

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), `PolyAccelComponent` defines `on_start` for regmap-triggered kernel entry and a `@sim_only` helper (`_inc_job`) excluded from synthesis extraction.

## Quick reference

- `pre_sim`: setup/validation before event loop.
- `run_proc`: free-running or passive component process entry.
- `on_start`: regmap launch body for invocation-style kernels.
- `post_sim`: final checks/reporting.
- Mark non-synthesizable helpers with `@sim_only`.
