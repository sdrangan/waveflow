#ifndef WAVEFLOW_XSI_BFM_H
#define WAVEFLOW_XSI_BFM_H
// xsi_bfm.h — reusable cycle-based BFM models for driving a free-running (ap_ctrl_none) kernel's
// RTL through XSI.  EXTRACTED from the four hand-written TBs (mem_r / mem_w / mem_copy /
// interleaver_canon), which had the same AXI4 FSM copy-pasted and renamed four times.  Nothing here
// is generated, and nothing here is per-design: this is the protocol layer those TBs were missing.
//
// Cycle protocol — every model implements the same three phases, and a TB's loop is:
//
//     sim.clock_low();                 // clk=0, settle: kernel outputs are now valid
//     for (m : models) m->sample();    // read kernel outputs, latch beat flags (VALID && READY)
//     sim.clock_high();                // clk=1: the rising edge
//     for (m : models) m->update();    // apply this cycle's beats, advance FSMs
//     for (m : models) m->drive();     // present held values for the next cycle
//
// Splitting sample/update is not stylistic: a beat is decided from values sampled BEFORE the edge,
// and applied AFTER it.  Collapsing them changes when a transfer is seen and breaks the models.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include "xsi_loader.h"
#include "xsi_bundle.h"

namespace wfbfm {

typedef s_xsi_vlog_logicval LV;

// ---------------------------------------------------------------------------
// XsiSimObj — the lifecycle a testbench participant shares, mirroring Python's
// SimObj: pre_sim -> (sample / update / drive, once per cycle) -> post_sim.  All
// five phases are virtual with no-op defaults, so a passive participant (a memory)
// overrides only pre_sim/post_sim while a per-cycle model overrides only
// sample/update/drive.  The Harness holds participants by base pointer and drives
// each phase over one list, exactly as Simulation.run_sim() drives its SimObjs.
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

// ---------------------------------------------------------------------------
// Dut — typed port access over the XSI loader.
// ---------------------------------------------------------------------------

struct Dut {
    Xsi::Loader& x;
    explicit Dut(Xsi::Loader& xx) : x(xx) {}

    /// Resolve a port, or die.  Loud on purpose: a silently-missing port would leave an input
    /// undriven and the failure would surface as an inscrutable hang thousands of cycles later.
    int port(const char* name) {
        int p = x.get_port_number(name);
        if (p < 0) { std::fprintf(stderr, "FATAL: port '%s' not found\n", name); std::exit(3); }
        return p;
    }
    /// Resolve a port that is allowed to be absent (returns -1) — for optional/unused channels.
    int port_opt(const char* name) { return x.get_port_number(name); }

    void put1(int p, uint32_t b)  { LV v; v.aVal = b & 1u; v.bVal = 0; x.put_value(p, &v); }
    void putW(int p, uint64_t val){ LV v[4]; for (int k=0;k<4;k++){ v[k].aVal=(uint32_t)(val>>(32*k)); v[k].bVal=0; } x.put_value(p, v); }
    uint32_t get1(int p)          { LV v[4]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return v[0].aVal & 1u; }
    uint64_t getW(int p)          { LV v[4]; std::memset(v,0,sizeof(v)); x.get_value(p, v); return ((uint64_t)v[1].aVal<<32) | v[0].aVal; }
};

// ---------------------------------------------------------------------------
// FlatMemory — the word-addressed arena both m_axi bundles serve out of.
// ---------------------------------------------------------------------------

/// One memory region tied to a burst bundle: for a load, `bundle`'s words go to `[off, off+len)`
/// (`len` taken from the bundle); for a dump, `[off, off+len)` is written to `bundle`.  A list of
/// these is the memory's "config" — set by whoever builds the harness (the generator emits it), so
/// new modes (more segments, later cycle-by-cycle logging) are just more entries, no config file.
struct MemSeg {
    size_t off = 0;          ///< first word index of the region
    size_t len = 0;          ///< words (dump only; a load uses the bundle's length)
    std::string bundle;      ///< bundle directory, relative to the xsi/ run dir
};

struct FlatMemory : public XsiSimObj {
    std::vector<uint64_t> w;
    int bpw;                                  ///< bytes per word (MEM_DW/8)

