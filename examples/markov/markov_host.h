#ifndef EXAMPLES_MARKOV_HOST_H
#define EXAMPLES_MARKOV_HOST_H
// markov_host.h -- the C++ realization of examples/markov/markov.py::MarkovHost: the same two
// threads, line for line -- a producer that sends each job's command once a slot is free, and a
// consumer that takes each response, reads that job's x back from memory and frees the slot.
// Everything else -- the endpoints (qcmd, qresp, mem_reader, named as in Python and bound to their
// views and interrupts), the bus master, the scenario, the cycle count, the report and the traces --
// is the generated MarkovHost_endpoints and the runtime (xsi_sw.h).
//
// The scenario is MarkovHost.write_scenario's bundle, one burst per job:
//     [x address, x words, <MkvCmd words>]
// max_in_flight is MarkovHost's DynParam of the same name, assigned by the harness.
#include <cstdint>
#include <cstdio>
#include <memory>
#include <vector>

#include "MarkovHost_endpoints.h"
#include "mkv_resp.h"
#include "xsi_sw_schema.h"

namespace wfbfm {

class MarkovHostModel : public MarkovHost_endpoints {
public:
    using MarkovHost_endpoints::MarkovHost_endpoints;

    long max_in_flight = 0;             ///< DynParam

    // ~ MarkovHost.main: start the writer, then be the reader.
    void main() override {
        decode();
        slots_.reset(new SwSemaphore(sched_, max_in_flight));
        t_done_.assign(jobs_.size(), 0);
        start("writer", [this] { writer(); });
        reader();
    }

    // The completion cycle of each job, for the run's JOBT lines.
    void report() override {
        for (size_t j = 0; j < t_done_.size(); ++j) std::printf("JOBT %zu t=%ld\n", j, t_done_[j]);
    }

private:
    struct Job { uint64_t xaddr; uint32_t xwords; std::vector<uint64_t> cmd; };

    // ~ MarkovHost._writer
    void writer() {
        for (const Job& j : jobs_) {
            slots_->acquire();
            qcmd.write(j.cmd);
        }
    }

    // ~ MarkovHost._reader -- ones and x are checked in Python, from the traces.
    void reader() {
        for (size_t k = 0; k < jobs_.size(); ++k) {
            const MkvResp r = qresp.get<MkvResp>();
            const Job& j = jobs_[(size_t)r.tx_id];
            mem_reader.read(j.xaddr, j.xwords);
            t_done_[(size_t)r.tx_id] = bus_.cycle();
            slots_->release();
        }
    }

    // ~ MarkovHost.pre_sim: the scenario bursts as jobs.
    void decode() {
        for (const std::vector<uint64_t>& b : scenario_bursts())
            jobs_.push_back(Job{b[0], (uint32_t)b[1], std::vector<uint64_t>(b.begin() + 2, b.end())});
    }

    std::vector<Job> jobs_;
    std::unique_ptr<SwSemaphore> slots_;
    std::vector<long> t_done_;
};

}  // namespace wfbfm

#endif  // EXAMPLES_MARKOV_HOST_H
