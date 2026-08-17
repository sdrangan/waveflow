 # Plan: the DSE agent surface — an MCP tool set, piloted on `fir_block`

> **Status (2026-08-17): M0 half-landed. Design plan otherwise.**
>
> This is the first plan whose deliverable is not a hardware capability but an **agent interface**.
> Everything it exposes exists; what does not exist is a surface an LLM can plan against, and a
> quality metric to optimize.
>
> Landed: [`examples/fir_block/fir_block_quality.py`](../examples/fir_block/fir_block_quality.py) —
> the quantized filter *design* step. The evaluation half is next; see
> [M0 in detail](#m0-in-detail--a-guide-for-the-implementer), written to be followed without a
> signal-processing background.
>
> **Part note.** The corpus and every resource number here are `xc7z020` (DSP48E1, 25×18). The RF work
> targets the RFSoC 4x2 (`xczu48dr`, DSP48E2, 27×18), where `dsp_per_mult` steps differently —
> `device_rules` already keys on the part, but no corpus exists for it. Nothing in M0–M3 depends on
> the part; M4 would, if the pilot ever retargets.
>
> The pilot is [`examples/fir_block`](../examples/fir_block) — green through pysim → csynth → XSI,
> already carrying a fitted resource model and a 24-point measured corpus
> ([`resource_model.md`](resource_model.md), phases D/E complete).

## Motivation

[`paper_cg_dse_vision.md`](paper_cg_dse_vision.md) commits to DSE over a large parameter
cross-product running in Python against calibrated cycle- and resource-approximate models. The models
now exist. What has never been built is the thing that *uses* them: an agent that chooses which point
to evaluate next, at which fidelity, under a budget.

**This example deliberately does not need Waveflow.** A bit-exact Python model of a quantized FIR is
a hundred lines of numpy, and the LT timing model buys nothing when the throughput is a closed form.
That is the point — the pilot must exercise the *tool surface*, not hide behind the framework's
strengths. If the agent loop only works because `fir_block` is unusually well-instrumented, the
surface is wrong. The target consumer is the CG matrix inverse, where `N` modules instantiate from a
subset of `P` system parameters and no closed form exists for anything.

### The problem, stated

Maximize filter quality subject to a throughput floor and a resource ceiling.

| knob | quality | throughput | resources |
|---|---|---|---|
| `ntap` | ↑ monotonically | — (II is 1 either way) | ↑ linearly in DSP/LUT/FF |
| `samp_w` | ↑ (resolution) | ↓ — `LW = mem_dwidth // samp_w` samples per word | non-monotone: `dsp_per_mult` steps at 8/18/25 while `LW` falls |
| `samp_i` | data path only — see below | — | **free** |
| `unroll_lane` | — (bit-identical golden) | `LW` samples/cycle vs 1 | `LW×` the multipliers |

`samp_w` is the interesting axis and the reason this is not a toy: it moves quality, throughput, and
area *in different directions with a non-monotone area term*. The user's framing — throughput =
`stream_bw / samp_bw` per cycle — is exactly `LW` under `unroll_lane=True`. Under `serial` throughput
is pinned at 1 sample/cycle regardless, and at `samp_w=24` (`LW=1`) the two realizations coincide.

`samp_i` needs a correction to the obvious reading. A continuous coefficient scale is a *fractional*
`samp_i` — scaling `h` by `2^k` and moving `I` by `k` are the same operation, and nothing requires the
taps to be a power-of-two scaling of the prototype. So `design_quantized` (below) already optimizes
the coefficient side of `samp_i` **continuously**, and it is not a DSE knob for the taps at all.
It survives only as the *data path* format: input headroom and output overflow, the latter under
`AP_WRAP`, where an overflow is catastrophic rather than graceful. It is currently frozen at
`DEFAULT_SAMP_I=2`; the DSE surface unfreezes it for the data path only.

## What already exists — do not rebuild it

| plan tool | existing mechanism |
|---|---|
| `synth(params)` | `SweepRunner` + `Stage("resources")` (`fir_block_sweep.py`), records to a platform store, never silently drops a point |
| `get_resource_hist()` | `ModuleStore` over `calib/`, plus the committed `fir_block_corpus.GRID` |
| `predict_resource(params)` | `FirCompute.get_rm(platform).predict(...)`, composed to design totals by `compose()` |
| `rtlsim(params)` | the XSI gate on `fir_block` |
| `pysim(params)` — *plumbing only* | `fir_block_sim.py` runs the design and checks bit-exactness |

The resource model is held out at 3.2% LUT / 2.8% FF mean on design totals, exact on DSP and BRAM.
That is the surrogate the agent will reason with; it does not need to be improved for this arc.

## What is missing

**1. There is no filter design.** `fir_block_sim._tap_set` fabricates test coefficients. Nothing
anywhere designs an actual filter from a specification.

**2. There is no quality metric.** `check()` asserts the RTL matches *its own* quantized golden — a
conformance test, not a performance measure. A design that is bit-exactly a bad filter passes today.
This is the single largest gap: **the DSE objective does not exist.** And it will not be recovered
from the frequency response of the taps alone, which is blind to input and output quantization — it
needs signals pushed through the fixed-point path. See
[Layer 2](#layer-2--the-signal-domain-metrics-these-are-the-objective).

**3. There is no agent surface.** `waveflow/mcp/registry.py` exposes schema and component tools only.

## The spine: a fidelity ladder

The whole surface is one idea — four rungs, each with a cost and a distinct thing it can tell you
that the rung below cannot. Everything else in this plan is bookkeeping around it.

| rung | cost | what it establishes that the rung below cannot |
|---|---|---|
| `predict_resource` | ~10 ms | area/feasibility **without synthesis** — and how much it trusts itself |
| `pysim` | ~10 ms | quality of the *quantized* filter through the real fixed-point path; throughput from the closed form |
| `synth` | ~50 s/point | measured LUT/FF/DSP/BRAM, achieved II, timing closure |
| `rtlsim` | ~90 s | the RTL computes what pysim said it would |

The agent's entire job is allocating a budget across these rungs. It cannot do that if it learns the
cost of `synth` by having spent it. **`get_dse_context()` returns this table.**

## The tools

Six. Not nine.

### `get_dse_context()`

The one tool the agent calls first, returning:

* the **objective and constraints**, explicitly — maximize `stopband_rej_db` (the Layer 2 signal-domain
  number, not the Layer 1 taps-only one), subject to `passband_sndr_db >= S`,
  `throughput_samp_per_cyc >= T`, and `top_dsp <= D`, `top_lut <= L`, … Without this the agent invents
  its own trade-off and every run is incomparable.
* **parameter domains** — legal values per knob, and the coupling rules (`LW` is derived, not free;
  `unroll_lane` is a no-op at `samp_w >= mem_dwidth`). An agent that proposes illegal points burns
  turns discovering the rules one error at a time.
* **the fidelity ladder above**, with costs.
* a **status summary** — points evaluated at each rung, budget spent, current best feasible.

### `pysim(params)` — design *and* evaluate

Absorbs the proposed `design(params)`. Two calls where the agent always makes both is two chances to
make an ordering error, and a designed-but-unevaluated point is not a state worth representing.
`pysim` designs the float prototype (Kaiser `firwin` against the spec in the context), quantizes to
`(samp_w, samp_i)` at the chosen coefficient scale, pushes test signals through the fixed-point path,
and returns quality **plus the quantized taps** so the agent can inspect a design that went wrong.

Returns [both layers of quality metric](#m0-in-detail--a-guide-for-the-implementer) — the Layer 1
taps-only bound *and* the Layer 2 signal-domain measurements, because their **gap** is the datapath's
contribution and is the diagnostic the agent needs when a point underperforms — the derived
throughput, wall-clock, and the run directory. Despite the name it runs no simulation:
`FirCompute.filter_block` is already the bit-exact fixed-point model, so this rung costs milliseconds.

### `synth(params)` and `rtlsim(params)`

Kept as separately-named tools rather than folded into `evaluate(params, fidelity=...)`. The tool
*name* is the strongest cost signal available to the model: `synth()` reads as expensive in a way
`evaluate(fidelity="synth")` does not.

Both **accept a list of points**. Four sequential blocking calls at ~50 s each is the worst possible
shape for an agent loop; one batched call that fans out is the same wall-clock as the slowest point.

### `predict_resource(params)`

Must return uncertainty, not a number:

```json
{"top_dsp": {"est": 64, "exact": true, "source": "prior"},
 "top_lut": {"est": 21840, "interval": [19100, 24600], "n_support": 24, "extrapolating": false},
 "model_version": "fir_compute@a3f1"}
```

A point estimate from a fit the agent cannot see the residuals of is *worse than no estimate* — the
agent will trust it and skip the synth that would have refuted it. `extrapolating` is the load-bearing
field: the corpus covers `ntap ∈ {8,16,32}`, and the resource-model plan's own framing is that an
agent must be able to ask "do you actually know?"

`model_version` exists because the model refits as synth records land. Without it, two identical
`predict_resource` calls can disagree and the history is not reproducible.

### `get_results()`

**One sparse table, not three histories.**

| samp_w | samp_i | ntap | real | atten_db | thru | dsp | lut | fmax | rtl | t_pysim | t_synth |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 16 | 2 | 32 | serial | 61.2 | 1.0 | 32 | 21840 | 118 | ✓ | 1.9 | 47 |
| 8 | 2 | 32 | unroll | 38.4 | 4.0 | 64 | — | — | — | 2.1 | — |
| 12 | 2 | 32 | unroll | — | 2.0 | 64* | — | — | — | — | — |

`get_perf_hist` / `get_resource_hist` / `get_timing_hist` force the agent to join three tables in
context, and that join is exactly where models fabricate rows. Nulls mark un-run rungs; `*` marks a
predicted rather than measured cell. **The sparsity is the agent's to-do list** — it is information,
not absence.

## Five commitments the tools must hold

### Failures are rows, not exceptions

A point that misses II, fails timing closure, or exceeds the device is a **measurement**. If `synth`
raises, the agent either retries it — burning the cost again — or treats the point as unexplored.
`fir_block_sweep.py` already takes this position for the sweep ("a csynth that fails is recorded as a
failure with its error, because a sweep that quietly covered 19 of 24 points and reported 24 would put
a hole in the fitted region exactly where an agent would later be told it was interpolating"). The
agent surface inherits it verbatim.

### Content-addressed cache, idempotent calls

Hash the parameter dict → one run directory per point with a `manifest.json`; `get_results` is a scan
of that tree. Re-calling `synth` on a known point returns instantly with `cached: true`. Without this
an agent *will* re-synthesize a point it already holds — the most predictable failure mode in the set.
The store is then stateless across restarts and the agent can be handed a path to read artifacts with
its own file tools in workspace mode.

### Cost is reported before the call and after it

Every result carries wall-clock; `get_dse_context` carries the estimate. Both, always.

### Cross-rung discrepancy is surfaced

`rtlsim` validates `pysim`; measured `synth` validates `predict_resource`. The result row should
carry both numbers and flag the gap, not overwrite the prediction. This is the same position the tree
already takes on cost — measured, never inherited — applied to the agent's beliefs.

### No silent scope reduction

If a batched `synth` drops points, or a prediction is served from an unfitted lookup, the tool says
so in the payload.

## The replay oracle — build this first

The committed 24-point corpus **is a ground-truth grid.** That is unusual and worth exploiting: it
turns the pilot from a demo into a **scoreable benchmark.**

Serve `synth` and `predict_resource` from a frozen table with simulated latency, behind the same tool
signatures. Then:

* the agent loop iterates in seconds instead of hours, so the tool descriptions and prompts can be
  tuned at all;
* the true constrained optimum is computable by exhaustion, so a run can be **scored**: did it find
  the optimum, and how many synth-equivalents did it spend getting there?
* run it 20 times and the variance is measurable — the only honest way to claim an agent loop works.

Nothing else in this space has an oracle. Build replay mode first and live `synth` second; same API,
and live mode is then a swap of one backend.

**Caveat that must be resolved before the benchmark means anything:** on `xc7z020`, the committed grid
never binds. `unroll` costs exactly `2·NTAP` DSPs at every width, so `ntap=32` peaks at 64 of 220
available. A DSE benchmark whose resource constraint never binds is not a benchmark. Two fixes, and
M1 must pick one: extend the grid to `ntap ∈ {64, 128}` (a real sweep, ~20 min, and it also tests the
resource model outside its fitted region — valuable independently), or declare an artificial budget
below the device. **Prefer extending the grid** — an artificial budget makes the extrapolation
question disappear, and that question is half of what the surrogate is for.

## M0 in detail — a guide for the implementer

*This section assumes no signal-processing background. The design half is landed in
[`fir_block_quality.py`](../examples/fir_block/fir_block_quality.py); the evaluation half is the work.*

### The DSP you need, in one page

A FIR filter is a **dot product against a sliding window**. You hold `T` numbers called *taps*
(`h[0] … h[T-1]`), and each output sample is `y[i] = Σ h[k]·x[i-k]`. That is all it is; `filter_block`
in [`fir_block.py`](../examples/fir_block/fir_block.py) is exactly this and nothing more.

The one fact you need: **the filter's frequency response is the FFT of its taps.**

```python
H = np.fft.rfft(taps, nfft)      # the response.  That's it.
```

So "designing a filter" means choosing `T` numbers whose FFT has a shape you want, and "evaluating a
filter" means taking that FFT and measuring the shape.

Frequency is *normalized*: the sample rate is 1, so frequencies run 0 … 0.5 (0.5 is Nyquist, the
fastest thing representable). A spec names two of them:

- **passband** `f < f_pass` — keep this. Response should be flat, gain ≈ 1.
- **stopband** `f > f_stop` — reject this. Response should be as close to 0 as possible.
- between them is the **transition band**, where you don't care what happens.

The score is **stopband attenuation in dB**: how far below the passband the *worst* stopband leak
sits. Higher is better. 20 dB = 10× smaller, 60 dB = 1000× smaller. It's `20·log10` because these are
amplitudes, not powers — using `10·log10` will halve every number you report.

Three facts drive the entire design space, and the third is the interesting one:

1. **More taps buy attenuation, linearly in dB.** At this spec, about 1.15 dB per tap.
2. **Quantizing the taps puts a floor under it**, at roughly 6 dB per bit of `samp_w`.
3. **So attenuation rises with `ntap` until it hits the floor, and then stops.** Past the knee, taps
   cost DSPs and buy nothing — and eventually they make it *worse*, because each extra tap is another
   quantization error source added to a floor that is already binding.

That third fact is why this is a DSE problem and not a maximization. Finding the knee for each width
is the agent's job.

**But the FFT of the taps is only half the story, and it is the optimistic half.** It describes a
filter with those coefficients computed in *infinite precision*. The real path also quantizes the
input, and requantizes the accumulator back down to `<W,I>` on the way out
([`fir_block.py:470`](../examples/fir_block/fir_block.py#L470), `AP_TRN`/`AP_WRAP`). The accumulator
itself is full precision, so those are the only two extra loss points — but they decide the answer,
because in the stopband the wanted output is tiny while the output quantization noise is exactly the
same size it always was. **Stopband rejection is floored by output requantization, and the tap FFT is
structurally blind to it.** So there are two layers of metric below, and only the second is the
objective.

### What is already built

```python
from examples.fir_block.fir_block_quality import FirSpec, design_float, design_quantized

spec = FirSpec(f_pass=0.20, f_stop=0.28)        # normalized: fs = 1, Nyquist = 0.5
q = design_quantized(spec, ntap=32, samp_w=16, samp_i=2)

q.stored      # int64 taps — the payload a LOAD_TAPS command carries.  Feed these to the sim.
q.taps_real   # the same taps as floats — THIS is what you evaluate
q.h           # the float64 prototype, before quantization — a reference, NOT the design
q.scale       # the gain the search chose
q.r2          # alignment of taps_real with h; the search's own objective
```

`design_quantized` picks the coefficient gain by maximizing R² between the prototype and its
quantization — see the module docstring for why, and why R² is *not* the DSE metric.

> ### ⚠ Known defect in the landed design step — read before using it
>
> The scale search maximizes coefficient fidelity with **no output-headroom constraint**, and it
> picks gains of about 4.5:
>
> ```
>  ntap   W    scale   sum|h|   dcgain   maxrep   max input before wrap
>    32  16    4.438    7.925    4.438    2.000                  0.2524
>    64  12    4.513    8.652    4.512    1.999                  0.2310
> ```
>
> With `max_repr` = 2.0 and a DC gain of 4.5, **any input above ~25% of full scale wraps the
> output** — and under `AP_WRAP` that is a sign flip, not a saturation. Every number in the
> taps-only table below is for a filter that would destroy a full-scale signal.
>
> The root cause is architectural, not a coding slip.
> [`samp_type()`](../examples/fir_block/fir_block.py#L154) gives **one** format to coefficients,
> samples, and output. Taps are small (peak ≈ 0.25 for a unity-DC-gain lowpass) and samples are large
> (up to 2.0); sharing a format wastes ~3 bits of coefficient precision, and the scale search is
> silently buying them back with gain — which costs input headroom one-for-one, so it is a wash at
> best.
>
> **Open decision, and the one thing to settle before writing Layer 2.** Either (a) constrain the
> scale against a declared input range — a change to `design_quantized` alone, no hardware impact; or
> (b) split the coefficient format from the sample format (`I_tap < I_samp`), which is the *correct*
> fix but changes `fir_block`, perturbs `FirCompute`'s module key, and invalidates the committed
> corpus. **Do (a) now, log (b).** Until (a) lands, drive every signal-domain test at
> `amp <= max_repr / sum(abs(q.taps_real))` and the tests are still valid — just quiet about
> headroom, which is a thing they should eventually measure.

### Layer 1 — the taps-only metrics (cheap, and an upper bound)

Four functions, all pure, all cheap. Nothing here needs Vitis or a simulator. **These snippets are
verified** — they produce the reference table below.

Keep them. They are a good diagnostic, they cost microseconds, and they bound what the real path can
achieve. Just never report them as the design's performance.

```python
def response_db(taps_real, nfft=8192):
    """Magnitude response in dB, and the normalized frequencies it was evaluated at."""
    f = np.fft.rfftfreq(nfft, d=1.0)
    mag = np.abs(np.fft.rfft(np.asarray(taps_real, dtype=np.float64), nfft))
    return f, 20.0 * np.log10(np.maximum(mag, 1e-300))     # clamp: log10(0) is -inf


def stopband_atten_db(taps_real, spec, nfft=8192):
    """THE objective.  Passband peak minus the worst stopband leak, in dB.  Higher is better."""
    f, db = response_db(taps_real, nfft)
    ref = db[f <= spec.f_pass].max()          # normalize out the arbitrary gain
    return float(ref - db[f >= spec.f_stop].max())


def passband_ripple_db(taps_real, spec, nfft=8192):
    """Flatness of the passband, peak-to-peak in dB.  Lower is better.  A constraint, not the goal."""
    f, db = response_db(taps_real, nfft)
    pb = db[f <= spec.f_pass]
    return float(pb.max() - pb.min())


def throughput_samp_per_cyc(samp_w, mem_dwidth=32, unroll_lane=False):
    """Closed form — no simulation.  The unrolled kernel eats one memory word per cycle."""
    return float(max(1, mem_dwidth // samp_w)) if unroll_lane else 1.0
```

### Layer 2 — the signal-domain metrics (**these are the objective**)

Push a signal through the actual fixed-point path and measure what comes out. Two tests, because the
passband and the stopband fail in different ways and one number cannot see both.

**The snippets below are skeletons — unlike Layer 1, they have not been run.** Treat them as the
shape of the answer, not as working code.

The one piece of machinery both tests need is a **test signal built as a sum of random-phase
sinusoids on the FFT bin grid**. That is not fussiness; it buys three things at once — the signal is
exactly bandlimited (no spectral leakage into the band you are measuring), exactly periodic (no edge
effects), and admits an **exact fractional delay** by phase rotation. The last one is load-bearing:
a linear-phase FIR delays by `(T-1)/2` samples, and the corpus `ntap` values are all *even*, so that
delay is a half-integer and **no integer shift will ever align the reference.**

```python
def bandlimited(nsamp, f_lo, f_hi, amp, seed=0):
    """Random-phase sinusoids on the bin grid: exactly bandlimited, exactly periodic."""
    rng = np.random.default_rng(seed)
    k = np.fft.rfftfreq(nsamp, d=1.0)
    X = np.zeros(k.size, dtype=complex)
    band = (k >= f_lo) & (k <= f_hi)
    X[band] = np.exp(2j * np.pi * rng.random(int(band.sum())))
    x = np.fft.irfft(X, nsamp)
    return amp * x / np.abs(x).max()          # peak-normalized, so `amp` is the peak


def frac_delay(x, d):
    """Exact delay by `d` samples (may be fractional) — a phase ramp on a periodic signal."""
    k = np.fft.rfftfreq(len(x), d=1.0)
    return np.fft.irfft(np.fft.rfft(x) * np.exp(-2j * np.pi * k * d), len(x))


def r2_aligned(ref, y):
    """Gain-invariant match — the same form the design step's scale search minimizes."""
    den = float(ref @ ref) * float(y @ y)
    if den <= 0.0:
        return 0.0
    num = float(ref @ y)
    return 0.0 if num <= 0.0 else (num * num) / den
```

**Test 1 — passband distortion.** Drive a signal confined to the passband, compare the output against
a *delayed copy of the input*, and score with gain-invariant R². Comparing against the input rather
than against a float reference filter is deliberate: it folds the filter's own passband ripple in
with every quantization effect, which is the quantity that actually matters. The gain invariance is
what stops the arbitrary tap scale from being counted as distortion.

```python
def passband_sndr_db(run_fixed, q, spec, ntap, nsamp=4096, seed=0):
    """Signal-to-noise-and-distortion in the passband, dB.  Higher is better."""
    amp = headroom_amp(q)                       # see the defect box above
    x   = bandlimited(nsamp, 0.0, spec.f_pass, amp, seed)
    y   = run_fixed(x)                          # through the real fixed-point path
    ref = frac_delay(x, (ntap - 1) / 2.0)       # half-integer for even ntap — hence frac_delay
    s   = slice(ntap, None)                     # discard the startup transient
    r2  = r2_aligned(ref[s], y[s])
    return 10.0 * np.log10(r2 / (1.0 - r2)) if 0.0 < r2 < 1.0 else float("-inf")
```

**Test 2 — stopband rejection.** Drive a signal confined entirely to the stopband at full scale (the
filter attenuates it, so there is no overflow risk and it maximizes measurable dynamic range) and
measure how much came out.

```python
def stopband_rej_db(run_fixed, spec, ntap, nsamp=4096, seed=1):
    """Stopband rejection in dB, and the DC bias separately.  Higher rejection is better."""
    amp = max_representable(...)                # full scale: the filter is rejecting this
    x   = bandlimited(nsamp, spec.f_stop, 0.5, amp, seed)
    y   = run_fixed(x)[ntap:]
    dc  = float(y.mean())                       # AP_TRN's -1/2 LSB, NOT noise — see below
    rej = 10.0 * np.log10(float((x[ntap:] ** 2).mean()) / float(((y - dc) ** 2).mean()))
    return rej, dc
```

**Why `dc` is returned rather than folded in.** `AP_TRN` floors, so it subtracts ~½ LSB from *every
output sample*. That is a **coherent DC term, not noise** — it does not average down with a longer
record. Leave it in the power measurement and it masquerades as a rejection floor that more samples
never improve, which is the kind of result that gets debugged for a day. Remove the mean, report the
bias next to the rejection, and both facts stay visible.

**What `run_fixed` is.** `FirCompute.filter_block` *is* the bit-exact fixed-point model — it uses
`mult` / `fixed_sum` / `quantize` throughout — so **neither test needs a simulation**. Instantiate a
`FirCompute`, load `q.stored` into its `taps` state, and call `filter_block` directly; evaluation is
milliseconds, not seconds, which is what keeps `pysim` cheap enough for the agent to use freely.
`filter_block` speaks *packed words*, so go through
[`pack_samples` / `unpack_samples`](../examples/fir_block/fir_block.py#L194) and quantize the input
with `fixpoint.from_real` — the input quantization is one of the two loss points being measured, so
skipping it silently deletes half the answer.

Then a `FirQuality` dataclass bundling Layer 1 and Layer 2 with the `QuantizedFir`, and
`evaluate(spec, ntap, samp_w, samp_i, unroll_lane)` returning it. That function is the body of the
`pysim` MCP tool.

### The reference table — Layer 1 only

Measured 2026-08-17 with the Layer 1 code at `FirSpec(0.20, 0.28)`, `samp_i=2`, Kaiser prototype.
Stopband attenuation in dB. **This is the taps-only upper bound**, not the design's performance —
Layer 2 will come in below every one of these numbers, and the gap is the datapath's contribution:

| `ntap` | float | W=8 | W=12 | W=16 | W=24 |
|---|---|---|---|---|---|
| 8 | 16.8 | 16.9 | 16.8 | 16.8 | 16.8 |
| 16 | 27.9 | 27.7 | 27.9 | 27.9 | 27.9 |
| 32 | 42.3 | **35.9** | 42.3 | 42.4 | 42.3 |
| 64 | 79.5 | 38.0 | **60.8** | 79.5 | 79.5 |
| 128 | 150.1 | 34.8 | 57.5 | **79.9** | 126.2 |

Read it column by column and every claim above is visible. The float column climbs without limit.
Each quantized column tracks it and then flattens — at ≈38 dB for 8 bits, ≈61 for 12, ≈80 for 16,
≈126 for 24, which is about 5–6 dB per bit. The **bold** cells are the knees, and they move right as
the width grows.

And look at the W=8 column: 35.9 → 38.0 → **34.8**. Going from 64 taps to 128 taps makes the filter
*worse* while doubling the DSPs. That is fact 3 above, and it is the single most important thing for
the agent surface: **the objective is not monotone in `ntap`.** Any agent — or test — that assumes
"more taps is never worse" is wrong on this table.

Note also that every knee except W=8's sits outside the committed corpus (`ntap ≤ 32`). That is fine
for M0, which needs no synthesis, but it is the second independent argument for M1's grid extension.

### Nine traps

Layer 1:

1. **Evaluate `q.taps_real`, not `q.h`.** The prototype gives you a beautiful number that describes
   hardware you did not build. This is *the* mistake, and it is silent — the code runs and the answer
   looks great. If your quantized column matches the float column at every width, this is why.
2. **`nfft` must be much larger than `ntap`.** The FFT samples the response at `nfft/2+1` points; too
   few and you step over the ripple peaks and report an attenuation better than reality. At
   `ntap=64` the measured error is 0.3 dB at `nfft=128` and gone by 512. Use `nfft >= 8*ntap`, and
   8192 is free.
3. **Normalize by the passband, not by DC and not at all.** `q.scale` is arbitrary by construction —
   an un-normalized "attenuation" measures the gain the search happened to pick.
4. **`20*log10` for amplitudes, `10*log10` for powers.** Layer 1 measures response magnitudes (20);
   Layer 2 measures mean-square signal powers (10). Mixing them up is a factor-of-two error in dB
   that looks entirely plausible.
5. **Never re-quantize the taps yourself.** `q.stored` is already representable; passing it through
   another rounding step, or writing `np.round(h/delta)*delta`, will drift from the hardware the
   first time a width or a mode changes. Use `fixpoint.from_real` / `to_real` and nothing else.

Layer 2:

6. **Quantize the input.** `bandlimited` returns float64; the hardware takes `ap_fixed<W,I>`. Input
   quantization is one of the two loss points the whole layer exists to measure — forget it and you
   have rebuilt Layer 1 with extra steps.
7. **Watch the amplitude.** See the defect box: at a DC gain of 4.5 a full-scale input wraps under
   `AP_WRAP`, and a wrap is a sign flip. A Layer 2 number that is *far* worse than Layer 1 —
   negative SNDR, rejection near 0 dB — is almost always this and not a subtle precision effect.
8. **Discard the startup transient.** The first block starts from a zero carry, so the first `T-1`
   outputs are filtering against zeros. Skip at least `ntap` samples.
9. **Do not integer-shift to align the passband reference.** Even `ntap` gives a half-integer group
   delay, so the best integer shift is always ½ sample wrong, and it shows up as *distortion that
   scales with frequency* — a plausible-looking, entirely fictitious result. Use `frac_delay`.

## Phases

**M0 — the quality metric.** The design half is **landed**
([`fir_block_quality.py`](../examples/fir_block/fir_block_quality.py)): `FirSpec`, `design_float`,
`design_quantized` and the scale search, smoke-tested against the Layer 1 reference table.

Remaining, in order: (i) the headroom constraint on `design_quantized` — option (a) in the defect box,
and a prerequisite for anything in Layer 2 being meaningful; (ii) Layer 1's response metrics and the
closed-form throughput; (iii) Layer 2's two signal-domain tests; (iv) the `FirQuality` bundle and
`evaluate()`; (v) the tests.

Gate, Layer 1: reproduce the reference table within 0.5 dB, and assert the *shape* rather than the
values — attenuation rises with `ntap` **up to the knee only**, plateaus at 5–6 dB per bit of
`samp_w`, and is **non-monotone in `ntap`** past the knee (the W=8 column falls from 38.0 to 34.8).
*An earlier draft of this plan asserted monotonicity in `ntap`; the measurement refutes it, and a
test written to the earlier claim would have failed against correct code.*

Gate, Layer 2: every signal-domain number lands **below** its Layer 1 counterpart — that ordering is
a theorem, not a coincidence, and violating it means a bug rather than a good design. Plus: stopband
rejection improves with `samp_w` even where Layer 1 has already plateaued (that is output
requantization becoming visible, and it is the whole reason Layer 2 exists), and the reported DC bias
tracks ~½ LSB.

*This is the objective function; nothing downstream is meaningful without it.*

**M1 — the oracle grid.** Resolve the non-binding-constraint question above. Extend the sweep over
`ntap` if that is the answer, and record what the resource model predicted for the new points
**before** measuring them — a held-out extrapolation test the model has not yet faced. Commit the
extended corpus. Sweep the (free) `samp_i` axis in pysim only.

**M2 — the run store.** Content-addressed run directories, `manifest.json`, the sparse joined table,
failure rows. Backed by `ModuleStore` where the record already fits; a thin index over it where not.

**M3 — the six tools, replay-backed.** Register in `waveflow/mcp/registry.py` under a new `dse`
profile. Tool descriptions are the prompt and get the same care as code. Gate: a headless-harness
test driving the full loop against replay.

**M4 — live backend and scoring.** Swap `synth`/`rtlsim` to the real flow behind the same signatures.
Add the scoring harness: N runs, optimum-found rate, synth-equivalents spent, versus a random-search
and a full-grid baseline.

M0–M3 need no toolchain. Only M1 and M4 require Vitis.

## Open questions

0. **Should the coefficient format split from the sample format?** The defect box in
   [M0 in detail](#m0-in-detail--a-guide-for-the-implementer) is the whole argument: taps peak around
   0.25 and samples reach 2.0, so one shared `<W,I>` wastes ~3 bits of coefficient precision and
   forces the scale search to buy them back with gain that costs input headroom one-for-one. `I_tap <
   I_samp` is the standard fix and is almost certainly right. It is listed as a question and not a
   task because it changes `fir_block` itself, perturbs `FirCompute`'s module key (see question 3 —
   the same hazard), and **invalidates the committed 24-point corpus**, which is also the replay
   oracle. So it is cheap as engineering and expensive as bookkeeping. *Leaning: constrain the scale
   for M0, and take the split only if M1 is re-sweeping the grid anyway — at which point it is nearly
   free.*

1. **Does the objective want to be scalar?** Constrained-scalar (maximize attenuation s.t. throughput
   and area) is the stated problem and is what M0 builds. But the honest artifact is a Pareto front
   over (quality, throughput, area), and with a grid this size the agent can extract one from
   `get_results` unaided. Decide whether the context declares a scalar objective or a front — it
   changes what "the agent succeeded" means, so it must be settled before M4's scoring.

2. **Where does the filter spec live?** If `get_dse_context` fixes one passband/stopband spec, the
   benchmark has one answer and can be memorized. If the spec is an argument, the surface generalizes
   and the benchmark can be randomized — at the cost of the corpus no longer being an oracle for
   quality (it still is for area, which is the expensive half). *Leaning: spec as argument, quality
   recomputed live, area from the oracle.*

3. **Does `samp_i` belong in the module key?** It currently rides in the sweep grid as a frozen
   single-value axis. Unfreezing it must not perturb `fir_compute`'s module key in a way that misses
   every existing store lookup — see [F2 in `resource_model.md`](resource_model.md), where attaching a
   model silently moved a key and broke exactly this. Check before M1, not after.

4. **Long-running calls.** M4's live `synth` blocks for ~50 s per point and a batch is minutes. Decide
   between blocking-with-declared-cost and a job handle + `get_results` polling. Replay mode sidesteps
   this entirely, which is another reason to build it first — but the decision cannot be deferred past
   M4.

## Non-goals

* Improving the resource model. It is held out at 3.2%/2.8% on design totals and is the surrogate as
  it stands.
* Making `fir_block` a better filter. It is a probe.
* A general DSE agent. This is one design's surface, built so the *shape* generalizes to CG; the
  generalization is a later plan and should be written only after M4 reports a score.
