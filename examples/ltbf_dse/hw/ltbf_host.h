#ifndef EXAMPLES_LTBF_DSE_HOST_H
#define EXAMPLES_LTBF_DSE_HOST_H
// ltbf_host.h -- the C++ twin of examples/ltbf_dse/hw/system.py::LtbfHost (HAND-WRITTEN, plan step
// 5.2, spec §5.7).  The same threads on the same endpoints: the dispatcher running the scheduler
// (hw/sched.py's static policy: each engine takes its jobs in the graphs' order, waiting for the next
// one's dependencies), the Resp FIFO's reader, the CPU workers, the ticker and the O-RU.  The
// decisions -- which job when, the ring credit, drops, the A53's charges -- are this file's; what the
// pysim host computes in Python comes from the scenario (system.scenario): the Python stages' and the
// A53 jobs' bus operations and A53 cycles, and the descriptors (the layout's arithmetic) in each
// engine's order, checked against the job this scheduler takes.
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "LtbfHost_endpoints.h"
#include "ltbf_ingest_status.h"
#include "ltbf_resp.h"
#include "xsi_sw_schema.h"

namespace wfbfm {

class LtbfHostModel : public LtbfHost_endpoints {
public:
    using LtbfHost_endpoints::LtbfHost_endpoints;

    // ~ LtbfHost.main: the dispatcher.
    void main() override {
        parse();
        start("resp", [this] { resp_reader(); });
        start("oru", [this] { oru(); });
        for (int k = 0; k < n_cores_; ++k) start("worker" + std::to_string(k), [this, k] { worker(k); });
        run_host(setup_, 0);
        ing_ctrl.write(icfg_);
        add(0, n_lt_);
        pump();
        bool rt_added = false;
        while (!(rt_added && finished())) {
            const Ev ev = inbox_.get();
            if (ev.kind == RESP) {
                on_resp((int)ev.a, (int)ev.b, (long)ev.c, (long)ev.d);
                if (error_) fatal("engine error: job %ld status %ld", err_gid_, err_status_);
            } else if (ev.kind == CPU_DONE) {
                complete((long)ev.a);
            } else if (ev.kind == TICK && idle() && !finished()) {
                // Nothing moves but arrivals: a drop after the last chunk seen shows only here.
                const IngestStatus st = decode_words<IngestStatus, 64>(ing_status.read());
                on_status((long)st.chunks.to_uint64(), (long)st.overruns.to_uint64());
            }
            bool lt_done = true;
            for (long g = 0; g < n_lt_; ++g) lt_done = lt_done && state_[g] == DONE;
            if (!rt_added && lt_done) {
                rt_added = true;
                add(n_lt_, n_rt_);
                slot_start_.set();
                start("ticker", [this] { ticker(); });
            }
            pump();
        }
        done_.set();
        t_done_ = now();
    }

    void report() override {
        std::printf("LTBF end=%ld dispatched_sys=%zu dispatched_vec=%zu overruns=%ld dropped=%ld "
                    "credit=%ld\n", t_done_, pos_[SYS], pos_[VEC], overruns_, dropped_, credit_);
    }

private:
    enum Engine { SYS = 0, VEC = 1, PY = 2, CPU = 3, INGEST = 4 };
    enum State { WAIT, READY, RUN, DONE, DROPPED };
    enum EvKind { RESP, CPU_DONE, TICK };
    enum Kind { K_HEAD, K_RELEASE, K_JOB, K_CHUNKS, K_DESC, K_HOST, K_ICFG, K_SYM };
    static constexpr long SEQ_MOD = 1L << 16;
    static constexpr uint64_t NO_GID = ~0ull;

    struct Job {
        int engine = 0;
        long pending = 0, left = 0;
        std::vector<long> succ, dsucc, roots;
    };
    struct Ev {
        int kind;
        uint64_t a, b, c, d;
    };
    struct Desc {
        long gid;
        std::vector<uint64_t> words;
    };

