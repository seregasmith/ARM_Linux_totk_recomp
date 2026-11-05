// Host check of the installer's first step, compiled with the bundled clang at install time. Prints one JSON object on
// stdout:
//   arch, levels, preferred: the code levels (tools/build_game.py CPU_LEVELS) this CPU runs, best first, and the one
//     the setup builds unless asked for another. x86-64: v3 and v2 (also as x86_64_v3 and x86_64_v2). AArch64: a64 and
//     oryon, from the HWCAP bits runtime/main.c check_cpu tests when the game starts; oryon is preferred only on
//     Qualcomm's Oryon cores (oryon_cores), as -mcpu=oryon-1 also tunes the code for them.
//   page_size, write_watch (Linux): the page size (the game needs 4 KiB pages) and whether userfaultfd's asynchronous
//     write protection works (runtime/platform/linux.c; without it runtime/memory.c tracks writes per 2 MiB region).
//   loader, instance, devices: does a Vulkan 1.3 device exist. The Vulkan loader is opened at run time, so a machine
//     without one gets "loader": false instead of a failed start.
#include <stdio.h>
#include <string.h>
#include <vulkan/vulkan.h>
#if defined(__x86_64__) || defined(__i386__)
#include <cpuid.h>
#endif
#ifdef _WIN32
#include <windows.h>
#define open_lib() LoadLibraryA("vulkan-1.dll")
#define lib_sym(l, n) ((void *)GetProcAddress((l), (n)))
#else
#include <dlfcn.h>
#define open_lib() dlopen("libvulkan.so.1", RTLD_NOW)
#define lib_sym(l, n) dlsym((l), (n))
#endif
#ifdef __linux__
#include <fcntl.h>
#include <linux/userfaultfd.h>
#include <stdlib.h>
#include <sys/auxv.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

static void json_string(const char *s) {
    putchar('"');
    for (; *s; s++) {
        if (*s == '"' || *s == '\\') printf("\\%c", *s);
        else if ((unsigned char)*s < 0x20) printf("\\u%04x", *s);
        else putchar(*s);
    }
    putchar('"');
}

#if defined(__x86_64__) || defined(__i386__)
// The x86-64-v2 level from CPUID: SSE3, SSSE3, SSE4.1, SSE4.2, POPCNT, CMPXCHG16B and LAHF/SAHF. Every CPU with
// SSE4.2 has the others, but the level is what the v2 build is compiled for, so check them all.
static int x86_64_v2(void) {
    unsigned a, b, c, d;
    if (!__get_cpuid(1, &a, &b, &c, &d)) return 0;
    const unsigned sse3 = 1u << 0, ssse3 = 1u << 9, cx16 = 1u << 13, sse41 = 1u << 19, sse42 = 1u << 20,
                   popcnt = 1u << 23;
    if ((c & (sse3 | ssse3 | cx16 | sse41 | sse42 | popcnt)) != (sse3 | ssse3 | cx16 | sse41 | sse42 | popcnt))
        return 0;
    if (!__get_cpuid(0x80000001, &a, &b, &c, &d)) return 0;
    return (c & (1u << 0)) != 0;   // LAHF/SAHF
}

