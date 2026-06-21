---
title: C and RTL simulation
parent: AXI-MM Command Queue (VMAC)
nav_order: 8
has_children: false
---

# C and RTL simulation

The Vitis stages turn the [Python model](./pymodel.md) into real hardware and check
it. Every check has the same spec: the **one `execute` golden**. If Vitis ever
disagrees, the rule is to fix the kernel, never loosen the compare. Vitis HLS is
installed in this environment, so the numbers below are measured, not estimated.

## Bit-exact against one golden

C-simulation and RTL co-simulation both compare against the single golden, through
the generic memory-image harness
[`vmac_golden_mem.apply_golden`](../../../examples/vmac/vmac_golden_mem.py) (it
deserializes operands from a memory image, runs `execute`, serializes the result
back). The conformance reference moved from an earlier `execute_mem`'s image to this
`execute` + generic deserialize path, so the kernel image and the golden image are
**byte-identical** by construction (see the [one-golden anatomy](./pymodel.md)).

## The conformance matrix

[`vmac_build.py`](../../../examples/vmac/vmac_build.py) is the conformance BuildDag.
It checks the datapath bit-for-bit across:

- **all three ops** — `scalar_mult`, `inner_prod`, `sum`;
- **× reduce** — each op with and without the row reduction;
- **× alpha mode** — direct (immediate) and indirect (per-row) `α` for `scalar_mult`;
- **× packing factor** — `mem_dwidth ∈ {16, 32, 64}` → **PF = 1, 2, 4** (cases use
  `n_cols` a multiple of 4 so every row is word-aligned at each PF);
- **× rounding/overflow** — the four `q_rnd` / `o_sat` modes at PF=1.

Run the Vitis conformance (bit-exact C-simulation per structural config):

```bash
cd examples/vmac
python vmac_build.py --through py_sim   # golden-vs-oracle parity, no Vitis
python vmac_build.py --through csim     # the bit-exact Vitis C-sim conformance
```

The auto-extracted top (the [generated, `m_axi`-only kernel](./codegen.md)) gets its
own RTL co-simulation, also bit-exact against `apply_golden`:

```bash
python vmac_build.py --through topgen_cosim   # the generated top, RTL cosim
```

## Throughput vs. packing factor

The packing factor sets how many complex columns move per memory word, so the
element-wise ops are expected to scale roughly **1× / 2× / 4×** with PF = 1 / 2 / 4.
The throughput sweep re-checks the golden as it sweeps PF and extracts cosim cycle
counts:

```bash
python vmac_build.py --through extract_cosim_timing   # throughput sweep (Vitis)
```

> The committed cycle artifacts are the **PF = 1** calibration sweep below; the
> per-PF throughput numbers are produced by the command above rather than cited here,
> to avoid quoting values that are not committed.

## Committed RTL cycle counts

The committed measurements that feed the [timing calibration](./timing.md) are the
PF = 1, `inner_prod + reduce` cosim cycles in
[`calibration/vmac_calibration.json`](../../../examples/vmac/calibration/vmac_calibration.json):

| size | trips | RTL cycles | read words |
| --- | --- | --- | --- |
| 4×4  | 16  | 347  | 32 |
| 6×6  | 36  | 725  | 72 |
| 8×8  | 64  | 1258 | 128 |
| 8×16 | 128 | 2435 | 256 |

(`trips = n_rows · ⌈n_cols / PF⌉`; tool: *Vitis HLS 2025.1 cosim (xsim),
xc7z020clg484-1, 10 ns clock*, per the committed JSON.) These are the RTL truth the
[timing](./timing.md) page calibrates the loosely-timed model against — and where the
`ab_eq` read-word halving (16 vs. 32 at 4×4) and the equal-latency result are
established against hardware.

## Next

- [Timing — the LT model + cosim calibration](./timing.md) — the capstone: how these
  cosim cycles calibrate the loosely-timed simulation.
