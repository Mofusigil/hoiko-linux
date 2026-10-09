#pragma once
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>

// CIL evaluation-stack values. All memory access uses memcpy so that original
// packed C++/CLI layouts work without alignment or strict-aliasing violations.
struct Slot {
    uint64_t u = 0;
    enum Kind : uint8_t { Int32, Int64, Real } kind = Int32;
    static Slot i32(int64_t v) { return {uint64_t(int64_t(int32_t(v))), Int32}; }
    static Slot i64(uint64_t v) { return {v, Int64}; }
    static Slot pointer(const void* p) { return i64(reinterpret_cast<uintptr_t>(p)); }
    static Slot real(double v) { uint64_t bits; std::memcpy(&bits,&v,8); return {bits,Real}; }
    void* ptr() const { return reinterpret_cast<void*>(uintptr_t(u)); }
    int64_t i() const { return int64_t(u); }
    uint64_t unsigned_value() const { return kind==Int32 ? uint32_t(u) : u; }
    double f() const { double v; std::memcpy(&v,&u,8); return v; }
    bool truth() const { return u!=0; }
};
template<class T> inline T read_value(const void* p) { T v; std::memcpy(&v,p,sizeof(v)); return v; }
inline Slot load_i8(const void* p) { return Slot::i32(read_value<int8_t>(p)); }
inline Slot load_u8(const void* p) { return Slot::i32(read_value<uint8_t>(p)); }
inline Slot load_i16(const void* p) { return Slot::i32(read_value<int16_t>(p)); }
inline Slot load_u16(const void* p) { return Slot::i32(read_value<uint16_t>(p)); }
inline Slot load_i32(const void* p) { return Slot::i32(read_value<int32_t>(p)); }
inline Slot load_i64(const void* p) { return Slot::i64(read_value<uint64_t>(p)); }
inline Slot load_f32(const void* p) { return Slot::real(read_value<float>(p)); }
inline Slot load_f64(const void* p) { return Slot::real(read_value<double>(p)); }
inline void store(void* p, Slot v, size_t n) {
    if(n>8) throw std::runtime_error("Unsupported CIL aggregate store");
    std::memcpy(p,&v.u,n);
}
inline Slot int_result(uint64_t v,Slot a,Slot b) {
    return a.kind==Slot::Int64 || b.kind==Slot::Int64 ? Slot::i64(v) : Slot::i32(v);
}
inline Slot binary_add(Slot a,Slot b) { return a.kind==Slot::Real ? Slot::real(a.f()+b.f()) : int_result(a.u+b.u,a,b); }
inline Slot binary_sub(Slot a,Slot b) { return a.kind==Slot::Real ? Slot::real(a.f()-b.f()) : int_result(a.u-b.u,a,b); }
inline Slot binary_mul(Slot a,Slot b) { return a.kind==Slot::Real ? Slot::real(a.f()*b.f()) : int_result(a.u*b.u,a,b); }
inline Slot binary_div(Slot a,Slot b) { return a.kind==Slot::Real ? Slot::real(a.f()/b.f()) : int_result(uint64_t(a.i()/b.i()),a,b); }
inline Slot binary_and(Slot a,Slot b) { return int_result(a.u&b.u,a,b); }
inline Slot binary_or(Slot a,Slot b) { return int_result(a.u|b.u,a,b); }
inline Slot binary_xor(Slot a,Slot b) { return int_result(a.u^b.u,a,b); }
inline Slot binary_shl(Slot a,Slot b) { return int_result(a.u<<(b.u&(a.kind==Slot::Int32?31:63)),a,a); }
inline Slot binary_shr(Slot a,Slot b) { return int_result(uint64_t(a.i()>>(b.u&(a.kind==Slot::Int32?31:63))),a,a); }
inline Slot binary_shr_un(Slot a,Slot b) { return int_result(a.unsigned_value()>>(b.u&(a.kind==Slot::Int32?31:63)),a,a); }
inline Slot unary_not(Slot a) { return int_result(~a.u,a,a); }
inline Slot unary_neg(Slot a) { return a.kind==Slot::Real ? Slot::real(-a.f()) : int_result(0-a.u,a,a); }
inline Slot convert_i1(Slot a) { return Slot::i32(int8_t(a.u)); }
inline Slot convert_u1(Slot a) { return Slot::i32(uint8_t(a.u)); }
inline Slot convert_i4(Slot a) { return Slot::i32(a.kind==Slot::Real ? int64_t(a.f()) : a.i()); }
inline Slot convert_i8(Slot a) { return Slot::i64(a.kind==Slot::Real ? uint64_t(int64_t(a.f())) : uint64_t(a.i())); }
inline Slot convert_r8(Slot a) { return Slot::real(a.kind==Slot::Real ? a.f() : double(a.i())); }
enum class Cmp { eq,ne,gt,lt,ge,le };
template<class T> inline bool cmp(T a,T b,Cmp op) {
    switch(op) { case Cmp::eq:return a==b; case Cmp::ne:return a!=b;
        case Cmp::gt:return a>b; case Cmp::lt:return a<b;
        case Cmp::ge:return a>=b; case Cmp::le:return a<=b; }
    return false;
}
inline bool compare(Slot a,Slot b,Cmp op,bool unsign) {
    if(a.kind==Slot::Real) {
        if(unsign && (std::isnan(a.f()) || std::isnan(b.f()))) return true;
        return cmp(a.f(),b.f(),op);
    }
    return unsign ? cmp(a.unsigned_value(),b.unsigned_value(),op) : cmp(a.i(),b.i(),op);
}

Slot native_weights_ready(const Slot*);
Slot native_sequential_worker(const Slot*);
Slot native_engine_init(const Slot*);
Slot native_partial_sort(const Slot*);
Slot native_partition_baby(const Slot*);
Slot native_partition_node(const Slot*);
inline Slot native_ticks(const Slot*) {
    auto ns=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
    return Slot::i64(uint64_t(ns/100));
}
inline Slot native_atomic_load(const Slot* a) { return load_i32(a[0].ptr()); }
inline Slot native_exchange(const Slot* a) {
    Slot old=load_i32(a[0].ptr()); store(a[0].ptr(),a[1],4); return old;
}

// The recovered TD path can propose a landing below row zero. Reject it before
// Board.LockMino writes rows or its bottom-row value indexes the dedup buckets.
// The Windows routine checks only the upper bound and trusts its caller.
inline bool valid_lock(const void* board_ptr, const void* mino_ptr) {
    const auto* board = static_cast<const unsigned char*>(board_ptr);
    const auto* mino = static_cast<const unsigned char*>(mino_ptr);
    const int x = read_value<int32_t>(mino), y = read_value<int32_t>(mino+4);
    if(x < -9 || x > 9 || y < 0 || y > 22) return false;
    const auto* shape = reinterpret_cast<const unsigned char*>(read_value<uint64_t>(mino+24));
    int cells = 0, bottom = 32;
    for(int dy=0;dy<4;++dy) {
        const auto mask = read_value<uint32_t>(shape+16+dy*4);
        if(!mask) continue;
        const uint64_t shifted = x<0 ? uint64_t(mask)<<(-x) : mask>>x;
        const int row = y-dy;
        if(row<0 || row>=32 || shifted>1023) return false;
        bottom = std::min(bottom,row);
        if(read_value<uint32_t>(board+row*4) & shifted) return false;
        for(auto bits=shifted;bits;bits>>=1) cells+=int(bits&1);
    }
    return cells==4 && bottom<20;
}
