#pragma once

#include "strata/core/progress.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <initializer_list>
#include <memory>
#include <utility>
#include <vector>

#if defined(__linux__)
#include <sys/mman.h>
#endif

namespace strata::core {

// Snapshot-owned host storage. Appending never relocates existing payloads;
// only the small segment directory can move. No sharing or copy-on-write is
// needed because a parked image and its retained active buffers have one owner.
class ConversationBuffer {
public:
    static constexpr size_t segment_bytes = 16 * 1024 * 1024;
    ConversationBuffer() = default;
    ConversationBuffer(std::initializer_list<uint8_t> values) {
        resize(values.size());
        visit(0, size(), [&](uint8_t* p, size_t n, size_t at) {
            std::memcpy(p, values.begin() + at, n);
            return true;
        });
    }
    ConversationBuffer(const ConversationBuffer&) = default;
    ConversationBuffer& operator=(const ConversationBuffer&) = default;
    ConversationBuffer(ConversationBuffer&& other) noexcept
        : segments_(std::move(other.segments_)), size_(std::exchange(other.size_, 0)) {}
    ConversationBuffer& operator=(ConversationBuffer&& other) noexcept {
        if (this == &other) return *this;
        segments_ = std::move(other.segments_);
        size_ = std::exchange(other.size_, 0);
        return *this;
    }

    size_t size() const { return size_; }
    bool empty() const { return size_ == 0; }
    size_t bytes() const {
        size_t n = segments_.capacity() * sizeof(Segment);
        for (const auto& segment : segments_) n += segment.capacity();
        return n;
    }
    // Includes the old directory while reserve allocates its replacement.
    // Saturation makes an overflowing estimate fail admission before resize.
    size_t allocation_peak(size_t n) const {
        size_t total = bytes();
        if (n <= size_) return total;
        const size_t spare = segments_.empty() ? 0 : segments_.back().capacity() - segments_.back().size();
        const size_t extra = n - size_ - std::min(n - size_, spare);
        const size_t full = extra / segment_bytes, tail = extra % segment_bytes;
        if (!add(total, full * segment_bytes)) return SIZE_MAX;
        if (tail && !add(total, segment_capacity(tail, full ? segment_bytes :
                segments_.empty() ? 0 : segments_.back().capacity(), size_ != 0, size_))) return SIZE_MAX;
        size_t count = segments_.size();
        if (!add(count, full + (tail != 0))) return SIZE_MAX;
        if (count > segments_.capacity() &&
            (count > SIZE_MAX / sizeof(Segment) || !add(total, count * sizeof(Segment)))) return SIZE_MAX;
        return total;
    }
    void resize(size_t n, uint8_t value = 0) { resize_impl(n, &value); }
    void resize_impl(size_t n, const uint8_t* value) {
        if (n > size_) {
            const size_t held = size_;
            const bool growing = held != 0;
            const size_t spare = segments_.empty() ? 0 : segments_.back().capacity() - segments_.back().size();
            const size_t extend = std::min(n - size_, spare);
            const size_t extra = n - size_ - extend;
            segments_.reserve(segments_.size() + extra / segment_bytes + (extra % segment_bytes != 0));
            if (extend) {
                if (value) segments_.back().resize(segments_.back().size() + extend, *value);
                else segments_.back().resize(segments_.back().size() + extend);
                size_ += extend;
            }
            while (size_ < n) {
                const size_t count = std::min(segment_bytes, n - size_);
                const size_t previous = segments_.empty() ? 0 : segments_.back().capacity();
                Segment segment;
                segment.reserve(segment_capacity(count, previous, growing, held));
                // The parked snapshots are the engine's largest pageable memory (4.9 GiB for a
                // 308k-token conversation), and they are what a restore has to page back in.
                // With 4 KiB pages the swap-in is a stream of tiny scattered reads: measured
                // 240-440 MB/s against the device's 1.1 GB/s sequential, so a 213k-token
                // conversation took 26-31 s to restore.  Asking for TRANSPARENT huge pages makes the
                // kernel back each 16 MiB segment with 2 MiB pages, so the swap-out and the swap-in
                // move 2 MiB units instead of 4 KiB ones.
#if defined(__linux__)
                if (segment.capacity() >= 2ull * 1024 * 1024 && segment.data())
                    (void) madvise(segment.data(), segment.capacity(), MADV_HUGEPAGE);
#endif
                if (value) segment.resize(count, *value);
                else segment.resize(count);
                segments_.push_back(std::move(segment));
                size_ += count;
                progress_beat();   // #2049: zero-filling 16 MiB of fresh pages is slow when the host is paging
            }
        } else {
            while (!segments_.empty() && size_ - segments_.back().size() >= n) {
                size_ -= segments_.back().size();
                segments_.pop_back();
            }
            if (size_ > n) segments_.back().resize(segments_.back().size() - (size_ - n));
            size_ = n;
        }
    }
    void pop_back() { resize(size_ - 1); }
    // resize() without the zero fill of what it adds (the fresh pages are not touched here): only for a caller that
    // writes every added byte before anything reads it (STRATA_PARK_ASYNC's background copy)
    void resize_for_overwrite(size_t n) { resize_impl(n, nullptr); }