    [[noreturn]] static void fatal(const char* fmt, long a = 0, long b = 0) {
        std::fprintf(stderr, "FATAL: ltbf host: ");
        std::fprintf(stderr, fmt, a, b);
        std::fprintf(stderr, "\n");
        std::exit(7);
    }
    static double f64(uint64_t bits) {
        double x;
        std::memcpy(&x, &bits, sizeof x);
        return x;
    }

    // -- the scenario --------------------------------------------------------------------------

    void parse() {
        for (const std::vector<uint64_t>& b : scenario_bursts()) {
            switch (b.at(0)) {
                case K_HEAD:
                    n_cores_ = (int)b[1], ring_depth_ = (long)b[2], n_chunks_ = (long)b[3];
                    cores_free_ = n_cores_;
                    max_in_flight_ = (size_t)b[4], cap_[SYS] = (long)b[5], cap_[VEC] = (long)b[6];
                    credit_addr_ = b[7], n_rx_ = (long)b[8], n_sym_ = (long)b[9];
                    sym_period_ = (long)b[10], tick_ = (long)b[11], n_lt_ = (long)b[12];
                    n_rt_ = (long)b[13], f_host_ = f64(b[14]), f_cpu_ = f64(b[15]);
                    switch_ = f64(b[16]), p_dispatch_ = f64(b[17]);  // b[18]: Ctrl, never written
                    jobs_.assign(n_lt_ + n_rt_, Job());
                    state_.assign(n_lt_ + n_rt_, WAIT);
                    break;
                case K_RELEASE: {
                    const size_t n = (size_t)b[1];
                    if (p_release_.size() <= n) p_release_.resize(n + 1);
                    for (size_t k = 0; k <= n; ++k) p_release_[n].push_back(f64(b[2 + k]));
                    break;
                }
                case K_JOB: {
                    size_t i = 1;
                    Job& j = jobs_.at(b[i++]);
                    j.engine = (int)b[i++];
                    j.pending = (long)b[i++];
                    for (int list = 0; list < 3; ++list) {
                        std::vector<long>& v = list == 0 ? j.succ : list == 1 ? j.dsucc : j.roots;
                        const size_t n = (size_t)b[i++];
                        for (size_t k = 0; k < n; ++k) v.push_back((long)b[i++]);
                    }
                    j.left = (long)b[i++];
                    break;
                }
                case K_CHUNKS:
                    for (size_t k = 1; k < b.size(); ++k) chunk_order_.push_back((long)b[k]);
                    break;
                case K_DESC:
                    desc_[b[1]].push_back(Desc{(long)b[2], std::vector<uint64_t>(b.begin() + 3, b.end())});
                    break;
                case K_HOST:
                    if (b[1] == NO_GID) setup_ = b;
                    else host_ops_[(long)b[1]] = b;
                    break;
                case K_ICFG:
                    icfg_.assign(b.begin() + 1, b.end());
                    break;
                case K_SYM:
                    symbols_.emplace_back(b.begin() + 1, b.end());
                    break;
                default:
                    fatal("scenario burst kind %ld", (long)b[0]);
            }
        }
        if ((long)symbols_.size() != n_sym_ * n_rx_) fatal("%ld symbol bursts for %ld", (long)symbols_.size(), n_sym_ * n_rx_);
    }

    // -- the scheduler (hw/sched.py, the static policy) -----------------------------------------

    void add(long base, long n) {
        added_ += n;
        for (long g = base; g < base + n; ++g)
            if (jobs_[g].engine != INGEST) order_[jobs_[g].engine].push_back(g);
        if (base > 0)  // the real-time graph: its chunk roots, in arrival order
            for (long c : chunk_order_) chunks_.push_back(c);
        for (long g = base; g < base + n; ++g)
            if (jobs_[g].pending == 0 && jobs_[g].engine != INGEST) state_[g] = READY;
    }

