# Circular-Buffer Waveform Player → RFDC DAC

A standalone Vivado / Vitis teaching project: a Vitis HLS accelerator plays IQ
samples out of a circular buffer in BRAM to the RF Data Converter (DAC) over
AXI-Stream.

## Goal

- The accelerator holds a **two-port BRAM** modeling a circular buffer of IQ samples.
- The **PS writes** the buffer (fills it once), then goes idle.
- The **accelerator reads** the BRAM continuously and circularly, feeding samples
  to the **RFDC** over AXI-Stream.
- Flow control on the stream rate-matches the DAC sample rate (see the timing
  correction at the end — this is only half true).

## Architecture

The cleanest mapping is a **true dual-port Block RAM** shared between the PS and
the accelerator:

```
        AXI (HPM0)                          BRAM port A
PS  ──────────────►  AXI BRAM Controller ───────────────┐
 │                                                       ▼
 │  AXI-Lite (control: n_words, enable, start)    [ Block Memory Generator ]
 └──────────────────────────────────┐            (True Dual Port, registered)
                                     ▼                   ▲ BRAM port B
                              [ HLS wave_player ] ───────┘
                                     │ AXI4-Stream (packed IQ)
                                     ▼
                              [ RF Data Converter ]  ──► DAC tile
```

- **Port A** of the BRAM → AXI BRAM Controller → PS. The PS fills it, then goes idle.
- **Port B** of the BRAM → the HLS kernel's `bram` interface. The kernel reads
  continuously, wrapping the index.
- The kernel's AXIS master → RFDC DAC slave stream.

This structure is required because the PS *cannot* write into an HLS-internal
array; you need an explicitly instantiated BRAM with one port exposed to each
master.

## Vitis HLS code

```cpp
#include <ap_int.h>
#include <hls_stream.h>
#include "ap_axi_sdata.h"

// Gearbox: the RFDC sends/receives many samples per fabric clock.
// These MUST match what the RF Data Converter GUI reports for your
// sample rate / interpolation / PL clock.
#define SPC       16                 // samples per AXIS beat
#define SAMPLE_W  16                 // bits per (real) sample component
#define WORD_W    (SPC * SAMPLE_W)   // 256-bit AXIS / BRAM word

typedef ap_uint<WORD_W>          word_t;
typedef ap_axiu<WORD_W,0,0,0>    axis_word_t;   // TDATA only

void wave_player(
    const word_t*           buffer,    // shared dual-port BRAM (PS writes)
    hls::stream<axis_word_t>& dac,     // to RFDC DAC
    unsigned int            n_words,   // circular length (valid words)
    volatile int            enable)    // run/stop, polled from PS
{
#pragma HLS INTERFACE mode=bram      port=buffer
#pragma HLS INTERFACE mode=axis      port=dac
#pragma HLS INTERFACE mode=s_axilite port=n_words
#pragma HLS INTERFACE mode=s_axilite port=enable
#pragma HLS INTERFACE mode=s_axilite port=return

    unsigned int idx = 0;
play_loop:
    while (enable) {
#pragma HLS PIPELINE II=1
        word_t s = buffer[idx];

        axis_word_t beat;
        beat.data = s;
        beat.keep = -1;     // all bytes valid
        beat.last = 0;      // free-running stream, no packet boundary
        dac.write(beat);    // blocks when RFDC TREADY is low

        idx = (idx + 1 == n_words) ? 0 : idx + 1;  // circular wrap
    }
}
```

### Run model

With `ap_ctrl_hs` (the default control above), the PS sets `n_words`, sets
`enable=1`, then pulses `ap_start` once. The kernel then streams "forever" until
the PS clears `enable`. `enable` is `volatile` so it's re-read every iteration.
This lets you stop and reload the buffer.

- If you never need to stop, simplify to `while(1)` and
  `#pragma HLS INTERFACE ap_ctrl_none port=return` for a truly free-running
  kernel (no PS start handshake) — but then drop the s_axilite control bundle and
  pass `n_words` another way (a fixed depth or an AXIS/constant).
- **IQ note:** if the RFDC tile is in I/Q mode, pack I and Q in the byte order the
  RFDC expects (interleaved or split I/Q streams depending on its config). For a
  real DAC, the word is just consecutive real samples. The packing must match the
  RFDC IP's exported AXIS layout exactly.

