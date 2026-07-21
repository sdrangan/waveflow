---
title: Instrumenting a calibration
parent: Calibration
nav_order: 9
audience: python
api: [CalibDataFrame, InterpCalibModel, LinCalibModel]
summary: "The calibration playbook: how to actually collect the data and close the loop so the LT simulation reproduces the RTL timeline. Instrument the sim with begin/end events at the top level, fit each block's primitive in isolation, let the sim compose the end-to-end timing, extract the datapoints from a cosim sweep, and feed the fitted model back into the component. Worked against the FIR example."
---

# Instrumenting a calibration

The [previous pages](./index.md) covered the *pieces* — the [corpus](./dataframe.md) and the
[models](./models.md) — and a [toy fit](./example.md) on synthetic numbers. This page is the
**playbook**: how to collect *real* data and close the loop so the loosely-timed simulation
reproduces the RTL.

The worked reference throughout is [`examples/rowwise_fir`](../../../examples/rowwise_fir/fir.py) — a
matrix-LT FIR whose [double-buffered timing model](../timing_model/double_buffered.md) is calibrated
this way.

## The goal: RTL timing == simulation timing

A Waveflow timing model is [loosely-timed](../timing_model/models.md): it *predicts* a timeline from a
few parameters. Calibration is the loop that makes that prediction match the cycle-accurate RTL:
**instrument → sweep in cosim → fit → predict in sim → check the residual on a held-out point.** When
the held-out error is small, the fast LT sim stands in for the slow RTL.

## 1. Instrument the simulation — `begin`/`end` at the top level

You compare *event timelines*, so the sim must emit a timestamped event at each boundary you intend
to measure. Two rules:

- **Bracket every transfer with a `begin`/`end` pair**, plus a command-arrival event as the **anchor**
  (`t = 0` for the comparison).
- **Only top-level, bus-visible events are measurable.** A cosim VCD shows bus activity, not internal
  sim state — so the events you fit against must correspond to something on a port (an AXI burst), not
  a private signal.

The FIR component logs seven events per command (`_log` in [`fir.py`](../../../examples/rowwise_fir/fir.py)):

| event | logged at | measurable in cosim? |
|---|---|---|
| `cmd_arrive` | the loader pulls the command | the anchor |
| `load_begin` / `load_end` | around the X-read | **yes** — the X-read burst span |
| `comp_begin` | compute starts | no — sim-internal |
| `store_begin` / `store_end` | around the Y-write | **yes** — the Y-write burst span |
| `resp_sent` | the response burst | **yes** |

