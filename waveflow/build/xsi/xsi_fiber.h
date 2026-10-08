#ifndef WAVEFLOW_XSI_FIBER_H
#define WAVEFLOW_XSI_FIBER_H
// xsi_fiber.h — software threads for an XSI testbench: fibers and their scheduler
// (plans/host_runtime.md).  Standard library and the OS's fiber API only -- no xsi.h -- so the
// scheduler is compiled and run by a unit test with a plain g++ (tests/build/test_xsi_fiber.py).
//
// A FIBER is a thread the program schedules itself: its own stack, but it runs only when code
// switches to it, and a switch is a user-mode register swap (no OS scheduler).  It is the C++ form of
// a SimPy process.  Windows provides fibers (CreateFiber / SwitchToFiber); elsewhere ucontext
// (makecontext / swapcontext).  Fiber hides the two.
//
// THE SCHEDULER (SwScheduler) is SimPy's discipline: one thread runs at a time.  A blocking call
// records what the thread waits for (a predicate) and switches back to the scheduler, which runs on
// the testbench's own (main) fiber.  Once per cycle tick() walks the threads in START ORDER: for each,
// it first steps the endpoints that thread uses (their state machines do the cycle work), then, if
// the thread's wait is satisfied, switches into it; the thread runs until its next blocking call.  A
// wait already satisfied when called returns without a switch.  Nothing else runs code, so the run is
// deterministic by construction.
//
// Per-thread stepping, in start order, is what keeps a thread's bus operations in the cycle and the
// order a hand-written state machine would issue them: each thread's endpoints are stepped just
// before that thread acts, as each hand-written process stepped its own endpoints in its update().
//
// Rule: only the scheduler's fiber (the testbench) may call the XSI API.  A thread's blocking call
// queues work on an endpoint; the endpoint's step(), called from tick(), touches the bus model.
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#else
#include <ucontext.h>
#endif

namespace wfbfm {

// ---------------------------------------------------------------------------
// Fiber -- one stack, switched to and from the scheduler's (main) fiber.
// ---------------------------------------------------------------------------

class Fiber {
public:
    static constexpr size_t STACK = 256 * 1024;

    explicit Fiber(std::function<void()> body) : body_(std::move(body)) {
#ifdef _WIN32
        main_fiber();                                       // the caller's thread becomes a fiber
        h_ = CreateFiber(STACK, &Fiber::entry, this);
        if (!h_) die("CreateFiber");
#else
        stack_.resize(STACK);
        if (getcontext(&ctx_) != 0) die("getcontext");
        ctx_.uc_stack.ss_sp = stack_.data();
        ctx_.uc_stack.ss_size = stack_.size();
        ctx_.uc_link = nullptr;
        makecontext(&ctx_, reinterpret_cast<void (*)()>(&Fiber::entry_ucontext), 0);
#endif
    }
    ~Fiber() {
#ifdef _WIN32
        if (h_) DeleteFiber(h_);
#endif
    }
    Fiber(const Fiber&) = delete;
    Fiber& operator=(const Fiber&) = delete;

    bool done() const { return done_; }

    /// From the scheduler's fiber: run this fiber until it yields or finishes.
    void resume() {
        Fiber* prev = current();
        current() = this;
#ifdef _WIN32
        SwitchToFiber(h_);
#else
        swapcontext(&main_ctx(), &ctx_);
#endif
        current() = prev;
    }

    /// From inside a fiber: give control back to the scheduler.
    static void yield() {
        Fiber* f = current();
        if (!f) die("Fiber::yield outside a fiber");
#ifdef _WIN32
        SwitchToFiber(main_fiber());
#else
        swapcontext(&f->ctx_, &main_ctx());
#endif
    }

    /// The fiber running now, or nullptr on the scheduler's fiber.
    static Fiber*& current() { static Fiber* c = nullptr; return c; }

private:
    void run() {
        body_();
        done_ = true;
        for (;;) yield();                                   // a fiber must never return
    }
#ifdef _WIN32
    static void WINAPI entry(LPVOID self) { static_cast<Fiber*>(self)->run(); }
    static LPVOID main_fiber() {
        static LPVOID m = nullptr;
        if (!m) {
            m = ConvertThreadToFiber(nullptr);
            if (!m) m = GetCurrentFiber();                  // already a fiber
        }
        return m;
    }
    LPVOID h_ = nullptr;
#else
    static void entry_ucontext() { current()->run(); }
    static ucontext_t& main_ctx() { static ucontext_t m; return m; }
    ucontext_t ctx_;
    std::vector<char> stack_;
#endif
    static void die(const char* what) {
        std::fprintf(stderr, "FATAL: fiber: %s failed\n", what);
        std::exit(6);
    }

