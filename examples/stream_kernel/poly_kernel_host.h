#ifndef EXAMPLES_STREAM_KERNEL_POLY_KERNEL_HOST_H
#define EXAMPLES_STREAM_KERNEL_POLY_KERNEL_HOST_H
// poly_kernel_host.h -- the C++ realization of examples/stream_kernel/stream_kernel_mm.py::PolyHost:
// the same two threads, line for line.  Everything else -- the endpoints (q_in, q_out, q_status, named
// as in Python and bound to their views and interrupts), the bus master, the scenario, the cycle
// count, the report and the traces -- is the generated PolyHost_endpoints and the runtime (xsi_sw.h).
//
// The scenario is PolyHost.write_scenario's bundle, one burst per command:
//     [nhdr, <header words>, <sample words>]
// -- what to send.  What comes back the reader learns from the results' header (Resp).
#include <cstdint>
#include <vector>

#include "PolyHost_endpoints.h"
#include "resp.h"
#include "status.h"
#include "xsi_sw_schema.h"

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
    struct Item { std::vector<uint64_t> hdr, samples; };

    // ~ PolyHost._writer
    void writer() {
        for (const Item& it : items_) {
            q_in.write(it.hdr);
            q_in.write(it.samples);
        }
    }

    // ~ PolyHost._reader -- in the order the kernel writes: the header, the results it announces (one
    // float32 a word at W = 32), the status.  The values are checked in Python, from the traces.
    // The typed reads name the word width: the schemas are generated for W = 32 only.
    static constexpr int W = 32;
    void reader() {
        for (size_t i = 0; i < items_.size(); ++i) {
            const Resp h = q_out.get<Resp, W>();
            q_out.get((uint32_t)h.nsamp);
            q_status.get<Status, W>();
        }
    }

    // ~ PolyHost.pre_sim: the scenario bursts as items.
    void decode() {
        for (const std::vector<uint64_t>& b : scenario_bursts()) {
            Item it;
            const size_t nhdr = (size_t)b[0];
            it.hdr.assign(b.begin() + 1, b.begin() + 1 + nhdr);
            it.samples.assign(b.begin() + 1 + nhdr, b.end());
            items_.push_back(it);
        }
    }

    std::vector<Item> items_;
};

}  // namespace wfbfm

#endif  // EXAMPLES_STREAM_KERNEL_POLY_KERNEL_HOST_H
