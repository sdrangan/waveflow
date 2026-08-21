#ifndef WAVEFLOW_XSI_RFDC_H
#define WAVEFLOW_XSI_RFDC_H
// xsi_rfdc.h — the two converter models: an ADC that masters an AXI-Stream into the fabric, and a
// DAC that slaves one out of it.  Each spans BOTH sides of the cut: RTL pins on the fabric side, a
// BlockChannel on the RF side.  That is what a converter *is*, and it is why these are one object
// per path rather than a boundary model plus a separate channel peer.
//
// WHY NOT AxisMaster / AxisSlave
//
// Not because the data differs — that would just be a bundle.  Because the PROTOCOL BEHAVIOUR does,
// and in the one direction that matters:
//
//   * `AxisMaster` waits when TREADY is low.  A real ADC does not: it presents a beat every cycle
//     and whatever the fabric fails to take is GONE.  So `RfdcAdcMaster` never waits, and counts.
//   * `AxisSlave` is always ready and takes what comes.  A real DAC is also always ready, but a beat
//     that does NOT come is an underflow — an analog glitch — and AXI-Stream has no signal for it.
//     So `RfdcDacSlave` counts the cycles where a beat was due and TVALID was low.
//
// A generic model blocks, and blocking hides exactly the failure a converter design must not have.
// The counters are the contract; see plans/adc_model.md, "Underflow and overflow are the contract".
//
// THE STARTUP TRANSIENT IS NOT A FAULT.  A DAC fed through a pipeline must underrun until data
// reaches it — the loop through the RF grids costs at least one block index, structurally.  So
// `underrun` is compared against a DECLARED transient, never against zero, and `last_underrun_cycle`
// exists because a count alone cannot tell a transient from a steady-state fault.
//
// The sample arithmetic — quantize, pack, and their inverses — is in xsi_rfdc_samp.h, which has no
// Vivado dependency and is gated bit-exactly against Python's FixedField and schema serializer by
// tests/build/test_xsi_rfdc_samp.py.  Everything below is pin driving and accounting.
#include <cstdint>
#include <string>
#include <vector>

#include "xsi_bfm.h"
#include "xsi_channel.h"
// RfBlockMsg / RfChannel and the file-backed peers on either end of the edge.  They live in their
// own header because none of them binds an RTL pin, so they compile (and are gated) without Vivado.
#include "xsi_rf_block.h"
#include "xsi_rfdc_samp.h"

namespace wfbfm {

// ---------------------------------------------------------------------------
// ADC: RF blocks in, AXI-Stream master out.
// ---------------------------------------------------------------------------

class RfdcAdcMaster : public XsiSimObj {
public:
    /// *rf_in* is the RF-side edge; *prefix* the DUT's AXIS slave port this masters into.
    /// *words_per_cycle* is derived, never declared: `samp_rate / (samp_per_word * f_axis)`.
    RfdcAdcMaster(Dut& dut, const char* prefix, RfChannel& rf_in,
                  const RfdcFormat& fmt, double words_per_cycle)
        : d_(dut), rf_(rf_in), fmt_(fmt), rate_(words_per_cycle),
          tvalid_(dut.port((std::string(prefix) + "_TVALID").c_str())),
          tready_(dut.port((std::string(prefix) + "_TREADY").c_str())),
          tdata_(dut.port((std::string(prefix) + "_TDATA").c_str())),
          tlast_(dut.port_opt((std::string(prefix) + "_TLAST").c_str())) {}

    void sample() override {
        beat_ = have_ && d_.get1(tready_);          // a transfer happens iff VALID && READY
    }

    void update() override {
        ++cycle_;
        if (beat_) { ++words_sent; ++wpos_; have_ = false; }

        if (rate_.tick()) {
            if (have_) {
                // The fabric did not take the previous word and the next one is already due.  A real
                // converter does not wait -- that word is gone.
                ++dropped;
                last_drop_cycle = cycle_;
                have_ = false;
                ++wpos_;
            }
            if (wpos_ >= words_.size()) refill_();
            if (wpos_ < words_.size()) { word_ = words_[wpos_]; have_ = true; }
        }
    }

