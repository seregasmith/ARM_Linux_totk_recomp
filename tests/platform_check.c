// Checks the architecture-specific parts of the Linux platform layer on the machine it runs on. tests/platform_check.py
// builds it with runtime/platform/linux.c and common.c.
//
// - fibers: plat_fiber_switch keeps each fiber's callee-saved registers and FP control (MXCSR, FPCR) across switches;
// - write faults: a write to a read-only page, a plain store and an atomic read-modify-write, reaches the hook
//   plat_mem_fault_hook installs (runtime/memory.c thaws frozen regions there), with the faulting address;
// - asynchronous preemption (AArch64): plat_intr_stop / plat_intr_resume send a thread spinning in a loop that keeps
//   known values in every register into kern_ap_stub (runtime/kern_ap_stub.inc), which calls kern_ap_yield here and
//   returns to the loop; when the loop ends, every register, NZCV, FPCR and FPSR must hold what it put there. x18 is
//   the one register the stub may change, as in the game's generated code (-ffixed-x18).
//
// Exit status 0 when every check passes.
#define _GNU_SOURCE
#include <math.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include "platform/platform.h"

static int failures;
#define CHECK(cond, ...) do { if (!(cond)) { failures++; printf("  FAIL: " __VA_ARGS__); putchar('\n'); } } while (0)

// runtime/platform/linux.c's clipboard calls SDL; nothing here uses it.
_Bool SDL_SetClipboardText(const char *text) { (void)text; return 0; }

// ---- fibers
static PlatFiber *fib_main, *fib[2];
enum { FIBER_ROUNDS = 2000 };
static volatile int fib_done[2];

// Values a fiber keeps in locals across its switches: read from volatile memory, so the compiler can neither fold the
// checks nor recompute the values after a switch, and keeps them in callee-saved registers (x19-x28 and d8-d15 on
// AArch64, rbx and r12-r15 plus the stack on x86-64).
static volatile uint64_t fib_vals[2][10];
static volatile double fib_dbl[2][4];

static void fiber_fn(void *arg) {
    int id = (int)(intptr_t)arg;
    // Each fiber its own rounding mode: fiber 0 to nearest, fiber 1 toward zero (all ones in MXCSR RC and FPCR.RMode).
    uint32_t mine = (plat_fp_get() & ~PLAT_FP_RC_MASK) | (id ? PLAT_FP_RC_MASK : 0);
    plat_fp_set(mine);
    volatile uint64_t *v = fib_vals[id];
    uint64_t a = v[0], b = v[1], c = v[2], d = v[3], e = v[4], f = v[5], g = v[6], h = v[7], i = v[8], j = v[9];
    double w = fib_dbl[id][0], x = fib_dbl[id][1], y = fib_dbl[id][2], z = fib_dbl[id][3];
    int bad = 0;
    for (int r = 0; r < FIBER_ROUNDS; r++) {
        plat_fiber_switch(fib[!id]);
        bad |= plat_fp_get() != mine;
        bad |= a != v[0] || b != v[1] || c != v[2] || d != v[3] || e != v[4] || f != v[5] || g != v[6] || h != v[7];
        bad |= i != v[8] || j != v[9];
        bad |= w != fib_dbl[id][0] || x != fib_dbl[id][1] || y != fib_dbl[id][2] || z != fib_dbl[id][3];
    }
    CHECK(!bad, "fiber %d: a register or the FP control changed across a switch", id);
    fib_done[id] = 1;
    plat_fiber_switch(fib_main);
}

static void check_fibers(void) {
    for (int id = 0; id < 2; id++) {
        for (int k = 0; k < 10; k++) fib_vals[id][k] = 0x9e3779b97f4a7c15ull * (uint64_t)(id * 10 + k + 1);
        for (int k = 0; k < 4; k++) fib_dbl[id][k] = 1.0 / (3 + id * 4 + k);
    }
    uint32_t own = plat_fp_get();
    fib_main = plat_fiber_self();
    fib[0] = plat_fiber_new(256 << 10, fiber_fn, (void *)0);
    fib[1] = plat_fiber_new(256 << 10, fiber_fn, (void *)1);
    CHECK(fib[0] && fib[1], "plat_fiber_new failed");
    if (!fib[0] || !fib[1]) return;
    plat_fiber_switch(fib[0]);
    CHECK(fib_done[0], "fiber 0 did not finish");
    CHECK(plat_fp_get() == own, "the thread's FP control changed: %#x, was %#x", plat_fp_get(), own);
    plat_fiber_unself();
    printf("fibers: %d switches each way, registers and FP control kept: %s\n", FIBER_ROUNDS,
           failures ? "FAIL" : "ok");
}

// ---- write faults
static uint8_t *ro_page;
static volatile uint64_t hook_addr;
static volatile int hook_calls;

