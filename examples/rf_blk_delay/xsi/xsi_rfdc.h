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
//
// ONE MODEL PER DIRECTION, N PORTS
//
// A Waveflow `Rfdc` is a TILE: it presents one AXI-Stream port per channel on the fabric side, and
// ONE RF edge carrying every channel of that direction in one `(n_ch, blksize)` block.  That
// asymmetry is why these models take a PORT LIST rather than a single pin group -- n_ch independent
// models could not each own the one edge behind them.
//
// Row `ch` of the RF block is what port `ch` carries, matching `pack`/`unpack` on the Python side:
// a channel-major block IS a per-port array, so neither side transposes anything.
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <string>
#include <vector>

#include "xsi_bfm.h"
#include "xsi_channel.h"
// RfBlockMsg / RfChannel and the file-backed peers on either end of the edge.  They live in their
// own header because none of them binds an RTL pin, so they compile (and are gated) without Vivado.
#include "xsi_rf_block.h"
#include "xsi_rfdc_samp.h"

namespace wfbfm {

/// The AXIS ports one converter model spans, in channel order.
///
/// Implicitly constructible from a single `const char*` so a one-channel converter's generated
/// constructor call is EXACTLY what it was before port lists existed -- `s_in(sim.dut(),
/// ports::s_in, chan, fmt, rate)` -- and from a braced list for the general case.  That is not a
/// convenience: the generator renders a group of one unbraced for precisely this reason, so adding
/// channels to the model never churned the harnesses of designs that have one.
struct AxisPortList {
    AxisPortList(const char* one) : names(1, one) {}                       // NOLINT: implicit
    AxisPortList(std::initializer_list<const char*> l) : names(l) {}       // NOLINT: implicit
    std::vector<const char*> names;
    std::size_t size() const { return names.size(); }
};

// ---------------------------------------------------------------------------
// ADC: RF blocks in, AXI-Stream master out.
// ---------------------------------------------------------------------------

class RfdcAdcMaster : public XsiSimObj {
public:
    /// *rf_in* is the RF-side edge; *ports* the DUT's AXIS slave ports this masters into, ONE PER
    /// CHANNEL and in channel order.  A bare `const char*` is a one-channel list.
    /// *words_per_cycle* is derived, never declared: `samp_rate / (samp_per_word * f_axis)`.
    RfdcAdcMaster(Dut& dut, AxisPortList ports, RfChannel& rf_in,
                  const RfdcFormat& fmt, double words_per_cycle)
        : d_(dut), rf_(rf_in), fmt_(fmt), rate_(words_per_cycle) {
        for (std::size_t i = 0; i < ports.names.size(); ++i) {
            const std::string p(ports.names[i]);
            Ch c;
            c.tvalid = dut.port((p + "_TVALID").c_str());
            c.tready = dut.port((p + "_TREADY").c_str());
            c.tdata  = dut.port((p + "_TDATA").c_str());
            c.tlast  = dut.port_opt((p + "_TLAST").c_str());
            ch_.push_back(c);
        }
        words_.resize(ch_.size());
        words_sent_ch.assign(ch_.size(), 0);
        dropped_ch.assign(ch_.size(), 0);
    }

    void sample() override {
        // A transfer happens iff VALID && READY -- per port, because one channel's consumer being
        // ready says nothing about another's.
        for (std::size_t i = 0; i < ch_.size(); ++i)
            ch_[i].beat = ch_[i].have && d_.get1(ch_[i].tready);
    }

    void update() override {
        ++cycle_;
        for (std::size_t i = 0; i < ch_.size(); ++i) {
            if (ch_[i].beat) {
                ++words_sent;
                ++words_sent_ch[i];
                ++ch_[i].wpos;
                ch_[i].have = false;
            }
        }

        if (rate_.tick()) {
            for (std::size_t i = 0; i < ch_.size(); ++i) {
                if (ch_[i].have) {
                    // The fabric did not take the previous word and the next one is already due.  A
                    // real converter does not wait -- that word is gone.
                    ++dropped;
                    ++dropped_ch[i];
                    last_drop_cycle = cycle_;
                    ch_[i].have = false;
                    ++ch_[i].wpos;
                }
            }
            // Every channel retires the word it was presenting in the same word period -- by a beat
            // or by a drop, never both and never neither -- so the positions stay equal and channel
            // 0's is the block's.  Refilling on one channel's exhaustion is refilling on all.
            if (ch_[0].wpos >= nwords_) refill_();
            if (ch_[0].wpos < nwords_) {
                for (std::size_t i = 0; i < ch_.size(); ++i) {
                    ch_[i].word = words_[i][ch_[i].wpos];
                    ch_[i].have = true;
                }
            }
        }
    }

