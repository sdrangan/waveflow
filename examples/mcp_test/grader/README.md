# The rotate grader

An independent check of a blind-test run of [`rotate_func.md`](../rotate_func.md) -- the
spec both arms read -- or of the fuller [`rotate.md`](../rotate.md).  Each arm writes its own tests
and passes them; this is the test neither arm wrote.  `plans/hook_first_flow.md`, Stage 0.

## What it does

1. Generates **hidden** transactions from a seed picked at grading time: assorted angles
   and lengths, full-scale and corner samples, coefficients outside the unit circle (to
   force overflow), and exact rounding ties.  Nothing is stored, so nothing can leak.
2. Writes its **own** C++ testbench, which plays the words into the arm's kernel, calls
   it, and records what comes out.  The stream word type is used through `.data`,
   `.last` and `.keep` when they exist, so `ap_axiu`, `hls::axis` and plain `ap_uint`
   streams all work.
3. C-simulates the arm's kernel with it on Vitis, at each word width.
4. Compares every output sample with a fixed-point reference under each of 20 rounding,
   overflow and quantization-order conventions.  **PASS** = one convention matches every
   sample at every width.  `rotate.md` does not fix the convention, so any consistent
   choice is legal; what is graded is that the kernel computes the one it chose, exactly.

Error handling is not graded: the spec says only "halts on error and sets a status".

## Grading a run

Write an **adapter** for the run -- copy [`reference/rotate_ref_adapter.py`](reference/rotate_ref_adapter.py)
-- from the arm's **report** (`rotate_func.md` makes the report state the word layout, the
top functions and how a transaction is driven, for exactly this purpose), never from its code (an arm's packing bug
must not be graded against itself).  It gives:

| Name | What |
| --- | --- |
| `ROOT` | the arm's folder; Vitis runs there, and every source must lie under it |
| `SOURCES`, `INCLUDE`, `CFLAGS` | the kernel's design files (relative to `ROOT`), the header declaring its tops, include flags |
| `WORD_BWS`, `TOPS` | the widths it supports and the top function for each |
| `OUT_BITS`, `OUT_FRAC` | the output samples' format |
| `cpp_decls(bw)`, `cpp_call(bw)`, `CPP_STATUS` | C++ declaring the streams `in` / `out` and every other argument; the call; status variables to print |
| `encode(txs, bw)` | per kernel call, the `(word, tlast)` list for the transactions |
| `decode(calls, txs, bw)` | per transaction, `(x1, y1)` as signed raw integers, or `None` |

Then:

```
python -m examples.mcp_test.grader.rotate_grader --adapter path/to/adapter.py --work path/to/grade
```

`grade.md` and `grade.json` land in the work directory, which should be beside the run's
folder, not in it.  The grader writes its Vitis projects (`_grader_w<N>/`) and testbenches
into `ROOT`: Vitis 2025.1 leaves a design or testbench file out of csim when it is not
below the directory Vitis runs in.

## Checking the grader

`tests/mcp/test_rotate_grader.py` grades the reference kernel in `reference/` and two
mutants of it on Vitis: the reference matches only `half_up/saturate/sum`, a truncating
mutant only `floor/saturate/sum`, and a sign error matches nothing.
