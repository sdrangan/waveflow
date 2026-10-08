#ifndef EXAMPLES_MM_FIR_HOST_H
#define EXAMPLES_MM_FIR_HOST_H
// mm_fir_host.h -- the C++ realization of examples/mm_fir/mm_fir.py::FirHost: the same two threads,
// line for line.  Everything else -- the endpoints (cfg, qin, qout, qresp, status, named as in
// Python and bound to their views and interrupts), the bus master, the scenario, the cycle count, the
// report and the traces -- is the generated FirHost_endpoints and the runtime (xsi_sw.h).
//
// The scenario is FirHost.write_scenario's bundle, one burst per item:
//     [CFG, <config words>]   or   [PKT, nsamp, tx_id, want, nhdr, <header words>, <sample words>]
#include <cstdint>
#include <vector>

#include "FirHost_endpoints.h"

namespace wfbfm {

class FirHostModel : public FirHost_endpoints {
public:
    using FirHost_endpoints::FirHost_endpoints;

    // ~ FirHost.main: start the writer, then be the reader.
    void main() override {
        decode();
        start("writer", [this] { writer(); });
        reader();
    }

private:
    enum { CFG = 0, PKT = 1 };
    struct Item { int kind; std::vector<uint64_t> words, samples; uint32_t nsamp; };

    // ~ FirHost._writer
    void writer() {
        for (const Item& it : items_) {
            if (it.kind == CFG) {
                cfg.write(it.words);
            } else {
                qin.write(it.words);          // the header
                qin.write(it.samples);        // the samples
            }
        }
    }

    // ~ FirHost._reader -- the responses are checked in Python, from the traces.
    void reader() {
        for (const Item& it : items_)
            if (it.kind == PKT) {
                qout.get(it.nsamp);
                qresp.get(1);
            }
        status.read();                        // final: published before the last response
    }

    // ~ FirHost.pre_sim: the scenario bursts as items.
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
};

}  // namespace wfbfm

#endif  // EXAMPLES_MM_FIR_HOST_H
