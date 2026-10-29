#ifndef EXAMPLES_LTBF_DSE_JOB_HOST_H
#define EXAMPLES_LTBF_DSE_JOB_HOST_H
// ltbf_dev_host.h -- a C++ twin of examples/ltbf_dse/hw/devsys.py's device hosts (HAND-WRITTEN,
// plans/ltbf_dse/ltbf_dse.md step 5.1).  Each reads the scenario its pysim twin wrote
// (DevHost.scenario_bursts: a kind word, then its fields) and does the same bus operations in the
// same order, so the flow's compare step checks the device's RTL against pysim word for word.
//
//   K_IMAGE  [0, word address, words...]   a plain bus write
//   K_JOB    [1, descriptor words...]      a job into the Job FIFO
//   K_READ   [2, word address, words]      a plain bus read, after every job's Resp
//   K_CFG    [3, IngestCfg words...]       the Ingest DMA's configuration
//   K_CHUNKS [4, chunks, slot words, ring base, ring depth, credit byte address]
//   K_SYM    [5, an antenna's words...]   the O-RU's burst into the DMA's qsym queue
#include <cstdint>
#include <utility>
#include <vector>
#include "LtbfJobHost_endpoints.h"

namespace wfbfm {

#ifndef EXAMPLES_LTBF_DSE_DEV_SCENARIO
#define EXAMPLES_LTBF_DSE_DEV_SCENARIO
struct DevScenario {
    std::vector<std::pair<uint64_t, std::vector<uint64_t> > > image;
    std::vector<std::vector<uint64_t> > jobs;
    std::vector<std::pair<uint64_t, uint32_t> > reads;
    std::vector<uint64_t> cfg;
    std::vector<std::vector<uint64_t> > symbols;
    uint64_t n_chunks = 0, slot = 0, base = 0, depth = 1, credit = 0;

    explicit DevScenario(const std::vector<std::vector<uint64_t> >& bursts) {
        for (const std::vector<uint64_t>& b : bursts) {
            switch (b[0]) {
                case 0: image.emplace_back(b[1], std::vector<uint64_t>(b.begin() + 2, b.end())); break;
                case 1: jobs.emplace_back(b.begin() + 1, b.end()); break;
                case 2: reads.emplace_back(b[1], (uint32_t)b[2]); break;
                case 3: cfg.assign(b.begin() + 1, b.end()); break;
                case 4: n_chunks = b[1]; slot = b[2]; base = b[3]; depth = b[4]; credit = b[5]; break;
                case 5: symbols.emplace_back(b.begin() + 1, b.end()); break;
            }
        }
    }
};
/// Plain reads in AXI4 bursts of at most 256 beats (as the pysim host: DevHost._read).
template <class Bus>
inline void read_bursts(Bus& bus, uint64_t word_addr, uint32_t n) {
    for (uint32_t i = 0; i < n; i += 256)
        bus.read((word_addr + i) * 8, (n - i < 256) ? (n - i) : 256u);
}
#endif  // EXAMPLES_LTBF_DSE_DEV_SCENARIO

class LtbfJobHostModel : public LtbfJobHost_endpoints {
public:
    using LtbfJobHost_endpoints::LtbfJobHost_endpoints;
    void main() override {
        const DevScenario sc(scenario_bursts());
        for (const auto& w : sc.image) bus.write(w.first * 8, w.second);
        for (const auto& j : sc.jobs) qjob.write(j);
        for (size_t i = 0; i < sc.jobs.size(); ++i) qresp.get(1);
        for (const auto& r : sc.reads) read_bursts(bus, r.first, r.second);
    }
};

}  // namespace wfbfm

#endif  // EXAMPLES_LTBF_DSE_JOB_HOST_H
