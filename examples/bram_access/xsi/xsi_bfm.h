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
// The participant lifecycle (pre_sim / sample / update / drive / post_sim).  It lives in its own
// header because it is not a bus model: an edge model (xsi_channel.h) implements the same five
// phases and must compile with no Vivado headers at all.  Included here so every existing user of
// xsi_bfm.h still gets `wfbfm::XsiSimObj` exactly as before.
#include "xsi_simobj.h"

namespace wfbfm {

typedef s_xsi_vlog_logicval LV;

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
///
/// THE SIDE CHANNELS ARE DRIVEN WHEN THE KERNEL HAS THEM, AND TLAST COMES FROM THE BUNDLE'S BOUNDS.
///
/// `port_opt` returns -1 for a port that is not there, so a kernel whose boundary stream is a plain
/// `hls::stream<ap_uint<W> >` (no TLAST pin — every free-running top before
/// `StreamIFSlave.boundary_tlast` existed) is driven exactly as before and this costs it nothing.
///
/// When the pins ARE there — the port is an `ap_axis`, which is what makes Vitis emit them — TLAST
/// comes from `bounds.bin`, which the burst bundle has always carried and this model used to
/// discard: burst k is `words[bounds[k-1] : bounds[k]]`, so the last word of every burst is a TLAST
/// beat.  That is what makes the two backends agree — the pysim StreamDriver's bursts and the RTL
/// driver's TLASTs are the SAME bytes on disk, not two encodings of one intent.  With no bundle (a
/// ctor word vector) the whole vector is one burst, which is what `BurstBundle::write_one` means and
/// what a continuous stream is.
///
/// TKEEP and TSTRB are held ALL-ONES, and that is not a placeholder.  `ap_axis` brings them whether
/// a design reads them or not, an undriven kernel input is X, and X on a qualifier is the kind of
/// thing that propagates into a comparison rather than into an error.  All-ones is "every byte of
/// this beat is a real data byte", which is what a DMA drives for a contiguous transfer and the only
/// thing this repo's designs ever mean.
class AxisMaster : public XsiSimObj {
public:
    AxisMaster(Dut& d, const std::string& prefix, std::vector<uint64_t> words)
        : d_(d), words_(std::move(words)) {
        P_data  = d.port((prefix + "_TDATA").c_str());
        P_valid = d.port((prefix + "_TVALID").c_str());
        P_ready = d.port((prefix + "_TREADY").c_str());
        P_last  = d.port_opt((prefix + "_TLAST").c_str());
        P_keep  = d.port_opt((prefix + "_TKEEP").c_str());
        P_strb  = d.port_opt((prefix + "_TSTRB").c_str());
        one_burst();
        h_valid_ = words_.empty() ? 0u : 1u;
    }

    //: Optional: if set, pre_sim (re)loads the presented words from this bundle instead of the ctor
    //: vector — so the driver plays the same on-disk vectors the pysim StreamDriver does.
    std::string in_bundle;
    void pre_sim() override {
        if (in_bundle.empty()) return;
        words_ = BurstBundle::read_words(in_bundle);
        bounds_ = BurstBundle::read_bounds(in_bundle);
        if (bounds_.empty()) one_burst();
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
        if (P_last >= 0) d_.put1(P_last, is_last(widx_));
        // All-ones, sized by the port itself: putW writes the low bits of a 64-bit value and the
        // pin is one bit per payload byte, so ~0 is right at every width this repo uses.
        if (P_keep >= 0) d_.putW(P_keep, ~(uint64_t)0);
        if (P_strb >= 0) d_.putW(P_strb, ~(uint64_t)0);
    }

    bool done() const { return widx_ >= (int)words_.size(); }
    int  sent() const { return widx_; }
    int  total() const { return (int)words_.size(); }

private:
    /// One burst spanning every word — the ctor's vector, and the fallback for a bundle whose
    /// bounds.bin is missing.  A continuous stream is a single frame, never a frameless one: with
    /// no bound at all the last word would carry no TLAST and a kernel waiting for one would hang
    /// at the very end of a run, which reads as a design deadlock rather than as a missing file.
    void one_burst() { bounds_.assign(1, (uint64_t)words_.size()); }

