---
name: waveflow-kernel-hls
description: Write the HLS body of a Waveflow leaf kernel -- the Vitis HLS C++ an hls::task (or a host-launched kernel) runs for each command -- in Waveflow's loop patterns -- one firing per message, the lane loop over packed samples with the generated array utils, PIPELINE II=1, state kept across commands, fixed-point datapaths -- and without the deadlocks and reset traps that csim does not show. Use whenever you write, review or restructure HLS C++ for a Waveflow kernel, or the user asks about its loops, pragmas, throughput, II or state.
---

# Waveflow HLS kernel bodies

A leaf kernel (`systems/kernels.md`) is written twice: its Python `run_iter` (the `waveflow-kernel`
skill) and its HLS body, this skill. The body is written to read like its Python golden: one command
in, a loop over its data, the answers out. The messages and packed samples are moved by generated code; the body supplies
the loop structure and the arithmetic. Several mistakes here pass C simulation and fail only in RTL
(a deadlock, a counter that moves during reset), so follow the rules even when csim would accept
something else.

## The rules (all of them, every time)

1. **One firing = one message.** An `hls::task` body is called again and again by the runtime. Write
   one command per call, shaped like the golden's `run_iter`: read the header with its schema reader,
   run **one pipelined loop** over the data, write the answers. No `while (true)` inside the body, no
   state machine with a `state` variable (an optimization, used only when a measurement shows that
   the pipeline drain between messages matters), and no straight-line body without a loop (it cannot
   reach II=1).
2. **Packed samples go through the lane loop.** With several elements a bus word, `PF =
   <elem>_array_utils::lane_capacity<W>()` and the generated lane routines
   (`read_axi4_stream_lane<W>`, `write_axi4_stream_lane<W>`, `read_array_lane<W>`, ...) move one word
   of elements. Pick the shape from the throughput the spec asks for:
   - **A word per iteration** (`i += PF`), the compute `UNROLL`-ed across the `PF` lanes: `PF` elements a
     clock, the full bus rate, `PF` copies of the datapath.
   - **An element per iteration**, reading a word every `PF` iterations: one element a clock, one
     copy of the datapath.
   - **Inputs and outputs packed differently** (int16 in at 4 a word, int32 out at 2): step the loop by
     the **output** word, `i += ZPF` at II=1, and read an input word every `XPF / ZPF` iterations
     (`if ((i / ZPF) % (XPF / ZPF) == 0)`), indexing the input lane at `k * ZPF + t`.
   Pass the tail count, `min(PF, n - i)`, so the last word is zero-filled as in Python. Never
   `.range()`, shift or mask a word by hand. The routine follows the stream type: `*_axi4_stream_lane`
   on a top's boundary port (`axi4s_word`), `*_framed_stream_lane` on a composite's internal edge
   (`framed_word`, and messages by `read_framed_stream` / `write_framed_stream`).
3. **Pragmas on the loop that does the work.** `#pragma HLS PIPELINE II=1` on the data loop;
   `ARRAY_PARTITION ... complete` on every lane array; `UNROLL` on the loop across lanes. Size every
   array with a compile-time constant (`PF`), not with `n`.
4. **Messages by their schema.** The header by `cmd.read_axi4_stream<W>(s_in)`, the answers by
   `msg.write_axi4_stream<W>(m_out, last)` (or `streamutils::read_msg` / `write_msg`); every code is the
   generated `enum class`, not an integer. (The `waveflow-schema` skill has the details.)
