#ifndef WAVEFLOW_XSI_MM_HOST_H
#define WAVEFLOW_XSI_MM_HOST_H
// xsi_mm_host.h — a bus master's endpoints onto a memory-mapped slave adaptor, for an XSI testbench.
//
// The C++ twin of waveflow/hw/mm_host.py (plans/mm_adaptor_host_endpoints.md, Stage 2).  A host
// program does not write addresses: it holds one endpoint per view, built from an MmView the Python
// address map generated (MemSlaveMap.to_cpp_header), and each endpoint turns a call into bus reads
// and writes on a shared AxiMmMaster:
//
//   view            endpoint          a call does, on the bus
//   queue in        MmQueueWriter     start(words): poll the free slots until the packet fits, then
//                                     write [len | words]  (a packet longer than the queue: pieces)
//   queue out       MmQueueReader     start(n): poll the ready count, pop, repeat until exactly n
//   register bank   MmRegBankCfg      start(words): write the shadow, then COMMIT
//                   MmStatusReader    start(): read the latest status message
//
// **Blocking on the view's interrupt** (plans/mm_irq.md): give a queue endpoint the IrqPin of its
// view's `irq` output (use_irq) and it never reads a count -- it sets the view's threshold, waits for
// the pin, and moves the words.  This is how the examples wait.  Without an IrqPin the endpoints fall
// back to polling, never stalling the bus.  The rules are mm_host.py's, line for line, so a pysim host
// and an XSI host issue the same sequence of bus operations.
//
// The cycle model: an XSI participant cannot block, so an endpoint is a small state machine.  The
// owning program calls start(...) once, then step() once per cycle from its own update(), until
// busy() is false.  Every bus operation goes through AxiMmMaster's queue, which serves them in order,
// one at a time -- so several endpoints (and several programs) can share one master.  A poll that
// finds too little re-issues `poll` cycles later (the pysim `sleep(poll_cycles)`); every other next
// operation is issued at once.
//
// Header-only and standard-library only beyond xsi_bfm.h.
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "xsi_bfm.h"

namespace wfbfm {

enum class MmKind { QueueIn, QueueOut, RegBank, Bram };

/// One view as a bus master sees it -- the C++ form of waveflow.hw.mm_host.ViewEntry.  The offsets
/// inside the window are written here once, as there.
struct MmView {
    const char* name;
    MmKind kind;
    uint64_t base;          ///< absolute bus address of the view's window
    uint32_t window;        ///< window size in bytes
    uint32_t bpw;           ///< bytes per bus word
    uint32_t depth;         ///< queue depth in words (queues)
    uint32_t ncfg;          ///< config words (register bank)
    uint32_t nstat;         ///< status words (register bank)
    uint32_t nelem;         ///< memory words (BRAM window)

    uint64_t commit_addr() const { return base + window / 2; }
    uint64_t status_addr() const {
        return kind == MmKind::QueueOut ? base + window / 2 : base + 3ull * window / 4;
    }
    uint32_t max_burst() const {
        const uint32_t span = kind == MmKind::QueueOut ? window / 2 : window;
        return std::min<uint32_t>(256, span / bpw);
    }
};

/// One interrupt pin of the DUT, sampled every cycle -- the C++ end of an IrqIF.  Put it in the
/// participant list with the endpoints that wait on it.
class IrqPin : public XsiSimObj {
public:
    IrqPin(Dut& d, const char* port) : d_(d), p_(d.port(port)) {}
    void sample() override { level = d_.get1(p_) != 0; }
    bool level = false;

private:
    Dut& d_;
    int p_;
};

/// What every endpoint shares: the master, the view, the poll period, and one operation in flight.
class MmEndpoint {
public:
    MmEndpoint(AxiMmMaster& m, const MmView& v, long poll, MmKind want, const char* who)
        : m_(m), v_(v), poll_(poll) {
        if (v.kind != want) {
            std::fprintf(stderr, "FATAL: %s on view '%s' of the wrong kind\n", who, v.name);
            std::exit(5);
        }
    }
    bool busy() const { return state_ != 0; }
    /// Wait on this interrupt pin instead of polling (plans/mm_irq.md).
    void use_irq(const IrqPin& pin) { irq_ = &pin; }
    /// Bus operations this endpoint issued that were polls (reads that found too little).
    long polls = 0;

protected:
    void read(uint64_t a, uint32_t n, long delay) { op_ = m_.read(a, n, m_.cycle() + delay); }
    void write(uint64_t a, std::vector<uint64_t> w) { op_ = m_.write(a, std::move(w)); }
    bool op_done() const { return m_.op(op_).done(); }
    const std::vector<uint64_t>& rdata() const { return m_.op(op_).rdata; }
    /// Queue *words* at *addr* in bursts of at most max_burst(), each restarting at *addr* -- every
    /// view but the register bank's shadow takes "the next word" anywhere in its window.  The bursts
    /// are queued together; op_ is the last, so op_done() means all of them are done.
    void write_bursts(uint64_t addr, const std::vector<uint64_t>& w) {
        const size_t step = v_.max_burst();
        for (size_t i = 0; i < w.size(); i += step)
            write(addr, std::vector<uint64_t>(w.begin() + i, w.begin() + (std::min)(w.size(), i + step)));
    }

