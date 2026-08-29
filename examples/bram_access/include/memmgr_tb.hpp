#ifndef WAVEFLOW_BUILD_MEMMGR_TB_HPP
#define WAVEFLOW_BUILD_MEMMGR_TB_HPP

#include "memmgr.hpp"

#include <map>
#include <stdexcept>

namespace waveflow {
namespace memmgr {

/**
 * @brief Fixed-size first-fit memory allocator over an existing word buffer.
 *
 * This testbench-only helper tracks contiguous allocations inside the caller-
 * provided ``mem`` buffer, expressed in words of width ``word_dwidth``.
 * Allocation returns the starting word index of a segment and ``free``
 * releases a segment only by that exact start index, mirroring the Python
 * ``Memory.alloc`` / ``Memory.free`` model.
 */
template <int word_dwidth>
class MemMgr {
public:
    using word_type = ap_uint<word_dwidth>;

    /**
     * @brief Construct a manager over an existing word buffer.
     *
     * @param mem Pointer to the underlying word-addressable memory.
     * @param total_nwords Total capacity of ``mem`` in words.
     *
     * @throws std::invalid_argument If ``mem`` is null or ``total_nwords`` is
     * not positive.
     */
    MemMgr(word_type* mem, int total_nwords);

    /**
     * @brief Allocate a contiguous segment using first-fit placement.
     *
     * @param nwords Number of words to allocate.
     * @return Starting word index of the allocated segment.
     *
     * The allocated range is zero-initialized before being returned.
     *
     * @throws std::invalid_argument If ``nwords`` is not positive.
     * @throws std::runtime_error If no contiguous segment of the requested size
     * fits within the managed buffer.
     */
    int alloc(int nwords);

    /**
     * @brief Free a previously allocated segment by its starting word index.
     *
     * @param addr Starting word index originally returned by ``alloc``.
     *
     * @throws std::out_of_range If ``addr`` is not the start of a live segment.
     */
    void free(int addr);

    /**
     * @brief Return the managed raw memory pointer.
     */
    word_type* data() const;

    /**
     * @brief Return the total managed capacity in words.
     */
    int total_nwords() const;

private:
    word_type* mem_;
    int total_nwords_;
    std::map<int, int> segments_;
};


template <int word_dwidth>
MemMgr<word_dwidth>::MemMgr(word_type* mem, int total_nwords)
    : mem_(mem), total_nwords_(total_nwords) {
    if (mem_ == nullptr) {
        throw std::invalid_argument("MemMgr requires a non-null memory pointer.");
    }
    if (total_nwords_ <= 0) {
        throw std::invalid_argument("MemMgr requires total_nwords to be positive.");
    }
}


template <int word_dwidth>
int MemMgr<word_dwidth>::alloc(int nwords) {
    if (nwords <= 0) {
        throw std::invalid_argument("MemMgr::alloc requires nwords to be positive.");
    }

    int next_free = 0;
    for (typename std::map<int, int>::const_iterator it = segments_.begin(); it != segments_.end(); ++it) {
        const int start = it->first;
        const int len = it->second;
        if (next_free + nwords <= start) {
            break;
        }
        const int end = start + len;
        if (end > next_free) {
            next_free = end;
        }
    }

    if (next_free + nwords > total_nwords_) {
        throw std::runtime_error("MemMgr::alloc could not find a contiguous segment.");
    }

    segments_[next_free] = nwords;
    for (int i = 0; i < nwords; ++i) {
        mem_[next_free + i] = 0;
    }
    return next_free;
}


template <int word_dwidth>
void MemMgr<word_dwidth>::free(int addr) {
    typename std::map<int, int>::iterator it = segments_.find(addr);
    if (it == segments_.end()) {
        throw std::out_of_range("MemMgr::free requires a valid segment start address.");
    }
    segments_.erase(it);
}


template <int word_dwidth>
typename MemMgr<word_dwidth>::word_type* MemMgr<word_dwidth>::data() const {
    return mem_;
}


template <int word_dwidth>
int MemMgr<word_dwidth>::total_nwords() const {
    return total_nwords_;
}

}  // namespace memmgr
}  // namespace waveflow

#endif // WAVEFLOW_BUILD_MEMMGR_TB_HPP