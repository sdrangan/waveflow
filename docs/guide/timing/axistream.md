---
title: AXI4-Stream Timing Analysis
parent: Timing Analysis Tools
nav_order: 5
has_children: false
summary: "Pulling AXI4-Stream activity out of a VCD: add_axiss_signals loads a stream's TDATA, TVALID, TREADY and TLAST from one name prefix, and the result plots on a timing diagram or extracts as accepted beats. Worked on the polynomial example's two streams."
---

# AXI4-Stream Analysis

The VcdParser class has specific methods for extracting data on AXI4 streams.

## Loading the AXI4-Stream Signals

After you have created a [VcdParser](./vcd.md), you can load the AXI4-stream signals into the parser as follows.
Consider the [streaming polynomial example](../../examples/stream_inband/), whose kernel `poly`
communicates over two AXI4-Stream interfaces (the capture here is its
[error-path waveform](../../examples/stream_inband/error_path.md), `vcd/error_path.vcd`):

- `s_in`: the input stream, carrying commands -- each a command header (with its coefficients)
  followed by its input samples
- `m_out`: the output stream, carrying a response header and the results for each command

The `TDATA` signals for the streams are:

- `apatb_poly_top.AESL_inst_poly.s_in_TDATA[31:0]`
- `apatb_poly_top.AESL_inst_poly.m_out_TDATA[31:0]`

You can find these names from the print out of the signals -- see the section on [parsing the VCD outputs](./parsing.md).  After identifying the signal names for the AXI4 streams, you can use the code below to load the stream signals:

```python
# Create a parsing class
vp = VcdParser(vcd)

# Get the clock signal name
clk_name = vp.add_clock_signal()

top_name = 'AESL_inst_poly'
in_stream_name = f"{top_name}.s_in_"
out_stream_name = f"{top_name}.m_out_"

# Get the  AXI-Stream command signals
in_str_sigs, in_bw = vp.add_axiss_signals(name=in_stream_name, short_name_prefix='s_in',
                                           ignore_multiple=True)
print(in_str_sigs)

# Get the output AXI-Stream signals
out_str_sigs, out_bw = vp.add_axiss_signals(name=out_stream_name, short_name_prefix='m_out',
                                            ignore_multiple=True)
print(out_str_sigs)
```

Note that we use the `short_prefix_name` so that the signal will have a smaller display name on the timinng diagram.  Running this code, will load the `TDATA`, `TVALID`, `TREADY`, and, if used, a `TLAST` signal for each stream.

## Plotting the Timing Diagram
We can then plot the timing diagram as:

```python
# Get the timing signals
sig_list = vp.get_td_signals()

# Create the timing diagram
td = TimingDiagram()
td.add_signals(sig_list)
trange = None
ax = td.plot_signals(add_clk_grid=True, trange=trange, 
                text_scale_factor=1e4, text_mode='never')
_ = ax.set_xlabel('Time [ns]')
```

## Extracting AXI4-Strea Bursts

AXI4-Streams arrive in **bursts** that end on each `TLAST`.  
You can indentify explicit transfer bursts in the stream:

```python
# Extract the AXI-Stream bursts and print the burst information for the input
bursts_in, clk_period= vp.extract_axis_bursts(clk_name, in_str_sigs)
print('Input AXI-Stream bursts:')
for i, burst in enumerate(bursts_in):
    nbeats = len(burst['beat_type'])
    nbeats_transfer = sum(1 for bt in burst['beat_type'] if bt == 0)
    print(f"Burst {i}: tstart = {burst['tstart']}, bt={burst['beat_type']}, nbeats_transfer = {nbeats_transfer}")

# Extract the AXI-Stream output bursts
print('\nOutput AXI-Stream bursts:')
bursts_out, clk_period= vp.extract_axis_bursts(clk_name, out_str_sigs)
for i, burst in enumerate(bursts_out):
    nbeats = len(burst['beat_type'])
    nbeats_transfer = sum(1 for bt in burst['beat_type'] if bt == 0)
    print(f"Burst {i}: tstart = {burst['tstart']}, bt={burst['beat_type']}, nbeats_transfer = {nbeats_transfer}")
```


## Deserializing Burst Data

In the error-path capture, the host sends two `DATA` commands; the second announces 10 samples
and sends 6.  The bursts are:

| Burst           | Stream | Contents |
|-----------------|--------|----------|
| `bursts_in[0]`  | input  | `PolyCmdHdr` -- `cmd_type`, `tx_id` 71, `nsamp` 8, the four coefficients |
| `bursts_in[1]`  | input  | 8 input samples (float32) |
| `bursts_in[2]`  | input  | `PolyCmdHdr` -- `tx_id` 72, `nsamp` 10 |
| `bursts_in[3]`  | input  | 6 input samples: TLAST came early |
| `bursts_out[0]` | output | `PolyRespHdr` -- `tx_id` 71 echoed |
| `bursts_out[1]` | output | 8 results |
| `bursts_out[2]` | output | `PolyRespHdr` -- `tx_id` 72 |
| `bursts_out[3]` | output | 6 results, closed with TLAST |

The status registers (`halted`, `error`, `tx_id`) are on AXI-Lite, not on these streams.

We can deserialize each burst to the corresponding Waveflow schema:

```python
word_bw = 32
for k in range(2):                      # the two DATA commands, in order
    cmd_hdr = PolyCmdHdr()
    cmd_hdr.deserialize(word_bw=word_bw, packed=bursts_in[2 * k]['data'])
    print("PolyCmdHdr values:")
    for f, v in cmd_hdr.val.items():
        print(f"    {f}: {v}")
    # The burst may end early: decode the samples actually sent.
    nsamp = int(cmd_hdr.val['nsamp'])
    nsent = min(nsamp, len(bursts_in[2 * k + 1]['data']))
    x = read_array(packed=bursts_in[2 * k + 1]['data'], word_bw=word_bw, elem_type=Float32,
                   shape=(nsent,))
    print(f"  x ({nsent} of {nsamp} sent): {x.val}")

    resp_hdr = PolyRespHdr()
    resp_hdr.deserialize(word_bw=word_bw, packed=bursts_out[2 * k]['data'])
    y = read_array(packed=bursts_out[2 * k + 1]['data'], word_bw=word_bw, elem_type=Float32,
                   shape=(nsent,))
    print(f"  response tx_id = {resp_hdr.val['tx_id']}, y: {y.val}")
```

This will return

```
PolyCmdHdr values:
    cmd_type: 0
    tx_id: 71
    nsamp: 8
    coeffs: [ 1. -2. -3.  4.]
  x (8 of 8 sent): [-1.5473033  -0.13626704 -1.6316441   0.5270088   0.46553516 -1.8716747
  1.2296911   1.147179  ]
  response tx_id = 71, y: [-17.905724     1.2067068  -21.09896     -0.3017503   -0.1776706
 -31.993298     1.4420589    0.79642355]
PolyCmdHdr values:
    cmd_type: 0
    tx_id: 72
    nsamp: 10
    coeffs: [ 1. -2. -3.  4.]
  x (6 of 10 sent): [ 1.6612331  0.6812266  0.7714489 -1.3450334 -1.9044447 -1.7377474]
  response tx_id = 72, y: [  7.736436    -0.49011576  -0.49183798 -11.470557   -33.700832
 -25.574167  ]
```

The example wraps this walk in `analyze_poly_vcd` -- see
[Reading the protocol off a waveform](../../examples/stream_inband/poly_axi_stream.md).
