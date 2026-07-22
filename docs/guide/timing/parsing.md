---
title: Parsing VCD Files
parent: Timing Analysis Tools
nav_order: 4
has_children: false
---

# Parsing a VCD File

Once you have [generated a VCD file](./vcd.md), Waveflow provides a `VcdParser` tool for reading the data into python for analysis.

## Loading the VCD file
First, we load the VCD file into a VCDVCD object.  For example, if the file is `dump.vcd`:

```python
from vcdvcd import VCDVCD
vcd = VCDVCD('dump.vcd', signals=None, store_tvs=True)
```

You can see the names of the signals in vcd with:

```python
# Print number of signals
nsig = len(vcd.signals)
print(f"Number of signals in VCD: {nsig}")

# Find the signals with TDATA in their names
tdatas = [s for s in vcd.signals if 'TDATA' in s]
print(tdatas)
```

## Creating a VCD Parser and Adding Signals


Then, we create a Waveflow `VcdParser` object:

```python
from waveflow.utils.vcd import VcdParser
vp = VcdParser(vcd)
```

You can add signals to the parser as:

```python
sig_name = 'apatb_poly_top.AESL_inst_poly.in_stream_TDATA[31:0]'
short_name = 'in_stream_TDATA'

vp.add_signal(name=sig_name, short_name=short_name)
```

where `sig_name` is the name of the signal in the VCD and `short_name` is an optional (typically shorter)
name since the Vitis names for simulation are typically very long and do not display well.

You can also load a clock signal:

```python
clk_name = vp.add_clock_signal()
```

## Viewing Signals on a Timing Diagram

To view signals on a timing diagram:

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







