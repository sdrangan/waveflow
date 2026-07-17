---
title: Parameterization
parent: Realization Flows
nav_order: 4
audience: python
summary: "How HwParam fields on a component become C++ template parameters of the generated kernel, and how param_supports emits additional concrete kernel variants. Applies to both flows."
---

# Parameterization

<!-- WRITE ME (draw from the old components/parameterization.md, 114 lines).
     - HwParam[T] fields: bound at instantiation (comp = MyComp(width=32)); collected from
       get_type_hints() and mapped to C++ template parameters of the kernel.
     - Concrete-by-default: the kernel is emitted with the values baked in; a templated mode is
       optional.
     - param_supports: emit additional named concrete variants (<kernel>_<key>) with overrides.
     - Applies to BOTH flows (sequential and concurrent) — the HwParam surface is on HwComponent.
     - Forward pointer: a third binding-site, XSIParam, is planned for TB participants (concurrent
       flow), but is not built. -->

**Source of truth:** `waveflow/hw/hw_component.py` (`HwParam`, `param_supports`, `_wrap_hw_params`);
see [Hardware components](./components.md) for where the synthesis surface sits.
