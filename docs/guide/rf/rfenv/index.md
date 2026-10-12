---
title: RF environment
parent: RF converters
nav_order: 4
audience: python
snippets: run
api: [PathSet, taps, ChannelApplier, SceneSpec, DeviceSpec, ArraySpec, SolverSpec, load_or_trace, TraceChannelStep, RtChannel, RtSignalSource, ZadoffChu, Tone, Pn, ArrayWaveform, gain_db_for_dbfs]
summary: "Ray-traced propagation on the RF sample interface. A scene traced with NVIDIA Sionna RT becomes a channel file of paths (amplitude, delay, Doppler, per antenna pair); RtChannel applies it between a DAC edge and an ADC edge, and RtSignalSource puts what a receiver hears onto an ADC edge. Sionna is optional: a committed channel file needs only numpy, and live tracing inside the simulation is there on request. Covers units, timing, wiring to an Rfdc with the I/Q word, the XSI replay, how it was verified, and its limits."
---

# RF environment

`Rfdc` sees the world through [`RFSampIF`](../rfdc/sampling.md) edges. `waveflow.rfenv` puts a
propagation environment on those edges: a channel computed by **ray tracing a 3D scene** with
[NVIDIA Sionna RT](https://nvlabs.github.io/sionna/rt/), applied block by block to complex-baseband
samples.

```text
  SceneSpec ──trace (Sionna RT, once)──► channel file ──► RtChannel       DAC edge ──► ADC edge
  (scene, carrier,                       (PathSet: every                  (the air between them)
   devices, solver)                       path, per antenna  ──► RtSignalSource          ──► ADC edge
                                          pair)                  (what a receiver hears)
```

Two nodes, one channel core:

- **[`RtChannel`](../../../../waveflow/rfenv/nodes.py)** is a **path** between two converter edges,
  like `RfSampDelay` but at multipath fidelity: every path's amplitude, sub-sample delay and Doppler
  shift, per antenna pair, plus thermal noise. Between an `Rfdc`'s `tx_rf` and `rx_rf` it is the
  air between the antennas.
- **[`RtSignalSource`](../../../../waveflow/rfenv/nodes.py)** is **what a receiver hears**: a
  waveform at a transmit power, through the channel, plus noise, onto an ADC edge, like
  `RfDataSource`.

## The channel file

A traced channel is a [`PathSet`](../../../../waveflow/rfenv/paths.py): four arrays shaped
`[n_rx_ch, n_tx_ch, n_paths]`, one channel per (receive antenna, transmit antenna) pair.

| array | holds |
|---|---|
| `a` | each path's complex coefficient, Sionna's, **carrier phase included** |
| `tau` | each path's delay in seconds, **absolute** (not relative to the first path) |
| `doppler` | each path's Doppler shift in Hz |
| `valid` | which entries are paths; Sionna pads the rest |

On disk it is `<stem>.npz` beside a `<stem>.json` manifest that records the scene specification, its
hash, and the versions of the tracer that produced it. `taps(paths, bandwidth, l_min, l_max)` gives
the discrete taps a receiver sampling at that bandwidth sees, by Sionna's own formula.

## Where a channel comes from

Each node takes its channel from one of three places:

- **`pathset=`** — a `PathSet` you already have.
- **`channel=`** alone — a channel file's stem, loaded. A relative stem resolves against
  `channel_root`, or the working directory.
- **`spec=`** — a [`SceneSpec`](../../../../waveflow/rfenv/sionna_backend.py), resolved by `mode`:
  - **cached** (the default) calls `load_or_trace(spec, channel, retrace=...)`, with `channel` as the
    cache's stem. `"if_stale"` reuses the file while its manifest still matches the spec and the
    installed tracer, and traces otherwise. `"never"` is for a committed file: it refuses one traced
    for another spec, and never imports Sionna. `"always"` retraces. `TraceChannelStep` does the
    same as a build-DAG rung;
  - **live** traces at `pre_sim`, inside the simulation, and reads no file.

Tracing needs the optional extra, `pip install -e ".[sionna]"`; it runs on a CPU (Dr.Jit's LLVM
backend), no GPU needed. Without it, everything except tracing works, and a live node or a stale
cache raises a message naming the install command.

```python
import numpy as np

from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec, SolverSpec

spec = SceneSpec(
    scene="simple_street_canyon",  # a Sionna built-in, an XML file, or "empty"
    frequency_hz=3.5e9,
    transmitters=(DeviceSpec("tx", (-30.0, 0.0, 20.0)),),
    receivers=(DeviceSpec("rx", (40.0, 5.0, 1.5)),),
    solver=SolverSpec(max_depth=3),
)

from examples.rt_sounder.rt_sounder import CHANNEL  # examples/rt_sounder/data/street_canyon
from waveflow.rfenv.cache import load_or_trace

paths = load_or_trace(spec, CHANNEL, retrace="never")  # the committed file: no Sionna needed
ok = paths.valid[0, 0]
for tau, a in zip(paths.tau[0, 0][ok], paths.a[0, 0][ok]):
    print(f"{tau * 1e9:6.1f} ns  {20 * np.log10(abs(a)):6.1f} dB")

from waveflow.rfenv import taps

h = taps(paths, 122.88e6, 0, 47)[0, 0, 0]  # rx 0, tx 0, time step 0
db = 20 * np.log10(abs(h) / abs(h).max())
print("taps above -30 dB at lags", np.flatnonzero(db > -30).tolist())
```

```text
 242.1 ns   -80.5 dB
 252.6 ns   -83.8 dB
 244.9 ns   -97.7 dB
 255.3 ns   -99.9 dB
 355.0 ns  -105.2 dB
taps above -30 dB at lags [23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 43, 44]
```

Five paths, four of them within a few nanoseconds of each other; at this sample rate a path rarely
lands on a sample, so each spreads into sinc sidelobes, and the taps a receiver sees form a cluster
plus a distant echo.

## Units and levels

Samples on both edges are the **complex envelope in √W**: |x|² is the power at the antenna port, in
watts.

- `RtSignalSource` scales a unit-power waveform (`ZadoffChu`, `Pn`, `Tone`, or your own
  `ArrayWaveform`) by √P for `tx_power_dbm`, applies the channel, adds thermal noise, and multiplies
  by `gain_db`.
- `RtChannel` reads its input as √W at the transmit antenna (`tx_gain_db` maps other units onto it),
  applies the channel, adds thermal noise, and multiplies by `gain_db`.
- **`gain_db` is the receive chain**, in amplitude dB, applied after the noise: it maps watts at the
  antenna onto the receiving converter's units, which its `full_scale` defines.
  `gain_db_for_dbfs(power_w, target_dbfs, full_scale)` gives the gain that puts a signal at a chosen
  level.
- **Thermal noise** is kTBF over the edge's sample rate, on when `noise_figure_db` is set. It is
  drawn by absolute sample index from `seed` and the node's name, so it is the same whatever the
  block size, and two nodes never share a stream.

## Timing

- **`delay_mode="absolute"`** (the default) delays each path by its true delay; `"relative"` moves the
  earliest path to zero.
- Each path's delay is realized by a Kaiser-windowed sinc interpolator of `filt_len` taps, which
  delays every path by a further **D = `filt_len` / 2** samples. That is the node's declared
  `latency_samp`; `blk_latency` is zero. A loop through the air subtracts the converter hop plus D,
  once.
- A node computes each block from the edge's absolute sample index (`RfBlock.idx`), so the output
  does not depend on the block size. A block that never arrived is replaced by zero input, so the
  output edge stays on its grid; such blocks are counted in `dropped_blocks`.
- Doppler is a per-path phase rotation at the path's shift.

`filt_len` sets accuracy against cost. The default, 64, keeps a sub-sample delay's error at or below
−60 dB of a signal that occupies up to 0.8 of the Nyquist band. Far sidelobes are where a short window departs most from
Sionna's exact sinc; to compare taps 30 dB down with Sionna's, use more (the sounder uses 256).

## Wiring to an Rfdc

Both of a node's edges are complex, so the converter's word must be an **I/Q word**: the RFSoC 4x2's
`Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)` carries two complex samples a beat on
the same 64-bit bus (see [real and I/Q](../rfdc/iqmode.md)). The channel counts come from the
channel: `rf_in.n_ch` is its transmit antennas, `rf_out.n_ch` (or the source's edge) its receive
antennas, and `pre_sim` refuses a mismatch, a real edge, or edges at different rates or block
sizes.

A source on an ADC, with the strongest tap at −10 dBFS:

```python
from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rfdc import Rfdc
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.rfenv.noise import dbm_to_w, gain_db_for_dbfs
from waveflow.rfenv.nodes import RtSignalSource
from waveflow.rfenv.waveforms import ZadoffChu
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamSink

sim = Simulation()
word = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)
rfdc = Rfdc(name="rfdc", sim=sim, n_rx=1, n_tx=0, word=word, full_scale=1.0)
p_strongest = abs(h).max() ** 2 * dbm_to_w(30.0)  # watts the strongest tap delivers
src = RtSignalSource(
    name="rx_ant",
    sim=sim,
    waveform=ZadoffChu(length=1021, root=25),
    tx_power_dbm=30.0,
    pathset=paths,
    gain_db=gain_db_for_dbfs(p_strongest, -10.0, rfdc.full_scale),
    noise_figure_db=7.0,
    n_blk=8,
)
sink = StreamSink(name="sink", sim=sim, bitwidth=rfdc.axis_bitwidth, has_tlast=True,
                  queue_size=256)

adc_if = RFSampIF(name="adc_if", sim=sim, samp_clk=Clock(name="samp", freq=122.88e6),
                  n_ch=1, blksize=256, n_blk=8, complex_samp=True)
adc_if.bind("tx", src.rf_ep)
adc_if.bind("rx", rfdc.rx_rf)
axis = StreamIF(name="adc_axis", sim=sim, clk=Clock(name="axis", freq=250e6), bitwidth=64)
axis.bind("master", rfdc.rx_streams[0])
axis.bind("slave", sink.stream_ep)

sim.run_sim()
adc_if.assert_clean()
print(adc_if.counters(), sum(len(w) for w in sink.words), "words")
```

```text
{'blocks_sent': 8, 'blocks_delivered': 8, 'underrun': 0, 'overrun': 0} 1024 words
```

`RtChannel` goes between the two directions of one converter, which owns both edges' epochs:

<!-- snippet: skip -->
```python
dac_if.bind("tx", rfdc.tx_rf)
dac_if.bind("rx", chan.rf_in)   # chan = RtChannel(..., pathset=paths)
adc_if.bind("tx", chan.rf_out)
adc_if.bind("rx", rfdc.rx_rf)
```

The [channel sounder](../../../examples/rt_sounder/index.md) builds both graphs whole.

### At RTL

`RtChannel` is pysim-only: an XSI graph cannot contain it. `RtSignalSource` can: its twin is the
existing `RfFileSource`, replaying the bundle `render_bundle()` writes from the same computation, so
the RTL sees the same bytes pysim did. Render at the bound edge's geometry (the method refuses
another), and give the graph the same name in both backends: the noise is keyed by the node's name.
`in_bundle` is the node's only `DynParam`.

## How it was verified

`plans/sionna_rt/sionna_rt.md` holds the criteria and the evidence. In brief:

| check | against | bound |
|---|---|---|
| free space | d/c and Friis | 1 ps; 0.01 dB |
| one reflector | the image method; the ITU-R P.2040 slab | 1 ps; 0.5 dB |
| block by block | the whole signal at once | relative error 1e-12 |
| sub-sample delay | the analytically delayed signal | −60 dB |
| Doppler | v·cos θ / λ, Sionna's sign | 0.1 Hz |
| `taps()` | Sionna's `Paths.taps()` | relative error 1e-3 |
| live mode | a channel file traced by the same install | relative error 1e-6 |
| the sounder | `taps()`, every tap within 30 dB of the strongest | 0.5 dB |
| the XSI replay | the pysim run | every word that reaches the sink, bit for bit |
| without Sionna | every non-Sionna test, imports blocked | all pass |

The sounder's measured worst case is 0.124 dB, over 17 taps.

## Limits

- **Static geometry.** A trace is one snapshot. Velocities give each path a Doppler shift, but delays,
  gains and the set of paths do not change during a run, so a moving device is modelled well only
  while it moves too little for the geometry to change. Retracing along a trajectory is not built.
- **Narrowband paths.** Each path's coefficient is computed at the carrier, so a path is flat across
  the sampled band; material dispersion across it is not modelled.
- **Single precision.** Sionna traces in single precision, which limits agreement with its own taps to
  the 1e-3 above, and makes a channel file the property of the install that traced it: the manifest
  records the versions, and `"if_stale"` retraces when they change.
- **Thermal noise only**: no phase noise, interference or nonlinearity.
- **Trace time** grows with the scene; tests and examples use small scenes and committed channel
  files.

**Source of truth:** `waveflow/rfenv/`; tests in `tests/rfenv/`; the worked example is
[the channel sounder](../../../examples/rt_sounder/index.md).