    /// Is word *i* the last of its burst?  Linear in the number of bursts and evaluated once per
    /// cycle against a cursor that only moves forward, so it is O(1) amortised; a scan is the right
    /// shape here because the bounds are cumulative and monotone.
    uint32_t is_last(int i) const {
        if (i < 0 || i >= (int)words_.size()) return 0u;
        for (size_t k = 0; k < bounds_.size(); ++k)
            if ((uint64_t)i + 1 == bounds_[k]) return 1u;
        return 0u;
    }

    Dut& d_;
    std::vector<uint64_t> words_;
    std::vector<uint64_t> bounds_;
    int P_data, P_valid, P_ready, P_last, P_keep, P_strb;
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
        // -1 when the kernel's port is a plain ap_uint stream, which is most of them.  A framed
        // output port has the pin, and then the frame bounds are recorded so the dumped bundle is
        // readable as BURSTS by the same Python that reads the pysim sink's — see the note on
        // AxisMaster.
        P_last  = d.port_opt((prefix + "_TLAST").c_str());
    }

    void sample() override {
        valid_ = d_.get1(P_valid);
        data_  = d_.getW(P_data);
        last_  = (P_last >= 0) ? d_.get1(P_last) : 0u;
        beat_  = (valid_ && d_ready_);
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
        if (beat_) {
            words_.push_back(data_); beat_cycles_.push_back(cycle_);
            if (P_last >= 0 && last_) bounds_.push_back((uint64_t)words_.size());
        }
    }

    void drive() override {
        d_ready_ = (h_ready_ && cycle_ >= ready_from) ? 1u : 0u;
        d_.put1(P_ready, d_ready_);
    }

    //: Optional: hold TREADY low until this cycle (1-based, the sink's own count).  Default 0 = ready
    //: from the start, exactly the always-ready sink every existing gate uses.  Exists so a gate can
    //: fill an upstream FIFO on purpose and measure the back-pressure (plans/mm_slave_adaptor.md).
    long ready_from = 0;

    //: Optional: if set, post_sim dumps the collected words + their arrival cycles (a capture bundle),
    //: so Python checks correctness AND completion timing off-line rather than in hand-written C++.
    std::string out_bundle;
    void post_sim() override {
        if (out_bundle.empty()) return;
        // A framed port dumps the frames it actually asserted; an unframed one dumps one burst,
        // which is what it is.  A framed port that ended mid-frame (words after the last TLAST)
        // gets a closing bound so no word is dropped from the bundle -- a truncated capture is a
        // finding for the checker, not something to hide by not writing the tail.
        if (P_last >= 0) {
            std::vector<uint64_t> b = bounds_;
            if (b.empty() || b.back() != (uint64_t)words_.size())
                b.push_back((uint64_t)words_.size());
            BurstBundle::write_capture(out_bundle, words_, beat_cycles_, b);
        } else {
            BurstBundle::write_capture(out_bundle, words_, beat_cycles_);
        }
    }

    const std::vector<uint64_t>& words() const { return words_; }
    size_t count() const { return words_.size(); }
    /// Cumulative word counts at each TLAST (empty for a port without the pin).
    const std::vector<uint64_t>& bounds() const { return bounds_; }

    /// The cycle each accepted word arrived on (parallel to `words()`).
    const std::vector<long>& beat_cycles() const { return beat_cycles_; }

    /// Cycle the `n`-th word arrived, or -1 if fewer than `n` words have.  The completion time of a
    /// run that expects `n` words is `cycle_of_word(n)`.
    long cycle_of_word(size_t n) const {
        return (n >= 1 && n <= beat_cycles_.size()) ? beat_cycles_[n - 1] : -1;
    }

private:
    Dut& d_;
    int P_data, P_valid, P_ready, P_last;
    std::vector<uint64_t> words_;
    std::vector<uint64_t> bounds_;
    std::vector<long> beat_cycles_;
    long cycle_ = 0;
    uint32_t h_ready_ = 1, d_ready_ = 1, valid_ = 0, last_ = 0;
    uint64_t data_ = 0;
    bool beat_ = false;
};

