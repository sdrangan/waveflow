#ifndef WAVEFLOW_XSI_SW_H
#define WAVEFLOW_XSI_SW_H
// xsi_sw.h — software threads on the bus: the C++ twin of waveflow.sw (plans/host_runtime.md).
//
// A host program is written as threads that BLOCK -- qin.write(words), qresp.get(n), irq.wait(),
// compute(n) -- exactly as its Python twin is written with `yield from`.  Underneath, each thread is a
// fiber (xsi_fiber.h) and each endpoint is the state machine of xsi_mm_host.h: a blocking call starts
// the endpoint's work and parks the thread until it is done; the scheduler steps the endpoints and
// resumes the threads once per cycle.  So a host program contains no state machine and no BFM.
//
// What the user writes: a class deriving from the GENERATED <Host>_endpoints (which declares the
// host's endpoints, named as in Python and bound to their views, pins and the bus), overriding
// main().  See examples/mm_fir/mm_fir_host.h.
//
//   Python (waveflow.sw / mm_host.py)            C++ (this file)
//   yield from q.write(words)                    q.write(words)            queue in: wait for room, send
//   irq = yield from q.room_irq(n); push(words)  q.room_irq(n); q.push(..)  without waiting for room
//   w = yield from q.get(n) / get_array / ...    q.get(n)                   queue out: wait, take n words
//   irq = yield from q.data_irq(n); q.pop(n)     q.data_irq(n); q.pop(n)    without waiting for data
//   yield from cfg.write(words)                  cfg.write(words)          register bank: shadow + COMMIT
//   yield from status.read()                     status.read()             the latest status words
//   yield from rd.read(n, addr) / write          bus.read(addr, n) / write  plain memory
//   yield from irq.wait()                        irq.wait()
//   yield from self.compute(n)                   compute(n)
//   self.start(fn)                               start("name", [this] { fn(); })
#include <cstdint>
#include <cstdio>
#include <functional>
#include <string>
#include <utility>
#include <vector>

#include "xsi_bfm.h"
#include "xsi_fiber.h"
#include "xsi_mm_host.h"