    void drive() override {
        d_.put1(tvalid_, have_ ? 1u : 0u);
        if (have_) d_.putW(tdata_, word_);
        if (tlast_ >= 0) d_.put1(tlast_, (have_ && wpos_ + 1 == words_.size()) ? 1u : 0u);
    }

    /// Words the fabric accepted.
    std::uint64_t words_sent = 0;
    /// Words the fabric was not ready for, which the converter therefore **discarded**.  A real ADC
    /// drops; it cannot stall.  Expected to be zero in any design whose fabric keeps up.
    std::uint64_t dropped = 0;
    std::uint64_t last_drop_cycle = 0;

private:
    void refill_() {
        RfBlockMsg blk;
        if (!rf_.pop(blk)) return;                     // starved: the channel counts it, not us
        words_.resize(blk.data.size() / fmt_.samp_per_word);
        rfdc_pack(blk.data.data(), (int)blk.data.size(), fmt_, words_.data());
        wpos_ = 0;
    }

    Dut&       d_;
    RfChannel& rf_;
    RfdcFormat fmt_;
    RateTick   rate_;
    int tvalid_, tready_, tdata_, tlast_;
    std::vector<std::uint64_t> words_;
    std::size_t   wpos_  = 0;
    std::uint64_t word_  = 0;
    bool          have_  = false;
    bool          beat_  = false;
    std::uint64_t cycle_ = 0;
};

// ---------------------------------------------------------------------------
// DAC: AXI-Stream slave in, RF blocks out.
// ---------------------------------------------------------------------------

class RfdcDacSlave : public XsiSimObj {
public:
    /// *rf_out* is the RF-side edge; *prefix* the DUT's AXIS master port this answers.
    /// *blk_samples* is `n_ch * blksize` — one block's worth, the unit the RF edge moves.
    RfdcDacSlave(Dut& dut, const char* prefix, RfChannel& rf_out,
                 const RfdcFormat& fmt, double words_per_cycle, std::size_t blk_samples)
        : d_(dut), rf_(rf_out), fmt_(fmt), rate_(words_per_cycle),
          // The BLOCK grid, derived from the word rate rather than declared: one block is
          // blk_samples/samp_per_word words, so blocks/cycle = words/cycle / words-per-block.  A DAC
          // plays continuously and the grid is what it plays ON -- see emit_on_grid_().
          blk_rate_(words_per_cycle * double(fmt.samp_per_word) / double(blk_samples)),
          blk_(blk_samples),
          tvalid_(dut.port((std::string(prefix) + "_TVALID").c_str())),
          tready_(dut.port((std::string(prefix) + "_TREADY").c_str())),
          tdata_(dut.port((std::string(prefix) + "_TDATA").c_str())) {}

    void sample() override {
        // TREADY is the DAC's METRONOME, and withholding it is the whole point.
        //
        // This used to be `put1(tready_, 1)` unconditionally, with the comment "a converter is always
        // ready".  That is true of the ADC side (an ADC cannot be told to wait) and **false of this
        // one**: an RFDC's AXIS slave accepts a word only as fast as its tile consumes samples, and
        // the depth of its input FIFO is what bounds how far the fabric may run ahead.
        //
        // The consequence of the old model was not subtle.  `RfSampBufPlayer` documents that "in RTL
        // this task is paced by TREADY"; against an always-ready slave it was paced by nothing and
        // ran at the FABRIC rate -- one word per `fire_cycles` = 3 cycles, where the converter's grid
        // wants one per 4.  The play pointer therefore advanced 33% faster than the ADC produced, and
        // in `rf_blk_delay` it overtook the loader after five blocks: every command from the sixth on
        // came back TOO_LATE, and the measured end-to-end delay was 960 samples instead of the 1024
        // the design asked for.  Nothing about the design was wrong; the converter model was.
        //
        // It also made `underrun` meaningless.  With beats arriving every 3 cycles and words due
        // every 4, "a word was due and no beat landed this cycle" counted the beat pattern of two
        // unrelated periods -- 10000 of 60000 cycles on a run that was bit-exact end to end.  Held to
        // the grid, the counter measures starvation again, which is what it is for.
        ready_ = (pending_ < cap_);
        beat_ = ready_ && d_.get1(tvalid_);
        if (beat_) word_ = d_.getW(tdata_);
    }