5. **State that outlives a message is a `static`, and only that.** Keep across firings exactly what the
   spec keeps (a running count, filter history, an error flag), declared `static`. Nothing else: no
   `static` that remembers an earlier command when the spec says nothing is kept.
   - **Reset.** A `static` alone is zero only when the FPGA is programmed; asserting the kernel's reset
     does not clear it. If the value must restart on reset ("since reset", "until a reset clears
     it"), add `#pragma HLS reset variable=<name>` under it. Vitis 2025.1 honours it, although csynth
     still warns "Register '<name>' is power-on initialization" (it prints that either way).
   - **A fatal error** is `static bool halted` under `#pragma HLS reset variable=halted`, set after
     the error status is written; every firing begins `if (halted) return;` (`stream_kernel`'s
     `poly_kernel_task.h`). That is the HLS form of pysim's `yield self.event()`.
   - **Write-first bodies.** A task whose firing starts with a write, not a blocking read, runs during
     reset and advances its state there: begin with the blocking read of the command, or reset every
     register with `config_rtl -reset state` in the build.
6. **Decide once per chunk, outside the loop; do not decide, compute and commit in one iteration.** A
   test that gates the loop (is there room, which buffer, how many) goes before the loop; the loop
   body only computes and moves data. Counting and accumulating inside the loop is fine.
7. **Never wait for a reply you just requested on another stream with a blocking read.** A write
   then a blocking read on two different streams may be scheduled into one state and deadlock; poll
   the reply with `read_nb` in a loop. On a credit stream, wait for room (`wait_room`) before writing a
   chunk.
8. **Arithmetic in the declared types, as the golden does it.** Fixed-point: compute in `ap_fixed`
   at full precision (the product of `ap_fixed<Wa, Ia>` and `ap_fixed<Wb, Ib>` is `<Wa+Wb, Ia+Ib>`, exact), then assign to the
   declared type, which rounds and saturates. No `float` / `double` in the datapath, and no casts the
   golden does not make.

**Where the body lives.** A hand-written body is a header in the design's `src/` (`<kernel>_task.h`,
a `template <int W>` function), named by the module's `kernel_task()` (`KernelTask(fn, header,
ports, template_args=...)`); `include/` is for generated headers only.

## Which page to read before which step

Read the page **before** writing the code, with `waveflow_get_doc` (MCP) or the `docs/guide/` checkout.

| step | page |
|---|---|
| the body's shape, the two lane-loop shapes, deciding per chunk, the timing rule | `vectorization/hls/loop_optimization.md` |
| the generated lane routines and their arguments | `vectorization/hls/arrayutils.md`, then `vectorization/hls/raw.md` |
| a worked body: header, lane loop with TLAST, response | `custom_hooks/stream.md` |
| state kept across firings, and reset | `memory/hwstate.md` |
| streams between kernels, blocking reads, deadlock | `systems/streams.md` |
| credit streams in HLS | `interface/axi_mm/credit_streams_hls.md` |
| the command / response / status shape | `systems/command_response.md` |
| fixed-point formats, rounding and saturation | `schema/python/fixpoint.md` |

For a worked example, `waveflow_get_example stream_inband` (`poly_body_impl.tpp`). If a name you need
is not on those pages, `waveflow_search` it -- **do not guess an API name.**

## The shape

```cpp
template <int W>
void kern(hls::stream<streamutils::axi4s_word<W>>& s_in,
          hls::stream<streamutils::axi4s_word<W>>& m_out) {
    static ap_uint<32> seen;                         // only what the spec keeps across commands
#pragma HLS reset variable=seen                      // when it must restart on reset
    Cmd cmd;
    cmd.read_axi4_stream<W>(s_in);                   // a blocking read first
    Resp r;  /* ... */  r.write_axi4_stream<W>(m_out, false);

    namespace xu = <elem>_array_utils;
    const int PF = xu::lane_capacity<W>();
    for (int i = 0; i < cmd.n; i += PF) {            // a word per iteration
#pragma HLS PIPELINE II=1
        xu::value_type x[PF];
#pragma HLS ARRAY_PARTITION variable=x complete dim=1
        const int m = (cmd.n - i < PF) ? (int)(cmd.n - i) : PF;
        xu::read_axi4_stream_lane<W>(s_in, x, m);
        for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
            /* compute lane k */
        }
        xu::write_axi4_stream_lane<W>(x, m_out, i + PF >= cmd.n, m);
    }
    seen += cmd.n;
}
```

## Before you finish, check your own code

- [ ] One message per call; no `while (true)`, no `state` variable, and one pipelined loop over the data.
- [ ] `PIPELINE II=1` on that loop; the lane arrays partitioned; the lane compute unrolled.
- [ ] The lane-loop shape matches the throughput asked for; the tail count is passed.
- [ ] No `.range(`, shifts or masks on words; every message by its schema's reader and writer.
- [ ] Every `static` is state the spec keeps across commands, under `#pragma HLS reset` if a reset
      must clear it; the firing starts with a blocking read.
- [ ] No blocking read of a reply after a write on another stream.
- [ ] The arithmetic and TLAST positions match the golden, line for line.
