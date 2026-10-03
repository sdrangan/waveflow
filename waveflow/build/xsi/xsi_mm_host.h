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
// **Blocking by polling, never by stalling the bus** -- the reason is in mm_host.py's docstring, and
// the rules (when to poll, when to sleep, how long a burst may be) are the same here, line for line,
// so a pysim host and an XSI host issue the same sequence of bus operations.
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

    AxiMmMaster& m_;
    const MmView& v_;
    long poll_;
    size_t op_ = 0;
    int state_ = 0;
};

/// Queue in: one start() is one packet.
class MmQueueWriter : public MmEndpoint {
public:
    MmQueueWriter(AxiMmMaster& m, const MmView& v, long poll)
        : MmEndpoint(m, v, poll, MmKind::QueueIn, "MmQueueWriter") {}

    void start(std::vector<uint64_t> words) {
        pkt_ = std::move(words); sent_ = 0;
        read(v_.base, 1, 0); state_ = POLL;
    }
    void step() {
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
    enum { POLL = 1, WRITE = 2 };
    std::vector<uint64_t> pkt_;
    uint64_t sent_ = 0;
};

/// Queue out: one start(n) takes exactly n words.  Unframed -- the caller names the count.
class MmQueueReader : public MmEndpoint {
public:
    MmQueueReader(AxiMmMaster& m, const MmView& v, long poll)
        : MmEndpoint(m, v, poll, MmKind::QueueOut, "MmQueueReader") {}

    void start(uint32_t n) {
        want_ = n; words.clear();
        read(v_.status_addr(), 1, 0); state_ = POLL;
    }
    void step() {
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
    enum { POLL = 1, POP = 2 };
    uint32_t want_ = 0;
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
