#ifndef EXAMPLES_MARKOV_HOST_H
#define EXAMPLES_MARKOV_HOST_H
// markov_host.h -- the C++ realization of examples/markov/markov.py::MarkovHost (its bfm_model()).
//
// The same host program on the C++ endpoints of xsi_mm_host.h: a Writer that sends each job's command
// on qcmd (room interrupt) while fewer than max_in_flight jobs are out, and a Reader that takes each
// response on qresp (data interrupt), then reads that job's x from the shared memory.  Nothing polls.
// Both share one AxiMmMaster, which keeps one read and one write in flight.
//
// **No scenario here.**  The jobs come from the burst bundle MarkovHost.write_scenario wrote -- the
// file the Python host runs too -- one burst per job: [x address, x words, <MkvCmd words>].  The one
// thing the Reader must pick out of a response -- which job it answers, tx_id -- is at a position
// handed in from MkvResp's own serializer.  Everything else that crossed the host's endpoints is
// recorded (xsi_mm_host.h), dumped to `trace_dir`, and decoded in Python.
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "xsi_bfm.h"
#include "xsi_mm_host.h"
#include "markov_gen_layout.h"
#include "markov_chain_layout.h"
#include "markov_bases.h"

namespace wfbfm {

class MarkovHostModel : public XsiSimObj {
public:
    /// One job: its command words, where its x lands, and how many words x is.
    struct Job { std::vector<uint64_t> cmd; uint64_t xaddr; uint32_t xwords; };
    /// Where a field sits in a message: word, low bit, width.
    struct Field { int word, bit, width; };

    // DynParams -- MarkovHost's fields of the same names, assigned by the generated harness.
    std::string scenario;       ///< the scenario bundle
    std::string trace_dir;      ///< where each endpoint's trace is dumped ("" = not dumped)

    /// The bus master on the top's AXI4 slave port *bus*, the two interrupt pins; then MarkovHost's
    /// settings: *poll* cycles between polls, at most *max_in_flight* jobs out, *resp_words* words per
    /// response, and tx_id's position in it.
    MarkovHostModel(Dut& d, const char* bus, Dut& d1, const char* irq_qcmd, Dut& d2,
                    const char* irq_qresp, long poll, int max_in_flight, int resp_words,
                    int tx_word, int tx_bit, int tx_width)
        : bus_(d, bus, (int)markov_gen_layout::qcmd.bpw, 0, /*overlap_rw=*/true),
          irq_qcmd_(d1, irq_qcmd), irq_qresp_(d2, irq_qresp),
          rd_(bus_, irq_qresp_, jobs_, in_flight_, poll, (uint32_t)resp_words,
              Field{tx_word, tx_bit, tx_width}),
          wr_(bus_, irq_qcmd_, jobs_, in_flight_, poll, max_in_flight),
          parts_{&irq_qcmd_, &irq_qresp_, &bus_, &rd_, &wr_} {}

    bool done() const { return rd_.done() && wr_.done(); }

    void pre_sim() override {
        const std::vector<uint64_t> w = BurstBundle::read_words(scenario);
        const std::vector<uint64_t> b = BurstBundle::read_bounds(scenario);
        size_t lo = 0;
        for (uint64_t hi : b) {
            jobs_.push_back(Job{std::vector<uint64_t>(w.begin() + lo + 2, w.begin() + hi), w[lo],
                                (uint32_t)w[lo + 1]});
            lo = hi;
        }
        rd_.t_done.assign(jobs_.size(), 0);
        for (XsiSimObj* p : parts_) p->pre_sim();
    }
    // The pins, the bus master, then the reader before the writer -- the pysim reader runs first at
    // t = 0 too (it is the host's run_proc).
    void sample() override { for (XsiSimObj* p : parts_) p->sample(); }
    void update() override {
        for (XsiSimObj* p : parts_) p->update();
        ++cycle_;
        if (!done_cycle_ && done()) done_cycle_ = cycle_;
    }
    void drive() override { for (XsiSimObj* p : parts_) p->drive(); }