    long next(int e) {
        std::deque<long>& q = order_[e];
        while (!q.empty() && state_[q.front()] == DROPPED) q.pop_front();
        return (!q.empty() && state_[q.front()] == READY) ? q.front() : -1;
    }

    /// The engine's jobs to write now (at most room, within flow control), charged as dispatched.
    std::vector<std::pair<long, long> > take(int e, long room) {
        std::vector<std::pair<long, long> > out;
        while (!error_ && room > 0) {
            if (in_flight_.size() >= max_in_flight_) break;
            const long gid = next(e);
            if (gid < 0) break;
            order_[e].pop_front();
            while (in_flight_.count(key(e, tx_[e]))) tx_[e] = (tx_[e] + 1) % SEQ_MOD;
            const long tx = tx_[e];
            tx_[e] = (tx + 1) % SEQ_MOD;
            in_flight_[key(e, tx)] = gid;
            state_[gid] = RUN;
            trace_.push_back(p_dispatch_);
            out.emplace_back(gid, tx);
            --room;
        }
        return out;
    }

    long take_host() {
        if (error_) return -1;
        for (int e : {PY, CPU}) {
            const long gid = next(e);
            if (gid >= 0) {
                order_[e].pop_front();
                state_[gid] = RUN;
                return gid;
            }
        }
        return -1;
    }

    void on_resp(int src, int status, long tx, long seq) {
        if (src == 2) {  // records.SRC_INGEST
            on_chunk(seq);
            return;
        }
        const auto it = in_flight_.find(key(src, tx));
        if (it == in_flight_.end()) fatal("a Resp from source %ld for tx %ld not in flight", src, tx);
        const long gid = it->second;
        in_flight_.erase(it);
        if (status) {
            error_ = true, err_gid_ = gid, err_status_ = status;
            return;
        }
        complete(gid);
    }

    void on_chunk(long seq) {
        const long gap = ((seq - next_seq_) % SEQ_MOD + SEQ_MOD) % SEQ_MOD;
        if (gap % n_chunks_) fatal("chunk seq %ld after %ld: not whole symbols", seq, next_seq_ - 1);
        for (long k = 0; k < gap; ++k) drop(pop_chunk());
        overruns_ += gap / n_chunks_;
        next_seq_ = (seq + 1) % SEQ_MOD;
        const long gid = pop_chunk();
        slot_of_[gid] = arrived_++;
        state_[gid] = RUN;
        complete(gid);
    }

    void on_status(long chunks, long overruns) {
        if (chunks != arrived_) return;  // records still on their way; their seq gaps will tell
        for (long k = overruns_; k < overruns; ++k) {
            for (long c = 0; c < n_chunks_; ++c)
                if (!chunks_.empty()) drop(pop_chunk());
            next_seq_ = (next_seq_ + n_chunks_) % SEQ_MOD;
            ++overruns_;
        }
    }

    long pop_chunk() {
        if (chunks_.empty()) fatal("a chunk record with no chunk root left");
        const long g = chunks_.front();
        chunks_.pop_front();
        return g;
    }

    void complete(long gid) {
        std::vector<long> work{gid};
        while (!work.empty()) {
            const long g = work.back();
            work.pop_back();
            for (long s : job_done(g)) {
                if (skip_.count(s)) work.push_back(s);
                else state_[s] = READY;
            }
        }
    }

    std::vector<long> job_done(long gid) {
        Job& j = jobs_[gid];
        const bool noop = skip_.count(gid) != 0;
        state_[gid] = noop ? DROPPED : DONE;
        if (noop) ++dropped_;
        for (long r : j.roots)
            if (--jobs_[r].left == 0) retire(r);
        std::vector<long> ready;
        for (long s : j.succ)
            if (--jobs_[s].pending == 0) ready.push_back(s);
        trace_.push_back(p_release_.at(j.succ.size()).at(ready.size()));
        if (j.engine == INGEST && j.left == 0) retire(gid);  // a chunk nobody reads
        return ready;
    }