    void update() override {
        ++cycle_;
        if (beat_) {
            ++words_recv;
            ++pending_;
            std::int64_t slot[64];
            rfdc_unpack_word(word_, fmt_, slot);
            for (int k = 0; k < fmt_.samp_per_word; ++k)
                samples_.push_back(rfdc_dequantize(slot[k], fmt_));
        }
        // A DAC plays on its GRID, not when its buffer happens to fill.
        if (blk_rate_.tick()) emit_on_grid_();
        if (rate_.tick()) {
            // One word period has elapsed: the tile consumes a word if it has one.
            if (pending_ > 0) {
                --pending_;
            } else {
                // A word was due and the FIFO was empty.  There is no protocol signal for this;
                // the analog output glitches and THIS COUNTER is the only evidence.
                ++underrun;
                last_underrun_cycle = cycle_;
            }
        }
    }

    void drive() override { d_.put1(tready_, ready_ ? 1u : 0u); }

    /// Words taken off the fabric.
    std::uint64_t words_recv = 0;
    /// Cycles where a beat was due and none came — underflow.  Compare against the pipeline's
    /// **declared** startup transient, never against zero: a DAC fed through a pipeline must
    /// underrun until data reaches it.
    std::uint64_t underrun = 0;
    std::uint64_t last_underrun_cycle = 0;
    /// Blocks pushed onto the RF edge.
    std::uint64_t blocks_out = 0;

    /// Blocks the grid emitted with nothing to play, so a ZERO block went out.  The direct
    /// analogue of pysim's ``RFSampIF.underrun``, and the same physics: there is no protocol signal
    /// for "you were late", so this counter is the only evidence.  A DAC fed through a pipeline
    /// MUST zero-fill its first blocks -- compare against the declared startup transient, never
    /// against zero.
    std::uint64_t blocks_zero_filled = 0;
    /// Grid index of the most recent zero-fill (0 = never).  A count cannot separate a startup
    /// transient from a steady-state fault; this can.
    std::uint64_t last_zero_fill_idx = 0;

private:
    /// One block period has elapsed: play whatever is buffered, or zero-fill and count.
    ///
    /// **This is the correction that matters.**  It used to emit on buffer fullness
    /// (`if (samples_.size() >= blk_) emit_()`), which is not what a converter does: a DAC's tile
    /// clock does not wait for the fabric.  pysim's `RFSampIF` metronome had it right all along --
    /// the grid is the physics, and the startup zero-fill it produces is a real converter behaviour,
    /// not "a pysim-side artifact" as an earlier session concluded from this model's silence.
    void emit_on_grid_() {
        RfBlockMsg blk;
        blk.idx = ++blocks_out;
        if (samples_.size() >= blk_) {
            blk.data.assign(samples_.begin(), samples_.begin() + (std::ptrdiff_t)blk_);
            samples_.erase(samples_.begin(), samples_.begin() + (std::ptrdiff_t)blk_);
        } else {
            blk.data.assign(blk_, 0.0);                // underflow: deterministic, visible, counted
            ++blocks_zero_filled;
            last_zero_fill_idx = blk.idx;
        }
        rf_.push(blk);                                 // full => the channel counts the drop
    }

    Dut&       d_;
    RfChannel& rf_;
    RfdcFormat fmt_;
    RateTick   rate_;
    RateTick   blk_rate_;
    std::size_t blk_;
    int tvalid_, tready_, tdata_;
    std::vector<double> samples_;
    std::uint64_t word_  = 0;
    bool          beat_  = false;
    bool          ready_ = true;
    std::uint64_t cycle_ = 0;

    /// Words accepted but not yet consumed by the tile — the IP's AXIS input FIFO.
    std::size_t pending_ = 0;
    /// Its depth.  **2, the AXI-Stream boundary depth Vitis gives a port**, not a tunable: a deeper
    /// one lets the fabric run further ahead of the converter and is exactly the fiction this model
    /// used to tell with an infinite one.  Small enough that the player is held to the grid within a
    /// couple of words, which is what makes the play pointer track real time.
    static constexpr std::size_t cap_ = 2;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_RFDC_H
