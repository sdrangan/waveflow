---
title: Code generation
parent: A memory-mapped FIR
nav_order: 3
has_children: false
summary: "What becomes the Vitis kernel, and what does not. The message structs and the free-running top are generated; the task body is hand-written and declared with kernel_task(). The memory-mapped side is not in the kernel at all — it is RTL beside it. And the lessons of the body: a first version with no pipelined loop ran at one sample per ten cycles; a single-firing state machine reached II=1 and never drained between packets; the body now is straight-line per packet with a pipelined sample loop -- the twin of run_iter -- at a measured ~15% cost on short packets."
---

# Code generation

```
python -m examples.mm_fir.mm_fir_build             # headers + top + tcl, then csynth
python -m examples.mm_fir.mm_fir_build --no-synth  # generate only
```

[`mm_fir_build.py`](../../../examples/mm_fir/mm_fir_build.py) produces the **Vitis kernel** — one of the
[components of the XSI simulation](../../guide/flows/concurrent_layers.md), and the only one Vitis builds. The
register bank and the queues are **not** in it: Vitis cannot generate an AXI slave, so they are RTL
beside the kernel, joined in the [RTL top](rtlsim.md). What the kernel sees of them is four streams.

| artifact | produced how | from |
|---|---|---|
| `include/fir_cfg.h`, `fir_status.h`, `int16_array.h` | generated (`DataSchemaStep`) | `FirCfg`, `FirStatus`, `Taps` |
| `gen/mm_fir.cpp` — the `ap_ctrl_none` top | generated (`composite_top_spec` + `render_top`) | `MmFir`'s endpoints and its `kernel_task()` |
| `mm_fir.tcl` | generated (`render_tcl`) | the top's name |
| `include/mm_fir_task.h` — the task body | **hand-written** | — (the twin of `MmFir.run_iter`) |

## The generated top

```cpp
void mm_fir(
    hls::stream<ap_uint<64> >& s_cfg,
    hls::stream<ap_uint<64> >& s_in,
    hls::stream<ap_uint<64> >& m_out,
    hls::stream<ap_uint<64> >& m_status
) {
#pragma HLS INTERFACE axis port=s_cfg
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE axis port=m_status
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::task t0(mm_fir_task<64>, s_cfg, s_in, m_out, m_status);
}
```

Four AXIS ports in the order `kernel_task()` declares them, no control interface, one free-running
task. The ports are plain `ap_uint<64>` streams — no TLAST pin — because the kernel needs no packet
boundaries: it filters one sample at a time and knows a config is five words. In the RTL top the
adaptor leaves' TLAST pins are simply left unconnected or tied low.

The body is declared, not extracted:

```python
    def kernel_task(self):
        """The hand-written HLS body, ``include/mm_fir_task.h`` -- the twin of :meth:`run_iter`."""
        from waveflow.hw.mem_stream import KernelTask
        return KernelTask("mm_fir_task", "mm_fir_task.h",
                          ("s_cfg", "s_in", "m_out", "m_resp", "m_status"), template_args=(DW,))
```

## The hand-written body

[`include/mm_fir_task.h`](../../../examples/mm_fir/include/mm_fir_task.h) is the HLS twin of
`run_iter`, and reads like it: **one firing is one packet**, written straight down.

```cpp
    FirCmdHdr h;
    h.read_stream<DW>(s_in);                         // 1. the header

CFG: while (ncfg < h.cfg_seq) {                      // 2. the config this packet needs
        FirCfg c;
        c.read_stream<DW>(s_cfg);
        ...                                          //    load the taps
        ncfg++;
    }

SAMP: for (ap_uint<32> i = 0; i < h.nsamp; ++i) {    // 3. the samples, one per cycle
#pragma HLS PIPELINE II=1
        const int k = (int)(i % PF);
        if (k == 0) {                                //    a fresh word every PF samples
            ap_uint<DW> w = s_in.read();
            int16_array_utils::read_array_lane<DW>(&w, x_lane, min(PF, h.nsamp - i));
        }
        const ap_int<16> x = x_lane[k];
        ...                                          //    16-tap MAC over (x, hist), shift history
        m_out.write(y_word);
    }

    FirStatus st;  ...  st.write_stream<DW>(m_status);   // 4. the status, then the response
    FirRespHdr r;  ...  r.write_stream<DW>(m_resp);
```

The taps, the filter's history and the counters are `static`, so they survive from packet to packet
(the `hls::task` runtime re-fires the body). Every message is decoded and encoded by generated code --
the `FirCmdHdr` / `FirCfg` / `FirRespHdr` / `FirStatus` structs and the `int16_array_utils` /
`int64_array_utils` lane routines -- so the body never shifts or masks a field out of a word. The
order every read and write happens in is fixed by the packet, so the blocking reads cannot deadlock.

**The lane loop, one sample per cycle.** Samples are int16, packed four to a 64-bit word by the
serializer, so a word is a *lane* of `PF = 4` samples.
[Poly](../../../examples/stream_inband/poly_body_impl.tpp) evaluates a whole lane per iteration, with
its compute unrolled four ways. This loop deliberately does not: it reads a word every fourth iteration
and filters **one sample per iteration**, so the MAC is 16 multipliers rather than 64. Both shapes are
in [Design patterns for loop optimization](../../guide/vectorization/hls/loop_optimization.md).

### Why it is shaped like this

Three bodies have been measured on the same scenario (200 samples in 13 packets, one switch):

| body | csynth | RTL cycles (`per_view` / `one_front`) |
|---|---|---|
| straight-line, **no pipelined loop**: a whole message read or written per firing | not pipelined; interval 3--16 cycles per firing | 2096 (with the host of the time) |
| a **single-firing state machine**: one word per stream per firing, the whole kernel one II=1 pipeline | II = 1, 7.7 ns | 520 / 529 |
| **this one**: straight-line per packet, the sample loop pipelined at II = 1 | II = 1, 6.8 ns | **618 / 611** |

The first could not pipeline at all: a firing that may read five words from one stream cannot run at
II = 1, so the kernel filtered about one sample per ten cycles. The state machine fixed that by moving
**at most one word on each stream per firing** -- and, as a side effect, never drained: it read the
next packet's header while the last samples were still in flight.

This body puts the pipeline where the work is -- the sample loop -- and writes everything else as the
Python does. The cost is measured: each packet pays the loop's fill and drain (its latency is 11) and a
few cycles of header, config check, status and response. With 16-sample packets that is about 15%; with
long packets it is noise. **A state machine is an optimization for short firings**, to be reached for
when a measurement says the drain matters; the loop is the pattern.

### Where the twins differ, deliberately

- Both take one packet per firing. pysim times the sample loop with the HLS body's interval and
  latency, and charges the body's fixed per-packet costs as measured at RTL -- `hdr_cycles`,
  `tail_cycles`, `restart_cycles` (see [Python simulation](pysim.md#how-close-is-pysims-timing)).
- Both publish the status once per packet, and both write the response after the packet's results.

## csynth

The build targets `xc7z020clg484-1` at 100 MHz (the default `render_tcl` emits). The sample loop pipelines at
II = 1 with latency 11; the kernel uses 16 DSP, 2190 LUT and 2690 FF, and closes timing at an estimated
6.8 ns. After csynth the build writes a source stamp beside the project, so the
[XSI gate](rtlsim.md) can refuse RTL that was not built from the sources on disk.

Next: [RTL simulation](rtlsim.md).
