# Stream kernel frame (read this before the spec)

A **stream kernel** is one free-running kernel that takes a command and its data on a stream and
answers on streams.  This file says what the frame **fixes** -- the rules every design in it follows --
and what a spec **must decide**, which is the checklist Stage 1 answers.  Where the spec and this frame
disagree, the spec wins; say so in Stage 1.

The pattern is command-response (`docs/guide/patterns/command_response.md`); the reference is
`stream_kernel`, a polynomial on float32 samples that follows every rule below.

## Fixed by the frame

1. **One `FreeRunMod`**, no start signal and no register map, with **framed** stream ports
   (`FramedStreamIFSlave` in, `FramedStreamIFMaster` out).  One firing is one command.
2. **The protocol** (`docs/guide/patterns/command_response.md`, *The protocol*):

   ```text
   s_in     : Cmd(tx_id, n, ...) | data        the command one burst, its data another
   m_out    : Resp(tx_id, n)     | results     framed as s_in: the header a burst, the results a burst
   m_status : Status(tx_id, status)            one per command, after its results
   ```

   The header lets any reader size the results from the stream.  The status is the last command seen.
3. **An error is fatal**: the kernel closes the results burst short with TLAST, writes the status, and
   stops taking `s_in` -- a `static bool halted` under `#pragma HLS reset` in the HLS body, and
   `yield self.event()` in `run_iter`.  A scenario that tests an error puts it last, or checks that
   nothing after it is answered.
4. **When the number of results is not known until the end** (detections, matches), `m_out` carries
   a tagged union ending in `END` -- one `DataList` per message type, each beginning with the same
   `EnumField` tag -- and `m_status` is unchanged (*Variable-length output* on the pattern page).
5. **Every message is a schema** (`DataList`, `EnumField`, `FixedField`, `ComplexField`), and every
   word is packed by the generated code: `serialize` / `write_array` in Python, `read_axi4_stream` /
   `write_axi4_stream` and the lane routines in C++.  Never by hand.
6. **The testbench is the kernel's harness**: `harness(Kernel, workload=...)`, a stream driver on each
   input and a sink on each output, generated from the ports.  The kernel names its scenarios in
   `workload(kind)`.  No hand-written testbench.
7. **The HLS body is hand-written** behind `kernel_task()` (a `KernelTask`), one word per iteration at
   II=1 with the lanes unrolled; the top is generated.
8. **Verification**, each against expected outputs computed from intents by a golden independent of
   the kernel:
   - the golden reproduces the spec's worked examples;
   - pysim in the harness, bit-exact;
   - the harness through the system DAG to `compare`: RTL against pysim, every recorded word and TLAST;
   - the error scenarios: nothing answered after the error, on either stream.

## Decided by the spec

Stage 1 answers each in one line:

- the command's fields, and the data's element type and format (integer, `FixedField`, float,
  `ComplexField`);
- the results' element type; whether their count is known from the command (a `Resp` header) or only
  at the end (a tagged union ending in `END`);
- the operation, bit-exactly: the operation order, every rounding and saturation;
- the errors the kernel detects, and their status codes;
- the word width -- one, or several built from one class -- and the elements per word;
- the scenarios and their expected outputs, and any throughput target.
