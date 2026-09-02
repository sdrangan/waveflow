#ifndef WAVEFLOW_XSI_SIMOBJ_H
#define WAVEFLOW_XSI_SIMOBJ_H
// xsi_simobj.h — the lifecycle a testbench participant shares, and nothing else.
//
// Split out of xsi_bfm.h (plans/behavioral_edges.md S2) because it is not a bus model: it is the C++
// mirror of Python's SimObj, and the two kinds of participant that implement it have nothing else in
// common.  A **node** model (xsi_bfm.h) binds RTL pins and therefore needs Vivado's xsi.h; an **edge**
// model (xsi_channel.h) binds two other models and needs nothing but the standard library.  Keeping
// the base here is what lets a channel be compiled and unit-tested with a plain g++ and no toolchain
// at all — the difference between an edge model having a gate and not having one.
//
// Everything that included xsi_bfm.h still gets this unchanged, because xsi_bfm.h includes it.
namespace wfbfm {

// ---------------------------------------------------------------------------
// XsiSimObj — pre_sim -> (sample / update / drive, once per cycle) -> post_sim.
//
// All five phases are virtual with no-op defaults, so a passive participant (a memory) overrides
// only pre_sim/post_sim while a per-cycle model overrides only sample/update/drive.  The Harness
// holds participants by base pointer and drives each phase over one list, exactly as
// Simulation.run_sim() drives its SimObjs.
//
// Splitting sample/update is not stylistic: a beat is decided from values sampled BEFORE the clock
// edge and applied AFTER it.  Collapsing them changes when a transfer is seen and breaks the models.
// The same split is what makes a model<->model channel order-independent -- see xsi_channel.h.
// ---------------------------------------------------------------------------

class XsiSimObj {
public:
    virtual ~XsiSimObj() = default;
    virtual void pre_sim()  {}   ///< before reset: seed memory / load vectors from files
    virtual void sample()   {}   ///< clk low: read kernel outputs, latch beat flags (VALID && READY)
    virtual void update()   {}   ///< after the rising edge: apply this cycle's beats, advance FSMs
    virtual void drive()    {}   ///< present held values for the next cycle
    virtual void post_sim() {}   ///< after the run: dump results to files, collect metrics
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_SIMOBJ_H
