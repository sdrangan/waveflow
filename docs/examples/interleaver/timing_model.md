---
title: The timing model
parent: Composite kernel interleaver
nav_order: 8
---
# The compute stage's timing model

The framework mem-streams ship their timing; `il_compute` does not — it is the design's **own** kernel, so
the design gives it a timing model. This page **declares** that model: where it lives on the stage, the
formula it evaluates, and what its parameters mean. [Fitting the timing model](./timing_fit.md) then
recovers those parameters from RTL.

The general facility is [Adding a timing model to a component](../../guide/timing_model/insertion.md); this
is the interleaver's worked instance of it.

## Why the stage needs one

In pysim the gather is **instantaneous** — `run_iter` computes `Y[i] = X[P[i]]` in one Python loop and the
result exists at once. Hardware takes cycles. So the stage must **charge** the time its computation would
take: predict a cycle count for this firing and wait it out before committing the output. Without the
model the compute would look free and the pysim timeline would run ahead of the RTL.

## Where it lives

`IlComputeInband` holds a model object and charges it once per firing. It builds the model in
`__post_init__` and, in `run_iter`, predicts the firing's cycles and yields a matching `timeout` right
after the (instant) functional gather:

```python
def _build_timing_model(self):
    seed = {"n": IL_COMPUTE_II_SEED, "intercept": IL_COMPUTE_LATENCY_SEED - IL_COMPUTE_II_SEED}
    path = None if self.calib_dir is None else Path(self.calib_dir) / "params.json"
    model = LinCalibModel(basis=["n"], target="cycles", fit_intercept=True,
                          coeff_names=["n"], seed=seed, path=path)
    model.load_or_default()          # a fitted params.json if calib_dir has one, else the seed
    return model

def run_iter(self):
    ...
    yblock.val[:n] = xblock.val[pblock.val[:n]]       # the gather — one vectorized numpy call
    cycles = float(self.timing.predict({"n": n}))     # what it would take in hardware, for this n
    yield self.timeout(cycles * self.clk.period)      # charge it
    ...
```

The charged delay is **only the compute's own cost**. The reads and writes are charged elsewhere — the
reader and writer stages block for their transfers — so the model owns just the gather loop, and any stall
grows the firing on its own (see
[the general pattern](../../guide/timing_model/insertion.md#the-delay-is-additional-not-end-to-end)).

## The formula

The gather is a pipelined loop, so its cycle count is the [loop model](../../guide/timing_model/loops.md) —
a line in the trip count:

```
cycles = latency + ii · (n − 1)
```

- **`n`** — the trip count: the number of output **elements**. The typed blocks hold one 32-bit element
  per slot (`ap_uint<32>[N]`), so the loop trips once per element — the basis is `n`, **not** the word
  count `nw`.
- **`ii`** — the initiation interval: cycles between successive loop iterations. `ii = 1` means one output
  element per cycle (a fully pipelined loop).
- **`latency`** — the fixed per-firing cost: pipeline fill plus constant setup, independent of `n`.

`LinCalibModel(basis=["n"], fit_intercept=True)` stores this as a coefficient on `n` and an intercept.
Matching `latency + ii·(n − 1) = ii·n + (latency − ii)`, the coefficient **is `ii`** and the intercept is
`latency − ii`. So two numbers describe the stage's timing, and fitting means finding them.

## The seed, and loading a fit

`IL_COMPUTE_LATENCY_SEED` / `IL_COMPUTE_II_SEED` (in `interleaver.py`) give a **no-calib fallback**, so the
stage charges a plausible cost before any fit is loaded. Point `calib_dir` (via
`InterleaverInband(compute_calib_dir=…)`) at a fitted `params.json` and `load_or_default` uses that
instead. Both currently agree — `cycles = n` (`ii = 1`, `latency = 1`) — because
[the fit](./timing_fit.md) measured exactly that from RTL and the seeds were set to match.

## See also

- [Adding a timing model to a component](../../guide/timing_model/insertion.md) — the general pattern this
  instantiates.
- [Timing models for loops](../../guide/timing_model/loops.md) — the `latency + ii·(n − 1)` law.
- [Fitting the timing model](./timing_fit.md) — recovering `latency` and `ii` from RTL (the next page).
