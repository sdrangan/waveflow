---
title: Protocol and interfaces
parent: Streaming polynomial
nav_order: 1
summary: "What crosses each of the kernel's three interfaces, word by word: the command header with its four coefficients, the sample burst, the response header and results, at 32 and 64 bits, with every layout taken from serializing the schemas; the status-only AXI-Lite register map; which bursts TLAST frames; and when -- and how -- to add a response footer."
---

# Protocol and interfaces

What crosses each interface, word by word?  This page is the wire-level detail behind the
[contract](./index.md#the-contract): the schemas the host and the kernel share, the layout of
every burst at both stream widths, and the register map.

## Three interfaces

| Interface | Direction | Carries | Port |
|---|---|---|---|
| `in_stream` | host → kernel | commands: `PolyCmdHdr`, then a `DATA` command's samples | `s_in`, AXI4-Stream |
| `out_stream` | kernel → host | responses: `PolyRespHdr`, then the results | `m_out`, AXI4-Stream |
| AXI-Lite | host ↔ kernel | `ap_start` / `ap_done`, and the status registers | `s_axi_control` |

The two streams carry everything the kernel computes with (rule 2).  AXI-Lite carries nothing
the kernel computes with -- only the start, the done, and what happened (rule 3).

## The schemas

The messages are [`DataList`](../../guide/schema/python/datalists.md) schemas declared once in
[`poly.py`](https://github.com/sdrangan/waveflow/blob/main/examples/stream_inband/poly.py).
Waveflow generates the C++ structs and their serializers from them, so the Python model and the
C++ kernel pack words with code from the same declaration, and nobody packs a word by hand.

```python
class PolyCmdType(IntEnum):
    DATA = 0
    END = 1

class CoeffArray(DataArray):            # c0..c3, constant term first
    ncoeff: HwConst[int] = 4
    element_type = Float32
    static = True
    max_shape = (ncoeff,)

class PolyCmdHdr(DataList):
    elements = {
        "cmd_type": {"schema": PolyCmdTypeField, "description": "DATA or END"},
        "tx_id":    {"schema": TxIdField,        "description": "Command ID: echoed, or reported on error"},
        "nsamp":    {"schema": NsampField,       "description": "Sample count (0 for END)"},
        "coeffs":   {"schema": CoeffArray,       "description": "c0..c3, constant term first"},
    }

class PolyRespHdr(DataList):
    elements = {
        "tx_id": {"schema": TxIdField, "description": "Echo of the DATA command's tx_id"},
    }
```

**The coefficients are in the command header.**  Every `DATA` command carries the four
coefficients it is to be evaluated with, so a command depends on nothing sent before it (rule 4)
and the kernel keeps no configuration between commands.  The cost is four extra words per command
at 32 bits -- small against a burst of samples.  [Decisions your spec must make](./decisions.md)
discusses when that cost stops being small.

An `END` command is a `PolyCmdHdr` too.  Its `nsamp` and `coeffs` are sent as zeros and ignored.

## Bursts on the wire

| Command | `in_stream` bursts | `out_stream` bursts |
|---|---|---|
| `DATA`, `nsamp > 0` | `PolyCmdHdr` (TLAST) · `nsamp` samples (TLAST on the last word) | `PolyRespHdr` (TLAST) · `nsamp` results (TLAST on the last word) |
| `DATA`, `nsamp = 0` | `PolyCmdHdr` (TLAST) | `PolyRespHdr` (TLAST) |
| `END` | `PolyCmdHdr` (TLAST) | nothing |

**TLAST frames the sample burst, and only the sample burst is checked.**  A header has a fixed
length set by its schema, and the generated reader takes exactly that many words; it does not look
at TLAST.  The sample burst is the only part whose length varies, and its TLAST is checked against
`nsamp`: early is `TLAST_EARLY_SAMP_IN`, missing is `NO_TLAST_SAMP_IN`.

## Word layouts

Each layout below is what `serialize()` produces for the example `DATA` command `tx_id = 42`,
`nsamp = 100`, coefficients `[1, -2, -3, 4]`.  (`tests/examples/test_poly_demo.py` re-serializes
these values and checks them against this page.)

**32-bit words** -- one float per word:

| Burst | Words |
|---|---|
| `PolyCmdHdr`, 6 words | `0x00000054` (`tx_id << 1 \| cmd_type`) · `0x00000064` (`nsamp`) · `0x3f800000 0xc0000000 0xc0400000 0x40800000` (c0..c3) |
| `END` | `0x00000001` followed by five zero words |
| `PolyRespHdr`, 1 word | `0x0000002a` |
| samples `[0.5, -1, 2]` | `0x3f000000` · `0xbf800000` · `0x40000000` |

`nsamp` does not fit in the 15 bits left over in the first word, so it starts the second: a
field is never split across words.

**64-bit words** -- two floats per word, element 0 in the low half:

| Burst | Words |
|---|---|
| `PolyCmdHdr`, 3 words | `0x0000000000c80054` (`nsamp << 17 \| tx_id << 1 \| cmd_type`) · `0xc00000003f800000` (c1, c0) · `0x40800000c0400000` (c3, c2) |
| `PolyRespHdr`, 1 word | `0x000000000000002a` |
| samples `[0.5, -1, 2]` | `0xbf8000003f000000` (-1, 0.5) · `0x0000000040000000` (pad, 2) |

An odd `nsamp` leaves the high half of the last sample word unused.  The host sends it as zero and
the kernel writes it as zero.

## The register map

The AXI-Lite register map holds the Vitis control block and three **read-only** status fields:

| Offset | Field | Meaning |
|---|---|---|
| `0x00` | `ap_start`, `ap_done`, `ap_idle`, `ap_ready` | the Vitis `ap_ctrl_hs` control bits |
| `0x04`, `0x08`, `0x0C` | `gier`, `ier`, `isr` | the `ap_done` interrupt |
| `0x10` | `halted` | 1 if the run ended on an error |
| `0x20` | `error` | the `PolyError` code |
| `0x30` | `tx_id` | the `tx_id` of the command that failed |

There is **no writable configuration field**.  The kernel's top-level function takes the two
streams and the three status outputs, and nothing else:

```cpp
void poly(hls::stream<streamutils::axi4s_word<32>>& s_in,
          hls::stream<streamutils::axi4s_word<32>>& m_out,
          ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id);
```

The error codes:

| Code | Name | Raised when |
|---|---|---|
| 0 | `NO_ERROR` | |
| 1 | `TLAST_EARLY_SAMP_IN` | TLAST arrives before the last sample word |
| 2 | `NO_TLAST_SAMP_IN` | the last sample word has no TLAST |

After a successful run `halted`, `error` and `tx_id` are all 0.

## Adding a footer

This kernel's response is `RespHdr | results`.  A kernel that also computes something **about** the
data -- a count of clipped samples, the index of a peak, a checksum -- has a value that is known only
after the last sample.  It cannot go in the response header without buffering the whole burst first,
which costs memory and a burst of latency.  That value goes in a **response footer**, after the data:

```text
# A sketch -- not part of this example: a gain-and-clip kernel's footer.
class ClipRespFtr(DataList):
    elements = {
        "n_clip_lo": {"schema": U16, "description": "samples clipped at the lower bound"},
        "n_clip_hi": {"schema": U16, "description": "samples clipped at the upper bound"},
    }
```

```
out_stream:   RespHdr (TLAST) | results (TLAST) | RespFtr (TLAST)
```

Three rules keep a footer honest:

1. **Prefer the header.**  A value known before the data -- the `tx_id` echo, the length -- belongs
   in the header.  The footer is only for what cannot be known earlier.
2. **A footer is response data, not an error channel.**  It is sent only when the command
   succeeds.  On an error the kernel closes its output and returns (rule 6), and the status
   registers say what happened.  A footer that reports errors is the first step toward the
   "report and keep going" design that [Why this contract](./why_not.md#a-response-footer-with-recovery)
   argues against.
3. **It is a fixed-length schema, sent as its own burst.**  The host then knows from the command
   alone exactly what comes back.

## Check your understanding

1. At 32 bits, how many `in_stream` words does a `DATA` command with `nsamp = 7` take, headers and
   samples together?  How many at 64 bits?
2. Why does the kernel not check TLAST on the command header?
3. A kernel reports the largest result in each burst.  Does that value go in the response header or
   a footer, and why?

---

Next: [Why this contract →](./why_not.md)
