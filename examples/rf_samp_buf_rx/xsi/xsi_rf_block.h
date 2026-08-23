#ifndef WAVEFLOW_XSI_RF_BLOCK_H
#define WAVEFLOW_XSI_RF_BLOCK_H
// xsi_rf_block.h — the RF-domain behavioral edge and the file-backed nodes on either end of it.
//
// Split out of xsi_rfdc.h for the reason XsiSimObj was split out of xsi_bfm.h, and RfdcFormat out of
// the models: **an edge and its file-backed peers bind no RTL pins**, so none of this needs Vivado's
// xsi.h, and a channel that could only be exercised inside a full toolchain run would in practice
// not be exercised at all.  xsi_rfdc.h includes this; the converter models keep their pins.
//
// WHAT LIVES HERE
//
//   RfBlockMsg    one block crossing the edge
//   RfChannel     BlockChannel<RfBlockMsg> — the edge itself
//   RfFileSource  plays an RF bundle into the channel   (the XSI twin of Python's RfDataSource)
//   RfFileSink    captures blocks out of it to a bundle (…of RfDataSink)
//
// BUNDLE I/O LIVES ON THE NODES, NOT THE EDGE.  Settled in plans/behavioral_edges.md: a source reads
// `in_bundle` in pre_sim, a sink writes `out_bundle` in post_sim, and the channel carries no file
// machinery at all.  Same split Python already has (StreamDriver/StreamSink vs StreamIF.depth).
//
// THE BUNDLE FORMAT is the one waveflow/simulation/rf_tb.py writes: one burst per block, each word
// one float64 sample bit-reinterpreted into a uint64.  Reinterpreted, not converted — the words are
// IEEE-754 bit patterns, so a `memcpy` is the whole of the decoding and there is no rounding step to
// disagree about.
//
// REAL SAMPLES ONLY, and now CHECKED.  Python's bundle can also be complex (two float64 components
// per sample), and the two are indistinguishable as bytes — so `RfFileSource` reads the manifest's
// element kind and aborts on a complex one rather than playing it back as twice as many real
// samples.  See rf_require_real_bundle().
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "xsi_bundle.h"
#include "xsi_channel.h"

namespace wfbfm {

/// One RF block as it crosses a behavioral edge: `n_ch * blksize` real samples, channel-major, and
/// its 1-based index on the interface's absolute grid.  The index rides beside the data for the same
/// reason it does in Python — a receiver places samples on the grid without counting arrivals, so a
/// dropped block does not shift everything after it.
struct RfBlockMsg {
    std::uint64_t       idx = 0;
    std::vector<double> data;
};

using RfChannel = BlockChannel<RfBlockMsg>;

// --- the bit-reinterpretation the bundle format is defined in terms of ---------------------------

inline double rf_word_to_sample(std::uint64_t w) {
    double d;
    std::memcpy(&d, &w, sizeof d);
    return d;
}

inline std::uint64_t rf_sample_to_word(double d) {
    std::uint64_t w;
    std::memcpy(&w, &d, sizeof w);
    return w;
}

/// The manifest key and value naming an RF bundle's element kind — the C++ half of
/// waveflow.simulation.rf_tb's RF_ELEMENT_KEY / RF_ELEMENT_REAL.
static const char* const RF_ELEMENT_KEY  = "rf_element";
static const char* const RF_ELEMENT_REAL = "float64";

/// Abort unless *dir* is a REAL bundle.  An absent key means real — because THIS side still writes
/// bundles without it: `BurstBundle::write` emits four keys and `rf_element` is not one of them.
/// That default is a contract with a live writer, not support for old files (no bundle is committed
/// in this repo); removing it as legacy cruft breaks the RF XSI gates immediately.
///
/// **This refusal is the whole point of the manifest field.**  A complex bundle carries two float64
/// components per sample, so read as real it is not corrupt and not short: it is a block of twice as
/// many plausible samples, and every counter in the run would agree with itself. The models here
/// carry real samples only (`RfBlockMsg::data` is `std::vector<double>`), so the honest answer is to
/// stop rather than to produce one.  Lifting it is stage D's job, not a TODO here.
inline void rf_require_real_bundle(const std::string& dir) {
    const std::string kind = BurstBundle::read_meta_str(dir, RF_ELEMENT_KEY, RF_ELEMENT_REAL);
    if (kind != RF_ELEMENT_REAL) {
        std::fprintf(stderr,
            "FATAL: RF bundle '%s' declares %s=\"%s\"; these models carry REAL samples "
            "only.\nA complex bundle read as real is not an error -- it is twice as many "
            "plausible samples -- so this stops instead.\n",
            dir.c_str(), RF_ELEMENT_KEY, kind.c_str());
        std::exit(5);
    }
}

// ---------------------------------------------------------------------------
// RfFileSource — plays an RF bundle into the channel.
// ---------------------------------------------------------------------------

/// The XSI twin of Python's ``RfDataSource``.  Loads `in_bundle` in pre_sim and offers one block at
/// a time, **only when the channel has room**.
///
/// Why "when there is room" rather than on a rate: the consumer downstream (an `RfdcAdcMaster`)
/// pulls at its own derived word rate, so the block cadence follows from *that* and the channel's
/// depth bounds how far ahead this may run.  It is the same shape as Python's `put()` blocking on a
/// full buffer — bounded lookahead, not free-running into memory.  A source that pushed
/// unconditionally would simply drop, and the drop counter would measure this model rather than the
/// design.
class RfFileSource : public XsiSimObj {
public:
    /// *blk_samples* is `n_ch * blksize` — one block's worth, the unit the edge moves.
    RfFileSource(RfChannel& out, std::size_t blk_samples)
        : ch_(out), blk_(blk_samples) {}

