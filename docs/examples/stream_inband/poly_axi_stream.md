---
title: AXI4-Stream Timing Analysis
parent: Streaming polynomial
nav_order: 10
has_children: false
summary: "How to read the protocol off a waveform: decoding a co-simulation VCD of the poly kernel, without rerunning co-simulation, into its commands (headers with their coefficients, sample bursts) and responses (headers, results), in order, at either word width -- including a run that ends in an error -- and plotting the bursts on a timing diagram."
---

# Reading the protocol off a waveform

How do you read this protocol off a waveform?  A co-simulation VCD holds every AXI4-Stream beat:
the words, TVALID, TREADY and TLAST.  `timing_analysis.py` turns those beats back into the protocol's
messages -- commands, responses, their fields -- and their timing, from a VCD you already have,
without rerunning co-simulation.  It uses Waveflow's
[AXI4-Stream VCD analysis tools](../../guide/timing/axistream.md).

The source is
[`examples/stream_inband/timing_analysis.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/timing_analysis.py).

## What it decodes

The analysis walks each stream in protocol order:

- **`s_in`** (`in_stream`): a `PolyCmdHdr`, then, for a `DATA` command with `nsamp > 0`, its sample
  burst, and so on until `END` or the end of the capture.  A sample burst that ended early decodes
  to the samples actually sent.
- **`m_out`** (`out_stream`): for each `DATA` command, a `PolyRespHdr`, then its results.

The status registers are on AXI-Lite and are not part of this decoding; the run's `status.json` has
them.

```python
class PolyTimingResult:
    clk_name: str               # full VCD name of the clock
    clk_period: float           # nanoseconds
    in_signals, out_signals     # AXI4-Stream signal names per stream
    bursts_in, bursts_out       # raw bursts: data, beat_type, tstart, complete
    commands: list[Command]     # each: hdr (PolyCmdHdr), x, hdr_burst, samp_burst, is_end
    responses: list[Response]   # each: hdr (PolyRespHdr), y, hdr_burst, data_burst
    cmd_hdr, x, resp_hdr, y     # shorthand: the first DATA command and its response
```

## Decoding the error-path capture

`vcd/error_path.vcd` is the co-simulation of `early_tlast_vcd`, the run drawn on
[The error path](./error_path.md):

```python
import sys
sys.path.insert(0, "examples/stream_inband")   # the sibling poly / timing_analysis modules
from timing_analysis import analyze_poly_vcd

r = analyze_poly_vcd("examples/stream_inband/vcd/error_path.vcd")
for c in r.commands:
    print(f"cmd  tx_id={c.hdr.tx_id} nsamp={c.hdr.nsamp} "
          f"coeffs={c.hdr.val['coeffs'].tolist()} samples_sent={len(c.x)}")
for s in r.responses:
    print(f"resp tx_id={s.hdr.tx_id} results={len(s.y)} "
          f"closed={bool(s.data_burst['complete'])}")
```

```
cmd  tx_id=71 nsamp=8 coeffs=[1.0, -2.0, -3.0, 4.0] samples_sent=8
cmd  tx_id=72 nsamp=10 coeffs=[1.0, -2.0, -3.0, 4.0] samples_sent=6
resp tx_id=71 results=8 closed=True
resp tx_id=72 results=6 closed=True
```

The coefficients come out of the command headers, because that is where they travel.  The second
command announced 10 samples and sent 6, and its response has 6 results in a **closed** burst:
rule 6, read straight off the wire.

For a 64-bit kernel, pass `word_bw=64, top="poly_bw64"`.

## Plotting the timing diagram

```python
import matplotlib
matplotlib.use("Agg")   # omit for interactive display
from timing_analysis import analyze_poly_vcd, plot_poly_timing

r = analyze_poly_vcd("examples/stream_inband/vcd/error_path.vcd")
ax = plot_poly_timing(r, show=True)
ax = plot_poly_timing(r, trange=(100, 500), show=True)    # zoom, in ns
```

Headers are shaded orange and data bursts green, on every stream signal.

## A stable test fixture

`tests/fixtures/poly/timing/poly_timing_fixture.vcd` is a small **synthetic** VCD -- one `DATA`
command with three samples, then `END` -- for tests.  It is rendered from the current schemas and
model by `python -m tests.poly.poly_timing_fixture`, and a test fails if the committed file drifts
from what that renders.  So a change to the wire format cannot leave it silently stale.

## Capturing a fresh VCD

The `error_vcd` build step does it for the error-path run (see [The error path](./error_path.md)).
For any other run, co-simulate with `-trace_level port` and convert the trace with
[`run_xsim_vcd`](../../guide/timing/vcd.md).

## Check your understanding

1. The decoder walks `s_in` header by header.  How does it know whether a sample burst follows a
   header, and how long it is?
2. Where in a VCD of this kernel would you look for the coefficients a command used?
3. Why can the status registers not be read from the stream VCD?

---

Back to: [Streaming polynomial](./index.md)
