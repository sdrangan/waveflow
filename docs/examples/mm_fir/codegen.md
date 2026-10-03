---
title: Code generation
parent: A memory-mapped FIR
nav_order: 3
has_children: false
summary: "What becomes the Vitis kernel, and what does not. The message structs and the free-running top are generated; the task body is hand-written and declared with kernel_task(). The memory-mapped side is not in the kernel at all — it is RTL beside it. And the lesson of the body: a first version that moved a whole message per firing could not pipeline and ran at one sample per ten cycles; moving at most one word per stream per firing pipelined it at II=1."
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
`run_iter`. Its state is `static` (what survives re-firing in an `hls::task` body), and it is a small
state machine over one packet — header, the config it names, its samples, its response:

```cpp
    if (state == HDR) {
        if (s_in.read_nb(w)) {
            ap_uint<DW> hb[1] = {w};
            FirCmdHdr h;
            h.read_array<DW>(hb);                    // the generated struct decodes it
            nleft = h.nsamp; npkt = h.nsamp; tx_id = h.tx_id; need = h.cfg_seq;
            lane = 0;                                // a packet's samples start on a fresh word
            state = (ncfg < need) ? CFG : ((h.nsamp != 0) ? SAMP : RESP);
        }
    } else if (state == CFG) {                       // wait for the config this packet needs
        if (s_cfg.read_nb(w)) {
            ...                                      // collect CW words, FirCfg::read_array, load taps
            ncfg++;
            if (ncfg >= need) state = (nleft != 0) ? SAMP : RESP;
        }
    } else if (state == SAMP) {
        bool have = (lane != 0);
        if (!have && s_in.read_nb(w)) {             // a new word every PF samples
            int16_array_utils::read_array_lane<DW>(&w, x_lane, (nleft < PF) ? (int)nleft : PF);
            have = true;
        }
        if (have) {
            const ap_int<16> x = x_lane[lane];
            ...                                      // 16-tap MAC over (x, hist), shift history
            int64_array_utils::value_type y_lane[1] = {acc};
            ap_uint<DW> yw;
            int64_array_utils::write_array_lane<DW>(y_lane, &yw, 1);
            m_out.write(yw);
            nsamp++; nleft--;
            lane = (lane == PF - 1 || nleft == 0) ? 0 : lane + 1;
            if (nleft == 0) state = RESP;
        }
    } else {                                         // RESP: which packet, which config it used
        FirRespHdr r;
        r.nsamp = npkt; r.tx_id = tx_id; r.cfg_seq = ncfg;
        ...                                          // FirRespHdr::write_array, then
        m_resp.write(rb[0]);
        want_pub = true;
        state = HDR;
    }
```

and, after that, a status word goes out if one is pending (below). Every message is decoded and encoded
by generated code — the `FirCmdHdr` / `FirCfg` / `FirRespHdr` / `FirStatus` structs, and the
`int16_array_utils` / `int64_array_utils` lane routines — so the body never shifts or masks a field
out of a word.

**The lane loop, one sample per cycle.** Samples are int16, packed four to a 64-bit word by the
serializer, so a word is a *lane* of `PF = 4` samples — the same lane routines
[stream_inband](../../../examples/stream_inband/poly_body_impl.tpp) uses. Poly evaluates a whole lane
per iteration. This body deliberately does not: it reads a lane every fourth sample and filters **one
sample per firing**, so the MAC is 16 multipliers rather than 64. Written as a loop, it is:

```cpp
j = 0;
for (i = 0; i < nsamp; i++) {
    if (j == 0) read_array_lane(word, x_lane);   // every PF samples
    x = x_lane[j];
    j = (j + 1) % PF;
    y[i] = MAC over the 16 taps;                 // unrolled: 16 multipliers, one result per cycle
}
```

— and in the task body the loop is the firing itself: `lane` is `j`, and it restarts at 0 for every
packet, whose samples begin on a fresh word.

### Why it is shaped like this: one word per stream per firing

The first version of this body was the obvious one: on a config, `c.read_stream<DW>(s_cfg)` — read
all five words; after samples, `st.write_stream<DW>(m_status)` — write both status words. It was
correct and bit-exact at RTL. It was also slow:

| | first body | this body |
|---|---|---|
| csynth | not pipelined; interval 3–16 cycles per firing | **pipelined, II = 1**, latency 10 |
| measured at RTL (200 samples, one switch, the host of the time) | **2096** cycles, 221 bus operations, 55 status polls | **857** cycles, 68 operations, 2 polls |
| resources | 16 DSP, 1758 LUT, 1614 FF | 16 DSP, 2169 LUT, 3672 FF |

A firing that may read five words from one stream, or write two to another, cannot be pipelined at
II = 1 — the loop's interval is set by its longest path. So the kernel ran at about one sample per ten
cycles, every drain the host did found only a few results, and the host spent its time polling.

The fix is a rule worth keeping: **in a pipelined task body, move at most one word on each stream per
firing.** Config words are collected into `cbuf`, one per firing, and decoded when the fifth arrives;
a status message is serialized once into `sbuf` and emitted one word per firing:

```cpp
    if (si == 0 && want_pub) {
        FirStatus st;
        st.nsamp = nsamp; st.ncfg = ncfg;
        st.write_array<DW>(sbuf);
        si = SW;
        want_pub = false;
    }
    if (si != 0) {
        m_status.write(sbuf[SW - si]);
        si--;
    }
```

A publish requested while one is still going out waits for it, rather than restarting: a restart would
hand the status bank the first words of one message and the rest of the next. (The status is one word
now; the rule still holds for any status that is not.) The price of II = 1 is registers — more flip-flops for the staged config and
the pipeline — not multipliers.

### Where the twins differ, deliberately

- pysim's `run_iter` takes a whole **packet** per firing — header, config, all its samples — and
  times it with the HLS body's interval and latency; the HLS body takes **one word per stream per
  firing**. The output is the same sample-for-sample; the timing granularity is not.
- Both publish the status once per packet, and both write the response after the packet's results.

## csynth

The build targets `xc7z020clg484-1` at 100 MHz (the default `render_tcl` emits). The body pipelines at
II = 1 with latency 10, uses 16 DSP, 1913 LUT and 2610 FF, and closes timing at an estimated 7.7 ns. After csynth the build writes a source stamp beside the project, so the
[XSI gate](rtlsim.md) can refuse RTL that was not built from the sources on disk.

Next: [RTL simulation](rtlsim.md).
