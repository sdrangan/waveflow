---
title: Running the loopback
parent: Measuring a delay with an address
grand_parent: Examples
nav_order: 1
audience: python
summary: "The build rungs for rf_shot_loopback and what each produces: the SimPy run that measures the address difference, its aliasing and its epoch sensitivity and files the numbers as JSON, and the two on-demand figure rungs. No toolchain is needed for any of it, and none of the rungs synthesizes anything — the two designs in the graph are RTL-gated by their own examples."
---

# Running the loopback

`rf_shot_loopback_build.py` is a `BuildDag`, and every rung of it runs with **no toolchain at all**.

```bash
cd examples/rf_shot_loopback
python rf_shot_loopback_build.py                      # through the figure
python rf_shot_loopback_build.py --through pysim      # the measurement alone
python rf_shot_loopback_build.py --through sync_docs_figures
python rf_shot_loopback_build.py --list-steps
```

| rung | what it does | what it produces |
|---|---|---|
| `pysim` | runs the loopback three ways — the demonstration, the aliasing pair, the epoch pair — and asserts every claim | `results/rf_shot_loopback_pysim.json` |
| `address_delay_figure` | draws both memories against one address axis | `results/address_delay.svg` |
| `sync_docs_figures` | promotes the SVG into the committed docs assets with a content hash | `docs/examples/rf_shot_loopback/images/` |

## What `pysim` files, and why it files it

The rung does not merely pass — it writes down **what it measured**, because for this example that is
the deliverable. A green tick would tell a reader nothing about a delay.

```json
{
  "configured_delay_samp": 96,
  "loop_latency_samp": 64,
  "raw_address_difference": 160,
  "measured_channel_delay": 96,
  "samples_in_agreement": 4576,
  "aliasing": { "near_delay": 96, "far_delay": 352,
                "near_reading": 96, "far_reading": 96 },
  "epoch":    { "raw_tied": 160, "raw_tx_one_block_late": 224 }
}
```

Read it as: the capture carries an address difference of **160**; the loop's own declared structural
latency is **64**; what is left is **96**, which is what the path was configured with. The aliasing
block is the same reading at a delay one whole buffer longer, and the epoch block is the same reading
with the transmit tile started one block late.

## No codegen rung, and that is a decision

Every other example in this family lowers to an `ap_ctrl_none` top and runs it through xsim. This one
does not, and the reason is recorded rather than glossed:

* **both designs here are already RTL-gated at this exact mode.**
  `tests/examples/test_rf_shot_tx_abs_xsi.py` (17 gates) and `test_rf_shot_rx_abs_xsi.py` (12) each
  synthesize their half at `absolute_index = 1` and assert its addressing against real Verilog.
* **what a loopback adds is a claim about the *pair***, and that claim is an address correspondence
  in the loosely-timed model — not a property of either kernel's RTL.
* closing the loop at RTL would need **a second locked memory inside one kernel** and **a C++ twin
  for the path's delay**, and would restate a number two green gate sets already stand behind.

`plans/rf_shot_absolute.md` S3 carries the decision and what it would take to change it.

## The gates

`tests/examples/test_rf_shot_loopback.py`, 14 of them, all toolchain-free. The two that matter most
are the controls:

| gate | what fails if it breaks |
|---|---|
| `test_the_channel_delay_is_an_address_difference` | the headline: the reading equals the configured delay |
| `test_the_two_ends_agree_on_phase` | one address difference across every captured sample |
| `test_the_reading_aliases_at_one_buffer` | a delay one buffer longer reads differently |
| `test_an_epoch_offset_moves_the_reading_exactly_as_a_path_delay_does` | `t0` stops being indistinguishable from a path delay |
| **`test_a_relative_pair_reads_no_single_delay_at_all`** | **the negative control** — at `absolute_index = 0` the same graph reads no single delay |
| **`test_the_first_waveform_is_lost_when_the_loads_are_too_close`** | **the spacing control** — remove the spacers and the first waveform never plays, silently |
| `test_the_two_standalone_examples_are_untouched` | either per-design example is retired |

`tests/hw/test_rf_samp_delay.py` gates the path node itself — the shift in whole samples, the tail
carried across a block boundary, and the two geometries it refuses.

## Next

- [Measuring a delay with an address](./index.md) — what the example shows and why.
- [Playing a stored waveform](../rf_shot_tx/) and
  [Capturing without losing anything](../rf_shot_rx/) — the two per-design examples, which stay.
