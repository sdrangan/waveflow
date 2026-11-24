# Skill test (schemas) 1: a pulse detector's messages

Write the code below with Waveflow. **Nothing is synthesized or simulated in Vitis**: the C++ is
written and read, not compiled. Python that runs on its own (no Vitis) may be run, and should be.

## 1. The kernel, in brief

A free-running kernel on two AXI4-Streams of `WORD_BW`-bit words, `WORD_BW` one of 32 and 64.  For each
command it reads a burst of samples and reports every sample above a threshold.

```text
--s_in--> [ pulse_det ] --m_out-->
```

## 2. The messages

Fields are listed in order.  `fixed<W, I>` means Vitis HLS `ap_fixed<W, I>` (`ufixed`: `ap_ufixed`),
truncation and wrap-around unless stated.  Pack every message into words as Waveflow does; bits a
message does not use are zero.

**`s_in`** -- a command (one burst), then its samples (one burst):

| field | type | meaning |
|---|---|---|
| `op` | uint8: 0 `DETECT`, 1 `END` | `END` stops the kernel; its other fields are zero |
| `tx_id` | uint16 | echoed in the reports |
| `nsamp` | uint16 | samples that follow, 1 to 2048 (0 for `END`) |
| `thresh` | `ufixed<16, 4>` | the detection threshold |

The samples: `nsamp` values, each a signed `fixed<12, 1>`.

**`m_out`** -- per `DETECT` command, one burst of messages of three kinds.  Every message begins with
`kind`, a uint8: 0 `START`, 1 `PEAK`, 2 `DONE`.

| message | fields after `kind` |
|---|---|
| `START` | `tx_id` (uint16) |
| `PEAK` | `index` (uint16, the sample's position), `value` (the sample, `fixed<12, 1>`) |
| `DONE` | `tx_id` (uint16), `count` (uint16, the number of `PEAK`s) |

TLAST is set on the last word of `DONE` only.

## 3. The operation

For a `DETECT` command: write `START`; for each sample `x[i]` with `x[i] > thresh`, in order, write
`PEAK(i, x[i])`; then write `DONE`.  On `END`, return.  Nothing is kept between commands.  (No error
handling is asked for: assume every burst is well formed.)

## 4. What to write

In this folder:

1. **`pulse_msgs.py`** -- every message above as Python, with Waveflow.
2. **`gen_headers.py`** -- generates the C++ headers the kernel needs, for `WORD_BW` 32 and 64, into
   `include/`.  Run it.
3. **`pulse_model.py`** -- `pulse_det_model(words_in, word_bw) -> words_out`: the operation as a Python
   function of the input words, returning the output words.  Include a check that runs it on the worked
   example below at both widths and asserts the decoded result.  Run it.
4. **`pulse_det_body.cpp`** -- the kernel body in Vitis HLS C++:
   `template <int W> void pulse_det(hls::stream<streamutils::axi4s_word<W>>& s_in,
   hls::stream<streamutils::axi4s_word<W>>& m_out)`, including only the headers from step 2.  It need
   not be compiled; it should be written as you would to synthesize it.
5. **`NOTES.md`** -- a few lines: each message's word layout at `WORD_BW = 32`, and anything you could not
   do with Waveflow.

### Worked example

`DETECT`, `tx_id = 7`, `thresh = 0.25`, samples `[0.0, 0.5, -0.75, 0.25, 0.3125]`.  Output:
`START(7)`, `PEAK(1, 0.5)`, `PEAK(4, 0.3125)`, `DONE(7, 2)`.  Then `END`.