static int write_hook(uint64_t addr) {
    if (addr < (uint64_t)(uintptr_t)ro_page || addr >= (uint64_t)(uintptr_t)ro_page + 4096) return 0;
    hook_addr = addr;
    hook_calls++;
    return mprotect(ro_page, 4096, PROT_READ | PROT_WRITE) == 0;
}

#if defined(__aarch64__)
// Whether this system's signal frames carry the ESR of a fault, which platform/linux.c reads to tell writes from reads.
// Linux puts it there; QEMU's user mode, which runs cross-built tests, does not.
static sigjmp_buf esr_jump;
static volatile int esr_found;
static void esr_probe(int sig, siginfo_t *si, void *uc) {
    (void)sig, (void)si;
    const ucontext_t *u = uc;
    const struct { uint32_t magic, size; } *h = (const void *)u->uc_mcontext.__reserved;
    const char *end = (const char *)u->uc_mcontext.__reserved + sizeof u->uc_mcontext.__reserved;
    for (; (const char *)h + 8 <= end && h->magic && h->size >= 8; h = (const void *)((const char *)h + h->size))
        if (h->magic == 0x45535201u) esr_found = 1;
    siglongjmp(esr_jump, 1);
}
static int has_esr(void) {
    struct sigaction sa = {.sa_sigaction = esr_probe, .sa_flags = SA_SIGINFO}, old;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, &old);
    uint8_t *page = mmap(NULL, 4096, PROT_READ, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (!sigsetjmp(esr_jump, 1)) *(volatile uint8_t *)page = 1;
    sigaction(SIGSEGV, &old, NULL);
    munmap(page, 4096);
    return esr_found;
}
#else
static int has_esr(void) { return 1; }
#endif

static void check_write_faults(void) {
    int before = failures;
    if (!has_esr()) {
        printf("write faults: skipped, this system's signal frames carry no ESR (QEMU's user mode)\n");
        return;
    }
    ro_page = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(ro_page != MAP_FAILED, "mmap failed");
    if (ro_page == MAP_FAILED) return;
    plat_mem_fault_hook(write_hook);
    mprotect(ro_page, 4096, PROT_READ);
    *(volatile uint32_t *)(ro_page + 8) = 5;
    CHECK(hook_calls == 1 && hook_addr == (uint64_t)(uintptr_t)(ro_page + 8) && ro_page[8] == 5,
          "store: hook calls %d at %#llx (page %p)", hook_calls, (unsigned long long)hook_addr, (void *)ro_page);
    mprotect(ro_page, 4096, PROT_READ);
    __atomic_fetch_add((uint32_t *)(ro_page + 16), 3, __ATOMIC_SEQ_CST);
    CHECK(hook_calls == 2 && hook_addr == (uint64_t)(uintptr_t)(ro_page + 16) && ro_page[16] == 3,
          "atomic add: hook calls %d at %#llx", hook_calls, (unsigned long long)hook_addr);
    printf("write faults: store and atomic read-modify-write reach the hook: %s\n", failures > before ? "FAIL" : "ok");
}

// ---- asynchronous preemption
#if defined(__aarch64__)
uint64_t kern_ap_rip(void);
void kern_ap_yield(void);
void kern_ap_stub(void);
#include "kern_ap_stub.inc"

static volatile uint64_t ap_rip;
static volatile int ap_yields;
static volatile double ap_sink;

uint64_t kern_ap_rip(void) { return ap_rip; }

// The stub's callee: C code that uses the caller-saved registers, the FP unit and the flags freely.
void kern_ap_yield(void) {
    ap_yields++;
    char buf[64];
    double v = 0;
    for (int i = 1; i < 50; i++) v += sqrt((double)i) * (i & 1 ? 1.5 : -0.25);
    snprintf(buf, sizeof buf, "%f", v);
    ap_sink = v + (double)strlen(buf);
    plat_yield();
}

