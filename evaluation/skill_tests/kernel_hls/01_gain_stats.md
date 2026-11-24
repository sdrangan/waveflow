# Skill test (HLS bodies) 1: a gain stage with statistics

Write the Vitis HLS kernel body below with Waveflow. **Nothing is synthesized or simulated in Vitis**:
the C++ is written and read, not compiled. Python that runs on its own (no Vitis) may be run, and
should be.

## 1. What you are given

Three files in this folder. Use them as they are; do not change them.

- **`gain_msgs.py`** -- the messages, as Waveflow schemas: `GainCmd`, `GainResp`, `GainStatus`, and the
  element types `Samp` (`ap_fixed<16, 1>`) and `Out` (`ap_fixed<16, 2, AP_RND, AP_SAT>`).
- **`gen_headers.py`** -- generates their C++ headers into `include/`, for `WORD_BW` 32 and 64. Run it
  first.
- **`gain_model.py`** -- the golden model. `GainStats.run_iter` is one command; the kernel must match
  it bit for bit, TLAST included. Run it to see the worked example pass.

## 2. The kernel

A free-running kernel (an `hls::task`) on three AXI4-Streams of `WORD_BW`-bit words:

```text
--s_in--> [ gain_stats ] --m_out-->
                         --m_status-->
```

For each command on `s_in` -- a `GainCmd` (`tx_id`, `n` from 1 to 4096, `gain`) followed by `n` samples
of `Samp`:

1. write a `GainResp` (`tx_id`, `n`) on `m_out`, then the `n` results `y[i] = gain * x[i]`, rounded and
   saturated to `Out`; TLAST on the last word of the results only;
2. then write one `GainStatus` on `m_status`, TLAST on its last word: the command's `tx_id`; `nsat`,
   how many of its results saturated (see `gain_model.py`); and `total`, the samples processed since
   reset, this command's included, a 32-bit count that wraps.

`total` is the only thing kept from one command to the next. No error handling is asked for: assume
every burst is well formed.

**Throughput.** The sample loop must keep up with the input bus: one `s_in` word per clock, at
`WORD_BW` 32 and 64.

## 3. What to write

In this folder:

1. **`gain_stats_body.cpp`** -- the body, one firing of the task:

   ```cpp
   template <int W>
   void gain_stats(hls::stream<streamutils::axi4s_word<W>>& s_in,
                   hls::stream<streamutils::axi4s_word<W>>& m_out,
                   hls::stream<streamutils::axi4s_word<W>>& m_status);
   ```

   The runtime calls it again and again, for as long as the kernel runs. Include only headers from
   `include/`. It need not be compiled; write it as you would to synthesize it.
2. **`NOTES.md`** -- a few lines: the body's shape and why; how many clocks a command of `n` samples
   takes, roughly, at each width; and anything you could not do with Waveflow.