    FlatMemory(size_t nwords, int bytes_per_word) : w(nwords, 0), bpw(bytes_per_word) {}

    //: Optional file-backed lifecycle.  Empty lists => the memory does nothing at pre/post_sim (the
    //: "do none" case); populated => it seeds / dumps the listed regions.
    std::vector<MemSeg> load_segs, dump_segs;

    void pre_sim() override {
        for (const MemSeg& s : load_segs) {
            std::vector<uint64_t> v = BurstBundle::read_words(s.bundle);
            for (size_t i = 0; i < v.size() && s.off + i < w.size(); ++i) w[s.off + i] = v[i];
        }
    }
    void post_sim() override {
        for (const MemSeg& s : dump_segs) {
            BurstBundle::write_one(s.bundle,
                std::vector<uint64_t>(w.begin() + s.off, w.begin() + s.off + s.len));
        }
    }

    uint64_t  operator[](size_t i) const { return w[i]; }
    uint64_t& operator[](size_t i)       { return w[i]; }
    size_t    size() const               { return w.size(); }
    uint64_t  word_index(uint64_t byte_addr) const { return byte_addr / (uint64_t)bpw; }

    /// One W-channel beat: byte-strobed read-modify-write.  WSTRB is honoured rather than assumed
    /// all-ones — a partial final beat would otherwise corrupt neighbouring bytes.
    void write_strobed(uint64_t widx, uint64_t data, uint32_t strb) {
        uint64_t cur = w[widx];
        for (int b = 0; b < bpw; ++b) {
            if (strb & (1u << b)) {
                uint64_t m = 0xFFull << (8 * b);
                cur = (cur & ~m) | (data & m);
            }
        }
        w[widx] = cur;
    }
};

// ---------------------------------------------------------------------------
// AxiMmReadSlave — serves the kernel's m_axi read bundle out of a FlatMemory.
// Kernel drives AR* / RREADY; we drive ARREADY / R*.
// ---------------------------------------------------------------------------

class AxiMmReadSlave : public XsiSimObj {
public:
    /// *prefix* is the bundle's port prefix, e.g. "m_axi_gmem0".
    AxiMmReadSlave(Dut& d, const std::string& prefix, FlatMemory& mem) : d_(d), mem_(mem) {
        P_arvalid = d.port((prefix + "_ARVALID").c_str());
        P_arready = d.port((prefix + "_ARREADY").c_str());
        P_araddr  = d.port((prefix + "_ARADDR").c_str());
        P_arlen   = d.port((prefix + "_ARLEN").c_str());
        P_rvalid  = d.port((prefix + "_RVALID").c_str());
        P_rready  = d.port((prefix + "_RREADY").c_str());
        P_rdata   = d.port((prefix + "_RDATA").c_str());
        P_rlast   = d.port((prefix + "_RLAST").c_str());
    }

    void sample() override {
        arvalid_ = d_.get1(P_arvalid);
        araddr_  = d_.getW(P_araddr);
        arlen_   = (uint32_t)(d_.getW(P_arlen) & 0xFF);
        rready_  = d_.get1(P_rready);
        ar_beat_ = (state_ == AR_IDLE) && arvalid_ && h_arready_;
        r_beat_  = (state_ == R_SEND)  && h_rvalid_ && rready_;
    }

    void update() override {
        if (ar_beat_) {
            addrw_ = mem_.word_index(araddr_); len_ = arlen_; beat_ = 0;
            state_ = R_SEND; h_arready_ = 0; h_rvalid_ = 1;
            h_rdata_ = mem_[addrw_]; h_rlast_ = (len_ == 0) ? 1u : 0u;
        } else if (r_beat_) {
            if (beat_ >= len_) { state_ = AR_IDLE; h_rvalid_ = 0; h_rlast_ = 0; h_arready_ = 1; }
            else { ++beat_; h_rdata_ = mem_[addrw_ + beat_]; h_rlast_ = (beat_ >= len_) ? 1u : 0u; }
        }
    }