## Vivado project sketch

1. **Create project**, select the RFSoC 4x2 board file (or part
   `xczu48dr-fsvg1517-2-e`).
2. **Block design**: add **Zynq UltraScale+ RFSoC PS**, run block automation
   (enables an HPM AXI master + a PL clock + reset).
3. **Add RF Data Converter IP.** Configure one DAC tile: sample rate,
   interpolation, and check the reported **AXIS data width** and **required AXIS
   clock**. Those two numbers fix `WORD_W`/`SPC` in the HLS code and the kernel
   clock.
4. **Clocking.** Drive the kernel and the DAC AXIS interface from the *same*
   clock — the one whose frequency equals `sample_rate / SPC` (use the RFDC's
   clock output or a Clocking Wizard / PS PL clock set to it).
5. **Export the HLS IP** (`Export RTL` in Vitis HLS) and add it to the IP catalog,
   then drop it in the BD.
6. **Add Block Memory Generator** (mode = *True Dual Port RAM*, registered output)
   and an **AXI BRAM Controller**.
   - BRAM controller ↔ PS HPM via an **AXI SmartConnect**.
   - BMG port A ↔ AXI BRAM Controller; BMG port B ↔ kernel `buffer` BRAM port.
7. **Connect** the kernel's `s_axi_control` to the PS (through the SmartConnect)
   and the kernel AXIS master to the RFDC DAC `s_axis`.
8. **Wire clocks/resets** (Processor System Reset per clock domain),
   **Validate Design**, generate bitstream, **Export XSA**.

**PS software (Vitis):** memcpy your IQ samples to the BRAM controller's base
address → write `n_words` and `enable=1` via the generated `XWave_player` driver →
pulse start. Then the PS can go idle.

## Timing correction: flow control is only half the story

The "flow control naturally matches the sample rate" idea is only half right, and
the difference matters for timing closure:

- The RFDC DAC AXIS clock and width are **exactly determined**:
  `f_axis = sample_rate / SPC`. In steady state the DAC consumes **one beat every
  clock**, so `TREADY` stays high almost continuously. It only deasserts during
  transient FIFO fill at startup.
- The real requirement isn't "fast enough" with slack — it's that the kernel
  **sustains II=1 at that exact AXIS clock**. The single BRAM read per beat makes
  II=1 easy here, so you're fine.
- If the kernel ever fails to produce a beat that cycle (II>1, or a stall),
  `TREADY` backpressure does **not** save you — the DAC **underflows** and you get
  a gap/glitch in the analog output. Backpressure protects against
  *over*-production, not *under*-production.

So: keep the datapath at II=1 (it is), match `WORD_W`/`SPC`/clock to the RFDC's
reported values, and the playout is glitch-free. The circular buffer just means
`idx` wraps; nothing about the rate matching changes.

## Packing multiple samples per beat (high sample rates)

When the **sample rate exceeds the PL clock** you can no longer send one sample
per AXIS beat — the fabric simply can't toggle fast enough. The fix is a
**gearbox**: widen the AXIS word so each beat carries `SPC = ceil(f_sample /
f_axis)` samples. With a 256-bit word and 16-bit samples that is `SPC = 16`, so a
250 MHz fabric clock sustains a 4 GSPS converter (`250 MHz × 16 = 4 GSa/s`).

### Is there a Vitis HLS API for RFDC packing?

**No — there is no RFDC-specific packing API in Vitis HLS.** The RF Data
Converter's AXI4-Stream is just a flat `TDATA` bus of width `SPC × sample_width`.
The Xilinx `XRFdc` driver exists only to *configure* the converter from the PS; it
does nothing for the datapath. So packing is your responsibility in the kernel (or
in the PS when it fills the BRAM). What HLS gives you are general bit-packing
facilities — `ap_(u)int` range selects, `hls::vector`, and aggregated structs —
none of them RFDC-aware.

**The layout the RFDC expects** (confirm against PG269 for your exact tile config):

- Samples are packed **time-ascending from the LSBs**. The oldest sample in the
  beat occupies the least-significant slot.
- Each sample sits in a fixed-width slot (e.g. 16 bits) even if the converter is
  14-bit — check PG269 for LSB-vs-MSB alignment within the slot.
