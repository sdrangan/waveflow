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
        return KernelTask("mm_fir_task", "mm_fir_task.h", ("s_cfg", "s_in", "m_out", "m_status"),
                          template_args=(DW,))
```

## The hand-written body

[`include/mm_fir_task.h`](../../../examples/mm_fir/include/mm_fir_task.h) is the HLS twin of
`run_iter`. Its state is `static` (what survives re-firing in an `hls::task` body), and each firing
is one of the same three cases — config first:

```cpp
    ap_uint<DW> w;
    if (s_cfg.read_nb(w)) {
        cbuf[ci] = w;
        if (ci == CW - 1) {
            ci = 0;
            FirCfg c;
            c.read_array<DW>(cbuf);          // the generated struct decodes it
            ncfg++;
            ap_uint<32> at = c.apply_at;
            if (at < nsamp) { late++; at = nsamp; }
            ...                              // supersede a pending config, stage this one
            want_pub = true;
        } else {
            ci++;
        }
    } else if (s_in.read_nb(w)) {
        ap_int<16> x = w;                    // one sample per word: the low 16 bits ARE the sample
        const bool apply = pvalid && pat <= nsamp;
        ...                                  // 16-tap MAC over (apply ? ptaps : taps), shift history
        m_out.write((ap_uint<DW>)acc);
        nsamp++;
        dirty = true;
    } else if (dirty) {
        want_pub = true;
        dirty = false;
    }
```

and, after that, a status word goes out if one is pending (below). The decoding is the generated
`FirCfg::read_array` and the encoding `FirStatus::write_array`: the body never shifts or masks a
field out of a word.

### Why it is shaped like this: one word per stream per firing

The first version of this body was the obvious one: on a config, `c.read_stream<DW>(s_cfg)` — read
all five words; after samples, `st.write_stream<DW>(m_status)` — write both status words. It was
correct and bit-exact at RTL. It was also slow:

| | first body | this body |
|---|---|---|
| csynth | not pipelined; interval 3–16 cycles per firing | **pipelined, II = 1**, latency 10 |
| measured at RTL (200 samples, one switch) | **2096** cycles, 221 bus operations, 55 status polls | **857** cycles, 68 operations, 2 polls |
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
        st.nsamp = nsamp; st.ncfg = ncfg; st.late = late;
        st.write_array<DW>(sbuf);
        si = SW;
        want_pub = false;
    }
    if (si != 0) {
        m_status.write(sbuf[SW - si]);
        si--;
    }
```

A publish requested while one is still going out waits for it, rather than restarting: the status bank
completes a message on its second word, and a restart would hand it the first word of one message and
the second of the next. The price of II = 1 is registers — more flip-flops for the staged config and
the pipeline — not multipliers.

### Where the twins differ, deliberately

- pysim's `run_iter` takes a whole **packet** of samples per firing; the HLS body takes **one sample**.
  The output is the same sample-for-sample; the timing granularity is not.
- pysim publishes status after every config and every packet; the HLS body after every config and on
  the first idle cycle after samples. Both end in the same final status, and status is latest-value,
  so a reader cannot tell — and publishing per sample would cost the pipeline two words per sample.

## csynth

The build targets `xc7z020clg484-1` at 100 MHz (the default `render_tcl` emits). The body closes
timing at an estimated 6.8 ns. After csynth the build writes a source stamp beside the project, so the
[XSI gate](rtlsim.md) can refuse RTL that was not built from the sources on disk.

Next: [RTL simulation](rtlsim.md).
