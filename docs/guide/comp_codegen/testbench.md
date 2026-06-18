---
title: Testbench
parent: Component Code Generation
nav_order: 6
audience: hls
applies_to: [HwTestbench]
api: [HwTestbench, HlsCodegenStep]
summary: "The codegen-source testbench path: HwTestbench.main() lowered to a sequential C++ int main() (DUT build, file I/O, stream push/pop, dut.run()), emitted as <kernel>_tb.cpp when is_testbench=True."
---

# Testbench

## Concept

`HwTestbench` provides a codegen-source testbench path where `main(self)` is lowered to a C++ `int main()` body. The mode is sequential by design: it supports DUT construction, file I/O, push/pop stream operations, regmap helpers, and `dut.run()` in order.

`HlsCodegenStep` switches into testbench mode when `is_testbench=True` (or auto-detected from `_is_testbench`). In this mode it emits a single `<kernel>_tb.cpp` file.

## API

- [`HwTestbench`](../../../waveflow/hw/hw_testbench.py) base class.
- [`HwTestbench.main(self)`](../../../waveflow/hw/hw_testbench.py) sequential entry point.
- [`HlsCodegenStep(is_testbench=True)`](../../../waveflow/build/hwcodegen_steps.py) enables TB emission.
- [`tb_files_to_str(...)`](../../../waveflow/build/hwgen.py) returns generated testbench source.

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), `PolyTBHls.main()` demonstrates DUT binding, stream push/pop, regmap status write, and `dut.run()`:

```python
dut = PolyAccelComponent()
dut.s_in.push(data_hdr)
dut.s_in.push_array(samp_in, count=data_hdr.nsamp)
dut.run()
dut.m_out.pop(resp_hdr)
dut.regmap.write_status_json(self.data_dir + "/regmap_status.json", fields=["halted", "error", "tx_id"])
```

## Quick reference

- Keep `main()` straight-line and sequential.
- Use `dut.run()` once per invocation flow.
- Stream operations: `push`, `push_array`, `pop`, `pop_array`.
- File I/O is allowed through schema/regmap helper methods.
- Concurrent SimPy-style `env.process(...)` patterns are future work.
