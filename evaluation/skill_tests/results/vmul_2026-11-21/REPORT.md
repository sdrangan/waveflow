# Skill trial: a vector-multiply device (2026-11-21)

**The ask, as a user would put it:** "write a kernel for a vector multiply that gets a command with a
len and addresses for x, y and z, reads x and y, writes z and sends a response."

**How it was run:** one agent, starting from `waveflow-start` and using only the `waveflow-*` skills
(start, kernel, device, schema, kernel-hls) and the guide / examples they point to (`waveflow kb`,
`waveflow frames`, `waveflow process freerun_pipeline`).  The design was built in its own folder after
`waveflow init`, as `AGENTS.md` says.  Waveflow at `main` f74096d (PR #326 merged).  No Vitis / Vivado.

## Verdict

**Yes, it works, as far as can be checked without Vitis.**  But the first design that followed the
device skill's example shape passed pysim and would not have made a good HLS kernel. The agent got to
a sound design only by also reading the HLS skill and the reference examples.

| check | result |
|---|---|
| pysim, the device in `harness(Vmul)`: one response per command, `tx_id` / `n` / `code` in order, the whole memory arena word for word against the golden, nothing after a fatal error | **pass** (`test_vmul.py`, 5 tests) |
| a planted bug (the last element off by one) | caught |
| the HLS top generated from the graph (`gen.py`: headers, `gen/vmul.cpp`, `vmul.tcl`) | **generates**; 5 tasks, 3 `m_axi` bundles `offset=off` + `stable` |
| the hand-written HLS bodies compiled with plain g++ (AMD's open-source `ap_int`, a stand-in `hls::stream`) and run with the framework reader / writer bodies, one command at a time | **match the golden**: memory and responses, both scenarios; the halted kernel leaves the next command unread (`csim/run_csim.py`) |
| a planted tail bug in the C++ (writing every lane of the last z word) | caught |
| csynth (II, resources), XSI at RTL, deadlock at RTL, cycle count | **not run**: needs Vitis / Vivado 2025.1 |

## What was built

Choices the user left open, picked and stated in `vmul.py`:

- x and y are int16. z is int32 and holds the exact product, so there is no rounding or saturation.
  Memory words are 64 bits wide.
- `VmulCmd(tx_id, n, x_addr, y_addr, z_addr)`. Addresses are in bus words, as `MemRCmd` and
  `MemWCmd` take them.
- `VmulResp(tx_id, n, code)`, with `code` an `EnumField` (OK, BAD_LEN). The writer echoes it on
  `m_done` only after z is stored, so the response means "done".
- `n == 0` is a fatal BAD_LEN. The answer still goes out through the writer (`MemWCmd(len=0)`), and
  then the device takes no more commands until a reset.
- When `n` is odd, z's last word is written whole, and its spare lane is set to 0.

```
s_cmd -> VmulIssue --MemRCmd--> MemRStream(x) --x--> VmulMul --[MemWCmd|VmulResp|z]--> MemWStream -> m_done
                   --MemRCmd--> MemRStream(y) --y-->
                   --VmulCmd (travels with the data)-->
```

The `VmulMul` loop writes one z word per iteration at II=1, which is the writer's rate. It reads one
x word and one y word every two iterations, using the generated `int16` / `int32` lane routines.

## Where the skills were unclear, wrong or missing (most important first)

1. **The device skill's shape leads to a poor HLS design** (`waveflow-device`, "The shape"). It shows
   one core kernel that sends `MemRCmd` to the reader and then reads the reply in the same firing. That
   contradicts `waveflow-kernel-hls` rule 7 ("never wait for a reply you just requested ... with a
   blocking read") and the `freerun_pipeline` process rule ("a write, then a blocking read on another
   stream, deadlocks"). For an element-wise op on two vectors, one reader also means x and y arrive one
   after the other, so the body has to buffer all of x, and `n` is unbounded. `vmul_v1_single_core.py`
   is that first try: it passes pysim, which never shows either problem. **Fix:** make the split shape
   the default in the device skill. An issue stage sends the read commands and the command itself; a
   compute stage takes the data and frames the write. Also add one line: "use one reader for each
   input the kernel needs at the same time."
2. **The kernel skill's example writes an ndarray where a `DataArray` is required.** `waveflow-kernel`
   "The shape" has `write_array(Elem, cmd.n, y)` with `y = golden_fn(...)`. pysim raises
   `TypeError: write_array(Int32, 16): the compute returned ndarray`. The fix is
   `array(Elem, y)` from `waveflow.hw.arrayutils`. The device skill's shape uses `array(...)` but never
   says where to import it from.
3. **The enum needs its own header step, and nothing reports it when it is missing.** `vmul_resp.h`
   includes `vmul_code.h`, which is generated only when the `EnumField` gets its own `DataSchemaStep`.
   The schema skill does say so (rule 6, "enums ... included"), but it is easy to miss, and the build
   gives no error until a C++ compile. Better: have `DataSchemaStep` emit nested enum headers itself,
   or refuse to run without them.
4. **The reference code hand-serializes, which the skills forbid.** `memcpy`, `interleaver` and the
   guide page `interface/axi_mm/master.md` all write
   `cmd_out.write(np.asarray(m.serialize(word_bw=w), dtype=np.uint64))`. `write(m)` already does this.
   An agent told to "copy the reference" copies that pattern, and `lint_schema_use.py` flags it.
5. **Where hand-written HLS bodies go is unclear.** The process says every header under `include/` is
   generated and must never be edited. But memcpy keeps its hand-written body there, while
   stream_kernel uses `src/`. Neither the device skill nor the HLS skill says which. I used `src/`.
6. **`MemStreamStep` copies other examples' bodies into the user's `include/`**: `il_cmd_rx_framed_task.h`,
   `il_compute_inband_task.h`, `mem_seq_framed_task.h`, and others. This is harmless, but confusing in a
   fresh project.
7. **The HLS skill doesn't cover input and output lanes of different widths.** Here int16 goes in at 4
   elements per word and int32 comes out at 2. The skill has shapes A and B, but nothing for "a word
   out per iteration, a word in every R iterations". I worked it out. One line in the skill, or in
   `loop_optimization.md`, would help.
8. **Smaller points:**
   - The kernel skill says ports are `FramedStreamIF*`, but the references use a plain
     `StreamIFSlave(has_tlast=False)` for the boundary command. I used Framed.
   - The generated top gives `m_done` no TLAST even though `done_framed=True`. It works the same way
     in memcpy, but nothing says whether that is intended.
   - The frame menu has nothing for "one kernel plus memory, no stages". `freerun_pipeline` fits once
     you see the device as a pipeline.
   - The example list includes `vecmult`, which sounds like this task but is a resource-modelling study.
   - The process says to stop after Stage 1 for approval. This trial carried on on purpose, because
     the user asked for an end-to-end test.

## Files

- `vmul.py` has the schemas, the golden, the two kernels, the device, the scenarios and the expected
  outputs. `test_vmul.py` holds the pysim tests.
- `vmul_v1_single_core.py` and `test_vmul_v1.py` are the first try (finding 1). They also pass pysim.
- `src/vmul_issue_task.h` and `src/vmul_mul_task.h` are the hand-written HLS bodies.
- `gen.py` generates `include/`, `gen/vmul.cpp` and `vmul.tcl`. Those outputs are not committed:
  run `python gen.py` to recreate them.
- `csim/` holds the g++ check: `run_csim.py`, `vmul_csim.cpp`, and stand-ins for the Vitis-only
  headers in `mock/`. It needs `AP_TYPES_INCLUDE` set to a clone of
  `Xilinx/HLS_arbitrary_Precision_Types/include`.

To run: `pytest` (pysim), then `python gen.py && python csim/run_csim.py`. Next, with Vitis: csynth
`vmul.tcl` and check that the `MUL` loop reaches II=1, then run the device's harness at RTL.