    std::function<void()> body_;
    bool done_ = false;
};

// ---------------------------------------------------------------------------
// The scheduler
// ---------------------------------------------------------------------------

/// Something a thread uses that does per-cycle work (an endpoint): stepped by tick() just before
/// the thread that uses it.
class SwStepper {
public:
    virtual ~SwStepper() = default;
    virtual void step() = 0;
};

class SwScheduler {
public:
    struct Thread {
        std::string name;
        std::unique_ptr<Fiber> fiber;
        std::function<bool()> wait;           // empty: ready
        std::vector<SwStepper*> steppers;
    };

    /// Start a thread; it first runs at the next tick() (a SimPy process started now also runs after
    /// the code that started it yields).
    void start(const std::string& name, std::function<void()> body) {
        threads_.push_back(std::unique_ptr<Thread>(new Thread{name, std::unique_ptr<Fiber>(), {}, {}}));
        threads_.back()->fiber.reset(new Fiber(std::move(body)));
    }

    /// Block the calling thread until pred() holds -- at once, with no switch, if it already does.
    void wait_until(std::function<bool()> pred) {
        if (pred()) return;
        Thread* t = running();
        t->wait = std::move(pred);
        Fiber::yield();
    }

    /// Block the calling thread for n ticks.
    void wait_ticks(long n) {
        if (n <= 0) return;
        const long until = ticks_ + n;
        wait_until([this, until] { return ticks_ >= until; });
    }

    /// Record that the running thread uses *s*: from now on s is stepped just before that thread.
    void uses(SwStepper* s) {
        Thread* t = running();
        for (SwStepper* x : t->steppers) if (x == s) return;
        for (auto& o : threads_)
            for (SwStepper* x : o->steppers)
                if (x == s) {
                    std::fprintf(stderr, "FATAL: an endpoint used by thread '%s' is also used by '%s'; "
                                 "one endpoint belongs to one thread\n", o->name.c_str(), t->name.c_str());
                    std::exit(6);
                }
        t->steppers.push_back(s);
    }

    /// One cycle: for each thread in start order, step its endpoints, then run it if its wait holds.
    /// Threads started during the tick run in the same tick, after the ones before them.
    ///
    /// Then **settle**: a thread can satisfy another's wait (set a flag, release a slot) after the
    /// pass has already looked at that other thread.  In SimPy the waiter runs at the same instant, so
    /// here it runs in the same tick -- further passes, in start order, run every thread whose wait
    /// now holds, until none does.  Endpoints are not stepped again: hardware moves once per cycle,
    /// software events take no time.
    void tick() {
        ++ticks_;
        for (size_t i = 0; i < threads_.size(); ++i) {
            Thread* t = threads_[i].get();
            if (t->fiber->done()) continue;
            for (SwStepper* s : t->steppers) s->step();
            try_run(t);
        }
        for (bool ran = true; ran;) {
            ran = false;
            for (size_t i = 0; i < threads_.size(); ++i) ran |= try_run(threads_[i].get());
        }
    }

    bool all_done() const {
        for (auto& t : threads_) if (!t->fiber->done()) return false;
        return !threads_.empty();
    }
    long ticks() const { return ticks_; }
    size_t size() const { return threads_.size(); }

private:
    /// Run *t* if it is alive and its wait holds; true if it ran.
    bool try_run(Thread* t) {
        if (t->fiber->done() || (t->wait && !t->wait())) return false;
        t->wait = nullptr;
        running_ = t;
        t->fiber->resume();
        running_ = nullptr;
        return true;
    }

    Thread* running() {
        if (!running_) {
            std::fprintf(stderr, "FATAL: a blocking call made outside a software thread\n");
            std::exit(6);
        }
        return running_;
    }

    std::vector<std::unique_ptr<Thread> > threads_;
    Thread* running_ = nullptr;
    long ticks_ = 0;
};

}  // namespace wfbfm

#endif  // WAVEFLOW_XSI_FIBER_H
