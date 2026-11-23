// A minimal stand-in for Vitis' ap_int.h, up to 128 bits: what a stream block's body uses -- construction,
// comparison, increment, conversion and range() -- for a g++ check of its logic only.
#pragma once
typedef unsigned __int128 ap_u128;
template <int W> struct ap_uint;
template <int W> struct ap_range_ref {
    ap_uint<W>& v; int hi, lo;
    ap_u128 mask() const { int n = hi - lo + 1; return n >= 128 ? ~ap_u128(0) : ((ap_u128(1) << n) - 1); }
    operator ap_u128() const { return (v.x >> lo) & mask(); }
    template <int V> ap_range_ref& operator=(const ap_uint<V>& a) {
        v.x = (v.x & ~(mask() << lo)) | ((ap_u128(a.x) & mask()) << lo); v.trim(); return *this;
    }
};
template <int W> struct ap_uint {
    ap_u128 x = 0;
    void trim() { if (W < 128) x &= (ap_u128(1) << W) - 1; }
    ap_uint() {}
    ap_uint(int a) : x(ap_u128((long long)a)) { trim(); }
    ap_uint(unsigned a) : x(a) { trim(); }
    ap_uint(unsigned long long a) : x(a) { trim(); }
    ap_uint(bool a) : x(a ? 1 : 0) {}
    template <int V> ap_uint(const ap_uint<V>& a) : x(a.x) { trim(); }
    template <int V> ap_uint(const ap_range_ref<V>& r) : x(ap_u128(r)) { trim(); }
    operator unsigned long long() const { return (unsigned long long)x; }
    ap_range_ref<W> range(int hi, int lo) { return {*this, hi, lo}; }
    ap_uint& operator++() { ++x; trim(); return *this; }
    template <int V> bool operator==(const ap_uint<V>& a) const { return x == a.x; }
    template <int V> bool operator!=(const ap_uint<V>& a) const { return x != a.x; }
    template <int V> bool operator<(const ap_uint<V>& a) const { return x < a.x; }
    bool operator==(int a) const { return x == ap_u128((long long)a); }
    bool operator!=(int a) const { return !(*this == a); }
    bool operator>=(int a) const { return x >= ap_u128((long long)a); }
};