    //: Set by the generated harness from the Python DynParam; loaded in pre_sim, like every other
    //: file-backed participant.
    std::string in_bundle;

    void pre_sim() override {
        if (in_bundle.empty()) return;
        rf_require_real_bundle(in_bundle);
        const std::vector<std::uint64_t> w = BurstBundle::read_words(in_bundle);
        samples_.resize(w.size());
        for (std::size_t i = 0; i < w.size(); ++i) samples_[i] = rf_word_to_sample(w[i]);
        pos_ = 0;
    }

    void update() override {
        if (ch_.full()) return;                      // bounded lookahead
        if (pos_ + blk_ > samples_.size()) return;   // every whole block has been offered
        RfBlockMsg blk;
        blk.idx = ++blocks_out;
        blk.data.assign(samples_.begin() + (std::ptrdiff_t)pos_,
                        samples_.begin() + (std::ptrdiff_t)(pos_ + blk_));
        ch_.push(blk);                               // cannot drop: full() was just checked
        pos_ += blk_;
    }

    /// Blocks offered to the edge.
    std::uint64_t blocks_out = 0;
    std::size_t   samples() const { return samples_.size(); }

private:
    RfChannel&          ch_;
    std::size_t         blk_;
    std::vector<double> samples_;
    std::size_t         pos_ = 0;
};

// ---------------------------------------------------------------------------
// RfFileSink — captures blocks out of the channel to a bundle.
// ---------------------------------------------------------------------------

/// The XSI twin of Python's ``RfDataSink``.  Always drains — a sink that stalled would make the
/// channel's drop counter measure the sink rather than the design — and dumps one burst per block
/// in post_sim, in the same format `RfFileSource` reads.  So a loopback is a **file-to-file byte
/// comparison**, exactly as it is in pysim.
class RfFileSink : public XsiSimObj {
public:
    explicit RfFileSink(RfChannel& in) : ch_(in) {}

    //: Emitted by the harness from the Python DynParam.  Empty writes nothing.
    std::string out_bundle;

    void update() override {
        ++cycle_;                                    // 1-based: the cycle now executing
        RfBlockMsg blk;
        while (ch_.pop(blk)) {                       // drain fully: never the bottleneck
            idx_.push_back(blk.idx);
            for (double s : blk.data) words_.push_back(rf_sample_to_word(s));
            ++blocks_in;
            last_block_cycle = cycle_;
            if (bounds_.empty()) bounds_.push_back(blk.data.size());
            else bounds_.push_back(bounds_.back() + blk.data.size());
        }
    }

    void post_sim() override {
        if (!out_bundle.empty()) BurstBundle::write(out_bundle, words_, bounds_);
    }

    /// Blocks taken off the edge.
    std::uint64_t blocks_in = 0;
    /// The cycle the LAST block landed — time-to-last-completion, which is a result.  Distinct from
    /// the harness's loop bound, which is a testbench constant; conflating the two is how three
    /// hand-written testbenches once reported a drain tail as latency.
    std::uint64_t last_block_cycle = 0;
    /// The grid index of each captured block — a dropped block leaves a GAP here, which is what
    /// makes loss visible in the data and not only in a counter.
    const std::vector<std::uint64_t>& indices() const { return idx_; }
    const std::vector<std::uint64_t>& words()   const { return words_; }

private:
    RfChannel&                 ch_;
    std::vector<std::uint64_t> words_, bounds_, idx_;
    std::uint64_t              cycle_ = 0;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_RF_BLOCK_H