    void drive() override {
        d_.put1(P_arready, h_arready_);
        d_.put1(P_rvalid,  h_rvalid_);
        d_.putW(P_rdata,   h_rdata_);
        d_.put1(P_rlast,   h_rlast_);
    }

    int state() const { return (int)state_; }   ///< for timeout diagnostics

private:
    Dut& d_; FlatMemory& mem_;
    int P_arvalid, P_arready, P_araddr, P_arlen, P_rvalid, P_rready, P_rdata, P_rlast;
    enum State { AR_IDLE, R_SEND };
    State    state_ = AR_IDLE;
    uint64_t addrw_ = 0; uint32_t len_ = 0, beat_ = 0;
    uint32_t h_arready_ = 1, h_rvalid_ = 0, h_rlast_ = 0; uint64_t h_rdata_ = 0;
    uint32_t arvalid_ = 0, rready_ = 0, arlen_ = 0; uint64_t araddr_ = 0;
    bool     ar_beat_ = false, r_beat_ = false;
};

// ---------------------------------------------------------------------------
// AxiMmWriteSlave — accepts the kernel's m_axi writes into a FlatMemory.
// Kernel drives AW* / W* / BREADY; we drive AWREADY / WREADY / B*.
// ---------------------------------------------------------------------------

class AxiMmWriteSlave : public XsiSimObj {
public:
    AxiMmWriteSlave(Dut& d, const std::string& prefix, FlatMemory& mem) : d_(d), mem_(mem) {
        P_awvalid = d.port((prefix + "_AWVALID").c_str());
        P_awready = d.port((prefix + "_AWREADY").c_str());
        P_awaddr  = d.port((prefix + "_AWADDR").c_str());
        P_awlen   = d.port((prefix + "_AWLEN").c_str());
        P_wvalid  = d.port((prefix + "_WVALID").c_str());
        P_wready  = d.port((prefix + "_WREADY").c_str());
        P_wdata   = d.port((prefix + "_WDATA").c_str());
        P_wstrb   = d.port((prefix + "_WSTRB").c_str());
        P_wlast   = d.port((prefix + "_WLAST").c_str());
        P_bvalid  = d.port((prefix + "_BVALID").c_str());
        P_bready  = d.port((prefix + "_BREADY").c_str());
    }

    void sample() override {
        awvalid_ = d_.get1(P_awvalid);
        awaddr_  = d_.getW(P_awaddr);
        awlen_   = (uint32_t)(d_.getW(P_awlen) & 0xFF);
        wvalid_  = d_.get1(P_wvalid);
        wdata_   = d_.getW(P_wdata);
        wstrb_   = (uint32_t)(d_.getW(P_wstrb) & 0xFF);
        wlast_   = d_.get1(P_wlast);
        bready_  = d_.get1(P_bready);
        aw_beat_ = (state_ == AW_IDLE) && awvalid_ && h_awready_;
        w_beat_  = (state_ == W_RECV)  && wvalid_  && h_wready_;
        b_beat_  = (state_ == B_RESP)  && h_bvalid_ && bready_;
    }

    void update() override {
        if (aw_beat_) {
            addrw_ = mem_.word_index(awaddr_); len_ = awlen_; beat_ = 0;
            state_ = W_RECV; h_awready_ = 0; h_wready_ = 1;
        } else if (w_beat_) {
            mem_.write_strobed(addrw_ + beat_, wdata_, wstrb_);
            ++w_count_;
            if (wlast_) { state_ = B_RESP; h_wready_ = 0; h_bvalid_ = 1; }
            else        { ++beat_; }
        } else if (b_beat_) {
            saw_b_ = true;
            state_ = AW_IDLE; h_bvalid_ = 0; h_awready_ = 1;
        }
    }

    void drive() override {
        d_.put1(P_awready, h_awready_);
        d_.put1(P_wready,  h_wready_);
        d_.put1(P_bvalid,  h_bvalid_);
    }