    void drive() override {
        for (std::size_t i = 0; i < ch_.size(); ++i) {
            const Ch& c = ch_[i];
            d_.put1(c.tvalid, c.have ? 1u : 0u);
            if (c.have) d_.putW(c.tdata, c.word);
            if (c.tlast >= 0) d_.put1(c.tlast, (c.have && c.wpos + 1 == nwords_) ? 1u : 0u);
        }
    }

    /// Words the fabric accepted, summed over channels.
    std::uint64_t words_sent = 0;
    /// Words the fabric was not ready for, which the converter therefore **discarded**, summed over
    /// channels.  A real ADC drops; it cannot stall.  Expected to be zero in any design whose fabric
    /// keeps up.
    std::uint64_t dropped = 0;
    std::uint64_t last_drop_cycle = 0;
    /// The same two, per channel.  The sums answer "did this converter lose anything"; these answer
    /// "which port's consumer is the one that cannot keep up", which a sum cannot.
    std::vector<std::uint64_t> words_sent_ch;
    std::vector<std::uint64_t> dropped_ch;
    /// AXIS ports this model masters -- the tile's receive channel count.
    std::size_t n_ch() const { return ch_.size(); }
    /// The sample format it was built with.  Exposed so a testbench can ASSERT what reached the
    /// model rather than what the generator was asked for -- an I/Q rule that quietly defaulted
    /// would otherwise be invisible until the data came back wrong.
    const RfdcFormat& fmt() const { return fmt_; }

private:
    /// Pull one block off the edge and pack it, ROW PER PORT.
    ///
    /// The block arrives channel-major and flat, which is the same convention Python's `pack`
    /// returns: row `ch` is what port `ch` carries, so this is a stride, not a transpose.
    ///
    /// **`blk.data` counts COMPONENTS, not samples** — one double each, and two per sample when the
    /// format is interleaved I/Q, `(re, im)` adjacent.  That is the unit `RfBlockMsg` has always
    /// carried (it is a `vector<double>`) and the unit the RF bundle stores, so nothing converts
    /// here; what the I/Q case changes is only that `slots_per_word()` components make a word where
    /// `samp_per_word` samples did.  The two are the same number for real data.
    void refill_() {
        RfBlockMsg blk;
        if (!rf_.pop(blk)) return;                     // starved: the channel counts it, not us
        const std::size_t per_ch = blk.data.size() / ch_.size();
        nwords_ = per_ch / (std::size_t)fmt_.slots_per_word();
        for (std::size_t i = 0; i < ch_.size(); ++i) {
            words_[i].resize(nwords_);
            rfdc_pack(blk.data.data() + i * per_ch, (int)per_ch, fmt_, words_[i].data());
            ch_[i].wpos = 0;
        }
    }

    /// One AXIS port, and the beat it is presenting.
    struct Ch {
        int tvalid = -1, tready = -1, tdata = -1, tlast = -1;
        std::size_t   wpos = 0;
        std::uint64_t word = 0;
        bool          have = false;
        bool          beat = false;
    };

