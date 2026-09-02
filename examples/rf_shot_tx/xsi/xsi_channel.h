#ifndef WAVEFLOW_XSI_CHANNEL_H
#define WAVEFLOW_XSI_CHANNEL_H
// xsi_channel.h — the C++ realization of a **behavioral edge**: a model↔model channel with no RTL
// between it and either peer.  The edge-side counterpart of xsi_bfm.h's node models
// (plans/behavioral_edges.md S2).
//
// Every model in xsi_bfm.h binds RTL *pins*.  This one binds two *models*.  That is the whole of the
// difference, and it is why a channel is a separate header and a separate registry rather than a row
// in BFM_DUALS: a dual answers "what must the testbench present against a DUT port of this kind?",
// and a model↔model edge has no DUT port kind to key on.
//
// ---------------------------------------------------------------------------
// The phase discipline — the load-bearing claim
// ---------------------------------------------------------------------------
//
// A harness drives one participant list through five phases:
//
//     pre_sim();
//     for each cycle: clock_low(); sample(); clock_high(); update(); drive();
//     post_sim();
//
// A direct call between two models would make the result depend on **participant list order** — if A
// hands B a value inside a phase, whether B sees it this cycle or next depends on which of them the
// harness happens to visit first.  That is a generator-ordering detail deciding a functional result,
// which is exactly the class of bug a testbench must not have.
//
// So a channel **stages**.  `push()` puts an item in a staging area; the channel's own `sample()`
// commits the staging area into the visible queue.  The channel is declared and registered **before
// both of its peers**, so its `sample()` runs first in every sweep.  The consequence:
//
//     an item pushed at any point in cycle c becomes visible at the START of cycle c+1,
//     and never within cycle c — whatever order the peers appear in.
//
// The producer therefore cannot race the consumer, and the consumer cannot race the producer.  It is
// the same reason `sample()` and `update()` are split in the pin-level models: a transfer is decided
// from values observed *before* the edge and applied *after* it.
//
// One consequence to state rather than discover: each behavioral hop costs **one cycle** of latency
// that the pysim graph does not have.  An N-hop chain adds N cycles.  Real, and by design.
//
// ---------------------------------------------------------------------------
// What an edge may own
// ---------------------------------------------------------------------------
//
// Transport: rate, buffering, ordering, loss accounting.  Not signal processing.  Everything here is
// hand-written twice — once in Python, once in C++ — and nothing checks that the two agree, so the
// bar is "obviously the same in ten lines".  A bounded deque plus two counters clears it; a filter
// does not.  The operational form of the rule: **if the edge can only record a quantity and never
// apply it, it does not belong on the edge.**
//
// Only the lifecycle base and the standard library: an edge model binds two other models, never RTL
// pins, so it needs none of Vivado's headers.  That is what lets this be gated by a plain g++ compile
// and run (tests/build/test_xsi_channel.py) instead of only by a full toolchain run.
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include "xsi_simobj.h"

namespace wfbfm {

// ---------------------------------------------------------------------------
// BlockChannel — a bounded, one-producer/one-consumer queue between two models.
// ---------------------------------------------------------------------------

/// A depth-bounded channel carrying items of type `T` from one peer model to another.
///
/// Counters, not silent behaviour.  A full channel **drops** and a starved one **reports empty**,
/// and each is a number: an overrun or an underrun that nobody can read is the "deadlock looks like
/// success" failure in a new costume.  These are the tuple the pysim/XSI equivalence gate compares
/// (S4, not built).
template <typename T>
class BlockChannel : public XsiSimObj {
public:
    /// *depth* is the channel's capacity in items — a **physical** property of the edge, single-
    /// sourced from the Python interface exactly as `StreamIF.depth` already is.
    explicit BlockChannel(std::size_t depth) : depth_(depth) {
        if (depth_ == 0) {
            std::fprintf(stderr, "FATAL: BlockChannel depth must be >= 1\n");
            std::exit(3);
        }
    }

