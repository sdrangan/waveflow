// A minimal stand-in for Vitis' hls_stream.h: a g++ check of a stream block's body logic only -- not
// Vitis csim, and nothing about timing or synthesis (tests/streams/test_dispatch.py).
#pragma once
#include <cstdio>
#include <cstdlib>
#include <deque>
namespace hls {
template <class T> class stream {
    std::deque<T> q;
public:
    T read() {
        if (q.empty()) { std::fprintf(stderr, "read on an empty stream\n"); std::abort(); }
        T v = q.front(); q.pop_front(); return v;
    }
    void write(const T& v) { q.push_back(v); }
    bool empty() const { return q.empty(); }
    size_t size() const { return q.size(); }
};
}  // namespace hls
