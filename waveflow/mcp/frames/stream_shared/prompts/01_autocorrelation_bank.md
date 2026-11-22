# Autocorrelation bank on one FFT

Build, in Waveflow, a kernel that computes the circular autocorrelation of four channels' sample blocks
on **one** FFT:

- Each channel sends commands on its own stream: a header with a `tx_id` and a block count, then the
  blocks, each 64 complex samples in `ap_fixed<16,2>`, four samples a word.
- For each block: `X = FFT(x)`, then `P = |X|^2` scaled back into the FFT's input format, then
  `r = IFFT(P)`, the block's autocorrelation; all 64 lags are the answer.
- Each block's answer is a header echoing the channel, the `tx_id` and the block number, then the lags.
  Each command ends with a status on the channel's own status stream.
- The channels arrive at different rates and their commands differ in length; the answers may come out
  in a different order across channels, but never within one.
- One FFT only: use Waveflow's `SsrFft`.  The RFSoC 4x2 at 250 MHz.