(The same table appears on [Double-buffered processing](../timing_model/double_buffered.md#where-to-log-critical-events),
where it is the validation hook for that model.)

## 2. Fit each block's primitive *in isolation*

The most important discipline: **measure and fit each block's primitive separately**, so a misfit is
attributable to *one* block, and so each primitive is *physical* rather than a blend.

The cautionary tale is the FIR write stage. The obvious thing — fit the **wall-clock Y-write span**
(`store_begin → store_end`) — gives a non-physical model: a per-burst "setup" of ~100 cycles. That
span conflated two different things:

- the **deterministic channel occupancy** (one `transfer` beat per word), a property of the bus, and
- the **stall** (the write channel sitting idle, `VALID = 0`, waiting for compute), a property of the
  *compute* block.

Separate them — [count only `transfer` beats](../timing/aximm.md#true-channel-occupancy-count-transfer-beats)
for occupancy, attribute the idle beats to compute — and each primitive becomes clean: the channel
occupancy is exactly `nwords` (no fit), compute is exactly `II=1` (no fit), and the *only* thing left
to fit is one physical curve (`row_depth(n_col)`, the per-row pipeline depth).

> **Rule of thumb:** if a fitted "constant" comes out physically implausible (a 100-cycle bus setup),
> you are probably fitting a *blend* of two blocks. Split the measurement.

## 3. Let the simulation compose the end-to-end timing

Do **not** fit the emergent whole-kernel latency directly. Fit the per-block primitives and let the
sim's structure — the three concurrent processes and their `max(load, compute, store)` overlap —
*produce* the end-to-end timeline. The whole-kernel is a **validation metric, never a fit target**.

Why it matters: the whole-kernel is a `max()` of overlapping spans (a kinked function). Fit it with a
smooth model and you manufacture a fudge term (FIR's first attempt grew a spurious `sqrt(n_col)`).
Fit the primitives and the kink falls out of the composition for free — and the model stays
asymptotically correct. This is the payoff of modeling with real processes (see
[Double-buffered processing](../timing_model/double_buffered.md)) rather than a closed-form latency.

## 4. Extract the datapoints from a cosim sweep

Now the measurement side. For each design size:

1. **Pick the measurable events** — usually the **bursts** into and out of the block on its top-level
   `m_axi` port. (This is why the block's interface must expose the transfers; a buried sub-block is
   not measurable.)
2. **Extract them from the VCD** with the [timing tools](../timing/aximm.md): `extract_aximm_bursts`
   returns the read/write bursts; the span you want is the **`transfer`-beat count** (true occupancy),
   not the wall-clock data-phase span.
3. **Sweep** over a grid of sizes to get one row per run, and collect them in a
   [`CalibDataFrame`](./dataframe.md).

In FIR this is [`fir_calibrate.py`](../../../examples/rowwise_fir/fir_calibrate.py)`measure_cosim`,
sketched:

```python
from waveflow.utils.vcd import VcdParser, AximmBeatType
from waveflow.calib import CalibDataFrame

def transfer_beats(burst):
    return sum(1 for b in burst["beat_type"] if b == AximmBeatType.TRANSFER)

db = CalibDataFrame(columns=["n_row", "n_col", "read_words", "write_words", "whole_kernel_cyc"])
for n_row in [1, 2, 4, 8]:                 # the sweep grid
    for n_col in [64, 256, 1024]:
        vcd = run_cosim(n_row, n_col)      # synth + RTL cosim at this size
        vp = VcdParser(vcd); clk = vp.add_clock_signal()
        sigs, _ = vp.add_aximm_signals(prefix=gmem_prefix, dir="both")
        writes, reads, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=sigs)
        db.add_datapoint({
            "n_row": n_row, "n_col": n_col,
            "read_words":  sum(transfer_beats(b) for b in reads),
            "write_words": sum(transfer_beats(b) for b in writes),
            "whole_kernel_cyc": whole_kernel_span(reads, writes, clk_ns),
        })
db.save("results/fir_grid.csv")            # the committed corpus
```

Choose the grid deliberately: **vary each feature over enough distinct values** to constrain it, and
**hold out an *interior* point** so the held-out error tests interpolation (the property the model
relies on) rather than extrapolation. FIR sweeps `n_row ∈ {1,2,4,8} × n_col ∈ {64,256,1024}` and holds
out `(2, 256)`.

## 5. Incorporate the model in the simulation

Three placements close the loop:

- **Where the model lives:** in the component's **timing class** — for FIR, `FIRTiming` in
  [`fir.py`](../../../examples/rowwise_fir/fir.py) holds the fitted `row_depth` (an
  [`InterpCalibModel`](./models.md)) plus the deterministic constants. The timing class's methods
  (`compute_body_cyc`, `t_fill_cyc`, the channel `bus_timing`) are what the stage processes call.
- **Where `fit` runs:** in a dedicated **`calibrate` step**, separate from the sim — for FIR a
  [build-DAG step](../build/) (`fir_build.py … calibrate`) that loads the cosim grid, fits the
  primitives, validates the held-out point, and writes `fir_calibration.json`.
- **Where `predict` is consumed:** the stage processes call the timing-class methods (which call
  `model.predict`) to set their spans/timeouts. The component loads the fitted parameters at
  construction (`FIRTiming.from_calibration(path)`); the sim then runs with calibrated timing.

```python
# the calibrate step: fit primitives from the swept corpus, validate, persist
db = CalibDataFrame.load("results/fir_grid.csv")
row_depth = InterpCalibModel(basis=["n_col"], target="row_depth").fit(db)   # the one fitted curve
# ... deterministic occupancy + II=1 compute need no fit ...
save_calibration("results/fir_calibration.json", row_depth, fill_const, setup)

# in the component: load it and predict during the sim
self.timing = FIRTiming.from_calibration("results/fir_calibration.json")
# stage process: span = self.timing.compute_body_cyc(n_row, n_col)   # -> row_depth.predict(...)
```

When the held-out point lands within tolerance, the loop is closed: the LT sim reproduces the RTL.

## See also

- [The corpus — `CalibDataFrame`](./dataframe.md) / [Models](./models.md) — the API this playbook uses.
- [A worked example](./example.md) — the same fit/score/holdout on synthetic data.
- [AXI4-MM timing analysis](../timing/aximm.md) — `extract_aximm_bursts`, the burst fields, and the transfer-beat occupancy technique.
- [Double-buffered processing](../timing_model/double_buffered.md) — the FIR timing model these primitives compose into.
- [Rowwise FIR example](../../examples/rowwise_fir/) — this playbook applied end-to-end ([cosim extraction](../../examples/rowwise_fir/cosim_timing.md), [fitting](../../examples/rowwise_fir/fit.md)).
- [`examples/rowwise_fir/fir_calibrate.py`](../../../examples/rowwise_fir/fir_calibrate.py) — the real cosim-sweep calibration.