namespace wfbfm {

/// The host end of an interrupt line, for a thread: wait() returns when the line is high (at once if
/// it already is).  Level-sensitive, as IrqIFSink.
class SwIrq {
public:
    SwIrq(SwScheduler& s, Dut& d, const char* port) : s_(s), pin_(d, port) {}
    void wait() { s_.wait_until([this] { return pin_.level; }); }
    bool level() const { return pin_.level; }
    IrqPin& pin() { return pin_; }

private:
    SwScheduler& s_;
    IrqPin pin_;
};

/// What every blocking endpoint shares: the scheduler, the bus, and a wait for one bus operation.
template <class Ep>
class SwEndpoint : public SwStepper {
public:
    SwEndpoint(SwScheduler& s, AxiMmMaster& m, const MmView& v, long poll)
        : s_(s), m_(m), ep_(m, v, poll) {}
    void step() override { ep_.step(); }
    long polls() const { return ep_.polls; }
    void write_trace(const std::string& dir) const { ep_.write_trace(dir); }

protected:
    void until_idle() { s_.wait_until([this] { return !ep_.busy(); }); }
    void until_op(size_t op) {
        if (op != MmEndpoint::NO_OP) s_.wait_until([this, op] { return m_.op(op).done(); });
    }
    SwScheduler& s_;
    AxiMmMaster& m_;
    Ep ep_;
};

/// A queue-in view.
class QueueWriter : public SwEndpoint<MmQueueWriter> {
public:
    QueueWriter(SwScheduler& s, AxiMmMaster& m, const MmView& v, long poll, SwIrq* irq = nullptr)
        : SwEndpoint(s, m, v, poll), irq_(irq) { if (irq) ep_.use_irq(irq->pin()); }
    /// One packet: wait for room (on the interrupt), then send it.
    void write(const std::vector<uint64_t>& w) { s_.uses(this); ep_.start(w); until_idle(); }
    /// Arm the room interrupt for n words; returns it, for wait().
    SwIrq& room_irq(uint32_t n) { s_.uses(this); until_op(ep_.arm_threshold(n)); return irq(); }
    /// One packet, without waiting for room.
    void push(const std::vector<uint64_t>& w) { s_.uses(this); until_op(ep_.push(w)); }

private:
    SwIrq& irq() {
        if (!irq_) { std::fprintf(stderr, "FATAL: QueueWriter on '%s' has no interrupt\n",
                                  ep_.view().name); std::exit(5); }
        return *irq_;
    }
    SwIrq* irq_;
};

/// A queue-out view.
class QueueReader : public SwEndpoint<MmQueueReader> {
public:
    QueueReader(SwScheduler& s, AxiMmMaster& m, const MmView& v, long poll, SwIrq* irq = nullptr)
        : SwEndpoint(s, m, v, poll), irq_(irq) { if (irq) ep_.use_irq(irq->pin()); }
    /// Exactly n words: wait for them (on the interrupt), take them.
    std::vector<uint64_t> get(uint32_t n) { s_.uses(this); ep_.start(n); until_idle(); return ep_.words; }
    /// Arm the data interrupt for n words; returns it, for wait().
    SwIrq& data_irq(uint32_t n) {
        s_.uses(this);
        until_op(ep_.arm_threshold(std::min<uint32_t>(n, ep_.view().depth)));
        return irq();
    }
    /// Exactly n words, without waiting for them.
    std::vector<uint64_t> pop(uint32_t n) {
        s_.uses(this);
        const std::vector<size_t> ops = ep_.pop(n);
        if (!ops.empty()) until_op(ops.back());
        return ep_.finish_pop(ops);
    }

private:
    SwIrq& irq() {
        if (!irq_) { std::fprintf(stderr, "FATAL: QueueReader on '%s' has no interrupt\n",
                                  ep_.view().name); std::exit(5); }
        return *irq_;
    }
    SwIrq* irq_;
};

/// A register bank's configuration: one write is one committed config.
class RegCfg : public SwEndpoint<MmRegBankCfg> {
public:
    using SwEndpoint::SwEndpoint;
    void write(const std::vector<uint64_t>& w) { s_.uses(this); ep_.start(w); until_idle(); }
};

/// A register bank's status: the latest status words.
class StatusReader : public SwEndpoint<MmStatusReader> {
public:
    using SwEndpoint::SwEndpoint;
    std::vector<uint64_t> read() { s_.uses(this); ep_.start(0); until_idle(); return ep_.words; }
};

/// Plain bus reads and writes of memory that is not a view -- recorded like an endpoint.
class BusRw {
public:
    BusRw(SwScheduler& s, AxiMmMaster& m) : s_(s), m_(m) {}
    std::vector<uint64_t> read(uint64_t addr, uint32_t n) {
        const size_t op = m_.read(addr, n, m_.cycle());
        s_.wait_until([this, op] { return m_.op(op).done(); });
        const std::vector<uint64_t> w = m_.op(op).rdata;
        record(w);
        return w;
    }
    void write(uint64_t addr, const std::vector<uint64_t>& w) {
        record(w);
        const size_t op = m_.write(addr, w);
        s_.wait_until([this, op] { return m_.op(op).done(); });
    }
    long polls() const { return 0; }
    void write_trace(const std::string& dir) const { BurstBundle::write(dir, words_, bounds_); }

private:
    void record(const std::vector<uint64_t>& w) {
        words_.insert(words_.end(), w.begin(), w.end());
        bounds_.push_back(words_.size());
    }
    SwScheduler& s_;
    AxiMmMaster& m_;
    std::vector<uint64_t> words_, bounds_;
};

// ---------------------------------------------------------------------------
// SwHostModel -- the XsiSimObj a host's C++ realization is.
// ---------------------------------------------------------------------------

/// The base of every generated <Host>_endpoints: owns the bus master, the interrupt pins, the
/// scheduler and the threads; loads the scenario; counts cycles; reports and dumps traces.  The user's
/// class overrides main() -- the first thread -- and may override report().
class SwHostModel : public XsiSimObj {
public:
    // DynParams -- SwHost's fields of the same names, assigned by the generated harness.
    std::string scenario;           ///< the scenario bundle ("" = none)
    std::string trace_dir;          ///< where each endpoint's trace is dumped ("" = not dumped)