// ---------------------------------------------------------------------------
// AxiMmMaster — the TB acting as an AXI4 *master*: a host, or any bus master that is not the DUT.
// We drive AW* / W* / BREADY / AR* / RREADY; the DUT (a crossbar, a slave) drives the rest.
// ---------------------------------------------------------------------------

/// Issues a queue of INCR bursts, one transaction outstanding at a time, in queue order.
///
/// Every BFM above is the SLAVE side of an AXI port — the memory behind a kernel's m_axi.  This is
/// the other side, and it exists for plans/mm_slave_adaptor.md: a host writing a kernel's register
/// bank or queue window through a crossbar.  It is deliberately simple — no outstanding-transaction
/// pipelining — because what the gates measure is the slave path, and a master that overlapped its
/// own transactions would fold its own policy into every number.
///
/// **`overlap_rw`** (constructor, default false) lets ONE read and ONE write be outstanding at once,
/// each channel still in queue order -- what AXI's independent read and write channels allow, what a
/// DMA engine or a multi-threaded host does, and what a pysim MMIFMaster does (plans/
/// mm_adaptor_host_endpoints.md, "Which bus-master model is right").  A host with a writer and a reader
/// program needs it to overlap the two.  Order BETWEEN a read and a write is then the master's own
/// business, as on a real bus: a program that needs a write to land before a read waits for the
/// write's B response first (the xsi_mm_host.h endpoints always do).  Off, the master is exactly the
/// one-at-a-time model every existing gate was measured with.
///
/// A write presents AW and its first W beat in the SAME cycle (AXI allows W before or with AW; a
/// master that waited for AWREADY first would add a cycle that belongs to the TB, not the DUT).
/// BREADY and RREADY are held high: the master never back-pressures a response.
///
/// Timing: `update()` runs once per cycle, so its counter is the cycle number (the AxisSlave
/// convention).  An op's `t_start` is the cycle its address phase is first presented, `t_end` the
/// cycle of its B beat (write) or RLAST beat (read).  `not_before` holds an op back until that cycle,
/// which is how a TB makes two masters collide on purpose.
class AxiMmMaster : public XsiSimObj {
public:
    struct Op {
        bool write = false;
        uint64_t addr = 0;
        std::vector<uint64_t> wdata;      ///< write payload (one word per beat)
        uint32_t nwords = 0;              ///< read length
        long not_before = 0;              ///< earliest cycle the address phase may be presented
        uint64_t wstrb = ~(uint64_t)0;    ///< WSTRB on every beat (masked to the bus); a fault knob
        int size = -1;                    ///< AxSIZE override; -1 = the bus width.  A fault knob.
        long t_start = -1, t_end = -1;    ///< cycles (see class comment)
        uint32_t resp = 0;                ///< BRESP, or the worst RRESP over the burst
        std::vector<uint64_t> rdata;      ///< read result
        bool done() const { return t_end >= 0; }
    };