    void retire(long chunk) {
        const auto it = slot_of_.find(chunk);
        if (it == slot_of_.end()) return;  // a chunk the DMA dropped holds no slot
        retired_.insert(it->second);
        while (retired_.count(credit_)) {
            retired_.erase(credit_);
            ++credit_;
        }
    }

    void drop(long gid) {
        std::vector<long> stack{gid};
        while (!stack.empty()) {
            const long g = stack.back();
            stack.pop_back();
            if (skip_.count(g)) continue;
            skip_.insert(g);
            for (long s : jobs_[g].dsucc) stack.push_back(s);
        }
        complete(gid);
    }

    bool finished() const {
        for (long g = 0; g < added_; ++g)
            if (state_[g] != DONE && state_[g] != DROPPED) return false;
        return true;
    }

    bool idle() {
        if (!in_flight_.empty()) return false;
        for (int e : {SYS, VEC, PY, CPU})
            if (next(e) >= 0) return false;
        return true;
    }

    static long key(long e, long tx) { return e * SEQ_MOD + tx; }

    // -- the dispatcher's pump (LtbfHost._pump) -------------------------------------------------

    void pump() {
        for (;;) {
            const long gid = take_host();
            if (gid < 0) break;
            charge_trace();
            if (jobs_[gid].engine == PY) {
                run_host(host_ops_.at(gid), 0);
                complete(gid);
            } else {
                cpu_q_.put(gid);
            }
        }
        for (int e : {SYS, VEC}) {
            long room = cap_[e];
            for (const auto& f : in_flight_)
                if (f.first / SEQ_MOD == e) --room;
            for (const auto& act : take(e, room > 0 ? room : 0)) {
                if (pos_[e] >= desc_[e].size() || desc_[e][pos_[e]].gid != act.first)
                    fatal("engine %ld: job %ld is not the one pysim dispatched here", e, act.first);
                (e == SYS ? qjob_sys : qjob_vec).write(desc_[e][pos_[e]++].words);
            }
        }
        if (credit_ != credit_sent_) {
            credit_sent_ = credit_;
            bus.write(credit_addr_, std::vector<uint64_t>{(uint64_t)credit_});
        }
        charge_trace();
    }

    void charge_trace() {
        while (traced_ < trace_.size()) charge(trace_[traced_++], 0);
    }

    // -- the A53 (LtbfHost.charge: the Processor's switch rule, whole host cycles) ----------------

    /// A call of *a53_cycles* by thread *owner*: wait for a core, pay a switch if the core last ran
    /// another thread (or none yet), then the call, rounded up to whole host cycles.  The owner a
    /// core holds is tracked for one core (AC9's); with more, any free core is taken.
    void charge(double a53_cycles, int owner) {
        sched_.wait_until([this] { return cores_free_ > 0; });
        --cores_free_;
        const double c = a53_cycles + (last_owner_ == owner ? 0.0 : switch_);
        last_owner_ = owner;
        compute((long)std::ceil(c * f_host_ / f_cpu_ - 1e-9));
        ++cores_free_;
    }

    /// A host job's recorded bus operations and charges, in order (the A53's writes under gp).
    void run_host(const std::vector<uint64_t>& b, int owner) {
        bool charged = false, locked = false;
        size_t i = 2;
        while (i < b.size()) {
            const uint64_t op = b[i++];
            if (op == 0) {
                const uint64_t addr = b[i++];
                bus.read(addr * 8, (uint32_t)b[i++]);
            } else if (op == 1) {
                const uint64_t addr = b[i++];
                const size_t n = (size_t)b[i++];
                if (charged && !locked) gp_lock_.acquire(), locked = true;
                bus.write(addr * 8, std::vector<uint64_t>(b.begin() + i, b.begin() + i + n));
                i += n;
            } else {
                charge(f64(b[i++]), owner);
                charged = true;
            }
        }
        if (locked) gp_lock_.release();
    }

