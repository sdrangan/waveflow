# Modeling the ADC in Waveflow

## Overview

Many applications, esp. in wireless, connect to and ADC block, like the RFDC block in Xilinx FPGAs.  This document is start of a plan of how to extend Waveflow to enable development of systems with ADCs, particularly in wireless applications.   
For the discussion below, we can think of the following classes of blocks in Waveflow. 

- *Digital logic* blocks representing synthesizable hardware for processing the digital signals to and from the ADC block.  These logic blocks would be `HwModules` that would be synthesized and represent blocks like FIR filters, FFTs and other standard communications processing blocks
- *RFDC emulator* an emulation of the RFDC block that the digital logic blocks would connect to and has the same interface as the true RFDC block.
- *RF environment*:  Blocks that emulate the RF environment or channel or RF sources and sinks.  THese blocks are only for simulation and are not synthesized. 

## Use cases

We would imagine three use cases:

- *Full python simulation*:  The system could consist of one or more wireless nodes.  Each wireless node would have digital logic blocks and at least one RFDC emulator block.  The RFDC blocks are connected to the RF environment blocks.  This simulation can be complex and is expected only for python simulation.  What is important is:   
   - ability to model rich environments, 
   - remain bit-exact in the digital logic so one can evaluate the effect of parameters like bit widths in python; and
   - fast.  The fast simulation is enabled by processing of vectors of RF samples at a time.  See below
   
- *Unit python and RTL simulation*:  This would be a smaller simulation that can be run in both python or XSI for unit function verification and resource, and timing modeling.  In XSI:
    - The digital logic blocks are synthesized to verilog and run as exact verilog blocks
    - The RFDC emulation is modeled as an XSI testbench object
    - The RF environment is limited to a small class (probably just data sources and sinks) that interface with the RFDC emulator and can also be run in XSI

- *Bitstream generation*:  This mode would not be initially supported, but nothing we do now should preclude it.  In this mode, the RFDC emulation block would somehow get substituted with an AMD RFDC block and combined with the synthesized digital logic.  The digital logic should not have to change its interface. The resulting system would not be simulated (since there is no AMD software that can emulate the RF environment).  But, the goal is that we can generate a complete bitstream.


## `RFDCEmulator` block

This block is an **emulation** of the RFDC block.  It is a `HwModule` that will synthesize to an XSI testbench task, but not (at least now) to an actual RFDC block.

### Interface Endpoints

- `tx_stream`, `rx_stream`:  AXI4-streaming interface emulating the interface to and from the programming logic.  Data is packed identically to the AMD RFDC block
- `tx_rf`, `rx_rf`:  Unsynthesized streams of complex baseband samples at the carrier frequency out of the block to the RF environment.  To make the simulation fast, RF data is sent in blocks of shape `(n_rx, blksize)` and `(n_tx,blksize)`.    The `rx_rf` endpoint is a *master* endpoint, meaning it pulls data at the sample boundaries.  

Design question:  Do we build a custom interface for `tx_rf` and `rx_rf`?  We do not need to synthesize this interface.  But, we need an interface that the blocks of size `(n_rx,blksize)` are numpy arrays in Python and C++ arrays in XSI.  

### Parameters

The module can be dynamically parameterized (`DynParam`) since it is XSI testbench block:

- `n_rx`, `n_tx`:  number of RX and TX channels
- `nbits`:  number of bits per channel 
- `iq_mode`:  boolean variable indicating if channels are mapped to IQ
- `samp_rate`:  Sample rate in Hz
- `rx_freq`, `tx_freq`:  floating point carrier frequency
- `blksize`:  Block sizes 


### Python model

- The master `rx_rf` enpoint pulls data every `blksize / samp_rate` seconds, quantizes the samples pushes that data into the `rx_stream`.
- There is a small inaccuracy since the `blksize` samples are pushed in at a time
- In the the transmit direction, it pulls data `blksize` sampels every `blksize / samp_rate` seconds and pushes that data into the `tx_rf` port.  
- One issue:  Since the streaming on the RF side is in blocks of `blksize` samples, there can be no dependency of the RX data on the TX data within less than `blksize` samples.  For fixed data source / sink, this is not an issue.  But, in interactive systems, this blocking places a limit on how large of blocks we can make.  In particular, it precludes loopback emulation where the TX-RX delay is very short.  It may also preclude RADAR experiments unless the return waveform can be pre-computed.  But this may be OK for now.  We may be able to pre-compute the response.

### XSI model

- In XSI, we lower this (somehow) to a similar system

## `RFDataSource` and `RFDataSink`

These would be initial blocks that connect to the `rx_rf` and `tx_if` endpoints of the `RFDCEmulator` block with fixed data sources and sinks from files.  These blocks will be good for unit testing.

## `RFSampBuf`

This would be an initial digital logic block that we would design that would actually get synthesized.  It provides a time-stamped packetized interface to and RFDC block that could be tested with a connection to an `RFDCEmulator`.

On the TX side it has a two port BRAM.

Data loader -> TX buffer (two port BRAM) -> TX player