    /// Write the view's interrupt threshold (upper half of the window) if it differs from the last
    /// one written.  Returns true when a write was issued.
    bool set_threshold(uint64_t value) {
        if (thr_ == value) return false;
        write(v_.base + v_.window / 2, {value});
        thr_ = value;
        return true;
    }

    AxiMmMaster& m_;
    const MmView& v_;
    long poll_;
    size_t op_ = 0;
    int state_ = 0;
    const IrqPin* irq_ = nullptr;
    uint64_t thr_ = 0;          ///< the threshold last written (the view resets it to 0)
};

/// Queue in: one start() is one packet.
class MmQueueWriter : public MmEndpoint {
public:
    MmQueueWriter(AxiMmMaster& m, const MmView& v, long poll)
        : MmEndpoint(m, v, poll, MmKind::QueueIn, "MmQueueWriter") {}

    void start(std::vector<uint64_t> words) {
        pkt_ = std::move(words); sent_ = 0;
        if (irq_) {
            const uint64_t n = pkt_.size();
            if (n > v_.depth) {
                std::fprintf(stderr, "FATAL: MmQueueWriter on '%s': a %llu-word packet does not fit "
                             "the queue in interrupt mode\n", v_.name, (unsigned long long)n);
                std::exit(5);
            }
            if (room_ >= n) { send(); return; }
            // Not enough known room: wait for vacancy >= max(n, depth/2), normally one threshold
            // write for the whole run.
            state_ = set_threshold(std::max<uint64_t>(n, v_.depth / 2)) ? THR : WAIT;
            return;
        }
        read(v_.base, 1, 0); state_ = POLL;
    }
    void step() {
        if (irq_) {
            if (state_ == THR && op_done()) state_ = WAIT;
            if (state_ == WAIT && irq_->level) { room_ = thr_; send(); return; }
            if (state_ == WRITE && op_done()) state_ = 0;
            return;
        }
        if (!state_ || !op_done()) return;
        const uint64_t n = pkt_.size();
        if (state_ == POLL) {
            const uint64_t vac = rdata()[0];
            if (n <= v_.depth) {                       // fits: wait for room for ALL of it
                if (vac < n) { ++polls; read(v_.base, 1, poll_); return; }
                std::vector<uint64_t> w; w.reserve(n + 1);
                w.push_back(n); w.insert(w.end(), pkt_.begin(), pkt_.end());
                write_bursts(v_.base, w); sent_ = n; state_ = WRITE; return;
            }
            const uint64_t room = std::min<uint64_t>(vac, n - sent_);
            if (room == 0) { ++polls; read(v_.base, 1, poll_); return; }
            std::vector<uint64_t> w;
            if (sent_ == 0) w.push_back(n);
            w.insert(w.end(), pkt_.begin() + sent_, pkt_.begin() + sent_ + room);
            write_bursts(v_.base, w); sent_ += room; state_ = WRITE; return;
        }
        if (sent_ < n) { read(v_.base, 1, 0); state_ = POLL; }   // a long packet's next piece
        else state_ = 0;
    }

private:
    enum { POLL = 1, WRITE = 2, THR = 3, WAIT = 4 };
    void send() {
        const uint64_t n = pkt_.size();
        std::vector<uint64_t> w; w.reserve(n + 1);
        w.push_back(n); w.insert(w.end(), pkt_.begin(), pkt_.end());
        write_bursts(v_.base, w);
        room_ -= n; sent_ = n; state_ = WRITE;
    }
    std::vector<uint64_t> pkt_;
    uint64_t sent_ = 0;
    uint64_t room_ = 0;         ///< interrupt mode: a lower bound on the queue's free slots
};

/// Queue out: one start(n) takes exactly n words.  Unframed -- the caller names the count.
class MmQueueReader : public MmEndpoint {
public:
    MmQueueReader(AxiMmMaster& m, const MmView& v, long poll)
        : MmEndpoint(m, v, poll, MmKind::QueueOut, "MmQueueReader") {}