// The x86-64-v3 level from CPUID: AVX, AVX2, BMI1, BMI2, F16C, FMA, LZCNT and MOVBE, with the OS saving the YMM state.
// Not __builtin_cpu_supports("x86-64-v3"): clang reads that from the C runtime's CPU model, and libgcc before GCC 12
// (the Linux release's sysroot has GCC 10) has no level bits, so it answers false on every CPU.
static int x86_64_v3(void) {
    if (!x86_64_v2()) return 0;
    unsigned a, b, c, d;
    if (!__get_cpuid(1, &a, &b, &c, &d)) return 0;
    const unsigned fma = 1u << 12, movbe = 1u << 22, osxsave = 1u << 27, avx = 1u << 28, f16c = 1u << 29;
    if ((c & (fma | movbe | osxsave | avx | f16c)) != (fma | movbe | osxsave | avx | f16c)) return 0;
    unsigned lo, hi;
    __asm__("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
    if ((lo & 6) != 6) return 0;   // XMM and YMM state enabled by the OS
    if (!__get_cpuid_count(7, 0, &a, &b, &c, &d)) return 0;
    const unsigned bmi1 = 1u << 3, avx2 = 1u << 5, bmi2 = 1u << 8;
    if ((b & (bmi1 | avx2 | bmi2)) != (bmi1 | avx2 | bmi2)) return 0;
    if (!__get_cpuid(0x80000001, &a, &b, &c, &d)) return 0;
    return (c & (1u << 5)) != 0;   // LZCNT (ABM)
}

static void cpu_json(void) {
    int v2 = x86_64_v2(), v3 = x86_64_v3();
    printf("\"arch\": \"x86_64\", \"x86_64_v2\": %s, \"x86_64_v3\": %s, \"levels\": [%s], \"preferred\": %s, ",
           v2 ? "true" : "false", v3 ? "true" : "false", v3 ? "\"v3\", \"v2\"" : v2 ? "\"v2\"" : "",
           v3 ? "\"v3\"" : v2 ? "\"v2\"" : "null");
}
#elif defined(__aarch64__) && defined(__linux__)
// AT_HWCAP and AT_HWCAP2 bits of the Linux arm64 ABI (uapi asm/hwcap.h), spelled out for older headers as
// runtime/main.c does
enum { HWC_ASIMD = 1, HWC_ATOMICS = 8, HWC_LRCPC = 15, HWC_ASIMDDP = 20, HWC_USCAT = 25, HWC_ILRCPC = 26,
       HWC_FLAGM = 27, HWC2_FLAGM2 = 7, HWC2_FRINT = 8, HWC2_I8MM = 13, HWC2_BF16 = 14 };
#define HAS(word, bit) (((word) >> (bit)) & 1)

// A core of Qualcomm's Oryon design: implementer 0x51 with part 0x001 (Snapdragon X) or 0x002 (Snapdragon 8 Elite), the
// MIDR fields /proc/cpuinfo lists per core (runtime/sysinfo.c names them the same way).
static int oryon_cores(void) {
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return 0;
    char line[256];
    unsigned long impl = 0, found = 0;
    while (fgets(line, sizeof line, f)) {
        const char *colon = strchr(line, ':');
        if (!colon) continue;
        unsigned long v = strtoul(colon + 1, NULL, 0);
        if (!strncmp(line, "CPU implementer", 15)) impl = v;
        else if (!strncmp(line, "CPU part", 8) && impl == 0x51 && (v == 0x001 || v == 0x002)) found = 1;
    }
    fclose(f);
    return (int)found;
}

static void cpu_json(void) {
    unsigned long hw = getauxval(AT_HWCAP), hw2 = getauxval(AT_HWCAP2);
    int a64 = HAS(hw, HWC_ASIMD) && HAS(hw, HWC_ATOMICS) && HAS(hw, HWC_LRCPC) && HAS(hw, HWC_ASIMDDP);
    int oryon = a64 && HAS(hw, HWC_USCAT) && HAS(hw, HWC_ILRCPC) && HAS(hw, HWC_FLAGM) && HAS(hw2, HWC2_FLAGM2) &&
                HAS(hw2, HWC2_FRINT) && HAS(hw2, HWC2_I8MM) && HAS(hw2, HWC2_BF16);
    int cores = oryon_cores();
    printf("\"arch\": \"aarch64\", \"levels\": [%s], \"preferred\": %s, \"oryon_cores\": %s, ",
           oryon ? "\"oryon\", \"a64\"" : a64 ? "\"a64\"" : "",
           oryon && cores ? "\"oryon\"" : a64 ? "\"a64\"" : "null", cores ? "true" : "false");
}
#else
static void cpu_json(void) { printf("\"arch\": \"unknown\", \"levels\": [], \"preferred\": null, "); }
#endif

#ifdef __linux__
// The write-watch ABI of Linux 6.7 (uapi linux/userfaultfd.h) for older headers, as runtime/platform/linux.c has it
#ifndef UFFD_FEATURE_WP_ASYNC
#define UFFD_FEATURE_WP_UNPOPULATED (1 << 13)
#define UFFD_FEATURE_WP_ASYNC (1 << 15)
#endif
#ifndef UFFD_USER_MODE_ONLY
#define UFFD_USER_MODE_ONLY 1
#endif

// The first test runtime/platform/linux.c makes before it relies on the kernel's write tracking: a userfaultfd that
// takes asynchronous write protection. Kernels before 6.7, and kernels built without CONFIG_USERFAULTFD, fail it.
static int write_watch(void) {
    int fd = (int)syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK | UFFD_USER_MODE_ONLY);
    if (fd < 0) return 0;
    struct uffdio_api api = {.api = UFFD_API, .features = UFFD_FEATURE_WP_ASYNC | UFFD_FEATURE_WP_UNPOPULATED};
    int ok = ioctl(fd, UFFDIO_API, &api) == 0;
    close(fd);
    return ok;
}
#endif

int main(void) {
    printf("{");
    cpu_json();
#ifdef __linux__
    printf("\"page_size\": %ld, \"write_watch\": %s, ", sysconf(_SC_PAGESIZE), write_watch() ? "true" : "false");
#endif
    void *lib = open_lib();
    PFN_vkGetInstanceProcAddr gipa = lib ? (PFN_vkGetInstanceProcAddr)lib_sym(lib, "vkGetInstanceProcAddr") : NULL;
    if (!gipa) {
        printf("\"loader\": false, \"devices\": []}\n");
        return 0;
    }
    printf("\"loader\": true, ");
    PFN_vkCreateInstance create = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance");
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "totk-install",
                             .apiVersion = VK_API_VERSION_1_3};
    VkInstanceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    VkInstance inst = VK_NULL_HANDLE;
    VkResult r = create ? create(&ci, NULL, &inst) : VK_ERROR_INITIALIZATION_FAILED;
    if (r != VK_SUCCESS) {
        printf("\"instance\": %d, \"devices\": []}\n", (int)r);
        return 0;
    }
    PFN_vkEnumeratePhysicalDevices enumerate = (PFN_vkEnumeratePhysicalDevices)gipa(inst, "vkEnumeratePhysicalDevices");
    PFN_vkGetPhysicalDeviceProperties props = (PFN_vkGetPhysicalDeviceProperties)gipa(inst, "vkGetPhysicalDeviceProperties");
    PFN_vkDestroyInstance destroy = (PFN_vkDestroyInstance)gipa(inst, "vkDestroyInstance");
    VkPhysicalDevice devs[16];
    uint32_t n = 16;
    if (enumerate(inst, &n, devs) < 0) n = 0;
    printf("\"instance\": 0, \"devices\": [");
    for (uint32_t i = 0; i < n; i++) {
        VkPhysicalDeviceProperties p;
        props(devs[i], &p);
        printf("%s{\"name\": ", i ? ", " : "");
        json_string(p.deviceName);
        printf(", \"api\": [%u, %u, %u], \"type\": %d}", VK_API_VERSION_MAJOR(p.apiVersion),
               VK_API_VERSION_MINOR(p.apiVersion), VK_API_VERSION_PATCH(p.apiVersion), (int)p.deviceType);
    }
    printf("]}\n");
    destroy(inst, NULL);
    return 0;
}