- For **I/Q** tiles, I and Q are interleaved (or carried on separate streams)
  according to the tile's digital-mixer / data-format settings.

```
256-bit TDATA, SPC=16, 16-bit real samples:

 bits [ 15:  0] = sample t0   (oldest in this beat)
 bits [ 31: 16] = sample t1
 bits [ 47: 32] = sample t2
        ...
 bits [255:240] = sample t15  (newest in this beat)
```

### Option A — manual bit-slice pack (most explicit, always correct)

Use this when the BRAM stores **individual** samples and the kernel assembles
`SPC` of them per beat. (In the initial sections the BRAM already held pre-packed
256-bit words — this is the alternative where the PS stores raw samples instead.)

```cpp
#define SPC       16
#define SAMPLE_W  16
#define WORD_W    (SPC * SAMPLE_W)   // 256

typedef ap_int<SAMPLE_W>      sample_t;   // signed DAC sample
typedef ap_uint<WORD_W>       word_t;
typedef ap_axiu<WORD_W,0,0,0> axis_word_t;

void wave_player(
    const sample_t*           buffer,   // BRAM of individual samples
    hls::stream<axis_word_t>& dac,
    unsigned int              n_samples,
    volatile int              enable)
{
#pragma HLS INTERFACE mode=bram      port=buffer
#pragma HLS INTERFACE mode=axis      port=dac
#pragma HLS INTERFACE mode=s_axilite port=n_samples
#pragma HLS INTERFACE mode=s_axilite port=enable
#pragma HLS INTERFACE mode=s_axilite port=return

    unsigned int idx = 0;
play_loop:
    while (enable) {
#pragma HLS PIPELINE II=1
        word_t beat_data = 0;
    pack:
        for (int k = 0; k < SPC; k++) {
#pragma HLS UNROLL
            sample_t s = buffer[idx];
            // oldest sample -> lowest slot
            beat_data.range((k+1)*SAMPLE_W - 1, k*SAMPLE_W) =
                ap_uint<SAMPLE_W>(s);          // reinterpret bits, no sign-extend
            idx = (idx + 1 == n_samples) ? 0 : idx + 1;
        }

        axis_word_t beat;
        beat.data = beat_data;
        beat.keep = -1;
        beat.last = 0;
        dac.write(beat);
    }
}
```

For II=1 the BRAM must deliver `SPC` reads per cycle — partition the buffer with
`#pragma HLS ARRAY_PARTITION variable=buffer cyclic factor=16`, or (simpler) keep
the BRAM storing **pre-packed 256-bit words** as in the initial design and do the
packing in the PS. The pre-packed approach keeps the kernel at a single read per
beat and is usually what you want.

### Option B — `hls::vector` (closest thing to a packing "API")

`hls::vector<T, N>` maps to exactly one packed bus of `N × sizeof(T)` bits, with
element 0 in the LSBs — the same ordering the RFDC wants. It gives you indexed
access without manual `.range()` math.

```cpp
#include "hls_vector.h"
typedef hls::vector<ap_int<16>, 16> beat_vec_t;   // 256-bit packed

// ... beat_vec_t v;  v[k] = sample_k;  axiu.data = *(ap_uint<256>*)&v;
```

### Option C — aggregated struct

A struct of `SPC` sample fields collapses to one wide word with
`#pragma HLS aggregate compact=bit`. Field order maps to bit order (first field in
the LSBs), so declare them time-ascending to match the RFDC.

```cpp
struct beat_s { ap_int<16> s0, s1, /* ... */ s15; };
#pragma HLS aggregate variable=beat_s compact=bit
```

**Whichever option you pick, the bit ordering is the part to get right** — verify
against PG269 and, ideally, against a cosim capture of the RFDC's `s_axis` before
trusting the analog output. The HLS tool will happily pack in either endianness;
only the RFDC datasheet tells you which is correct.

## Possible follow-ups

- PS-side C code (BRAM fill + driver calls).
- A SimPy / Waveflow `HwComponent` model of this player to simulate the
  buffer-to-DAC timing before hardware bring-up (signal-source-in-sim /
  real-IP-in-hw duality, per the RFSoC 4x2 plan).
```