    int  w_count() const { return w_count_; }   ///< total W beats accepted (a progress metric)
    int  state() const   { return (int)state_; }
    /// True once a B response has been consummated.  A write is not observable until B: "all the
    /// data went out" is not the same as "the write completed", so a TB that drains on w_count
    /// alone can stop before the last burst is acknowledged.
    bool saw_b() const   { return saw_b_; }

private:
    Dut& d_; FlatMemory& mem_;
    int P_awvalid, P_awready, P_awaddr, P_awlen, P_wvalid, P_wready, P_wdata, P_wstrb, P_wlast,
        P_bvalid, P_bready;
    enum State { AW_IDLE, W_RECV, B_RESP };
    State    state_ = AW_IDLE;
    uint64_t addrw_ = 0; uint32_t len_ = 0, beat_ = 0;
    int      w_count_ = 0;
    bool     saw_b_ = false;
    uint32_t h_awready_ = 1, h_wready_ = 0, h_bvalid_ = 0;
    uint32_t awvalid_ = 0, wvalid_ = 0, wstrb_ = 0, wlast_ = 0, bready_ = 0, awlen_ = 0;
    uint64_t awaddr_ = 0, wdata_ = 0;
    bool     aw_beat_ = false, w_beat_ = false, b_beat_ = false;
};

// ---------------------------------------------------------------------------
// AxisMaster / AxisSlave — the TB side of the kernel's AXIS ports.
// ---------------------------------------------------------------------------

/// Presents a fixed word vector on an AXIS slave port of the kernel, one word per accepted beat,
/// dropping TVALID once every word has gone out.
class AxisMaster : public XsiSimObj {
public:
    AxisMaster(Dut& d, const std::string& prefix, std::vector<uint64_t> words)
        : d_(d), words_(std::move(words)) {
        P_data  = d.port((prefix + "_TDATA").c_str());
        P_valid = d.port((prefix + "_TVALID").c_str());
        P_ready = d.port((prefix + "_TREADY").c_str());
        h_valid_ = words_.empty() ? 0u : 1u;
    }

    //: Optional: if set, pre_sim (re)loads the presented words from this bundle instead of the ctor
    //: vector — so the driver plays the same on-disk vectors the pysim StreamDriver does.
    std::string in_bundle;
    void pre_sim() override {
        if (in_bundle.empty()) return;
        words_ = BurstBundle::read_words(in_bundle);
        widx_ = 0;
        h_valid_ = words_.empty() ? 0u : 1u;
    }

    void sample() override { ready_ = d_.get1(P_ready); beat_ = (h_valid_ && ready_); }

    void update() override {
        if (beat_ && widx_ < (int)words_.size()) {
            ++widx_;
            h_valid_ = (widx_ < (int)words_.size()) ? 1u : 0u;
        }
    }

    void drive() override {
        d_.putW(P_data, (widx_ < (int)words_.size()) ? words_[widx_] : 0);
        d_.put1(P_valid, h_valid_);
    }

    bool done() const { return widx_ >= (int)words_.size(); }
    int  sent() const { return widx_; }
    int  total() const { return (int)words_.size(); }

private:
    Dut& d_;
    std::vector<uint64_t> words_;
    int P_data, P_valid, P_ready;
    int widx_ = 0;
    uint32_t h_valid_ = 0, ready_ = 0;
    bool beat_ = false;
};

/// Always-ready sink for an AXIS master port of the kernel; collects every word it emits.
class AxisSlave : public XsiSimObj {
public:
    AxisSlave(Dut& d, const std::string& prefix) : d_(d) {
        P_data  = d.port((prefix + "_TDATA").c_str());
        P_valid = d.port((prefix + "_TVALID").c_str());
        P_ready = d.port((prefix + "_TREADY").c_str());
    }

    void sample() override {
        valid_ = d_.get1(P_valid);
        data_  = d_.getW(P_data);
        beat_  = (valid_ && h_ready_);
    }

    /// Records the cycle each word arrived, not just the word.
    ///
    /// The sink counts its own cycles: the phase contract calls `update()` exactly once per cycle,
    /// so an internal counter IS the cycle number — no clock reference, no argument, no change to
    /// the uniform model API.  This is what lets the TB's loop carry no measurement logic, and it
    /// keeps a hard separation the hand-written TBs got wrong: **the sink reports when work
    /// COMPLETED; the loop only decides when to stop looking.** Conflating those is how three of
    /// four TBs printed a drain tail as if it were the design's latency.
    void update() override {
        ++cycle_;                                   // 1-based: this is the cycle now executing
        if (beat_) { words_.push_back(data_); beat_cycles_.push_back(cycle_); }
    }