    SwHostModel(Dut& d, const char* bus, int bytes_per_word)
        : bus_(d, bus, bytes_per_word, 0, /*overlap_rw=*/true) {}

    virtual void main() = 0;
    /// The run is over when every thread has finished.
    bool done() const { return started_ && sched_.all_done(); }

    void pre_sim() override {
        if (!scenario.empty()) {
            const std::vector<uint64_t> w = BurstBundle::read_words(scenario);
            size_t lo = 0;
            for (uint64_t hi : BurstBundle::read_bounds(scenario)) {
                scenario_.push_back(std::vector<uint64_t>(w.begin() + lo, w.begin() + hi));
                lo = hi;
            }
        }
        bus_.pre_sim();
        sched_.start("main", [this] { main(); });
        started_ = true;
    }
    void sample() override { for (SwIrq* p : pins_) p->pin().sample(); bus_.sample(); }
    void update() override {
        bus_.update();
        sched_.tick();
        ++cycle_;
        if (!done_cycle_ && done()) done_cycle_ = cycle_;
    }
    void drive() override { bus_.drive(); }
    void post_sim() override {
        bus_.post_sim();
        long polls = 0;
        for (auto& e : traced_) polls += e.polls();
        std::printf("DONE done=%d cycles=%ld polls=%ld nops=%zu\n", (int)done(),
                    done_cycle_ ? done_cycle_ : cycle_, polls, bus_.nops());
        report();
        for (size_t i = 0; i < bus_.nops(); ++i) {
            const AxiMmMaster::Op& o = bus_.op(i);
            std::printf("OP %c 0x%llx n=%zu s=%ld e=%ld\n", o.write ? 'W' : 'R',
                        (unsigned long long)o.addr, o.write ? o.wdata.size() : (size_t)o.nwords,
                        o.t_start, o.t_end);
        }
        if (!trace_dir.empty())
            for (auto& e : traced_) e.dump(trace_dir + "/" + e.name);
    }

protected:
    /// Start another thread; it first runs later in this cycle.
    void start(const std::string& name, std::function<void()> body) { sched_.start(name, std::move(body)); }
    /// n host clock cycles of software execution time.
    void compute(long n) { sched_.wait_ticks(n); }
    /// The scenario bundle, one burst per item, as the Python host's scenario_bursts().
    const std::vector<std::vector<uint64_t> >& scenario_bursts() const { return scenario_; }
    /// The current cycle.
    long now() const { return cycle_; }
    /// Extra report lines, printed after DONE.
    virtual void report() {}

    // Registration, done by the generated <Host>_endpoints constructor.
    void add_pin(SwIrq* p) { pins_.push_back(p); }
    template <class E>
    void traced(const char* name, E* e) {
        traced_.push_back(Traced{name, [e](const std::string& d) { e->write_trace(d); },
                                 [e] { return e->polls(); }});
    }

    AxiMmMaster bus_;
    SwScheduler sched_;

private:
    struct Traced {
        std::string name;
        std::function<void(const std::string&)> dump;
        std::function<long()> polls;
    };
    std::vector<SwIrq*> pins_;
    std::vector<Traced> traced_;
    std::vector<std::vector<uint64_t> > scenario_;
    bool started_ = false;
    long cycle_ = 0, done_cycle_ = 0;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_SW_H
