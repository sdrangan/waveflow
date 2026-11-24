// Minimal stand-in for Vitis' hls_stream.h, for a g++ check of the task bodies only (not Vitis csim).
#pragma once
#include <deque>
#include <initializer_list>
#include <cstdio>
#include <cstdlib>
namespace hls {
template <class T> class stream {
    std::deque<T> q;
public:
    stream() {}
    stream(const char*) {}
    T read() { if (q.empty()) { std::fprintf(stderr, "read on empty stream\n"); std::abort(); } T v = q.front(); q.pop_front(); return v; }
    void read(T& v) { v = read(); }
    bool read_nb(T& v) { if (q.empty()) return false; v = read(); return true; }
    void write(const T& v) { q.push_back(v); }
    bool write_nb(const T& v) { write(v); return true; }
    bool empty() const { return q.empty(); }
    bool full() const { return false; }
    size_t size() const { return q.size(); }
};
struct fence_any { template <class T> fence_any(const T&) {} };
inline void fence(std::initializer_list<fence_any>, std::initializer_list<fence_any>) {}
}