    void start(uint32_t n) {
        want_ = n; words.clear();
        if (irq_) { chunk(); return; }
        read(v_.status_addr(), 1, 0); state_ = POLL;
    }
    void step() {
        if (irq_) {
            if (state_ == THR && op_done()) state_ = WAIT;
            if (state_ == WAIT && irq_->level) { left_ = k_; pop_next(); return; }
            if (state_ == POP && op_done()) {
                words.insert(words.end(), rdata().begin(), rdata().end());
                if (left_ > 0) pop_next();
                else if (words.size() < want_) chunk();
                else state_ = 0;
            }
            return;
        }
        if (!state_ || !op_done()) return;
        if (state_ == POLL) {
            const uint64_t occ = rdata()[0];
            if (occ == 0) { ++polls; read(v_.status_addr(), 1, poll_); return; }
            const uint64_t k = std::min<uint64_t>({occ, (uint64_t)(want_ - words.size()),
                                                   (uint64_t)v_.max_burst()});
            read(v_.base, (uint32_t)k, 0); state_ = POP; return;
        }
        words.insert(words.end(), rdata().begin(), rdata().end());
        if (words.size() < want_) { read(v_.status_addr(), 1, 0); state_ = POLL; }
        else state_ = 0;
    }
    /// The words taken by the last start(n), once busy() is false.
    std::vector<uint64_t> words;

private:
    enum { POLL = 1, POP = 2, THR = 3, WAIT = 4 };
    /// Interrupt mode: the next chunk -- set the threshold to the words still wanted (up to the
    /// depth), then wait for the pin.  The pin high MEANS they are there.
    void chunk() {
        k_ = std::min<uint32_t>(want_ - (uint32_t)words.size(), v_.depth);
        state_ = set_threshold(k_) ? THR : WAIT;
    }
    void pop_next() {
        const uint32_t m = std::min<uint32_t>(left_, v_.max_burst());
        read(v_.base, m, 0); left_ -= m; state_ = POP;
    }
    uint32_t want_ = 0, k_ = 0, left_ = 0;
};

/// Register bank, config: one start() is one config message (the shadow, then COMMIT).  As in pysim,
/// the COMMIT can stall the bus if the kernel has not taken the previous config.
class MmRegBankCfg : public MmEndpoint {
public:
    MmRegBankCfg(AxiMmMaster& m, const MmView& v, long poll)
        : MmEndpoint(m, v, poll, MmKind::RegBank, "MmRegBankCfg") {}

    void start(const std::vector<uint64_t>& words) {
        if (words.size() != v_.ncfg) {
            std::fprintf(stderr, "FATAL: config for '%s' is %u words, got %zu\n", v_.name, v_.ncfg,
                         words.size());
            std::exit(5);
        }
        const size_t step = v_.max_burst();
        for (size_t i = 0; i < words.size(); i += step)
            write(v_.base + i * v_.bpw, std::vector<uint64_t>(
                words.begin() + i, words.begin() + (std::min)(words.size(), i + step)));
        write(v_.commit_addr(), {1});
        state_ = 1;
    }
    void step() { if (state_ && op_done()) state_ = 0; }
};

/// Register bank, status: one start() reads the latest complete status message.
class MmStatusReader : public MmEndpoint {
public:
    MmStatusReader(AxiMmMaster& m, const MmView& v, long poll)
        : MmEndpoint(m, v, poll, MmKind::RegBank, "MmStatusReader") {}

    /// *delay* holds the read back that many cycles -- a host re-checking the status after a sleep.
    void start(long delay = 0) { read(v_.status_addr(), v_.nstat, delay); state_ = 1; }
    void step() {
        if (state_ && op_done()) { words = rdata(); state_ = 0; }
    }
    /// The status words read by the last start(), once busy() is false.
    std::vector<uint64_t> words;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_MM_HOST_H
