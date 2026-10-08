#ifndef EXAMPLES_MM_FIR_HOST_H
#define EXAMPLES_MM_FIR_HOST_H
// mm_fir_host.h -- the C++ realization of examples/mm_fir/mm_fir.py::FirHost (its bfm_model()).
//
// The same host program on the C++ endpoints of xsi_mm_host.h: a Writer that commits each config and
// sends each packet as two queue-in packets (its header, then its samples), and a Reader that takes
// one output packet per input packet and its response, then reads the final status once.  Both wait
// on the queue views' interrupts -- nothing polls -- and share one AxiMmMaster, which keeps one read
// and one write in flight.
//
// **No scenario here.**  The configs and packets come from the burst bundle FirHost.write_scenario
// wrote -- the file the Python host runs too -- one burst per item:
//     [CFG, <config words>]   or   [PKT, nsamp, tx_id, want, nhdr, <header words>, <sample words>]
// and no address either: the views are the FIR type's layout placed at this system's base, from the
// headers bus_address_headers generates.  The responses are not checked here: every endpoint records
// what crossed it (xsi_mm_host.h), the traces are dumped to `trace_dir`, and Python decodes them.
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "xsi_bfm.h"
#include "xsi_mm_host.h"
#include "mm_fir_layout.h"
#include "mm_fir_bases.h"

namespace wfbfm {

class FirHostModel : public XsiSimObj {
public:
    enum { CFG = 0, PKT = 1 };
    /// One scenario item: a config (words), or a packet (its header words, its samples, its size).
    struct Item { int kind; std::vector<uint64_t> words, samples; uint32_t nsamp; };

    // DynParams -- FirHost's fields of the same names, assigned by the generated harness.
    std::string scenario;       ///< the scenario bundle
    std::string trace_dir;      ///< where each endpoint's trace is dumped ("" = not dumped)

    /// The bus master on the top's AXI4 slave port *bus*; the three interrupt pins; *poll* the cycles
    /// between polls (FirHost.poll_cycles), should an endpoint poll.
    FirHostModel(Dut& d, const char* bus, Dut& d1, const char* irq_qin, Dut& d2, const char* irq_qout,
                 Dut& d3, const char* irq_qresp, long poll)
        : bus_(d, bus, (int)mm_fir_layout::regs.bpw, 0, /*overlap_rw=*/true),
          irq_qin_(d1, irq_qin), irq_qout_(d2, irq_qout), irq_qresp_(d3, irq_qresp),
          rd_(bus_, irq_qout_, irq_qresp_, items_, poll), wr_(bus_, irq_qin_, items_, poll),
          parts_{&irq_qin_, &irq_qout_, &irq_qresp_, &bus_, &rd_, &wr_} {}

    bool done() const { return rd_.done() && wr_.done(); }

    void pre_sim() override {
        const std::vector<uint64_t> w = BurstBundle::read_words(scenario);
        const std::vector<uint64_t> b = BurstBundle::read_bounds(scenario);
        size_t lo = 0;
        for (uint64_t hi : b) {
            Item it{(int)w[lo], {}, {}, 0};
            if (it.kind == CFG) {
                it.words.assign(w.begin() + lo + 1, w.begin() + hi);
            } else {
                it.nsamp = (uint32_t)w[lo + 1];
                const size_t nhdr = (size_t)w[lo + 4], h0 = lo + 5;
                it.words.assign(w.begin() + h0, w.begin() + h0 + nhdr);
                it.samples.assign(w.begin() + h0 + nhdr, w.begin() + hi);
            }
            items_.push_back(it);
            lo = hi;
        }
        for (XsiSimObj* p : parts()) p->pre_sim();
    }
    // The pins, the bus master, then the reader before the writer -- the pysim reader runs first at
    // t = 0 too (it is the host's run_proc).
    void sample() override { for (XsiSimObj* p : parts()) p->sample(); }
    void update() override {
        for (XsiSimObj* p : parts()) p->update();
        ++cycle_;
        if (!done_cycle_ && done()) done_cycle_ = cycle_;
    }
    void drive() override { for (XsiSimObj* p : parts()) p->drive(); }

