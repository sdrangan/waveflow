#ifndef TESTS_QUEUED_FIR_HOST_H
#define TESTS_QUEUED_FIR_HOST_H
// queued_fir_host.h -- the C++ twin of tests/examples/test_sw_channels_xsi.py::QueuedFirHost
// (plans/host_runtime.md S6): FirHost with its writer split into two threads joined by a software
// queue -- a packer that queues the scenario items, a sender that takes them and writes the bus.
// Software events take no time, so the bus behaviour, and the cycle count, must be FirHost's.
#include <cstdint>
#include <memory>
#include <vector>

#include "QueuedFirHost_endpoints.h"

namespace wfbfm {

class QueuedFirHostModel : public QueuedFirHost_endpoints {
public:
    using QueuedFirHost_endpoints::QueuedFirHost_endpoints;

    void main() override {
        decode();
        q_.reset(new SwQueue<size_t>(sched_, 2));
        start("packer", [this] { packer(); });
        start("sender", [this] { sender(); });
        reader();
    }

private:
    enum { CFG = 0, PKT = 1 };
    struct Item { int kind; std::vector<uint64_t> words, samples; uint32_t nsamp; };

    void packer() { for (size_t i = 0; i < items_.size(); ++i) q_->put(i); }

    void sender() {
        for (size_t n = 0; n < items_.size(); ++n) {
            const Item& it = items_[q_->get()];
            if (it.kind == CFG) {
                cfg.write(it.words);
            } else {
                qin.write(it.words);
                qin.write(it.samples);
            }
        }
    }

    void reader() {
        for (const Item& it : items_)
            if (it.kind == PKT) {
                qout.get(it.nsamp);
                qresp.get(1);
            }
        status.read();
    }

    void decode() {
        for (const std::vector<uint64_t>& b : scenario_bursts()) {
            Item it{(int)b[0], {}, {}, 0};
            if (it.kind == CFG) {
                it.words.assign(b.begin() + 1, b.end());
            } else {
                it.nsamp = (uint32_t)b[1];
                const size_t nhdr = (size_t)b[4];
                it.words.assign(b.begin() + 5, b.begin() + 5 + nhdr);
                it.samples.assign(b.begin() + 5 + nhdr, b.end());
            }
            items_.push_back(it);
        }
    }

    std::vector<Item> items_;
    std::unique_ptr<SwQueue<size_t> > q_;
};

}  // namespace wfbfm

#endif  // TESTS_QUEUED_FIR_HOST_H