    Dut&       d_;
    RfChannel& rf_;
    RfdcFormat fmt_;
    RateTick   rate_;
    std::vector<Ch> ch_;
    std::vector<std::vector<std::uint64_t> > words_;
    /// Words one channel's row of the current block occupies.  ONE number, not one per channel: the
    /// rows of a block are the same length by construction.
    std::size_t   nwords_ = 0;
    std::uint64_t cycle_  = 0;
};

// ---------------------------------------------------------------------------
// DAC: AXI-Stream slave in, RF blocks out.
// ---------------------------------------------------------------------------

class RfdcDacSlave : public XsiSimObj {
public:
    /// *rf_out* is the RF-side edge; *ports* the DUT's AXIS master ports this answers, ONE PER
    /// CHANNEL and in channel order.  A bare `const char*` is a one-channel list.
    ///
    /// *blk_comps* is one block's worth in **components** — `n_ch * blksize` for real data and
    /// twice that for interleaved I/Q, because `RfBlockMsg::data` is a `vector<double>` and a
    /// complex sample is two of them.  It was named `blk_samples` while only real data existed,
    /// where the two are the same number; naming the unit is the whole of what I/Q changes here.
    RfdcDacSlave(Dut& dut, AxisPortList ports, RfChannel& rf_out,
                 const RfdcFormat& fmt, double words_per_cycle, std::size_t blk_comps)
        : d_(dut), rf_(rf_out), fmt_(fmt), rate_(words_per_cycle),
          blk_(blk_comps) {
        for (std::size_t i = 0; i < ports.names.size(); ++i) {
            const std::string p(ports.names[i]);
            Ch c;
            c.tvalid = dut.port((p + "_TVALID").c_str());
            c.tready = dut.port((p + "_TREADY").c_str());
            c.tdata  = dut.port((p + "_TDATA").c_str());
            ch_.push_back(c);
        }
        per_ch_ = blk_ / ch_.size();
        underrun_ch.assign(ch_.size(), 0);
        words_recv_ch.assign(ch_.size(), 0);
        // The BLOCK grid, derived from the word rate rather than declared: one CHANNEL's row of a
        // block is per_ch_/slots_per_word() words, so blocks/cycle = words/cycle / words-per-row.  A
        // DAC plays continuously and the grid is what it plays ON -- see emit_on_grid_().
        //
        // Per ROW, not per block: `words_per_cycle` is one port's rate and every port runs it
        // concurrently, so dividing the whole block's word count into a single-port rate would make
        // the grid n_ch times too slow.  It read that way while there was only ever one channel,
        // where the two are the same number.
        //
        // COMPONENTS over SLOTS, and the ratio is what makes I/Q free here: both terms double, so a
        // complex block plays on exactly the grid its real twin does at half the samp_per_word --
        // which is the arithmetic that keeps an I/Q design on the same 64-bit bus.
        blk_rate_ = RateTick(words_per_cycle * double(fmt.slots_per_word()) / double(per_ch_));
    }

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
        for (std::size_t i = 0; i < ch_.size(); ++i) {
            Ch& c = ch_[i];
            c.ready = (c.pending < cap_);
            c.beat = c.ready && d_.get1(c.tvalid);
            if (c.beat) c.word = d_.getW(c.tdata);
        }
    }

    void update() override {
        ++cycle_;
        for (std::size_t i = 0; i < ch_.size(); ++i) {
            Ch& c = ch_[i];
            if (c.beat) {
                ++words_recv;
                ++words_recv_ch[i];
                ++c.pending;
                // Through `rfdc_unpack`, not a hand-rolled unpack-then-dequantize loop.  That
                // function is the twin gated bit-exactly against Python (tests/build/
                // test_xsi_rfdc_samp.py) and it is where the I/Q slot order lives; re-spelling its
                // two steps here is how this model would come to disagree with the thing it is
                // supposed to be a twin of.  It writes slots_per_word() COMPONENTS, `(re, im)`
                // adjacent for I/Q, which is the unit `samples` has always been in.
                double comp[RFDC_MAX_SLOTS];
                rfdc_unpack(&c.word, 1, fmt_, comp);
                for (int k = 0; k < fmt_.slots_per_word(); ++k) c.samples.push_back(comp[k]);
            }
        }
        // A DAC plays on its GRID, not when its buffer happens to fill.
        if (blk_rate_.tick()) emit_on_grid_();
        if (rate_.tick()) {
            // One word period has elapsed: each tile consumes a word if it has one.
            for (std::size_t i = 0; i < ch_.size(); ++i) {
                if (ch_[i].pending > 0) {
                    --ch_[i].pending;
                } else {
                    // A word was due and the FIFO was empty.  There is no protocol signal for this;
                    // the analog output glitches and THIS COUNTER is the only evidence.
                    ++underrun;
                    ++underrun_ch[i];
                    last_underrun_cycle = cycle_;
                }
            }
        }
    }