    /// *prefix* names the port group, e.g. "s0_axi" for ports "s0_axi_AWADDR" ...; *id* is driven on
    /// AWID/ARID when the port has them (a crossbar SI routes the response back by it).
    AxiMmMaster(Dut& d, const std::string& prefix, int bytes_per_word, uint32_t id = 0,
                bool overlap_rw = false)
        : d_(d), bpw_(bytes_per_word), id_(id), overlap_(overlap_rw) {
        size_ = 0; while ((1 << size_) < bpw_) ++size_;
        auto P = [&](const char* s) { return d.port((prefix + s).c_str()); };
        auto O = [&](const char* s) { return d.port_opt((prefix + s).c_str()); };
        P_awaddr = P("_AWADDR"); P_awlen = P("_AWLEN"); P_awvalid = P("_AWVALID"); P_awready = P("_AWREADY");
        P_wdata = P("_WDATA"); P_wlast = P("_WLAST"); P_wvalid = P("_WVALID"); P_wready = P("_WREADY");
        P_bvalid = P("_BVALID"); P_bready = P("_BREADY");
        P_araddr = P("_ARADDR"); P_arlen = P("_ARLEN"); P_arvalid = P("_ARVALID"); P_arready = P("_ARREADY");
        P_rdata = P("_RDATA"); P_rlast = P("_RLAST"); P_rvalid = P("_RVALID"); P_rready = P("_RREADY");
        P_wstrb = O("_WSTRB"); P_bresp = O("_BRESP"); P_rresp = O("_RRESP");
        P_awid = O("_AWID"); P_arid = O("_ARID");
        P_awsize = O("_AWSIZE"); P_arsize = O("_ARSIZE"); P_awburst = O("_AWBURST"); P_arburst = O("_ARBURST");
        P_awcache = O("_AWCACHE"); P_arcache = O("_ARCACHE");
        // Held-zero sidebands: present on a full AXI4 port, meaningless to these tests, and X if
        // left undriven -- which a crossbar would happily route into a decision.
        const char* zero[] = {"_AWLOCK", "_AWPROT", "_AWQOS", "_AWREGION", "_ARLOCK", "_ARPROT",
                              "_ARQOS", "_ARREGION", "_AWUSER", "_ARUSER", "_WUSER"};
        for (const char* z : zero) { int p = O(z); if (p >= 0) zero_.push_back(p); }
    }

    /// Queue a write burst of `words` at byte address `addr`.  Returns the op's index.
    size_t write(uint64_t addr, std::vector<uint64_t> words, long not_before = 0) {
        check_len(words.size());
        Op o; o.write = true; o.addr = addr; o.wdata = std::move(words); o.not_before = not_before;
        ops_.push_back(std::move(o)); return ops_.size() - 1;
    }
    /// Queue a read burst of `nwords` at byte address `addr`.  Returns the op's index.
    size_t read(uint64_t addr, uint32_t nwords, long not_before = 0) {
        check_len(nwords);
        Op o; o.write = false; o.addr = addr; o.nwords = nwords; o.not_before = not_before;
        ops_.push_back(std::move(o)); return ops_.size() - 1;
    }

    bool idle() const {
        if (!overlap_) return cur_ >= ops_.size();
        return !wact_ && !ract_ && next_of(true, wcur_) >= ops_.size() &&
               next_of(false, rcur_) >= ops_.size();
    }
    bool overlap_rw() const { return overlap_; }
    const Op& op(size_t i) const { return ops_[i]; }
    /// Mutable access, for the fault knobs (`wstrb`, `size`) on an op already queued.
    Op& op_mut(size_t i) { return ops_[i]; }
    size_t nops() const { return ops_.size(); }
    long cycle() const { return cycle_; }

    void sample() override {
        awready_ = d_.get1(P_awready); wready_ = d_.get1(P_wready);
        bvalid_ = d_.get1(P_bvalid);   bresp_ = (P_bresp >= 0) ? (uint32_t)(d_.getW(P_bresp) & 3) : 0;
        arready_ = d_.get1(P_arready); rvalid_ = d_.get1(P_rvalid);
        rdata_ = d_.getW(P_rdata);     rlast_ = d_.get1(P_rlast);
        rresp_ = (P_rresp >= 0) ? (uint32_t)(d_.getW(P_rresp) & 3) : 0;
        aw_beat_ = h_awvalid_ && awready_;
        w_beat_  = h_wvalid_ && wready_;
        b_beat_  = bvalid_ && h_bready_;
        ar_beat_ = h_arvalid_ && arready_;
        r_beat_  = rvalid_ && h_rready_;
    }