    void post_sim() override {
        for (XsiSimObj* p : parts()) p->post_sim();
        std::printf("DONE done=%d cycles=%ld polls=%ld nops=%zu\n", (int)done(),
                    done_cycle_ ? done_cycle_ : cycle_, rd_.polls() + wr_.polls(), bus_.nops());
        for (size_t i = 0; i < bus_.nops(); ++i) {
            const AxiMmMaster::Op& o = bus_.op(i);
            std::printf("OP %c 0x%llx n=%zu s=%ld e=%ld\n", o.write ? 'W' : 'R',
                        (unsigned long long)o.addr, o.write ? o.wdata.size() : (size_t)o.nwords,
                        o.t_start, o.t_end);
        }
        if (!trace_dir.empty()) {
            wr_.cfg.write_trace(trace_dir + "/cfg");
            wr_.qin.write_trace(trace_dir + "/qin");
            rd_.qout.write_trace(trace_dir + "/qout");
            rd_.qresp.write_trace(trace_dir + "/qresp");
            rd_.status.write_trace(trace_dir + "/status");
        }
    }

private:
    /// Where this system placed the FIR -- its views are this plus the type's layout offsets.
    static constexpr uint64_t FIR = mm_fir_bases::FIR_BASE;

    /// Commits each config; sends each packet as two queue-in packets -- its header, then its
    /// samples.  It never waits for a config to be received: the header's cfg_id makes the kernel
    /// wait.  It waits for room in queue in on queue in's interrupt.
    class Writer : public XsiSimObj {
    public:
        Writer(AxiMmMaster& m, const IrqPin& qin_irq, const std::vector<Item>& items, long poll)
            : cfg(m, at(mm_fir_layout::regs, FIR), poll), qin(m, at(mm_fir_layout::qin, FIR), poll),
              items_(items) { qin.use_irq(qin_irq); }
        bool done() const { return i_ >= items_.size() && phase_ == IDLE; }
        long polls() const { return qin.polls; }
        void update() override {
            cfg.step(); qin.step();
            if (phase_ == SEND_CFG && !cfg.busy()) next();
            else if (phase_ == SEND_HDR && !qin.busy()) { qin.start(items_[i_].samples); phase_ = SEND_SAMP; }
            else if (phase_ == SEND_SAMP && !qin.busy()) next();
            if (phase_ == IDLE && i_ < items_.size()) {
                const Item& it = items_[i_];
                if (it.kind == CFG) { cfg.start(it.words); phase_ = SEND_CFG; }
                else                { qin.start(it.words); phase_ = SEND_HDR; }
            }
        }
        MmRegBankCfg cfg;
        MmQueueWriter qin;

    private:
        enum { IDLE, SEND_CFG, SEND_HDR, SEND_SAMP };
        void next() { ++i_; phase_ = IDLE; }
        const std::vector<Item>& items_;
        size_t i_ = 0;
        int phase_ = IDLE;
    };

    /// Takes one output packet per input packet, then that packet's response -- each on its queue's
    /// interrupt -- then reads the final status once (the kernel publishes it before each response).
    class Reader : public XsiSimObj {
    public:
        Reader(AxiMmMaster& m, const IrqPin& qout_irq, const IrqPin& qresp_irq,
               const std::vector<Item>& items, long poll)
            : qout(m, at(mm_fir_layout::qout, FIR), poll), qresp(m, at(mm_fir_layout::qresp, FIR), poll),
              status(m, at(mm_fir_layout::regs, FIR), poll), items_(items) {
            qout.use_irq(qout_irq); qresp.use_irq(qresp_irq);
        }
        bool done() const { return phase_ == DONE; }
        long polls() const { return qout.polls + qresp.polls; }
        void update() override {
            qout.step(); qresp.step(); status.step();
            if (phase_ == READ && !qout.busy()) { qresp.start(1); phase_ = RESP; }
            else if (phase_ == RESP && !qresp.busy()) { ++i_; phase_ = IDLE; }
            else if (phase_ == STATUS && !status.busy()) phase_ = DONE;
            if (phase_ == IDLE) {
                while (i_ < items_.size() && items_[i_].kind != PKT) ++i_;
                if (i_ < items_.size()) { qout.start(items_[i_].nsamp); phase_ = READ; }
                else { status.start(0); phase_ = STATUS; }
            }
        }
        MmQueueReader qout, qresp;
        MmStatusReader status;

    private:
        enum { IDLE, READ, RESP, STATUS, DONE };
        const std::vector<Item>& items_;
        size_t i_ = 0;
        int phase_ = IDLE;
    };

    const std::vector<XsiSimObj*>& parts() const { return parts_; }

    std::vector<Item> items_;
    AxiMmMaster bus_;
    IrqPin irq_qin_, irq_qout_, irq_qresp_;
    Reader rd_;
    Writer wr_;
    std::vector<XsiSimObj*> parts_;
    long cycle_ = 0, done_cycle_ = 0;
};

}  // namespace wfbfm

#endif  // EXAMPLES_MM_FIR_HOST_H
