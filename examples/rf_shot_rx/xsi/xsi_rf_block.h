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
// one float64 COMPONENT bit-reinterpreted into a uint64.  Reinterpreted, not converted — the words
// are IEEE-754 bit patterns, so a `memcpy` is the whole of the decoding and there is no rounding
// step to disagree about.
//
// REAL OR COMPLEX, and the bundle SAYS WHICH.  A real sample is one component; a complex one is two,
// `(re, im)` adjacent — indistinguishable as bytes, which is why the kind is a manifest field and
// why both models take it as an argument and CHECK it rather than reading it off the file.  See
// rf_require_bundle_kind().
//
// Nothing between those two ends interprets a pair: `RfBlockMsg::data` is a `vector<double>`, the
// channel moves doubles, and the source slices them.  That is why complex support costs one
// constructor argument at each end and no branch in between.
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

/// The manifest key and its two values — the C++ half of waveflow.simulation.rf_tb's
/// RF_ELEMENT_KEY / RF_ELEMENT_REAL / RF_ELEMENT_COMPLEX.
static const char* const RF_ELEMENT_KEY     = "rf_element";
static const char* const RF_ELEMENT_REAL    = "float64";
static const char* const RF_ELEMENT_COMPLEX = "complex128";

/// The value a bundle of *complex_samp* blocks declares.
inline const char* rf_element(int complex_samp) {
    return complex_samp ? RF_ELEMENT_COMPLEX : RF_ELEMENT_REAL;
}

/// Abort unless *dir* declares the kind the caller expects — and **a bundle that does not say is an
/// error**, not a default.
///
/// **Checked, not obeyed**, which is the same direction Python's `read_rf_bundle` takes and for the
/// same reason: the two kinds differ only in how many doubles a sample takes, so a reader that let
/// the file decide would silently reframe every block.  The caller knows what its edge carries; the
/// file says what it holds; a disagreement is an error.
///
/// The absent case *was* a default meaning real, and the reason is worth keeping: `BurstBundle::write`
/// emitted four keys and `rf_element` was not among them, so every bundle `RfFileSink` produced
/// lacked it and Python read those back in the gates.  That made "absent means real" a contract with
/// a live writer rather than support for old files — no bundle is committed in this repo, so there
/// was never any legacy data.  Once the writer emitted the key the default had nothing left to
/// serve, and keeping it would have meant a bundle from some *third* writer being misread in silence.
inline void rf_require_bundle_kind(const std::string& dir, int complex_samp) {
    const std::string want = rf_element(complex_samp);
    const std::string kind = BurstBundle::read_meta_str(dir, RF_ELEMENT_KEY);
    if (kind.empty()) {
        std::fprintf(stderr,
            "FATAL: RF bundle '%s' has no %s in its meta.json, so it does not say whether its "
            "samples are real or complex.\nThe two are the same bytes at different lengths, so "
            "there is nothing safe to assume. Rewrite it with a writer that declares the kind "
            "(waveflow.simulation.rf_tb.write_rf_bundle, or RfFileSink).\n",
            dir.c_str(), RF_ELEMENT_KEY);
        std::exit(5);
    }
    if (kind != want) {
        std::fprintf(stderr,
            "FATAL: RF bundle '%s' declares %s=\"%s\" but this model was built for \"%s\".\n"
            "A complex bundle read as real is not an error -- it is twice as many plausible "
            "samples -- so this stops instead.\n",
            dir.c_str(), RF_ELEMENT_KEY, kind.c_str(), want.c_str());
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
    /// *blk_comps* is one block's worth in **components** — `n_ch * blksize` doubles for real
    /// blocks, twice that for complex ones, because `RfBlockMsg::data` is a `vector<double>` and a
    /// complex sample is two of them.  *complex_samp* is what this source's EDGE carries, and it is
    /// checked against the bundle rather than read from it (see rf_require_bundle_kind).
    ///
    /// Slicing is the same either way: this model moves doubles and never interprets a pair, which
    /// is why interleaved I/Q costs it exactly one argument.
    RfFileSource(RfChannel& out, std::size_t blk_comps, int complex_samp = 0)
        : ch_(out), blk_(blk_comps), complex_(complex_samp) {}

    //: Set by the generated harness from the Python DynParam; loaded in pre_sim, like every other
    //: file-backed participant.
    std::string in_bundle;

    void pre_sim() override {
        if (in_bundle.empty()) return;
        rf_require_bundle_kind(in_bundle, complex_);
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
    int                 complex_ = 0;
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
    /// *complex_samp* is what this sink's EDGE carries, and it is what the written bundle DECLARES.
    /// A sink cannot infer it: a captured block is doubles either way, and an empty capture has
    /// nothing to infer from at all — the same reason Python's `RfDataSink` states it rather than
    /// letting `write_rf_bundle` guess.
    explicit RfFileSink(RfChannel& in, int complex_samp = 0)
        : ch_(in), complex_(complex_samp) {}

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
        // The kind is DECLARED, not left to the reader -- and saying it is what lets
        // rf_require_bundle_kind() and Python's read_rf_bundle() treat a silent bundle as an error
        // rather than a guess.
        if (!out_bundle.empty())
            BurstBundle::write(out_bundle, words_, bounds_, RF_ELEMENT_KEY, rf_element(complex_));
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
    int                        complex_ = 0;
    std::vector<std::uint64_t> words_, bounds_, idx_;
    std::uint64_t              cycle_ = 0;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_RF_BLOCK_H
