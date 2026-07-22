---
title: AXI4 Memory-Mapped Timing Analysis
parent: Timing Analysis Tools
nav_order: 5
has_children: false
---

# AXI4 Memory-Mapped Analysis

The VcdParser class has specific methods for identifying AXI4 memory-mapped signals in a VCD, plotting those signals on a timing diagram, and extracting accepted read and write bursts.

This page uses the [histogram example](../../examples/shared_mem/) as the reference design. In that example, the `hist` accelerator uses an AXI4 memory-mapped master interface named `m_axi_gmem` to read input samples and bin edges from memory and to write histogram counts back to memory.

## Loading the AXI4 Memory-Mapped Signals

After you have created a [VcdParser](./vcd.md), you can load the AXI4 memory-mapped signals into the parser as follows.

The histogram example uses signals with names like:

- `apatb_hist_top.AESL_inst_hist.m_axi_gmem_ARADDR[63:0]`
- `apatb_hist_top.AESL_inst_hist.m_axi_gmem_RDATA[31:0]`
- `apatb_hist_top.AESL_inst_hist.m_axi_gmem_AWADDR[63:0]`
- `apatb_hist_top.AESL_inst_hist.m_axi_gmem_WDATA[31:0]`

You can find these names from the printout of the VCD signals as described in [parsing the VCD outputs](./parsing.md). After identifying the AXI-MM prefix, you can load the interface signals with:

```python
# Create a parsing class
vp = VcdParser(vcd)

# Get the clock signal name
clk_name = vp.add_clock_signal()

top_name = 'AESL_inst_hist'
gmem_prefix = f"{top_name}.m_axi_gmem_"

# Load the AXI-MM interface signals
aximm_sigs, aximm_bw = vp.add_aximm_signals(
    prefix=gmem_prefix,
    dir='both',
    lite_only=False,
    short_name_prefix='gmem',
)

print(aximm_sigs)
print(aximm_bw)
```

This loads the standard AXI4 read and write address, data, and handshake signals. For AXI4-Full interfaces it will also load burst-length and last-beat signals such as `ARLEN`, `AWLEN`, `RLAST`, and `WLAST` when they are present.

The `short_name_prefix` option keeps the labels compact on the timing diagram.

## Plotting the Timing Diagram

Once the AXI-MM signals are loaded, you can plot the timing diagram using the same flow as for other VCD signals:

```python
# Get the timing signals
sig_list = vp.get_td_signals()

# Create the timing diagram
td = TimingDiagram()
td.add_signals(sig_list)
trange = None
ax = td.plot_signals(
    add_clk_grid=True,
    trange=trange,
    text_scale_factor=1e4,
    text_mode='never',
)
_ = ax.set_xlabel('Time [ns]')
```

For AXI-MM analysis it is often useful to zoom into a smaller range after burst extraction so you can inspect:

- `ARVALID` and `ARREADY` for accepted read-address requests
- `RVALID` and `RREADY` for returned read data
- `AWVALID` and `AWREADY` for accepted write-address requests
- `WVALID` and `WREADY` for write-data beats

## Extracting AXI4-MM Bursts

You can extract both write bursts and read bursts directly from the AXI-MM signals:

```python
write_bursts, read_bursts, clk_period = vp.extract_aximm_bursts(
    clk_name=clk_name,
    aximm_sigs=aximm_sigs,
)

print('Write bursts:')
for i, burst in enumerate(write_bursts):
    print(
        f"Burst {i}: addr=0x{int(burst['addr']):x}, "
        f"tstart={burst['tstart']}, data_tstart={burst['data_tstart']}, "
        f"data_tend={burst['data_tend']}, beat_type={burst['beat_type']}"
    )

print('\nRead bursts:')
for i, burst in enumerate(read_bursts):
    print(
        f"Burst {i}: addr=0x{int(burst['addr']):x}, "
        f"tstart={burst['tstart']}, data_tstart={burst['data_tstart']}, "
        f"data_tend={burst['data_tend']}, beat_type={burst['beat_type']}"
    )
```

For the histogram example, the extracted read bursts typically correspond to:

- input sample data reads
- bin-edge reads

and the extracted write bursts correspond to:

- histogram count writes

If you want a JSON report, the histogram example wrapper already does this through:

```python
report = hist_test.extract_bursts()
```

The JSON includes both decimal `data` and fixed-width hexadecimal `data_hex` so the bus words are easier to inspect.

## Meaning of Burst Timing Fields

Each extracted burst includes several timing fields. These fields distinguish the address phase from the data phase.

### `tstart`

`tstart` is the time of the accepted address handshake for the burst.

- For reads, this is the cycle where `ARVALID && ARREADY` is true.
- For writes, this is the cycle where `AWVALID && AWREADY` is true.

This means `tstart` marks when the request was accepted, not when the first data beat for that burst appeared.

With AXI4 it is legal for multiple read addresses to be accepted in consecutive cycles. In that case, several bursts can have back-to-back `tstart` values even though their returned data beats do not overlap.

### `data_tstart`

`data_tstart` is the time of the first cycle represented in `beat_type` for this burst.