    void drive() override {
        for (std::size_t i = 0; i < ch_.size(); ++i)
            d_.put1(ch_[i].tready, ch_[i].ready ? 1u : 0u);
    }

    /// Words taken off the fabric, summed over channels.
    std::uint64_t words_recv = 0;
    /// Cycles where a beat was due and none came — underflow — summed over channels.  Compare
    /// against the pipeline's **declared** startup transient, never against zero: a DAC fed through
    /// a pipeline must underrun until data reaches it.
    std::uint64_t underrun = 0;
    std::uint64_t last_underrun_cycle = 0;
    /// The same two, per channel: which port is starving, which a sum cannot say.
    std::vector<std::uint64_t> underrun_ch;
    std::vector<std::uint64_t> words_recv_ch;
    /// Blocks pushed onto the RF edge.
    std::uint64_t blocks_out = 0;
    /// AXIS ports this model answers — the tile's transmit channel count.
    std::size_t n_ch() const { return ch_.size(); }

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
    /// A block is ALL OR NOTHING across the channels, and that is physics rather than convenience:
    /// the rows of one block are the same instant on n_ch converters of one tile, so a block
    /// assembled from a full row and a short one would claim samples that were never played
    /// together.  If any channel is short, the whole block is the zero-fill.
    ///
    /// Everything here counts COMPONENTS, so the I/Q case needs no branch: a zero-filled complex
    /// block is `blk_` zero doubles, which reads back as complex zeros because the components come
    /// in `(re, im)` pairs.
    void emit_on_grid_() {
        RfBlockMsg blk;
        blk.idx = ++blocks_out;
        bool complete = true;
        for (std::size_t i = 0; i < ch_.size(); ++i)
            if (ch_[i].samples.size() < per_ch_) complete = false;
        if (complete) {
            blk.data.reserve(blk_);
            for (std::size_t i = 0; i < ch_.size(); ++i) {
                // Channel-major and flat: row i is what port i played, which is the layout the
                // Python side's `(n_ch, blksize)` block and `unpack` already agree on.
                std::vector<double>& s = ch_[i].samples;
                blk.data.insert(blk.data.end(), s.begin(), s.begin() + (std::ptrdiff_t)per_ch_);
                s.erase(s.begin(), s.begin() + (std::ptrdiff_t)per_ch_);
            }
        } else {
            blk.data.assign(blk_, 0.0);                // underflow: deterministic, visible, counted
            ++blocks_zero_filled;
            last_zero_fill_idx = blk.idx;
        }
        rf_.push(blk);                                 // full => the channel counts the drop
    }

    /// One AXIS port, the beat it is taking, and the samples it has not yet played.
    ///
    /// `pending` is words accepted but not yet consumed by the tile — the IP's AXIS input FIFO —
    /// and it is PER PORT because each port has its own.
    struct Ch {
        int tvalid = -1, tready = -1, tdata = -1;
        std::vector<double> samples;
        std::size_t   pending = 0;
        std::uint64_t word    = 0;
        bool          beat    = false;
        bool          ready   = true;
    };

    Dut&       d_;
    RfChannel& rf_;
    RfdcFormat fmt_;
    RateTick   rate_;
    RateTick   blk_rate_;
    /// COMPONENTS in one whole block, and in one channel's row of it — doubles, so two per sample
    /// under interleaved I/Q.  See the constructor: naming the unit is the whole of what I/Q changes
    /// in this model's accounting.
    std::size_t blk_;
    std::size_t per_ch_ = 0;
    std::vector<Ch> ch_;
    std::uint64_t cycle_ = 0;
    /// The depth of one port's input FIFO (`Ch::pending`).  **2, the AXI-Stream boundary depth
    /// Vitis gives a port**, not a tunable: a deeper
    /// one lets the fabric run further ahead of the converter and is exactly the fiction this model
    /// used to tell with an infinite one.  Small enough that the player is held to the grid within a
    /// couple of words, which is what makes the play pointer track real time.
    static constexpr std::size_t cap_ = 2;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_RFDC_H