    void post_sim() override {
        for (XsiSimObj* p : parts_) p->post_sim();
        std::printf("DONE done=%d cycles=%ld polls=%ld nops=%zu\n", (int)done(),
                    done_cycle_ ? done_cycle_ : cycle_, rd_.polls() + wr_.polls(), bus_.nops());
        for (size_t j = 0; j < jobs_.size(); ++j) std::printf("JOBT %zu t=%ld\n", j, rd_.t_done[j]);
        for (size_t i = 0; i < bus_.nops(); ++i) {
            const AxiMmMaster::Op& o = bus_.op(i);
            std::printf("OP %c 0x%llx n=%zu s=%ld e=%ld\n", o.write ? 'W' : 'R',
                        (unsigned long long)o.addr, o.write ? o.wdata.size() : (size_t)o.nwords,
                        o.t_start, o.t_end);
        }
        if (!trace_dir.empty()) {
            wr_.qcmd.write_trace(trace_dir + "/qcmd");
            rd_.qresp.write_trace(trace_dir + "/qresp");
            rd_.mem.write_trace(trace_dir + "/mem");
        }
    }

private:
    static constexpr uint64_t GEN = markov_bases::GEN_BASE, CHAIN = markov_bases::CHAIN_BASE;

    /// Sends each job's command on qcmd -- room on qcmd's interrupt -- once fewer than max_in_flight
    /// are out.
    class Writer : public XsiSimObj {
    public:
        Writer(AxiMmMaster& m, const IrqPin& irq, const std::vector<Job>& jobs, int& in_flight,
               long poll, int max_in_flight)
            : qcmd(m, at(markov_gen_layout::qcmd, GEN), poll), jobs_(jobs), in_flight_(in_flight),
              max_(max_in_flight) { qcmd.use_irq(irq); }
        bool done() const { return i_ >= jobs_.size() && !qcmd.busy(); }
        long polls() const { return qcmd.polls; }
        void update() override {
            qcmd.step();
            if (!qcmd.busy() && i_ < jobs_.size() && in_flight_ < max_) {
                qcmd.start(jobs_[i_].cmd); ++in_flight_; ++i_;
            }
        }
        MmQueueWriter qcmd;

    private:
        const std::vector<Job>& jobs_;
        int& in_flight_;
        int max_;
        size_t i_ = 0;
    };

    /// Takes each response on qresp -- data on qresp's interrupt -- then reads that job's x.
    class Reader : public XsiSimObj {
    public:
        Reader(AxiMmMaster& m, const IrqPin& irq, const std::vector<Job>& jobs, int& in_flight,
               long poll, uint32_t resp_words, Field tx)
            : qresp(m, at(markov_chain_layout::qresp, CHAIN), poll), mem(m), m_(m), jobs_(jobs),
              in_flight_(in_flight), resp_words_(resp_words), tx_(tx) { qresp.use_irq(irq); }
        bool done() const { return phase_ == DONE; }
        long polls() const { return qresp.polls; }
        void update() override {
            qresp.step(); mem.step();
            if (phase_ == RESP && !qresp.busy()) {
                const uint64_t v = qresp.words[tx_.word] >> tx_.bit;
                j_ = (size_t)(tx_.width >= 64 ? v : (v & ((1ull << tx_.width) - 1)));
                mem.start(jobs_[j_].xaddr, jobs_[j_].xwords);
                phase_ = READX;
            } else if (phase_ == READX && !mem.busy()) {
                t_done[j_] = m_.cycle(); --in_flight_; ++n_;
                phase_ = IDLE;
            }
            if (phase_ == IDLE) {
                if (n_ < jobs_.size()) { qresp.start(resp_words_); phase_ = RESP; }
                else phase_ = DONE;
            }
        }
        MmQueueReader qresp;
        MmBusReader mem;
        std::vector<long> t_done;           ///< the cycle each job's x was read back

    private:
        enum { IDLE, RESP, READX, DONE };
        AxiMmMaster& m_;
        const std::vector<Job>& jobs_;
        int& in_flight_;
        uint32_t resp_words_;
        Field tx_;
        size_t j_ = 0, n_ = 0;
        int phase_ = IDLE;
    };

    std::vector<Job> jobs_;
    int in_flight_ = 0;
    AxiMmMaster bus_;
    IrqPin irq_qcmd_, irq_qresp_;
    Reader rd_;
    Writer wr_;
    std::vector<XsiSimObj*> parts_;
    long cycle_ = 0, done_cycle_ = 0;
};

}  // namespace wfbfm

#endif  // EXAMPLES_MARKOV_HOST_H