    // fn(pointer, count, absolute_offset); false aborts a transfer immediately.
    template<class Fn> bool visit(size_t offset, size_t count, Fn fn) {
        return visit_impl(*this, offset, count, fn);
    }
    template<class Fn> bool visit(size_t offset, size_t count, Fn fn) const {
        return visit_impl(*this, offset, count, fn);
    }
    /// Touch every page of the buffer, so a page the kernel has swapped out is read back in.
    ///
    /// A parked conversation's K/V is anonymous memory: when the engine's cgroup is over its high,
    /// the kernel writes the cold snapshots to the NVMe swap, and a restore then pays a scattered
    /// 4 KiB page-in at ~240 MB/s instead of RAM speed.  Touching the pages WHILE the request is
    /// still waiting for a slot overlaps that page-in with the wait, so the restore itself is a
    /// RAM-speed copy.  The read is one byte per page: the kernel faults the whole page in.
    void prefetch_pages() const {
        static volatile uint8_t sink = 0;
        visit(0, size(), [](const uint8_t* p, size_t n, size_t) {
            for (size_t i = 0; i < n; i += 4096) sink = static_cast<uint8_t>(sink + p[i]);
            return true;
        });
    }
    bool read(void* target, size_t offset, size_t count) const {
        return visit(offset, count, [&](const uint8_t* p, size_t n, size_t at) {
            std::memcpy(static_cast<uint8_t*>(target) + at - offset, p, n);
            return true;
        });
    }
    bool operator==(const ConversationBuffer& other) const {
        if (size_ != other.size_) return false;
        size_t a = 0, b = 0, x = 0, y = 0;
        while (a < segments_.size() && b < other.segments_.size()) {
            const auto& left = segments_[a];
            const auto& right = other.segments_[b];
            const size_t n = std::min(left.size() - x, right.size() - y);
            if (std::memcmp(left.data() + x, right.data() + y, n)) return false;
            x += n; y += n;
            if (x == left.size()) { ++a; x = 0; }
            if (y == right.size()) { ++b; y = 0; }
        }
        return true;
    }

private:
    // value-initializes only when asked (resize's explicit fill value): resize_for_overwrite leaves the bytes alone
    template<class T> struct DefaultInit : std::allocator<T> {
        template<class U> struct rebind { using other = DefaultInit<U>; };
        DefaultInit() = default;
        template<class U> DefaultInit(const DefaultInit<U>&) noexcept {}
        template<class U> void construct(U* p) { ::new ((void*) p) U; }
        template<class U, class... A> void construct(U* p, A&&... a) { ::new ((void*) p) U(std::forward<A>(a)...); }
    };
    // A segment is an mmap, not a malloc: the parked snapshots are what a restore has to page back
    // in, and MADV_HUGEPAGE only works on a 2 MiB-ALIGNED range (on an unaligned one it fails with
    // EINVAL and the memory stays 4 KiB-paged: measured 240-440 MB/s of scattered page-in against the
    // device's 1.1 GB/s sequential, and a 213k-token conversation took 26-31 s to restore).  With a
    // 2 MiB-aligned mmap the kernel backs each segment with 2 MiB pages, so the swap-out and the
    // swap-in move 2 MiB units.
// A segment is an mmap, not a malloc: the parked snapshots are what a restore has to page back
// in, and MADV_HUGEPAGE only works on a 2 MiB-ALIGNED range (on an unaligned one it fails with
// EINVAL and the memory stays 4 KiB-paged: measured 240-440 MB/s of scattered page-in against the
// device's 1.1 GB/s sequential, and a 213k-token conversation took 26-31 s to restore).  With a
// 2 MiB-aligned mmap the kernel backs each segment with 2 MiB pages, so the swap-out and the
// swap-in move 2 MiB units.
class Segment {
public:
    Segment() = default;
    Segment(const Segment& o) { copy_from(o); }
    Segment& operator=(const Segment& o) {
        if (this != &o) { release(); copy_from(o); }
        return *this;
    }
    Segment(Segment&& o) noexcept
        : p_(o.p_), base_(o.base_), map_len_(o.map_len_), cap_(o.cap_), size_(o.size_) {
        o.p_ = nullptr; o.base_ = 0; o.map_len_ = 0; o.cap_ = o.size_ = 0;
    }
    Segment& operator=(Segment&& o) noexcept {
        if (this != &o) {
            release();
            p_ = o.p_; base_ = o.base_; map_len_ = o.map_len_; cap_ = o.cap_; size_ = o.size_;
            o.p_ = nullptr; o.base_ = 0; o.map_len_ = 0; o.cap_ = o.size_ = 0;
        }
        return *this;
    }
    ~Segment() { release(); }
    size_t size() const { return size_; }
    size_t capacity() const { return cap_; }
    uint8_t* data() { return p_; }
    const uint8_t* data() const { return p_; }
    bool empty() const { return size_ == 0; }
    // Reserve the room for `n` bytes; the pages are untouched (the caller fills them).
    void reserve(size_t n) {
        if (n <= cap_) return;
        release();
        cap_ = n;
        allocate(n);
    }
    void resize(size_t n, uint8_t value = 0) {
        if (n > cap_) reserve(n);
        if (n > size_ && p_) std::memset(p_ + size_, value, n - size_);
        size_ = n;
    }
    void clear() { size_ = 0; }
private:
    void release() {
        if (base_) {
            munmap(reinterpret_cast<void*>(base_), map_len_);
            base_ = 0; map_len_ = 0;
        }
        p_ = nullptr; cap_ = 0; size_ = 0;
    }
    void copy_from(const Segment& o) {
        cap_ = o.cap_; size_ = o.size_;
        allocate(o.cap_);
        if (p_ && o.p_) std::memcpy(p_, o.p_, o.size_);
    }
    // The mapping is larger than the segment (it is 2 MiB-aligned), so the base and the whole
    // length are kept: releasing it must hand back exactly what mmap took.
    void allocate(size_t n) {
        p_ = nullptr; base_ = 0; map_len_ = 0;
        if (!n) return;
#if defined(__linux__)
        const size_t align = 2ull * 1024 * 1024;
        void* raw = mmap(nullptr, n + align, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (raw == MAP_FAILED) return;
        uintptr_t base = reinterpret_cast<uintptr_t>(raw);
        uintptr_t aligned = (base + align - 1) & ~(align - 1);
        if (aligned > base) munmap(raw, aligned - base);
        size_t tail = (base + n + align) - (aligned + n);
        if (tail) munmap(reinterpret_cast<void*>(aligned + n), tail);
        (void) madvise(reinterpret_cast<void*>(aligned), n, MADV_HUGEPAGE);
        base_ = reinterpret_cast<uintptr_t>(raw); map_len_ = n + align;
        p_ = reinterpret_cast<uint8_t*>(aligned);
#else
        void* raw = std::malloc(n);
        base_ = reinterpret_cast<uintptr_t>(raw); map_len_ = n;
        p_ = static_cast<uint8_t*>(raw);
#endif
    }
    uint8_t* p_ = nullptr;
    uintptr_t base_ = 0;
    size_t map_len_ = 0, cap_ = 0, size_ = 0;
};
    // A fresh capture allocates exactly its payload. Later appends reserve
    // geometrically growing segments, bounded by 16 MiB. Small chat turns then
    // extend the final allocation instead of adding one transfer per turn.
    // The reserve is also bounded by an eighth of what the buffer held before
    // the append (at least 64 KiB): a parked conversation carries it in every
    // K/V buffer, and at up to 16 MiB each it came to 0.4-0.5 GB of a ~1 GB
    // conversation (Qwen3.8-Flash-Next), counted against the cache's budget.
    static size_t segment_capacity(size_t count, size_t previous, bool growing, size_t held) {
        if (!growing) return count;
        const size_t geometric = std::min(segment_bytes, std::max<size_t>(65536, std::min(previous, segment_bytes/2)*2));
        return std::max(count, std::min(geometric, std::max<size_t>(65536, held / 8)));
    }
    static bool add(size_t& n, size_t extra) {
        if (extra > SIZE_MAX - n) return false;
        n += extra;
        return true;
    }
    template<class Self, class Fn> static bool visit_impl(Self& self, size_t offset, size_t count, Fn fn) {
        if (offset > self.size_ || count > self.size_ - offset) return false;
        size_t begin = 0;
        for (auto& segment : self.segments_) {
            if (!count) break;
            const size_t end = begin + segment.size();
            if (offset < end) {
                const size_t n = std::min(count, end - offset);
                if (!fn(segment.data() + offset - begin, n, offset)) return false;
                offset += n; count -= n;
            }
            begin = end;
        }
        return count == 0;
    }
    std::vector<Segment> segments_;
    size_t size_ = 0;
};

} // namespace strata::core
