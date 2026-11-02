#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <sys/random.h>

/* .NET 10 NativeAOT targets newer glibc symbols than our glibc 2.35 sysroot. */
unsigned long long __isoc23_strtoull(const char *s, char **end, int base) {
    return strtoull(s, end, base);
}

unsigned long __isoc23_strtoul(const char *s, char **end, int base) {
    return strtoul(s, end, base);
}

long long __isoc23_strtoll(const char *s, char **end, int base) {
    return strtoll(s, end, base);
}

int __isoc23_sscanf(const char *s, const char *format, ...) {
    va_list args;
    va_start(args, format);
    int result = vsscanf(s, format, args);
    va_end(args);
    return result;
}

void arc4random_buf(void *buffer, size_t length) {
    unsigned char *p = buffer;
    while (length) {
        ssize_t n = getrandom(p, length, 0);
        if (n <= 0) abort();
        p += n;
        length -= (size_t)n;
    }
}