The TX player task continuously reads out from the TX buffer in a circular manner.  There are no dropped samples.  TX buffer is not a FIFO.  It is a circular buffer.   
The data loader tasks gets a transactional command with fields:

`TxCmd`:
- `tid`:  transaction ID
- `samp_ind_start`:  index in the buffer to place the first sample
- `nsamp`:  number of samples
- `data_addr`:  Address in memory for the `(nsamp,ntx)` samples in row-major form

Alternatively, we could have a streaming in-band data so that the data follows after the `TxCmd`.

On the RX side, 

RX stream IF -> RX buffer (two port BRAM) -> Data capture

RX stream IF is a free-running task constantly filling up a two port BRAM
Data Capture gets a similar `RxCmd` to capture samples from a particular sample

## Synchronization

It is important that the the counters on the Tx and Rx are aligned across antennas and between TX and RX.  So, receive sample 0 has a fixed time relation with TX sample 0.  This needs careful design in the digital logic (somehow? maybe via shared counters).  Also, somehow MTS needs to be configured.  

Claude Comments

Structural issues

> **[2026-08-11] Point 1 below is SUPERSEDED — see `plans/design_cut.md`.** The `ExternalIP` / `ExtMod`
> proposal ("a third kind") was **rejected** on further review: it freezes a *per-build role* into a
> *class fact*. The boundary is already derived from the graph (`hw_freerun.py::boundary` — an endpoint
> not bound to an internal interface **is** a boundary port), and `MemRStream` is already realized both
> ways today (its own top, XSI gate 158; a task inside `mem_copy`, gate 2835). The resolution is **one
> `HwModule` with two symmetric optional hooks** — `kernel_task()` (inside the cut) and `bfm_model()`
> (outside it) — with the cut chosen per build and `check(mod, target)` answering lowerability.
> `adc_model.md` is **parked** until `design_cut.md` lands; it then reduces to "three plain
> `HwModule`s". Points 2-8 and the gaps below are unaffected. (The "Nits" reference to "the ExternalIP
> framing" reads as "the `bfm_model()` hook" instead.)

1. RFDCEmulator is not a HwModule in today's vocabulary — and that's the interesting part.

The sketch says it "is a HwModule that will synthesize to an XSI testbench task." Nothing does that. XSI TB participants are SimObjs that declare bfm_model() returning a BfmModel naming a hand-written C++ class (composite_gen.py:98); HwModules lower to hls::task bodies inside the DUT. Those are different sides of the boundary.

But the sketch is reaching for something real that neither side covers: in Flow 3 the RFDC is a node in the system graph — it just gets realized as vendor IP instantiated by IPI TCL rather than generated. That is a third kind, and naming it now is the highest-value thing this plan can do:

ExternalIP — a graph node whose behavior is Python, and whose realizations are (a) a pysim run_proc, (b) a hand-written XSI BFM class, (c) an IPI create_bd_cell + set_property CONFIG.* block. The invariant across all three is the AXIS boundary contract (width, packing, framing, rate).

That framing is what makes your Flow-3 requirement — "the digital logic should not have to change its interface" — a checkable property rather than a hope, and it generalizes past RFDC (BRAM controller, DMA, SmartConnect). It also slots cleanly into codegen_targets.py, where bitstream is already a declared-but-unimplemented name.

2. The RFDC is not backpressure-honest, and the existing BFMs will hide exactly the bug you care about.

This is the strongest technical reason for an RFDC-specific model, and it's absent from the plan. AxisMaster/AxisSlave and pysim's StreamDriver/StreamSink model a well-behaved AXIS peer: the master waits when TREADY is low, the sink is always ready. The RFDC is neither. Per your own circ_buf_fac.md:134-148: the DAC consumes one beat every clock and backpressure does not protect you — a missed beat is an underflow, an analog glitch, not a stall. Symmetrically the ADC produces one beat every clock regardless of TREADY; a slow consumer drops samples.

So both the pysim model and the BFM need non-blocking variants that count the violations:

RfdcDacSlave — always ready; after the first beat, every cycle with TVALID low is an underrun. Assert zero.
RfdcAdcMaster — presents a beat every cycle unconditionally; every cycle with TREADY low is a dropped sample. Assert zero.
Without this, a design that fails on hardware passes in both sim backends — the "deadlock looks like success" failure mode in a new costume. Make "underrun/overrun count == 0" the gate assertion for every RFDC-connected example.

3. Most of the parameter list is HwParam, not DynParam.

n_rx, n_tx, nbits, iq_mode and the samples-per-beat factor determine the AXIS word layout — which the synthesized logic must be built against. They bind at elaboration and are shared with the DUT; distinct values are distinct artifacts. Only samp_rate, rx_freq/tx_freq, and the vector file paths are genuinely init-time. Splitting them is not pedantry: if nbits is a DynParam you can set it to a value the generated RTL cannot represent, and nothing will complain.

Concrete landmine on the ones that do stay DynParam: discover_dyn_params skips falsy values (hw_module.py:150) — rx_freq = 0.0 (baseband!) and iq_mode = False emit nothing and silently take the C++ default. Both are ordinary values here. Either sentinel them or fix the discovery predicate before relying on it.

