---
title: CG Vector Unit
parent: Linear Algebra
nav_order: 2
has_children: false
snippets: run
summary: "CgVectorCore and CgVectorUnit run the vector steps of fixed-point conjugate gradient, multi-RHS: the start and, per iteration, the column dots, the two guarded divisions and the updates of X, R and P, bit for bit like the model in waveflow.linalg.cg. The matrix multiply S = A·P of each iteration comes from outside: a systolic core job, or a STEP message. Covers the model, the parameters, the core's command and blocks, the unit's START/STEP messages, a runnable example, building and testing, the calibrated cost model with its held-out scores, and the limits."
---

# CG Vector Unit

`CgVectorCore` and `CgVectorUnit` (`waveflow/linalg/cg_vector.py`) run the vector steps of
fixed-point **conjugate gradient** on `A X = B`: `A` Hermitian positive definite (`k × k`) and `n`
right-hand sides (`B`, `k × n`), each column with its own step sizes. A job runs `nit` iterations
from `X = 0`. The matrix multiply of each iteration, `S = A·P`, is not in this unit: in a design, a
[systolic core](./systolic.md) job with `nb = nit` computes the `nit` matrices `S` from the `nit`
matrices `P` this unit writes; the standalone unit takes `S` in its messages.

The bit-exact model is `waveflow.linalg.cg`. The pysim modules call it, and the HLS body equals it
in C-simulation (with the tasks fired in order; see [Limits](#limits)) and at RTL. For what the
linear-algebra components share (formats, lane groups, messages, the build step), see
[Linear Algebra](./index.md).

## The model

From `X = 0`, `R = q_R(B)`, `P = q_P(R)`, `rz = q_rz(Σ|R|²)` (`cg_init`), each iteration performs,
in this order (`q_V` rounds and saturates to the register format of `V`; `w` widens the dividend by
`g_div` fraction bits, which is exact; the divisions return 0 for a zero divisor):

| Step | Operation | Where |
|---|---|---|
| 1 | `S = q_S(A·P)`, an exact sum over `k` | `mm_step`: the matrix multiply |
| 2 | `ps = q_ps(Σ Re(conj(P)·S))` per column | `vec_step`: this unit |
| 3 | `alpha = q_alpha(w(rz) / ps)` | |
| 4 | `X = q_X(X + P·alpha)` | |
| 5 | `R = q_R(R − S·alpha)`, the recurrence | |
| 6 | `rz' = q_rz(Σ|R|²)` | |
| 7 | `beta = q_beta(w(rz') / rz)` | |
| 8 | `rz = rz'` | |
| 9 | `P = q_P(R + P·beta)` | |

Every product and sum before a `q_` is exact. A column whose residual reaches zero freezes: `alpha`
and `beta` take the zero guard, and `R` and `P` stay zero. The model also has the explicit form
`R = q_R(B − A·X)` (`vec_step`'s `residual` hook, built by `residual_hook`), which the hardware does
not implement.

The formats are one `CgFormats`: the ten registers `A`, `B`, `P`, `R`, `S`, `X`, `ps`, `rz`,
`alpha`, `beta`, each a `Format`, and the dividend widening `g_div`. `cg_solve(ar, ai, br, bi, nit,
formats)` is the reference solve on stored integers; every function is batched over leading
dimensions.

## Parameters

| Parameter | Default | Meaning | Constraint |
|---|---|---|---|
| `Kmax`, `Nmax` | 8, 32 | the largest `k` and `n` a job may have | `L` divides `Nmax` |
| `nitmax` | 8 | the most iterations a job may run | at least 1 |
| `L` | 4 | lanes: columns processed side by side, each with its own pair of dividers | a power of two |
| `formats` | required | a `CgFormats` | signed; `B`, `S`, `P`, `X` each at most `lane_bits` wide (unit) |
| `sob_depth` | 2 | blocks in each stream-of-blocks buffer | |
| `word_bits` | 64 | message word width (unit only) | 32 or 64 |
| `lane_bits` | 16 | width of each part of a memory element (unit only) | `2·lane_bits` divides `word_bits` |
| `clk` | 250 MHz | the clock of the pysim timing | |

**Run-time values.** Each job states its own `nit`, `k` and `n`: `1 ≤ nit ≤ nitmax`,
`1 ≤ k ≤ Kmax`, and `1 ≤ n ≤ Nmax` with `L` dividing `n`. `waveflow.linalg.cg_vector.cmd_status`
says whether a job is valid. The iteration count is fixed per job, so a job's time does not depend
on its data.

## The core

`CgVectorCore` is a `FreeRunMod` with five ports:

| Port | Kind | Carries |
|---|---|---|
| `cmd_in` | stream, 64 bits | one `CgVectorCmd` (`nit`, `k`, `n`) per job |
| `b_blk` | stream of blocks, in | `B`, once per job |
| `s_blk` | stream of blocks, in | `S`, once per iteration |
| `p_blk` | stream of blocks, out | `P₀ … P_nit−1`, one per iteration |
| `x_blk` | stream of blocks, out | `X`, after the last iteration |

Every block holds a `k × n` matrix as row-major lane groups of `L`, the systolic core's layout. A
job reads `B`, starts, and writes `P₀`; then, per iteration, it reads `S` and writes the next `P`,
or `X` after the last. Per iteration and per group of `L` columns it makes three pipelined passes
over the `k` rows: the dot `ps`, then `X`, `R` and `rz'`, then `P`, with the two divisions between
them. The core does not check its command, and the pysim core raises on an invalid one.

Paired with a `SystolicCore`, a CG job is a `SystolicCore` job with `nb = nit` (`A` once, the `nit`
matrices `P` in, the `nit` matrices `S` out) beside one `CgVectorCore` job; the two cores must then
share `L`, and the dimensions must also meet the systolic core's rules.

The body is `waveflow/build/cg_vector_task.h`, a template on the maxima, `L`, `sob_depth` and the
format id. Its traits carry the registers and the exact types the body needs: `rzw_t` (the widened
dividend) and the dot accumulators `dot_ps_t` and `dot_rz_t`, sums of `Kmax` terms. The example
below prints these three for one configuration.

## The standalone unit

`CgVectorUnit` puts the core behind three tasks and speaks the framed messages of
[Linear Algebra](./index.md#messages) on `s_in` and `s_out`:

| Task | Body | Does |
|---|---|---|
| `CgVectorRx` | `cg_vector_rx_task.h` | reads each request, validates it against the job in progress, sends an accepted `START`'s job to the loader and the core and every reply header to the store, then forwards the payload, or drains it if rejected |
| `CgVectorLoad` | `cg_vector_load_task.h` | unpacks a job's `B` and then its `nit` matrices `S` into lane groups |
| `CgVectorCore` | `cg_vector_task.h` | the core (above) |
| `CgVectorStore` | `cg_vector_store_task.h` | writes each reply header, then `P`, or `X` for the job's last step |

**A job** is a `START` request and then `nit` `STEP` requests:

| Request | Header | Payload | Reply |
|---|---|---|---|
| `START` (op 1) | `k`, `n`, `nfollow` = `nit` | `B` (`k × n`) | `P₀` |
| `STEP` (op 2) | the same `k` and `n`, `nfollow` = the steps still to come | `S` (`k × n`) | the next `P`, or `X` when `nfollow` is 0 |

A reply carries the request's `tag`, `op`, `k`, `n` and `nfollow`, a `status` and, if served, a
payload of `k × n` values (`length` = `message_words(k, n)`). The receiver checks a request in this
order:

1. `BAD_OP`: `op` is neither `START` nor `STEP`.
2. `BAD_SEQUENCE`: a `STEP` outside a job, a `START` inside one, or a `STEP` whose `k`, `n` or
   `nfollow` disagree with its job.
3. `BAD_DIMS`: a `START` whose `nit`, `k` or `n` break a rule of [Parameters](#parameters).
4. `BAD_LENGTH`: `length` is not `message_words(k, n)`.

A rejected request is answered with its status and no payload and drained by its `length`, and a
job in progress continues with its next valid `STEP`. One receiver firing handles a request outside
a job, or a whole job with the requests rejected inside it, so a job's state lives in the firing.

## A worked example

This page's Python blocks form one script, which the docs tests run; the output under each block is
what it prints. It runs a three-iteration job through the unit in pysim, with a rejected request
inside the job, and checks `X` against the model.

The formats are the 12-bit set the calibration's centre build used: vectors, `alpha` and `beta` 12
bits, `ps` and `rz` 20 bits, `g_div` 6. The traits derive the exact types from them:

```python
from waveflow.linalg import cg
from waveflow.linalg.cg_vector import CgVectorUnit
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode


def reg(W, I):
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


f = cg.CgFormats(
    A=reg(12, 3), B=reg(12, 4), P=reg(12, 4), R=reg(12, 4), S=reg(12, 5), X=reg(12, 3),
    ps=reg(20, 10), rz=reg(20, 9), alpha=reg(12, 5), beta=reg(12, 3), g_div=6,
)
sim = Simulation()
unit = CgVectorUnit(name="cg", sim=sim, Kmax=8, Nmax=32, nitmax=8, L=4, formats=f)
for name, fmt in unit.core.traits.types[-3:]:
    print(f"{name:9} W={fmt.W:2} I={fmt.int_bits}")
```

```text
rzw_t     W=26 I=9
dot_ps_t  W=28 I=13
dot_rz_t  W=28 I=12
```

A system of 8 unknowns and 32 right-hand sides, quantized to the formats. The reference solve gives
`X` after three iterations, and the model's matrix multiply gives the three matrices `S` the unit
will be sent:

```python
import numpy as np
from waveflow.utils import fixputils as fx

rng = np.random.default_rng(3)
k, n, nit = 8, 32, 3
H = (rng.standard_normal((64, k)) + 1j * rng.standard_normal((64, k))) / np.sqrt(2)
Y = (rng.standard_normal((64, n)) + 1j * rng.standard_normal((64, n))) / np.sqrt(2)
A = (H.conj().T @ H + 0.1 * np.eye(k)) / 64
B = H.conj().T @ Y / 64
ar, ai = fx.quantize_real(A.real, f.A), fx.quantize_real(A.imag, f.A)
br, bi = fx.quantize_real(B.real, f.B), fx.quantize_real(B.imag, f.B)
x = cg.cg_solve(ar, ai, br, bi, nit, f)
state, ss = cg.cg_init(br, bi, f), []
for _ in range(nit):
    s = cg.mm_step(ar, ai, state.pr, state.pi, f)
    ss.append(s)
    state, _ = cg.vec_step(state, *s, f)
print(np.array_equal(state.xr, x.xr) and np.array_equal(state.xi, x.xi))
```

```text
True
```

The requests: a `START` with `B`, then the three `STEP` requests with `S`, and between the first
and the second step one more `STEP` whose `nfollow` is wrong:

```python
from waveflow.linalg.cg_vector import CgOp
from waveflow.linalg.lanes import to_words
from waveflow.linalg.message import header


def request(tag, op, nfollow, payload, fmt):
    words = to_words(*payload, fmt)
    h = header(tag, op, k=k, n=n, nfollow=nfollow, length=len(words))
    return [h.serialize(word_bw=64), words]


bursts = request(1, CgOp.START, nit, (br, bi), f.B)
for i, s in enumerate(ss, start=1):
    if i == 2:
        bursts += request(9, CgOp.STEP, 0, s, f.S)  # nfollow should be 1: refused
    bursts += request(1 + i, CgOp.STEP, nit - i, s, f.S)
```

In pysim, a `StreamDriver` plays the requests onto `s_in` and a `StreamSink` collects the replies:

```python
import tempfile
from pathlib import Path
from waveflow.hw.interface import StreamIF
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

drv = StreamDriver(name="drv", sim=sim, has_tlast=True, in_bundle="req")
sink = StreamSink(name="sink", sim=sim, has_tlast=True, queue_size=256)
for name, src, dst in (("in", drv.stream_ep, unit.s_in), ("out", unit.s_out, sink.stream_ep)):
    link = StreamIF(name=name, sim=sim, clk=unit.clk, bitwidth=64, framed=True)
    link.bind("master", src)
    link.bind("slave", dst)
with tempfile.TemporaryDirectory() as tmp:
    write_burst_bundle([np.asarray(b, np.uint64) for b in bursts], Path(tmp) / "req")
    drv.root = Path(tmp)
    sim.run_sim()

from waveflow.linalg.lanes import from_words
from waveflow.linalg.message import LinalgHeader, Status

replies = list(sink.words)
while replies:
    h = LinalgHeader().deserialize(replies.pop(0), word_bw=64)
    line = f"tag {int(h.tag)}: {Status(int(h.status)).name:12} nfollow {int(h.nfollow)}"
    if int(h.length):
        payload = replies.pop(0)
        line += f", {len(payload)} words"
    print(line)
xr, xi = from_words(payload, k * n, f.X)
print(np.array_equal(xr.reshape(k, n), x.xr) and np.array_equal(xi.reshape(k, n), x.xi))
```

```text
tag 1: OK           nfollow 3, 128 words
tag 2: OK           nfollow 2, 128 words
tag 9: BAD_SEQUENCE nfollow 0
tag 3: OK           nfollow 1, 128 words
tag 4: OK           nfollow 0, 128 words
True
```

Every request got a reply. The refused step left the job running, and the last reply, `X`, equals
the reference solve's.

The cost model prices this configuration per task, plus the unit's channels, and gives the cycles a
`START` and a `STEP` of this job add to a stream of messages, with the core's share of each:

```python
from waveflow.linalg import cg_cost

for task, row in cg_cost.predict_unit(unit).items():
    print(f"{task:23} " + " ".join(f"{key}={row[key]:5.0f}" for key in ("lut", "ff", "dsp", "bram")))
coef = cg_cost.message_model()
for op in (CgOp.START, CgOp.STEP):
    feats = cg_cost.message_features(unit, op, k, n)
    share = cg_cost.core_interval(coef, op, k, n, L=4, formats=f)
    print(op.name, round(cg_cost.message_interval(coef, feats)), round(share))
```

```text
cg_vector_rx_task       lut= 1090 ff=  485 dsp=    1 bram=    0
cg_vector_load_task     lut= 3010 ff= 1078 dsp=    1 bram=    0
cg_vector_task          lut=15091 ff= 9352 dsp=   49 bram=    0
cg_vector_store_task    lut= 1890 ff=  832 dsp=    1 bram=    0
cg_vector_unit_channels lut= 2628 ff= 2606 dsp=    0 bram=   20
total                   lut=23709 ff=14353 dsp=   52 bram=   20
START 475 196
STEP 1420 1141
```

This configuration was a calibration build. Measured with Vitis HLS / Vivado xsim 2024.1
(`examples/mimo_cg/paper_data/cg_modules.csv` and `cg_cycles.csv`), it took 24,224 LUT, 14,482 FF,
52 DSP and 20 block RAM, and its `8 × 32` steps came 1,411 to 1,413 cycles apart.

## Building and testing it

**Headers.** As for the systolic unit, `collect_parts` gathers what the unit needs and
`gen_linalg_headers` writes it:

```python
from waveflow.linalg.build import collect_parts, gen_linalg_headers

parts = collect_parts(unit)
with tempfile.TemporaryDirectory() as tmp:
    inc = gen_linalg_headers(tmp, parts.traits, parts.bodies, schemas=parts.schemas)
    print(sorted(p.name for p in inc.iterdir() if p.name.startswith(("wf_", "cg_"))))
```

```text
['cg_vector_load_task.h', 'cg_vector_rx_task.h', 'cg_vector_store_task.h', 'cg_vector_task.h', 'wf_cg_vector_cmd.h', 'wf_cg_vector_cmd_tb.h', 'wf_lanes.h', 'wf_linalg_header.h', 'wf_linalg_header_tb.h', 'wf_linalg_msg.h', 'wf_linalg_traits.h', 'wf_matrix_io.h']
```

**The top and the RTL test.** `tests/linalg/_cg_unit_bench.py` feeds the unit from memory through
an in-band `MemRStream` and lands its replies through an in-band `MemWStream`, as the systolic
unit's bench does, with one memory read per request and one write per reply. A scenario is a list of
CG jobs, whose `S` matrices the model computes, and requests meant to be rejected, between jobs or
inside them.

**The tests** in `tests/linalg/`:

| File | Checks | Marker |
|---|---|---|
| `test_cg_model.py` | the model against 228 golden cases frozen from the implementation it replaced (`data/cg_golden.npz`: 17 format sets, K = 4, 8, 16, zero guards, saturation, both residual forms), and the reference solve | none |
| `test_cg_vector_core.py` | pysim and C-simulation of the core against the model, two instances with different formats in one design, and csynth at 4 ns at four configurations | `vitis` for C-sim and csynth |
| `test_cg_vector_unit.py` | the unit in pysim and at RTL (XSI): jobs of several shapes, formats and iteration counts back to back, with every kind of rejection between and inside jobs | `xsi` for RTL |
| `test_cg_cost.py` | the counted rules against measured builds, the fitting procedures, and the packaged models | none |

## The cost model

`waveflow/linalg/cg_cost.py` holds the model forms, the CG twin of the systolic unit's
`waveflow/linalg/cost.py`. The fitted numbers live in the same packaged platform
([Linear Algebra](./index.md#cost-data)).

### Resources

**DSPs and block RAM are counted**; **LUTs and flip-flops are fitted**:

| Part | DSP (counted) | Block RAM (counted) | LUT, FF (fitted on) |
|---|---|---|---|
| core | 12 per lane from 12-bit vectors, 5 at 10 bits, 3 at 8 bits (of the twelve multiplies per lane, the rest are built from LUTs); plus 1 index product | the six state arrays (`X`, `R`, `P`, real and imaginary; `Kmax × Nmax/L` per lane) by the device rule | lanes, lanes × vector width, lanes × `ps` width, the dividers' size, the multiplies built from LUTs, the width |
| receiver | 1 (index product) | none | the word width |
| loader | 1 | none | `L·W`, `L·log₂L·W`, `W` over `B` and `S` |
| store | 1 | none | `L·W`, `L·log₂L·W`, `W` over `P` and `X`, the word width |
| channels | none | the four stream-of-blocks buffers, by the systolic unit's buffer rule; plus the `m_axi` adapters: 8 with 64-bit words, 4 with 32-bit | the word width |

The binding of narrow multiplies to LUTs is Vitis HLS 2024.1's on this part, kept with the model.
`cg_cost.predict_unit(unit)` gives each part and the sum; or, as for the systolic unit,
`top.add_rm(cost.platform())` and `compose(top)`, where the `CgVectorUnit` carries its channels.

### Cycles

The cycle model gives a message's **steady interval**, the time it adds to a stream of messages fed
from memory, as a sum of compute and transfer terms:

<!-- snippet: skip -->
```python
interval = c0 + c · (start, start_rows, start_groups, rows, groups, groups_div, w_in, w_out)
```

| Term | Value |
|---|---|
| `start` | 1 for a `START`, 0 for a `STEP` |
| `start_rows`, `start_groups` | a `START`'s pass, `k·ng`, and its column groups, `ng` (`ng = n/L`) |
| `rows`, `groups` | a `STEP`'s passes, `k·ng`, and its column groups, `ng` |
| `groups_div` | `ng ×` the dividend's width (the dividers' latency grows with it) |
| `w_in`, `w_out` | words in (header and payload) and words out |

A rejected request costs `q0 + q1·w_in` right after a served request (it overlaps that request's
work) and `r0 + r1·w_prev` after another rejection, where `w_prev` is the previous request's words
in: the receiver answers a request before draining it, so the gap follows the previous drain.

**Simulated time.** pysim takes its time from the same model: the core spends the intercept and
its compute terms per `START` and per `STEP` (`cg_cost.core_interval`). Transfers take a word per
cycle and overlap the compute, so a stream sent straight to the unit runs at the core's share in
pysim: 1,141 cycles per step for the job of the example, where the memory-fed sum is 1,420.

### Calibration and accuracy

The models were fitted on **31 builds** chosen by rule: the centre, each parameter varied alone,
the corners, the stress set at both word widths, small buffers at 32-bit words, state arrays on
both sides of the block-RAM threshold, and other widths at many lanes. Each build was synthesized at
4 ns for `xczu48dr-ffvg1517-2-e` and simulated at RTL on four job shapes, each twice back to back,
a job with a rejected request inside it, and rejected requests after served ones and back to back.
Every reply was checked bit for bit. One fitted term, `start_groups`, was added on the calibration
builds before the models were frozen.

The models were then frozen and scored on **12 held-out builds**, drawn at random with a fixed
seed from the parameter space minus the calibration builds and committed before the campaign ran.
All 12 were bit-exact at RTL. The scores (Vitis HLS / Vivado xsim 2024.1;
`examples/mimo_cg/paper_data/cg_validation_metrics.csv`, per build in `cg_validation.csv`):

| Quantity | Unit | Core |
|---|---|---|
| DSP exact | 100% of builds | 100% of builds |
| block RAM exact | 100% of builds | 100% of builds |
| LUT, mean error (worst) | 2.6% (6.4%) | 1.3% (3.2%) |
| FF, mean error (worst) | 4.3% (8.3%) | 5.1% (17.4%) |
| served intervals, mean error (worst) | 2.0% (30.8%) | |

The three RTL runs of `tests/linalg/test_cg_vector_unit.py`, predicted as the sum of their
requests' intervals: centre 14,007.5 cycles against 14,070 measured (0.4%), smallest 2.4%, stress
1.8% (`paper_data/cg_unit_8_3_cycles.csv`). Every one of the 43 builds met 4 ns in synthesis
(estimated 3.352 ns, `cg_builds.csv`). The study tooling is `examples/mimo_cg/hw/cg_cal.py`.

## Limits

* **One tool version, one part, one clock.** The cost models describe Vitis HLS / Vivado xsim
  2024.1 on `xczu48dr-ffvg1517-2-e` at 250 MHz.
* **Steady intervals only.** The model gives the time a message adds to a stream. The first reply
  after reset comes later: on the held-out builds it came 38 to 115 cycles after the model's steady
  `START` interval (`cg_validation_metrics.csv`).
* **The calibrated range.** The calibration builds spanned `Kmax` in {4, 8, 16} with `nitmax =
  Kmax`, `Nmax` in {16, 32}, `L` in {1, 2, 4, 8, 16}, the example's formats with vector width `W` in
  {8, 10, 12, 14, 16} and guard `g` in {0, 4, 8} (`ps` and `rz` `W + g` bits, `g_div` 6) and its
  stress set, and both word widths. Every build used `lane_bits = 16` and `sob_depth = 2`. Nothing
  outside this range was measured.
* **Rejections after rejections.** A rejected request after another rejection is priced from the
  previous request's drain, which misses chains of overlaps: on the held-out builds such requests
  were off by 26% on average and 84% at worst, a few dozen cycles each. Right after a served
  request, a rejection was off by 9.3% on average.
* **`S` comes from outside.** The standalone unit takes `S` in its messages. The loop closed
  around a systolic core is not tested here.
* **A job must finish.** A job in progress waits for its next valid `STEP`; a client that abandons a
  job blocks the unit.
* **The explicit residual form is the model's only.** The hardware computes the recurrence.
* **Memory-fed designs.** As for the systolic unit, the in-band reader and writer in one generated
  top must fire equally often; the unit's bench reads once per request and writes once per reply.
* **The cycle model's setting.** The sum form was measured with the unit fed from memory. pysim
  overlaps transfers with compute, and a unit fed straight from a stream at RTL is untested.
* **Back-pressure.** Every RTL test drains `s_out` with an always-ready sink.
* **C-simulation of a design with the core.** Vitis HLS 2024.1's threaded C-simulation hands a
  stream-of-blocks block to its reader as soon as the writer acquires it; the tests C-simulate the
  task bodies one after another.
* **Reported confidence.** `compose` reports the fitted LUT and FF as `UNCALIBRATED` (the
  framework's saved Vitis models keep no fit summary); the held-out scores are the evidence.