    void update() override {
        if (overlap_) { update_overlap(); return; }
        ++cycle_;
        if (active_) {
            Op& o = ops_[cur_];
            if (o.write) {
                if (aw_beat_) h_awvalid_ = 0;
                if (w_beat_) {
                    ++wbeat_;
                    if (wbeat_ >= o.wdata.size()) h_wvalid_ = 0;
                }
                if (b_beat_) {
                    if (h_awvalid_ || h_wvalid_) bad("B before the burst was fully sent");
                    o.resp = bresp_; finish(o);
                }
            } else {
                if (ar_beat_) h_arvalid_ = 0;
                if (r_beat_) {
                    if (h_arvalid_) bad("R before AR was accepted");
                    o.rdata.push_back(rdata_);
                    if (rresp_ > o.resp) o.resp = rresp_;
                    if (rlast_) {
                        if (o.rdata.size() != o.nwords) bad("RLAST on the wrong beat");
                        finish(o);
                    }
                }
            }
        }
        if (!active_ && cur_ < ops_.size() && cycle_ >= ops_[cur_].not_before) start(ops_[cur_]);
    }

    void drive() override {
        // One op on both channels (the default), or the write channel's op and the read channel's
        // op separately (overlap_rw).  Either way: AW/W from the write op, AR from the read op.
        const Op* o = active_ ? &ops_[cur_] : nullptr;
        const Op* wo = overlap_ ? (wact_ ? &ops_[wop_] : nullptr) : ((o && o->write) ? o : nullptr);
        const Op* ro = overlap_ ? (ract_ ? &ops_[rop_] : nullptr) : ((o && !o->write) ? o : nullptr);
        d_.putW(P_awaddr, wo ? wo->addr : 0);
        d_.putW(P_awlen, wo ? wo->wdata.size() - 1 : 0);
        d_.put1(P_awvalid, h_awvalid_);
        d_.putW(P_wdata, (wo && wbeat_ < wo->wdata.size()) ? wo->wdata[wbeat_] : 0);
        d_.put1(P_wlast, (wo && wbeat_ + 1 == wo->wdata.size()) ? 1u : 0u);
        d_.put1(P_wvalid, h_wvalid_);
        const uint64_t strb_all = (bpw_ >= 64) ? ~(uint64_t)0 : ((1ull << bpw_) - 1);
        const Op* so = overlap_ ? wo : o;      // the default model drives WSTRB from any active op
        if (P_wstrb >= 0) d_.putW(P_wstrb, (so ? so->wstrb : ~(uint64_t)0) & strb_all);
        d_.put1(P_bready, h_bready_);
        d_.putW(P_araddr, ro ? ro->addr : 0);
        d_.putW(P_arlen, ro ? ro->nwords - 1 : 0);
        d_.put1(P_arvalid, h_arvalid_);
        d_.put1(P_rready, h_rready_);
        if (P_awid >= 0) d_.putW(P_awid, id_);
        if (P_arid >= 0) d_.putW(P_arid, id_);
        if (overlap_) {
            if (P_awsize >= 0) d_.putW(P_awsize, (wo && wo->size >= 0) ? (uint64_t)wo->size : (uint64_t)size_);
            if (P_arsize >= 0) d_.putW(P_arsize, (ro && ro->size >= 0) ? (uint64_t)ro->size : (uint64_t)size_);
        } else {
            const uint64_t sz = (o && o->size >= 0) ? (uint64_t)o->size : (uint64_t)size_;
            if (P_awsize >= 0) d_.putW(P_awsize, sz);
            if (P_arsize >= 0) d_.putW(P_arsize, sz);
        }
        if (P_awburst >= 0) d_.putW(P_awburst, 1);   // INCR
        if (P_arburst >= 0) d_.putW(P_arburst, 1);
        if (P_awcache >= 0) d_.putW(P_awcache, 3);   // normal non-cacheable bufferable (AXI default)
        if (P_arcache >= 0) d_.putW(P_arcache, 3);
        for (int p : zero_) d_.putW(p, 0);
    }

private:
    void check_len(size_t n) {
        if (n < 1 || n > 256) { std::fprintf(stderr, "FATAL: AXI4 burst of %zu beats\n", n); std::exit(3); }
    }
    void bad(const char* why) {
        std::fprintf(stderr, "FATAL: AxiMmMaster op %zu: %s (cycle %ld)\n", cur_, why, cycle_);
        std::exit(4);
    }
    void start(Op& o) {
        active_ = true; wbeat_ = 0; o.t_start = cycle_ + 1;   // presented by the drive() after this
        if (o.write) { h_awvalid_ = 1; h_wvalid_ = 1; }
        else         { h_arvalid_ = 1; }
    }
    void finish(Op& o) { o.t_end = cycle_; active_ = false; ++cur_; }

