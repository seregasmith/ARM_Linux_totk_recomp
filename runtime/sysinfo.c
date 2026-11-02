// What a bug report needs about the machine (sysinfo.h). The OS calls sit behind platform.h; the GPU and driver come
// from the Vulkan device the game renders on.
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#if defined(__x86_64__) || defined(__i386__)
#include <cpuid.h>
#endif
#if defined(__aarch64__) && defined(__linux__)
#include <stdlib.h>
#endif

#include "cfg.h"
#include "host.h"
#include "platform/platform.h"
#include "sysinfo.h"
#include "vk.h"

void sysinfo_cpu_name(char *out, size_t n) {
    out[0] = 0;
#if defined(__x86_64__) || defined(__i386__)
    unsigned r[12];
    if (__get_cpuid_max(0x80000000, NULL) < 0x80000004) return;
    for (unsigned i = 0; i < 3; i++) __get_cpuid(0x80000002 + i, &r[i * 4], &r[i * 4 + 1], &r[i * 4 + 2], &r[i * 4 + 3]);
    char b[49];
    memcpy(b, r, 48);
    b[48] = 0;
    const char *p = b;
    while (*p == ' ') p++;
    snprintf(out, n, "%s", p);
    for (char *e = out + strlen(out); e > out && e[-1] == ' '; e--) e[-1] = 0;
#elif defined(__aarch64__) && defined(__linux__)
    // AArch64 has no brand string. Each core's MIDR (implementer and part number) names its design; the cores are listed
    // by design with their count, biggest first as the kernel numbers them last: "Cortex-X3 x1, Cortex-A715 x2, ...".
    static const struct { unsigned impl, part; const char *name; } known[] = {
        {0x51, 0x001, "Qualcomm Oryon"}, {0x51, 0x002, "Qualcomm Oryon"}, {0x41, 0xd03, "Cortex-A53"},
        {0x41, 0xd05, "Cortex-A55"},     {0x41, 0xd08, "Cortex-A72"},     {0x41, 0xd09, "Cortex-A73"},
        {0x41, 0xd0b, "Cortex-A76"},     {0x41, 0xd0d, "Cortex-A77"},     {0x41, 0xd41, "Cortex-A78"},
        {0x41, 0xd44, "Cortex-X1"},      {0x41, 0xd46, "Cortex-A510"},    {0x41, 0xd47, "Cortex-A710"},
        {0x41, 0xd48, "Cortex-X2"},      {0x41, 0xd4d, "Cortex-A715"},    {0x41, 0xd4e, "Cortex-X3"},
        {0x41, 0xd80, "Cortex-A520"},    {0x41, 0xd81, "Cortex-A720"},    {0x41, 0xd82, "Cortex-X4"},
        {0x41, 0xd85, "Cortex-X925"},    {0x41, 0xd87, "Cortex-A725"},    {0x61, 0, "Apple"},
    };
    struct { unsigned long long midr; int count; } kinds[8];
    int nk = 0;
    for (int cpu = 0; cpu < 256; cpu++) {
        char path[96], line[32];
        snprintf(path, sizeof path, "/sys/devices/system/cpu/cpu%d/regs/identification/midr_el1", cpu);
        FILE *f = fopen(path, "r");
        if (!f) break;
        int got = fgets(line, sizeof line, f) != NULL;
        fclose(f);
        if (!got) break;
        unsigned long long midr = strtoull(line, NULL, 16) & 0xff00fff0ull;   // implementer and part, not the revision
        int k = 0;
        while (k < nk && kinds[k].midr != midr) k++;
        if (k == nk && nk < 8) kinds[nk++] = (__typeof__(kinds[0])){midr, 0};
        if (k < nk) kinds[k].count++;
    }
    size_t at = 0;
    for (int k = nk - 1; k >= 0 && at + 1 < n; k--) {
        unsigned impl = (unsigned)(kinds[k].midr >> 24), part = (unsigned)(kinds[k].midr >> 4) & 0xfff;
        const char *name = NULL;
        for (size_t i = 0; i < sizeof known / sizeof *known && !name; i++)
            if (known[i].impl == impl && (known[i].part == part || (impl == 0x61 && !known[i].part))) name = known[i].name;
        int w = name ? snprintf(out + at, n - at, "%s%s x%d", at ? ", " : "", name, kinds[k].count)
                     : snprintf(out + at, n - at, "%simplementer %#x part %#x x%d", at ? ", " : "", impl, part,
                                kinds[k].count);
        if (w < 0) break;
        at += (size_t)w < n - at ? (size_t)w : n - at - 1;
    }
#endif
}