// regs_loop(flag, out): puts known values into x0-x17, x19-x26, x29, x30, q0-q31, NZCV, FPCR (FZ, round toward zero)
// and FPSR, spins between regs_loop_body and regs_loop_end until *flag is non-zero (x27 and x28 run the loop), then
// stores them all to out: x0-x30 at 0 (x18, x27 and x28 unchecked), NZCV, FPCR, FPSR at 248, q0-q31 at 272.
void regs_loop(volatile uint64_t *flag, uint64_t *out);
extern const char regs_loop_body[], regs_loop_end[];
static const uint64_t q_init[64] = {
#define Q(n) 0x0123456789abcdefull ^ ((uint64_t)(n) << 40), 0xfedcba9876543210ull ^ ((uint64_t)(n) << 8)
    Q(0), Q(1), Q(2), Q(3), Q(4), Q(5), Q(6), Q(7), Q(8), Q(9), Q(10), Q(11), Q(12), Q(13), Q(14), Q(15),
    Q(16), Q(17), Q(18), Q(19), Q(20), Q(21), Q(22), Q(23), Q(24), Q(25), Q(26), Q(27), Q(28), Q(29), Q(30), Q(31),
#undef Q
};
#define XPAT(n) (0x5a00000000000000ull | (0x11ull * (n)) << 16 | (n))
#define NZCV_SET 0xa0000000ull
#define FPCR_SET ((1ull << 24) | (3ull << 22))
#define FPSR_SET 0x0800009full
__asm__(".text\n"
        ".globl regs_loop\n"
        ".type regs_loop, %function\n"
        ".p2align 4\n"
        "regs_loop:\n"
        "    sub sp, sp, #176\n"
        "    stp x19, x20, [sp, #0]\n"
        "    stp x21, x22, [sp, #16]\n"
        "    stp x23, x24, [sp, #32]\n"
        "    stp x25, x26, [sp, #48]\n"
        "    stp x27, x28, [sp, #64]\n"
        "    stp x29, x30, [sp, #80]\n"
        "    stp d8, d9, [sp, #96]\n"
        "    stp d10, d11, [sp, #112]\n"
        "    stp d12, d13, [sp, #128]\n"
        "    stp d14, d15, [sp, #144]\n"
        "    str x1, [sp, #160]\n"
        "    mov x28, x0\n"
        "    adrp x9, q_init\n"
        "    add x9, x9, :lo12:q_init\n"
        "    ldp q0, q1, [x9, #0]\n"
        "    ldp q2, q3, [x9, #32]\n"
        "    ldp q4, q5, [x9, #64]\n"
        "    ldp q6, q7, [x9, #96]\n"
        "    ldp q8, q9, [x9, #128]\n"
        "    ldp q10, q11, [x9, #160]\n"
        "    ldp q12, q13, [x9, #192]\n"
        "    ldp q14, q15, [x9, #224]\n"
        "    ldp q16, q17, [x9, #256]\n"
        "    ldp q18, q19, [x9, #288]\n"
        "    ldp q20, q21, [x9, #320]\n"
        "    ldp q22, q23, [x9, #352]\n"
        "    ldp q24, q25, [x9, #384]\n"
        "    ldp q26, q27, [x9, #416]\n"
        "    ldp q28, q29, [x9, #448]\n"
        "    ldp q30, q31, [x9, #480]\n"
        "    mov x9, #0xa0000000\n"
        "    msr nzcv, x9\n"
        "    mov x9, #0x1c00000\n"
        "    msr fpcr, x9\n"
        "    movz x9, #0x0800, lsl #16\n"
        "    movk x9, #0x009f\n"
        "    msr fpsr, x9\n"
#define SETX(n) "    movz x" #n ", #0x5a00, lsl #48\n" \
                "    movk x" #n ", #(0x11 * " #n "), lsl #16\n" \
                "    movk x" #n ", #" #n "\n"
        SETX(0) SETX(1) SETX(2) SETX(3) SETX(4) SETX(5) SETX(6) SETX(7) SETX(8) SETX(9) SETX(10) SETX(11) SETX(12)
        SETX(13) SETX(14) SETX(15) SETX(16) SETX(17) SETX(19) SETX(20) SETX(21) SETX(22) SETX(23) SETX(24)
        SETX(25) SETX(26) SETX(29) SETX(30)
#undef SETX
        ".globl regs_loop_body\n"
        "regs_loop_body:\n"
        "    ldr x27, [x28]\n"
        "    cbz x27, regs_loop_body\n"
        ".globl regs_loop_end\n"
        "regs_loop_end:\n"
        "    ldr x28, [sp, #160]\n"
        "    stp x0, x1, [x28, #0]\n"
        "    stp x2, x3, [x28, #16]\n"
        "    stp x4, x5, [x28, #32]\n"
        "    stp x6, x7, [x28, #48]\n"
        "    stp x8, x9, [x28, #64]\n"
        "    stp x10, x11, [x28, #80]\n"
        "    stp x12, x13, [x28, #96]\n"
        "    stp x14, x15, [x28, #112]\n"
        "    stp x16, x17, [x28, #128]\n"
        "    stp x19, x20, [x28, #152]\n"
        "    stp x21, x22, [x28, #168]\n"
        "    stp x23, x24, [x28, #184]\n"
        "    stp x25, x26, [x28, #200]\n"
        "    stp x29, x30, [x28, #232]\n"
        "    mrs x0, nzcv\n"
        "    mrs x1, fpcr\n"
        "    mrs x2, fpsr\n"
        "    stp x0, x1, [x28, #248]\n"
        "    str x2, [x28, #264]\n"
        "    add x0, x28, #272\n"
        "    stp q0, q1, [x0, #0]\n"
        "    stp q2, q3, [x0, #32]\n"
        "    stp q4, q5, [x0, #64]\n"
        "    stp q6, q7, [x0, #96]\n"
        "    stp q8, q9, [x0, #128]\n"
        "    stp q10, q11, [x0, #160]\n"
        "    stp q12, q13, [x0, #192]\n"
        "    stp q14, q15, [x0, #224]\n"
        "    stp q16, q17, [x0, #256]\n"
        "    stp q18, q19, [x0, #288]\n"
        "    stp q20, q21, [x0, #320]\n"
        "    stp q22, q23, [x0, #352]\n"
        "    stp q24, q25, [x0, #384]\n"
        "    stp q26, q27, [x0, #416]\n"
        "    stp q28, q29, [x0, #448]\n"
        "    stp q30, q31, [x0, #480]\n"
        "    msr fpcr, xzr\n"
        "    msr fpsr, xzr\n"
        "    ldp d14, d15, [sp, #144]\n"
        "    ldp d12, d13, [sp, #128]\n"
        "    ldp d10, d11, [sp, #112]\n"
        "    ldp d8, d9, [sp, #96]\n"
        "    ldp x29, x30, [sp, #80]\n"
        "    ldp x27, x28, [sp, #64]\n"
        "    ldp x25, x26, [sp, #48]\n"
        "    ldp x23, x24, [sp, #32]\n"
        "    ldp x21, x22, [sp, #16]\n"
        "    ldp x19, x20, [sp, #0]\n"
        "    add sp, sp, #176\n"
        "    ret\n"
        ".size regs_loop, .-regs_loop\n");