    // -- overlap_rw: a write channel and a read channel, each one op at a time, in queue order ----
    /// The first op at or after *from* on the write (true) or read (false) channel.
    size_t next_of(bool write, size_t from) const {
        while (from < ops_.size() && ops_[from].write != write) ++from;
        return from;
    }
    void update_overlap() {
        ++cycle_;
        if (wact_) {
            Op& o = ops_[wop_];
            if (aw_beat_) h_awvalid_ = 0;
            if (w_beat_) { ++wbeat_; if (wbeat_ >= o.wdata.size()) h_wvalid_ = 0; }
            if (b_beat_) {
                if (h_awvalid_ || h_wvalid_) bad("B before the burst was fully sent");
                o.resp = bresp_; o.t_end = cycle_; wact_ = false;
            }
        }
        if (ract_) {
            Op& o = ops_[rop_];
            if (ar_beat_) h_arvalid_ = 0;
            if (r_beat_) {
                if (h_arvalid_) bad("R before AR was accepted");
                o.rdata.push_back(rdata_);
                if (rresp_ > o.resp) o.resp = rresp_;
                if (rlast_) {
                    if (o.rdata.size() != o.nwords) bad("RLAST on the wrong beat");
                    o.t_end = cycle_; ract_ = false;
                }
            }
        }
        if (!wact_) {
            wcur_ = next_of(true, wcur_);
            if (wcur_ < ops_.size() && cycle_ >= ops_[wcur_].not_before) {
                wop_ = wcur_++; wact_ = true; wbeat_ = 0;
                ops_[wop_].t_start = cycle_ + 1; h_awvalid_ = 1; h_wvalid_ = 1;
            }
        }
        if (!ract_) {
            rcur_ = next_of(false, rcur_);
            if (rcur_ < ops_.size() && cycle_ >= ops_[rcur_].not_before) {
                rop_ = rcur_++; ract_ = true;
                ops_[rop_].t_start = cycle_ + 1; h_arvalid_ = 1;
            }
        }
    }

    Dut& d_;
    int bpw_, size_;
    uint32_t id_;
    bool overlap_;
    std::vector<Op> ops_;
    size_t cur_ = 0, wbeat_ = 0;
    bool active_ = false;
    size_t wcur_ = 0, rcur_ = 0, wop_ = 0, rop_ = 0;   ///< overlap_rw only
    bool wact_ = false, ract_ = false;                ///< overlap_rw only
    long cycle_ = 0;
    int P_awaddr, P_awlen, P_awvalid, P_awready, P_wdata, P_wlast, P_wvalid, P_wready, P_bvalid,
        P_bready, P_araddr, P_arlen, P_arvalid, P_arready, P_rdata, P_rlast, P_rvalid, P_rready,
        P_wstrb, P_bresp, P_rresp, P_awid, P_arid, P_awsize, P_arsize, P_awburst, P_arburst,
        P_awcache, P_arcache;
    std::vector<int> zero_;
    uint32_t h_awvalid_ = 0, h_wvalid_ = 0, h_bready_ = 1, h_arvalid_ = 0, h_rready_ = 1;
    uint32_t awready_ = 0, wready_ = 0, bvalid_ = 0, bresp_ = 0, arready_ = 0, rvalid_ = 0,
             rlast_ = 0, rresp_ = 0;
    uint64_t rdata_ = 0;
    bool aw_beat_ = false, w_beat_ = false, b_beat_ = false, ar_beat_ = false, r_beat_ = false;
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
