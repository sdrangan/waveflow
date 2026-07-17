---
title: Parameterization
parent: Sequential (host-activated)
grand_parent: Realization Flows
nav_order: 7
audience: python
summary: "How HwParam fields on a component become C++ template parameters of the generated kernel, and how param_supports emits additional concrete kernel variants."
---

# Parameterization

<!-- WRITE ME. How a component is parameterized and how that reaches the generated kernel.
     - HwParam[T] fields: bound at instantiation (comp = MyComp(width=32)); collected from
       get_type_hints() and mapped to C++ template parameters of the kernel.
     - Concrete-by-default: the kernel is emitted with the values baked in; a templated mode is
       optional.
     - param_supports: emit additional named concrete variants (<kernel>_<key>) with overrides.
     - (Forward pointer: a third binding-site, XSIParam, is planned for TB participants — see the
       concurrent flow — but is not built.) -->

**Source of truth:** `waveflow/hw/hw_component.py` (`HwParam`, `param_supports`,
`_wrap_hw_params`). See also [Components / Parameterization](../../components/parameterization.md) and
`plans/parameterization` notes.