int sysinfo_log_folder(char *out, size_t n) {
    const char *log = rt_log_path();
    if (log) snprintf(out, n, "%s", log);
    else if (!plat_stderr_path(out, n)) return cfg_data_dir(out, n) != 0;   // no file yet: where the logs folder goes
    char *s = strrchr(out, '\\'), *f = strrchr(out, '/');
    if (f > s) s = f;
    if (!s) return 0;
    *s = 0;
    return 1;
}

typedef struct { char *buf; size_t n, at; } Out;

static void put(Out *o, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void put(Out *o, const char *fmt, ...) {
    if (o->at + 1 >= o->n) return;
    va_list ap;
    va_start(ap, fmt);
    int k = vsnprintf(o->buf + o->at, o->n - o->at, fmt, ap);
    va_end(ap);
    if (k > 0) o->at = o->at + (size_t)k < o->n ? o->at + (size_t)k : o->n - 1;
}

static void put_setting(const char *key, const char *value, int from_env, void *ctx) {
    put(ctx, from_env ? "  TOTK_%s=%s (environment)\n" : "  %s = %s\n", key, value);
}

int sysinfo_text(char *buf, size_t n) {
    if (!n) return 0;
    Out o = {buf, n, 0};
    buf[0] = 0;
    char s[256];
    put(&o, "TotK Recomp system info\n");
    put(&o, "OS: %s\n", plat_os_version(s, sizeof s) ? s : "unknown");
    sysinfo_cpu_name(s, sizeof s);
    put(&o, "CPU: %s, %u logical processors\n", s[0] ? s : "unknown", plat_cpu_count());
    float used, total, proc;
    plat_mem_info(&used, &total, &proc);
    if (total > 0) put(&o, "RAM: %.1f GB, %.1f GB in use, the game %.1f GB\n", total / 1024, used / 1024, proc / 1024);
    VkCtx *c = vk_ctx();
    if (c) {
        VkPhysicalDeviceDriverProperties dp = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES};
        VkPhysicalDeviceProperties2 p2 = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &dp};
        vkGetPhysicalDeviceProperties2(c->phys, &p2);
        const VkPhysicalDeviceProperties *p = &p2.properties;
        uint64_t vram = 0;
        for (uint32_t h = 0; h < c->mem_props.memoryHeapCount; h++)
            if (c->mem_props.memoryHeaps[h].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) vram += c->mem_props.memoryHeaps[h].size;
        put(&o, "GPU: %s (vendor 0x%04x, device 0x%04x), %.1f GB device-local memory\n", p->deviceName, p->vendorID,
            p->deviceID, vram / 1073741824.0);
        put(&o, "Driver: %s %s, driver version 0x%08x, Vulkan %u.%u.%u\n", dp.driverName, dp.driverInfo,
            p->driverVersion, VK_API_VERSION_MAJOR(p->apiVersion), VK_API_VERSION_MINOR(p->apiVersion),
            VK_API_VERSION_PATCH(p->apiVersion));
    } else {
        put(&o, "GPU: no Vulkan device yet\n");
    }
    put(&o, "Settings with a value:\n");
    size_t before = o.at;
    cfg_each_active(put_setting, &o);
    if (o.at == before) put(&o, "  none\n");
    return (int)o.at;
}