4. Your design question — a custom tx_rf/rx_rf interface — mostly dissolves.

The hard part you named ("numpy arrays in Python and C++ arrays in XSI") only exists if the RF boundary must cross into XSI. It doesn't. In use case 2 the RF environment is restricted to file sources/sinks, so the honest lowering is: quantize in Python, write a burst bundle, drive the RTL with the existing AxisMaster. The RF-domain interface then never exists in C++ at all, and you inherit the project's established "one on-disk bundle drives both backends, so both provably play identical bytes" discipline (stream_tb.py:38-51).

So: build one sim-only block interface (numpy (n_rx, blksize) transfer, @sim_only, master-pull) for use case 1 only. Note the existing bundle format is UINT64 words — RF-domain float/complex vectors need a format decision, but it's a Python-side one.

The master-pull direction on rx_rf is a good call and worth stating why in the plan: pull = lazy channel evaluation, which is what lets the environment compute a block only when the converter needs it.

5. The missing parameter is the gearbox, SPC.

Samples-per-beat is the number that ties everything together: f_axis = samp_rate / SPC fixes the fabric clock, and SPC × nbits fixes the AXIS width. It's the anchor for the CDC model, the rate model, and the packing layout — and it's fully worked out in circ_buf_fac.md:154-190 (time-ascending from the LSBs) but absent from the param list here. Add it, and cite that layout section as the packing contract rather than re-deriving it.

Related: the bit-exactness goal in use case 1 ("evaluate the effect of bit widths in python") means the quantizer must be the integer-backed FixedField, and the sample↔word packing must go through the generated <stem>_array_utils.h twins — not hand-rolled .range() math. That's a standing rule in this codebase and the bug it prevents hides at the degenerate widths.

6. RFSampBuf as drawn is a Flow-3 system, not a kernel.

"Two-port BRAM, PS writes port A" is a block-diagram structure (Block Memory Generator + AXI BRAM Controller), not an HLS interface — Waveflow has no BRAM-port endpoint type and Flow 3 isn't built. The variant that is expressible today is the alternative you already floated: in-band data following the TxCmd, which is precisely the mem_copy/interleaver shape (framed command → forwarded payload) and is XSI-proven. I'd make in-band the primary design and the shared-BRAM version the Flow-3 note.

On data_addr: m_axi in a free-running ap_ctrl_none hls::task top is proven — see the generated mem_copy.cpp, which carries m_axi ... offset=slave alongside ap_ctrl_none. So a descriptor carrying an address isn't blocked. What I'd verify before betting on it is the host-writable offset register story under ap_ctrl_none — an address the PS must set is a different claim than an address that arrives in-band.

7. Synchronization needs to become a contract, not a to-do.

The section currently says "somehow (maybe via shared counters)". Split it into the part that is modelable and the part that isn't:

Modelable: define a single sample-index time base (sample 0 at t=0) plus a per-tile constant latency parameter; TX/RX alignment becomes an assertion on sample indices in pysim and on beat counts in XSI. This is checkable today.
Not modelable: MTS is a bring-up procedure (SYSREF distribution, tile calibration). Model it as "a fixed, measured offset parameter", and say so — pretending to simulate it would be worse than declaring it out of scope.
8. This plan overlaps rfsoc_4x2_bringup.md substantially, with different names.

That plan already specifies RfdcAdc / RfdcDac / Channel, the block-LT architecture, the inter-block overlap-state discipline (channels and stateful DSP must carry state across block boundaries), and — importantly — the fidelity boundary: feedforward DSP is block-perfect, sample-level feedback loops (AGC, carrier/timing recovery) are not resolvable at block granularity. Two divergent component sets for the same thing is the failure mode here. I'd either fold adc_model.md in as the component-level spec that page's "Components to model" section points to, or explicitly supersede it — and pull the overlap-state and fidelity-boundary paragraphs into this plan, since both bite RFDCEmulator directly.

Gaps worth a section each
Golden / conformance. Every accelerator in this repo that worked has a conformance twin and a checkable golden. For RF, the natural one is the channel sounder from the bring-up plan: transmit a known sequence, correlate at RX, compare the estimated CIR to the channel you configured. State it as the acceptance test.
Staging. No milestone ordering. Suggested first gate, deliberately small: RFDCEmulator + RFDataSource/RFDataSink + a trivial pass-through DUT, pysim only, asserting zero underrun/overrun and a byte-identical loopback. No RTL, no DSP. That exercises the kind question (#1), the backpressure contract (#2), the param split (#3), and the file-bundle boundary (#4) — i.e. every structural decision above — before any of them is expensive to change.
Nits: §RFDataSource says tx_if, should be tx_rf. "Emulator" vs "model" is used interchangeably — worth fixing on one, since under the ExternalIP framing the same class is a model in sim and a binding to real IP in Flow 3.
Want me to fold this into plans/adc_model.md as a revised draft (restructured around the ExternalIP framing with the param split and milestone list), or leave the sketch as yours and add a review-notes section at the bottom?