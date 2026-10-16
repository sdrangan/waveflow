# 2D rotation accelerator

## Function

The kernel rotates each sample (x, y) by an angle θ, given as cos θ and sin θ:

    x1 = x·cos θ − y·sin θ
    y1 = x·sin θ + y·cos θ

## Interfaces

- `in_stream`, `out_stream`: AXI-Stream, `WORD_BW` bits wide.
- `WORD_BW` is a build parameter. Build and test both 32 and 64.

## Protocol

- The host sends a command header with a transaction id, `len` (the number of (x, y)
  samples), `cos_theta` and `sin_theta`. `cos_theta` and `sin_theta` are Q10.8: 10 bits
  in total including the sign, 8 of them fractional.
- The host then sends the data interleaved: x[0], y[0], x[1], y[1], …, x[len−1], y[len−1].
  Every value is Q16.8: 16 bits in total including the sign, 8 of them fractional.
- The kernel outputs x1[0], y1[0], x1[1], y1[1], …, x1[len−1], y1[len−1], also in Q16.8.
- On an error the kernel halts and sets a status.

## What to deliver

1. **A Python fixed-point model** that is bit-exact with the kernel. Run it on vectors
   with different lengths and angles, measure its error against a floating-point
   rotation, and write the result to `fixp_mse.json`.
2. **Test vectors** generated from the Python model.
3. **The Vitis HLS kernel.** Its C simulation must match the Python model bit for bit on
   every test vector, at both widths, and C synthesis must complete. Co-simulation and
   timing are not part of this task.
4. **`report.md`**, readable by someone who has not seen the code:
   - the results: the model's error, and the C-simulation comparison at each width;
   - the exact word layout of the command header, the input data and the output, at each
     width;
   - the kernel's top function or functions, their arguments, and how a testbench drives
     one transaction through them;
   - the rounding and overflow behaviour;
   - every assumption you made where this spec is silent.