    // -- the other threads ----------------------------------------------------------------------

    /// ~ LtbfHost._resp_reader, ending with the run: the queue's data interrupt is armed for one
    /// record (the line rises only once armed), then the record or the run's end, whichever first.
    void resp_reader() {
        for (;;) {
            SwIrq& irq = qresp.data_irq(1);
            if (wait_any(sched_, {[&irq] { return irq.level(); }, [this] { return done_.is_set(); }}) == 1) return;
            const Resp r = decode_words<Resp, 64>(qresp.pop(1));
            inbox_.put(Ev{RESP, r.src.to_uint64(), r.status.to_uint64(), r.tx_id.to_uint64(),
                          r.seq.to_uint64()});
        }
    }

    void worker(int k) {
        for (;;) {
            if (wait_any(sched_, {[this] { return cpu_q_.ready(); }, [this] { return done_.is_set(); }}) == 1) return;
            const long gid = cpu_q_.get();
            run_host(host_ops_.at(gid), 1 + k);
            inbox_.put(Ev{CPU_DONE, (uint64_t)gid, 0, 0, 0});
        }
    }

    void ticker() {
        while (!done_.is_set()) {
            const long until = now() + tick_;
            sched_.wait_until([this, until] { return now() >= until || done_.is_set(); });
            if (!done_.is_set()) inbox_.put(Ev{TICK, 0, 0, 0, 0});
        }
    }

    /// The O-RU stand-in: once the slot is released, a symbol every sym_period, an antenna a packet.
    void oru() {
        slot_start_.wait();
        const long t0 = now();
        for (long s = 0; s < n_sym_; ++s) {
            const long at = t0 + (s + 1) * sym_period_;
            sched_.wait_until([this, at] { return now() >= at; });
            for (long a = 0; a < n_rx_; ++a) qsym.write(symbols_[s * n_rx_ + a]);
        }
    }

    // -- state ----------------------------------------------------------------------------------

    int n_cores_ = 1;
    long ring_depth_ = 1, n_chunks_ = 1, n_rx_ = 0, n_sym_ = 0, sym_period_ = 0, tick_ = 0;
    long n_lt_ = 0, n_rt_ = 0, added_ = 0;
    size_t max_in_flight_ = 1;
    long cap_[2] = {1, 1};
    uint64_t credit_addr_ = 0;
    double f_host_ = 1, f_cpu_ = 1, switch_ = 0, p_dispatch_ = 0;
    std::vector<std::vector<double> > p_release_;
    std::vector<Job> jobs_;
    std::vector<int> state_;
    std::deque<long> order_[4];
    std::deque<long> chunks_;
    std::vector<long> chunk_order_;
    std::map<long, long> in_flight_;
    long tx_[2] = {0, 0};
    std::vector<Desc> desc_[2];
    size_t pos_[2] = {0, 0};
    std::map<long, std::vector<uint64_t> > host_ops_;
    std::vector<uint64_t> setup_, icfg_;
    std::vector<std::vector<uint64_t> > symbols_;
    std::set<long> skip_, retired_;
    std::map<long, long> slot_of_;
    long next_seq_ = 0, arrived_ = 0, credit_ = 0, credit_sent_ = 0, overruns_ = 0, dropped_ = 0;
    bool error_ = false;
    long err_gid_ = 0, err_status_ = 0;
    std::vector<double> trace_;
    size_t traced_ = 0;
    int last_owner_ = -1;
    long t_done_ = 0;
    long cores_free_ = 1;
    SwLock gp_lock_{sched_};
    SwEvent slot_start_{sched_}, done_{sched_};
    SwQueue<Ev> inbox_{sched_};
    SwQueue<long> cpu_q_{sched_};
};

}  // namespace wfbfm

#endif  // EXAMPLES_LTBF_DSE_HOST_H