    /// Offer one item.  `false` means the channel was full and the item was **dropped** (counted).
    ///
    /// Never blocks, because nothing in this loop can: a model that waits would stall the single
    /// harness thread.  Backpressure, where an edge has it, is the *producer model's* business —
    /// it asks `full()` and holds its own item back.
    bool push(const T& v) {
        if (size() >= depth_) { ++dropped; return false; }
        staged_.push_back(v);
        return true;
    }

    /// Take the oldest **committed** item into *out*.  `false` means the channel was empty and the
    /// read **starved** (counted).  An item pushed this cycle is deliberately not available.
    bool pop(T& out) {
        if (q_.empty()) { ++starved; return false; }
        out = q_.front();
        q_.pop_front();
        ++transferred;
        return true;
    }

    /// Look at the oldest committed item without consuming it.  Does **not** count a starve: a peek
    /// is a question, and only a failed *read* is a loss.
    bool peek(T& out) const {
        if (q_.empty()) return false;
        out = q_.front();
        return true;
    }

    /// Commit everything staged since the last commit.  Runs FIRST in each cycle's sweep because the
    /// channel is registered before both peers — see the phase-discipline note in this file's header.
    void sample() override {
        while (!staged_.empty()) { q_.push_back(staged_.front()); staged_.pop_front(); }
    }

    /// Items occupying the channel: committed **plus** staged.  Staged items count, or a producer
    /// could push past `depth` within one cycle and the bound would only apply between cycles.
    std::size_t size()  const { return q_.size() + staged_.size(); }
    std::size_t ready() const { return q_.size(); }        ///< committed, i.e. readable now
    std::size_t depth() const { return depth_; }
    bool full()  const { return size() >= depth_; }
    bool empty() const { return q_.empty(); }

    //: The contract.  `transferred` counts successful `pop`s, `dropped` counts `push`es refused
    //: because the channel was full, `starved` counts `pop`s that found it empty.  Public because
    //: they are the thing a testbench asserts on, exactly like `AxisSlave::count()`.
    long transferred = 0;
    long dropped = 0;
    long starved = 0;

private:
    std::size_t depth_;
    std::deque<T> q_;         ///< committed: visible to `pop`
    std::deque<T> staged_;    ///< pushed this cycle: visible from the next `sample()`
};

// ---------------------------------------------------------------------------
// RateTick — the fractional-credit accumulator.
// ---------------------------------------------------------------------------

/// Turns a **derived, fractional** rate ratio into a per-cycle boolean: "is one edge-tick due?"
///
/// A behavioral edge typically runs on its own clock (a sample rate) while the harness steps on the
/// fabric clock, and the conversion between them is a ratio, not a count — `f_edge / f_axis` is
/// `256e6 / 300e6 = 0.853` on a plausible RFSoC configuration, and no integer expresses that.
///
/// **Derived, never declared.**  Both frequencies already exist elsewhere (the edge's clock, the
/// harness's), so a `ticks_per_cycle` parameter would be a third statement of a quantity the design
/// already fixes twice — the same single-source rule that keeps `samp_rate` off the converter.
///
/// The ratio must be in `[0, 1]`.  Above 1 means more than one tick is due per cycle, which this
/// cannot express and would silently *lose* ticks under the `if` below — so it aborts instead.  That
/// is not a limitation being papered over: a ratio above 1 is a design error (the port cannot carry
/// the rate), and `plans/adc_model.md` requires it be caught loudly on the Python side too.
class RateTick {
public:
    explicit RateTick(double ratio = 0.0) : ratio_(ratio) {
        if (!(ratio_ >= 0.0 && ratio_ <= 1.0)) {
            std::fprintf(stderr,
                "FATAL: RateTick ratio %g is outside [0, 1]. Above 1 means more than one edge-tick "
                "is due per fabric cycle, which a one-bit answer cannot carry -- ticks would be "
                "silently lost. Raise the fabric clock or lower the edge rate.\n", ratio_);
            std::exit(3);
        }
    }

    /// Advance one fabric cycle; `true` when an edge-tick falls in it.
    bool tick() {
        credit_ += ratio_;
        if (credit_ >= 1.0) { credit_ -= 1.0; return true; }
        return false;
    }

    double ratio()  const { return ratio_; }
    double credit() const { return credit_; }

private:
    double ratio_;
    double credit_ = 0.0;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_CHANNEL_H
