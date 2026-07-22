---
title: AXI4-Stream Timing Analysis
parent: Timing Analysis Tools
nav_order: 5
has_children: false
---

# AXI4-Stream Analysis

The VcdParser class has specific methods for extracting data on AXI4 streams.

## Loading the AXI4-Stream Signals

After you have created a [VcdParser](./vcd.md), you can load the AXI4-stream signals into the parser as follows.
Consider  [polynomial example demo](../../examples/stream_inband/) where the polynomial accelerator kernel,`poly`,  communicates over two AXI4-Stream interfaces:

- `in_stream`: An AXI4 input stream that receives the command header followed by input sample data
- `out_stream`: An AXI4 output stream where the kernel sends response header, output sample data, and response footer

The `TDATA` signals for the streams are:

- `apatb_poly_top.AESL_inst_poly.in_stream_TDATA[31:0]`
- `apatb_poly_top.AESL_inst_poly.out_stream_TDATA[31:0]`

You can find these names from the print out of the signals -- see the section on [parsing the VCD outputs](./parsing.md).  After identifying the signal names for the AXI4 streams, you can use the code below to load the stream signals:

```python
# Create a parsing class
vp = VcdParser(vcd)

# Get the clock signal name
clk_name = vp.add_clock_signal()

top_name = 'AESL_inst_poly'
in_stream_name = f"{top_name}.in_stream_"
out_stream_name = f"{top_name}.out_stream_"

# Get the  AXI-Stream command signals
in_str_sigs, in_bw = vp.add_axiss_signals(name=in_stream_name, short_name_prefix='in_stream',
                                           ignore_multiple=True)
print(in_str_sigs)

# Get the output AXI-Stream signals
out_str_sigs, out_bw = vp.add_axiss_signals(name=out_stream_name, short_name_prefix='out_stream',
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

In the polynomial example, the burst in the simulattion are:

| Burst           | Stream | Contents |
|-----------------|--------|----------|
| `bursts_in[0]`  | input  | `PolyCmdHdr` (DATA) — cmd_type, tx_id, nsamp |
| `bursts_in[1]`  | input  | `nsamp` input samples (float32) |
| `bursts_in[2]`  | input  | `PolyCmdHdr` (END) — terminates the kernel loop |
| `bursts_out[0]` | output | `PolyRespHdr` — tx_id echo |
| `bursts_out[1]` | output | `nsamp` output samples (float32) |

Coefficients no longer appear on the stream — they are configured via the AXI-Lite register map before launch. Halt status (`halted` / `error` / `tx_id`) is read from the regmap after the kernel returns, not from a streamed footer.

We can deserialize each burst to the corresponding Waveflow DataSchma:

```python
word_bw = 32
cmd_hdr = PolyCmdHdr()
nwords = PolyCmdHdr.nwords_per_inst(word_bw=word_bw)

words = bursts_in[0]['data']
cmd_hdr.deserialize(word_bw=word_bw, packed=words)
print("PolyCmdHdr (DATA) values:")
for k, v in cmd_hdr.val.items():
    print(f"    {k}: {v}")

print("\nInput sample data values, x")
nsamp = cmd_hdr.val['nsamp']
x = read_array(packed=bursts_in[1]['data'], word_bw=word_bw, elem_type=Float32, shape=(nsamp,))
print(x)

resp_hdr = PolyRespHdr()
words = bursts_out[0]['data']
resp_hdr.deserialize(word_bw=word_bw, packed=words)
print("\nPolyRespHdr values:")
for k, v in resp_hdr.val.items():
    print(f"    {k}: {v}")

print("\nOutput sample data values, y")
y = read_array(packed=bursts_out[1]['data'], word_bw=word_bw, elem_type=Float32, shape=(nsamp,))
print(y)
```

This will return

```python
PolyCmdHdr (DATA) values:
    cmd_type: 0
    tx_id: 42
    nsamp: 100

Input sample data values, x
[0.         0.01010101 0.02020202 0.03030303 0.04040404 0.05050505
 0.06060606 0.07070707 0.08080808 0.09090909 0.1010101  0.11111111
 0.12121212 0.13131313 0.14141414 0.15151516 0.16161616 0.17171717
 0.18181819 0.1919192  0.2020202  0.21212122 0.22222222 0.23232323
 0.24242425 0.25252524 0.26262626 0.27272728 0.28282827 0.2929293
 0.3030303  0.3131313  0.32323232 0.33333334 0.34343433 0.35353535
 0.36363637 0.37373737 0.3838384  0.3939394  0.4040404  0.41414142
 0.42424244 0.43434343 0.44444445 0.45454547 0.46464646 0.47474748
 0.4848485  0.4949495  0.5050505  0.5151515  0.5252525  0.53535354
 0.54545456 0.5555556  0.56565654 0.57575756 0.5858586  0.5959596
 0.6060606  0.61616164 0.6262626  0.6363636  0.64646465 0.65656567
 0.6666667  0.67676765 0.68686867 0.6969697  0.7070707  0.7171717
 0.72727275 0.7373737  0.74747473 0.75757575 0.7676768  0.7777778
 0.7878788  0.7979798  0.8080808  0.8181818  0.82828283 0.83838385
 0.8484849  0.85858583 0.86868685 0.8787879  0.8888889  0.8989899
 0.90909094 0.9191919  0.9292929  0.93939394 0.94949496 0.959596
 0.969697   0.97979796 0.989899   1.        ]

PolyRespHdr values:
    tx_id: 42

Output sample data values, y (truncated)
[ 1.          0.979496    0.9584046   0.9367504   0.9145583   0.8918529
  0.868659    0.8450014   0.8209047   0.7963937   0.7714931   0.74622774
  0.7206222   0.6947013   0.6684898   0.64201236  0.61529386  0.5883589
  0.5612321   0.5339385   0.5065026   0.47894925  0.45130318  0.423589
  0.39583158  0.36805564  0.34028584  0.31254697  0.28486377  0.25726092
  0.22976321  0.20239538  0.17518204  0.14814812  0.12131834  0.0947172
  0.06836957  0.0423004   0.01653403 -0.00890446 -0.0339905  -0.05869937
 -0.08300638 -0.10688663 -0.13031542 -0.15326822 -0.17571998 -0.19764626
 -0.21902215 -0.23982298 -0.26002395 -0.27960038 -0.2985276  -0.3167807
 -0.3343351  -0.351166   -0.36724865 -0.38255835 -0.3970703  -0.41076005
 -0.42360246 -0.43557298 -0.44664693 -0.45679927 -0.46600592 -0.47424138
 -0.48148143 -0.48770118 -0.4928758  -0.4969809  -0.49999118 -0.50188243
 -0.50262964 -0.5022081  -0.50059307 -0.49775994 -0.49368393 -0.48834014
 -0.481704   -0.47375083 -0.46445584 -0.45379412 -0.44174123 -0.42827213
 -0.41336226 -0.39698696 -0.3791213  -0.3597406  -0.33882022 -0.31633544
 -0.29226148 -0.26657355 -0.23924696 -0.21025681 -0.17957866 -0.14718747
 -0.11305892 -0.07716799 -0.03948998  0.        ]
```

