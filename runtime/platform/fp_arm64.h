// FP control on AArch64: FPCR (rounding mode bits 22-23, FZ bit 24, DN bit 25, AHP bit 26, trap enables bits 8-15).
// The counterpart of fp_x86.h. Inline on purpose: lifted FP code (simd.h) reads and writes it on every operation that
// depends on the guest FPCR. The host FPCR has the guest's layout, so a guest rounding mode needs no translation.
#pragma once
#include <stdint.h>

#define PLAT_FP_RC_MASK (3u << 22)   // rounding mode: 0 nearest, 1 << 22 up, 2 << 22 down, 3 << 22 toward zero
#define PLAT_FP_FZ (1u << 24)        // flush denormal inputs and results to zero (MXCSR FTZ and DAZ together)
#define PLAT_FP_DN (1u << 25)        // default NaN

// The "memory" clobber keeps loads and stores of FP data on their side of a mode change, like _mm_setcsr.
static inline uint32_t plat_fp_get(void) {
    uint64_t v;
    __asm__ volatile("mrs %0, fpcr" : "=r"(v) :: "memory");
    return (uint32_t)v;
}
static inline void plat_fp_set(uint32_t v) { __asm__ volatile("msr fpcr, %0" :: "r"((uint64_t)v) : "memory"); }
