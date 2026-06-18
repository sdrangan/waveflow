---
title: HwTestbench
parent: Hardware Components
nav_order: 5
audience: python
applies_to: [HwComponent]
api: [HwTestbench]
summary: "HwTestbench — a HwComponent subclass whose sequential main() (stream push/pop, dut.run()) defines a test sequence in Python; the codegen source for a C++ testbench main()."
---

# HwTestbench

## Concept

`HwTestbench` is a `HwComponent` subclass for codegen-source testbenches. You write the test sequence as a Python `main(self)` method; that method is the source the codegen extracts.

> The codegen side — extracting and lowering `main()` into a C++ testbench `main()` (the
> `HlsCodegenStep` testbench mode) — is in
> [Component Code Generation: Testbench](../comp_codegen/testbench.md).

The v1 model is sequential: blocking file I/O, stream push/pop operations, and `dut.run()` are supported in `main()`. Concurrent SimPy-style stimulus/capture (`env.process(...)`) is not currently supported in this pathway.

## API

- [`HwTestbench`](../../../waveflow/hw/hw_testbench.py)
- [`main(self)`](../../../waveflow/hw/hw_testbench.py)
- [`HlsCodegenStep(is_testbench=True)`](../../../waveflow/build/hwcodegen_steps.py)

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), `PolyTBHls.main()`:

```python
dut = PolyAccelComponent()
dut.s_in.push(data_hdr)
dut.s_in.push_array(samp_in, count=data_hdr.nsamp)
dut.run()
dut.m_out.pop(resp_hdr)
```

This is the reference sequential pattern for generated C++ testbench mains.

## Quick reference

- Subclass `HwTestbench` for codegen-source TBs.
- Put the test sequence in `main()`.
- Use stream push/pop and regmap helpers directly.
- Keep flow sequential in v1.
- See [Component Code Generation: Testbench](../comp_codegen/testbench.md) for emitter behavior.
