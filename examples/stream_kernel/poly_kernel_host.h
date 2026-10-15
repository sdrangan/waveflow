#ifndef EXAMPLES_STREAM_KERNEL_POLY_KERNEL_HOST_H
#define EXAMPLES_STREAM_KERNEL_POLY_KERNEL_HOST_H
// poly_kernel_host.h -- the C++ realization of examples/stream_kernel/stream_kernel_mm.py::PolyHost:
// the same two threads, line for line.  Everything else -- the endpoints (q_in, q_out, q_resp, named
// as in Python and bound to their views and interrupts), the bus master, the scenario, the cycle
// count, the report and the traces -- is the generated PolyHost_endpoints and the runtime (xsi_sw.h).
//
// The scenario is PolyHost.write_scenario's bundle, one burst per command:
//     [k, nresp, nhdr, <header words>, <sample words>]
#include <cstdint>
#include <vector>

#include "PolyHost_endpoints.h"

namespace wfbfm {

class PolyHostModel : public PolyHost_endpoints {
public:
    using PolyHost_endpoints::PolyHost_endpoints;

    // ~ PolyHost.main: start the writer, then be the reader.
    void main() override {
        decode();
        start("writer", [this] { writer(); });
        reader();
    }

private:
    struct Item { uint32_t k, nresp; std::vector<uint64_t> hdr, samples; };

    // ~ PolyHost._writer
    void writer() {
        for (const Item& it : items_) {
            q_in.write(it.hdr);
            q_in.write(it.samples);
        }
    }

    // ~ PolyHost._reader -- the results and responses are checked in Python, from the traces.
    void reader() {
        for (const Item& it : items_) {
            q_out.get(it.k);
            q_resp.get(it.nresp);
        }
    }

    // ~ PolyHost.pre_sim: the scenario bursts as items.
    void decode() {
        for (const std::vector<uint64_t>& b : scenario_bursts()) {
            Item it{(uint32_t)b[0], (uint32_t)b[1], {}, {}};
            const size_t nhdr = (size_t)b[2];
            it.hdr.assign(b.begin() + 3, b.begin() + 3 + nhdr);
            it.samples.assign(b.begin() + 3 + nhdr, b.end());
            items_.push_back(it);
        }
    }

    std::vector<Item> items_;
};

}  // namespace wfbfm

#endif  // EXAMPLES_STREAM_KERNEL_POLY_KERNEL_HOST_H