static volatile uint64_t loop_flag;
static uint64_t loop_out[34 + 64];
static PlatIntr *volatile loop_intr;

static void loop_thread(void *arg) {
    (void)arg;
    loop_intr = plat_intr_self();
    if (loop_intr) regs_loop(&loop_flag, loop_out);
}

static void check_preemption(void) {
    enum { WANT = 200 };
    int before = failures;
    PlatThread *t = plat_thread_new(loop_thread, NULL, "regs_loop");
    CHECK(t != NULL, "plat_thread_new failed");
    if (!t) return;
    uint64_t start = plat_now_ns();
    while (!loop_intr && plat_now_ns() - start < 2000000000ull) plat_sleep_ms(1);
    int sent = 0, outside = 0, stops = 0;
    while (sent < WANT && plat_now_ns() - start < 30000000000ull) {
        uint64_t pc = 0, sp = 0;
        if (plat_intr_stop(loop_intr, &pc, &sp)) {
            stops++;
            uint64_t to = 0;
            int in_loop = pc >= (uint64_t)(uintptr_t)regs_loop_body && pc < (uint64_t)(uintptr_t)regs_loop_end;
            if (in_loop && ap_yields == sent) {
                ap_rip = pc;
                to = (uint64_t)(uintptr_t)kern_ap_stub;
            } else {
                outside++;
            }
            plat_intr_resume(loop_intr, to);
            if (to) sent++;
        }
        plat_sleep_ms(1);
    }
    while (ap_yields < sent && plat_now_ns() - start < 35000000000ull) plat_sleep_ms(1);
    loop_flag = 1;
    plat_thread_join(t);
    CHECK(sent >= WANT, "only %d of %d interruptions sent (%d stops, %d outside the loop)", sent, WANT, stops, outside);
    CHECK(ap_yields == sent, "kern_ap_yield ran %d times for %d redirects", ap_yields, sent);
    for (int n = 0; n <= 30; n++) {
        if (n == 18 || n == 27 || n == 28) continue;
        CHECK(loop_out[n] == XPAT(n), "x%d %#llx, want %#llx", n, (unsigned long long)loop_out[n],
              (unsigned long long)XPAT(n));
    }
    CHECK(loop_out[31] == NZCV_SET, "NZCV %#llx, want %#llx", (unsigned long long)loop_out[31], NZCV_SET);
    CHECK(loop_out[32] == FPCR_SET, "FPCR %#llx, want %#llx", (unsigned long long)loop_out[32], FPCR_SET);
    CHECK(loop_out[33] == FPSR_SET, "FPSR %#llx, want %#llx", (unsigned long long)loop_out[33], FPSR_SET);
    CHECK(!memcmp(&loop_out[34], q_init, sizeof q_init), "a q register changed");
    printf("asynchronous preemption: %d interruptions through kern_ap_stub, every register kept: %s\n", sent,
           failures > before ? "FAIL" : "ok");
}
#else
static void check_preemption(void) { printf("asynchronous preemption: checked on aarch64 only\n"); }
#endif

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    check_fibers();
    check_write_faults();
    check_preemption();
    printf(failures ? "FAILED (%d)\n" : "all checks passed\n", failures);
    return failures != 0;
}
