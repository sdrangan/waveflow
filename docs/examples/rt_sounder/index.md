---
title: A channel sounder in a ray-traced street
parent: Examples
nav_order: 10.5
audience: python
snippets: run
example_dir: examples/rt_sounder
summary: "The worked example for the RF environment: a street canyon traced with NVIDIA Sionna RT into a committed channel file, then sounded through the converter Waveflow already models. In channel mode a Zadoff-Chu sequence goes out of an Rfdc DAC, through RtChannel, back into the ADC, and a circular deconvolution recovers every ray-traced tap within 30 dB of the strongest, at its own lag, within 0.5 dB. In source mode RtSignalSource feeds the RF loopback's pass-through, and its rendered bundle is replayed through that design's RTL word for word."
---

# A channel sounder in a ray-traced street

The worked example for the [RF environment](../../guide/rf/rfenv/index.md): the classic sounding
experiment, run through the converter model, with the air between the antennas ray traced.

```bash
python -m examples.rt_sounder.rt_sounder_build                 # trace the scene (the file is committed)
python -m examples.rt_sounder.rt_sounder                       # channel mode: the tap table
pytest tests/examples/test_rt_sounder.py
python -m examples.rf_loopback.rf_dut_build --through csynth   # source mode at RTL: the DUT's RTL
pytest -m xsi -rs tests/examples/test_rt_sounder_xsi.py
```

## The street

Sionna RT's `simple_street_canyon` at 3.5 GHz, with the transmitter 20 m up over an open cross
street and the receiver at street level in the canyon, 72.6 m away. `rt_sounder_build.py` traces it
once into `examples/rt_sounder/data/street_canyon.{npz,json}`; after that, nothing here needs Sionna.

| path | delay | gain |
|---|---|---|
| line of sight | 242.1 ns | −80.5 dB |
| | 244.9 ns | −97.7 dB |
| | 252.6 ns | −83.8 dB |
| | 255.3 ns | −99.9 dB |
| | 355.0 ns | −105.2 dB |

Four paths arrive within 13.2 ns, so at 122.88 MSa/s they blur into a cluster of taps over lags 23
to 37; the fifth is a separate echo at lags 43 and 44. A sounder has to resolve both.

## Channel mode: sounding the air

```text
  StreamDriver ──► Rfdc.tx_streams[0] │ Rfdc.tx_rf ──RFSampIF──► RtChannel
                                                                     │
  StreamSink   ◄── Rfdc.rx_streams[0] │ Rfdc.rx_rf ◄──RFSampIF───────┘
```

`RtSounderTB` is wired like [`rf_shot_loopback`](../rf_shot_loopback/index.md), with
`RtChannel` in place of the bulk delay. One `Rfdc`, with the I/Q word, owns both epochs, so an ADC
sample's index minus its DAC sample's index is the loop's declared latency plus the path's delay:

- the **converter hop**, one block of 256 samples: a block exists at its grid tick and crosses the
  next period;
- **D**, the channel's interpolator delay, `filt_len` / 2 = 128 samples.

The DAC plays a Zadoff-Chu sequence of length 1021 at half full scale, and `gain_db` puts the
strongest tap at −10 dBFS at the ADC, with noise off. One period of the capture, deconvolved by the
same period of what the DAC sent and read 384 samples earlier, is the channel's circular response:

```python
from examples.rt_sounder.rt_sounder import main

main()
```

```text
simple_street_canyon, 3.5 GHz, 122.88 MSa/s; gain_db 77.36; loop latency 384 samples
lag   Sionna    sounder   diff (dB, re Sionna's strongest tap)
 23   -29.90    -29.94   -0.047
 24   -28.45    -28.48   -0.032
 25   -26.72    -26.74   -0.020
 26   -24.58    -24.59   -0.010
 27   -21.77    -21.77   -0.005
 28   -17.66    -17.66   -0.000
 29    -9.99     -9.99   +0.001
 30    -0.16     -0.16   -0.000
 31     0.00     -0.00   -0.001
 32   -20.59    -20.60   -0.009
 33   -23.90    -23.92   -0.021
 34   -25.96    -26.00   -0.033
 35   -27.57    -27.62   -0.046
 36   -28.86    -28.93   -0.061
 37   -29.91    -29.99   -0.079
 43   -26.62    -26.71   -0.089
 44   -29.07    -28.94   +0.124
worst |diff| 0.124 dB; strongest lag Sionna 31, sounder 31; strongest tap off Sionna's set: lag 38 at -30.84 dB
```

Every tap within 30 dB of the strongest is recovered at the lag where Sionna puts it, within 0.5 dB,
and nothing else rises above that window. The comparison has teeth: read at any other latency within
a block either way, the same estimate fails.

**Why `filt_len` is 256 here.** A path between two samples spreads into sinc sidelobes, so this
channel has taps 30 dB down well away from its paths. `RtChannel` realizes each delay with a
Kaiser-windowed sinc, and the window attenuates exactly those far sidelobes: with the default 64
taps, the taps above the window differ from Sionna's exact sinc by up to 1.49 dB, more than the
bound; with 256, by 0.125 dB. The price is the longer D.

## Source mode: the street at RTL

`RtSignalSource` puts what the receiver hears onto the ADC edge: the same sequence sent at 30 dBm,
through the channel, with a 7 dB noise figure, and the strongest tap at −10 dBFS. `RtSourceTB` feeds
it through the [RF loopback](../rf_loopback/index.md)'s pass-through, at the geometry of that
example's I/Q gate: 128 MSa/s, 8 blocks of 256 samples.

- **pysim**: the DAC plays the ADC-quantized source, bit for bit, behind the declared two-block
  startup.
- **RTL**: `rt_sounder_xsi.py` lowers the same graph to an XSI harness in this example's `xsi/`. The
  source's twin is `RfFileSource`, replaying the bundle `render_bundle()` wrote, and the DUT is
  `rf_loopback`'s `rf_pass_through` RTL, used as it is.

That pass-through reads a whole 64-word block before it writes one, so while the DAC paces its writes
the ADC's words have nowhere to go. At RTL it drops 164 of the 1024 words, in 9 gaps, and 6 data
blocks reach the sink, among 31 grid periods. The first survives whole; the rest are spliced across
source blocks at the gaps. So the gate checks every word: each one that reaches the sink is the
matching word of the quantized source, in order, and the words between are exactly the recorded
gaps. The counters are the I/Q loopback gate's own expectations.

The noise is drawn from the source node's name, so both backends name the testbench the same; the
gate checks that the replayed bundle is pysim's, bit for bit.

## Files

| file | holds |
|---|---|
| `rt_sounder.py` | the scene, both testbenches, the deconvolution and the comparison |
| `rt_sounder_build.py` | the trace rung |
| `rt_sounder_xsi.py` | the XSI workspace generator |
| `data/street_canyon.*` | the committed channel file |
| `src/rt_sounder_src_counters.cpp` | the hand-written counters main (copied into the generated `xsi/`) |

Tests: `tests/examples/test_rt_sounder.py` (the channel file, channel mode, source mode) and
`tests/examples/test_rt_sounder_xsi.py` (the RTL replay).