- For reads, this is the first cycle when this burst becomes the active burst on the `R` channel.
- For writes, this is the first cycle when this burst becomes the active burst on the `W` channel.

If the burst starts immediately, then `data_tstart == tstart`. If the burst waits behind earlier requests, then `data_tstart > tstart`.

### `data_tend`

`data_tend` is the time of the final cycle represented in `beat_type` for this burst.

This is the final active cycle tracked for the burst on the data channel, including transfer, idle, or stall cycles while that burst is active.

If all beats transfer without stalls, then the total active duration is approximately:

$$
\text{data\_tend} - \text{data\_tstart} = (N - 1) \cdot T_{clk}
$$

where $N$ is the length of `beat_type` and $T_{clk}$ is the extracted clock period.

### `beat_type`

`beat_type` is a per-cycle list describing what happened on the data channel while this burst was active.

The numeric values are defined by the `beat_type_enum` section of the JSON report:

- `0 = transfer`
- `1 = idle`
- `2 = stall`

The JSON also includes `beat_type_names`, which is usually easier to read.

For read bursts:

- `transfer` means `RVALID && RREADY`
- `idle` means `RVALID == 0`
- `stall` means `RREADY == 0`

For write bursts:

- `transfer` means `WVALID && WREADY`
- `idle` means `WVALID == 0`
- `stall` means `WREADY == 0`

The important point is that `beat_type` only refers to the cycles when this burst is the active burst on the data channel. It does not mean that other outstanding requests do not exist.

## True channel occupancy: count transfer beats

A common mistake is to treat the data-phase *span* (`data_tend − data_tstart`) as the channel's busy
time. It is not — that span includes `idle` and `stall` beats. The **true occupancy** of the data
channel is the number of **`transfer`** beats, and at `II=1` that is exactly one beat per word:

```python
from waveflow.utils.vcd import AximmBeatType

def transfer_beats(burst) -> int:
    """Words actually moved on the data channel (excludes idle / stall cycles)."""
    return sum(1 for bt in burst["beat_type"] if bt == AximmBeatType.TRANSFER)

write_words = sum(transfer_beats(b) for b in write_bursts)   # == the bytes/word written
```

Why the distinction matters for a timing model: an **`idle`** beat (`VALID == 0`) is the *master not
driving the channel* — i.e. an **upstream stall** (the producer hasn't supplied the next word yet,
e.g. compute hasn't finished the row), **not** time the channel itself is busy. So summing only
`transfer` beats cleanly separates two things that the wall-clock span conflates:

- the **deterministic channel occupancy** (`transfer` beats `== nwords`), a property of the bus, and
- the **pipeline stall** (the `idle` beats), a property of the *compute*, which a load-compute-store
  timing model should let *emerge* from the compute stage rather than fold into the transfer time.

In the SimPy model these three bus properties — **contention, occupancy timing, and duplex** — live on
the **interconnect/slave** (the contended memory port), not on the accelerator: each `MMIFSlave` owns
independent `read_channel` / `write_channel` resources and a per-direction `bus_timing` occupancy model,
reached by a `Region` slice *through the interconnect* (which decodes the address to the serving slave).
A read and a write on one AXI bundle never contend (full-duplex is the default); a single-port BRAM or a
DDR model that shares R/W bandwidth *declares* itself `half_duplex=True` to re-couple the channels.

This is precisely the measurement that underpins the matrix-LT FIR calibration: occupancy is read off
the `transfer` beat count (deterministic), and the per-row stall is attributed to compute. See the
beat-counting helper in [`fir_calibrate.py`](../../../examples/rowwise_fir/fir_calibrate.py), the
[double-buffered worked example](../concurrency/python/sob.md),
and the [Calibration](../calib/) package.

## Queueing and Outstanding Reads

When analyzing AXI4 read traffic, it is common to see:

- several bursts with consecutive `tstart` values
- later `data_tstart` values for the second or third burst

This means the design issued multiple read requests quickly on the address channel and the bursts then completed later on the data channel in FIFO order.

The `queue_wait_cycles` field is useful here. It measures how many clock cycles elapsed between address acceptance and the start of the burst's active data phase.

For example:

- `tstart = 425 ns`
- `data_tstart = 585 ns`
- `clk_period_ns = 10 ns`

implies:

$$
\text{queue\_wait\_cycles} = \frac{585 - 425}{10} = 16
$$

so that burst waited 16 clock cycles after the address handshake before its data phase began.

## Reading the Burst Data

Each burst includes:

- `data`: bus words in decimal form
- `data_hex`: the same words in fixed-width hexadecimal form

The hex form is often easier to compare against bus waveforms, while the decimal form is convenient for direct deserialization.

For example, if a read burst returns 32-bit float payload words, you can deserialize the decimal `data` list directly:

```python
words = np.asarray(read_bursts[0]['data'], dtype=np.uint32)
values = read_array(words, elem_type=Float32, word_bw=32, shape=(16,))
print(values)
```

If you only want to inspect the raw bus traffic, `data_hex` is usually the better starting point.