    void drive() override { d_.put1(P_ready, h_ready_); }

    //: Optional: if set, post_sim dumps the collected words + their arrival cycles (a capture bundle),
    //: so Python checks correctness AND completion timing off-line rather than in hand-written C++.
    std::string out_bundle;
    void post_sim() override {
        if (!out_bundle.empty()) BurstBundle::write_capture(out_bundle, words_, beat_cycles_);
    }

    const std::vector<uint64_t>& words() const { return words_; }
    size_t count() const { return words_.size(); }

    /// The cycle each accepted word arrived on (parallel to `words()`).
    const std::vector<long>& beat_cycles() const { return beat_cycles_; }

    /// Cycle the `n`-th word arrived, or -1 if fewer than `n` words have.  The completion time of a
    /// run that expects `n` words is `cycle_of_word(n)`.
    long cycle_of_word(size_t n) const {
        return (n >= 1 && n <= beat_cycles_.size()) ? beat_cycles_[n - 1] : -1;
    }

private:
    Dut& d_;
    int P_data, P_valid, P_ready;
    std::vector<uint64_t> words_;
    std::vector<long> beat_cycles_;
    long cycle_ = 0;
    uint32_t h_ready_ = 1, valid_ = 0;
    uint64_t data_ = 0;
    bool beat_ = false;
};

// ---------------------------------------------------------------------------
// XsiSim — open/close, the clock phases, reset, and pinning undriven inputs.
// ---------------------------------------------------------------------------

// The xsim engine ships under a different name per platform: xv_simulator_kernel.dll beside
// Vivado's win64.o libraries, libxv_simulator_kernel.so under lib/lnx64.o.  run.bat and run.sh
// each put the right directory on the loader path; this picks the matching file name.
#ifdef _WIN32
#  define WAVEFLOW_XSI_ENGINE "xv_simulator_kernel.dll"
#else
#  define WAVEFLOW_XSI_ENGINE "libxv_simulator_kernel.so"
#endif

class XsiSim {
public:
    XsiSim(const std::string& design, const std::string& wdb,
           const std::string& engine = WAVEFLOW_XSI_ENGINE)
        : xsi_(design, engine), d_(xsi_) {
        s_xsi_setup_info info; std::memset(&info, 0, sizeof(info));
        std::vector<char> wdbbuf(wdb.begin(), wdb.end()); wdbbuf.push_back('\0');
        info.wdbFileName = wdbbuf.data();
        xsi_.open(&info);
        P_clk_   = d_.port("ap_clk");
        P_rst_n_ = d_.port("ap_rst_n");
    }

    Dut& dut() { return d_; }
    Xsi::Loader& loader() { return xsi_; }

    /// Drive listed TB-side inputs to 0.  Absent ports are skipped: the set is written per-design as
    /// "every input this TB does not otherwise drive", and which of those exist depends on the
    /// kernel's bundles.
    void pin_low(const char* const* names, size_t n) {
        for (size_t i = 0; i < n; ++i) {
            int p = xsi_.get_port_number(names[i]);
            if (p >= 0) d_.putW(p, 0);
        }
    }

    void clock_low()  { d_.put1(P_clk_, 0); xsi_.run(10); }
    void clock_high() { d_.put1(P_clk_, 1); xsi_.run(10); }

    /// Hold reset for *cycles* clocks with *drive* presenting held values throughout, then release.
    template <typename DriveFn>
    void reset(DriveFn drive, int cycles = 16) {
        d_.put1(P_rst_n_, 0);
        drive();
        for (int k = 0; k < cycles; ++k) { clock_low(); clock_high(); }
        d_.put1(P_rst_n_, 1);
        drive();
    }

    void close() { xsi_.close(); }

private:
    Xsi::Loader xsi_;
    Dut d_;
    int P_clk_, P_rst_n_;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_BFM_H
