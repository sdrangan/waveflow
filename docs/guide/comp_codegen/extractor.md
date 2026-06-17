---
title: Extractor
parent: Component Code Generation
nav_order: 3
audience: hls
api: [HwStmtExtractor, extract_kernel, SynthesisError]
summary: "How HwStmtExtractor parses the synthesizable subset of a component's Python (on_start / run_proc, or main for a testbench) into the HwStmt IR, failing fast with SynthesisError on unsupported patterns."
---

# Extractor

## Concept

The extractor stage parses Python component methods into a constrained hardware IR (`HwStmt`). It enforces the synthesizable subset at build time, so unsupported patterns fail fast with `SynthesisError` instead of generating ambiguous C++.

Kernel extraction selects `on_start` when a component carries a `VitisRegMapMMIFSlave`, otherwise `run_proc`. Testbench extraction routes to `main()` and enables a different rule profile through `is_testbench=True`.

## API

- [`HwStmtExtractor`](../../../waveflow/build/hwcodegen.py) parses method source into `HwStmt`.
- [`extract_kernel(comp) -> HwStmt`](../../../waveflow/build/hwcodegen.py) chooses kernel entry point and resolves symbols.
- [`extract_testbench(comp) -> HwStmt`](../../../waveflow/build/hwcodegen.py) extracts `HwTestbench.main()`.
- [`@synthesizable`](../../../waveflow/hw/synth.py) marks callable methods for extraction and lowering.
- Implicit-capture checks and pipelined-op restrictions are enforced in [`HwStmtExtractor`](../../../waveflow/build/hwcodegen.py).

## Example

From [`examples/stream_inband/poly.py`](../../../examples/stream_inband/poly.py), `PolyAccelComponent.on_start()` and `@synthesizable evaluate(...)` are extracted as kernel IR, while `PolyTBHls.main()` is extracted through testbench mode.

```python
@synthesizable
def evaluate(self, cmd_hdr, s_in, m_out, coeffs):
    samp_in, tstart = yield from s_in.get_pipelined(Float32, count=cmd_hdr.nsamp)
    yield from m_out.write_pipelined(array(Float32, y), t_out_start)
```

The extractor permits this hook body, but blocks pipelined stream ops at top-level kernel/testbench bodies.

## Quick reference

- Use `extract_kernel` for normal `HwComponent` classes.
- Use `extract_testbench` for `HwTestbench` classes.
- Mark hardware-callable helpers with `@synthesizable`.
- Keep non-hardware helpers as `@sim_only` when they appear in extracted methods.
- Expect build-time errors for non-synthesizable AST patterns.
