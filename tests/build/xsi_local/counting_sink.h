#ifndef TESTS_XSI_LOCAL_COUNTING_SINK_H
#define TESTS_XSI_LOCAL_COUNTING_SINK_H
// counting_sink.h -- an example-local BFM model (plans/xsi_system_top.md S2): a class that lives
// beside the Python module declaring it, not in waveflow/build/xsi/.  Trivial on purpose: an AXIS
// sink that is always ready and counts beats.  test_xsi_system_top.py resolves and compiles it.
#include <string>

#include "xsi_bfm.h"

namespace wfbfm {

class CountingSink : public XsiSimObj {
public:
    CountingSink(Dut& d, const std::string& prefix) : d_(d) {
        P_valid = d.port((prefix + "_TVALID").c_str());
        P_ready = d.port((prefix + "_TREADY").c_str());
    }
    void sample() override { beat_ = d_.get1(P_valid) != 0; }
    void update() override { if (beat_) ++beats; }
    void drive() override { d_.put1(P_ready, 1); }
    long beats = 0;
    std::string out_bundle;     // the StreamSink DynParam's C++ half (unused: nothing is dumped)

private:
    Dut& d_;
    int P_valid = -1, P_ready = -1;
    bool beat_ = false;
};

}  // namespace wfbfm

#endif  // TESTS_XSI_LOCAL_COUNTING_SINK_H
