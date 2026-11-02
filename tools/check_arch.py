#!/usr/bin/env python3
"""Fails when runtime code uses x86-64 specifics outside a branch for x86-64, so the aarch64 build keeps compiling.

The runtime builds for x86-64 (Windows, Linux) and aarch64 (Linux; docs/ARM64_PORT_PLAN.md). x86 intrinsics and their
headers, __builtin_ia32_*, CPUID, x86 inline assembly and the x86 signal-context registers (REG_RIP, ...) are fine in a
preprocessor branch that only x86-64 compiles: #if/#elif conditions naming __x86_64__, _M_X64, __i386__ or _WIN32
(Windows builds are x86-64 only), or an #else/#elif after a condition naming __aarch64__. Files that only x86-64
compiles are skipped: the Win32 back ends (*_win.*, platform/win32.c), platform/fp_x86.h and runtime/fma.c (the
x86-64-v2 build's software fma). runtime/third_party is not ours and is not scanned.

Usage: python tools/check_arch.py [--root DIR]   (DIR holds runtime/; default: the repository root)
"""
import argparse
import re
import sys
from pathlib import Path

SKIP_NAMES = {'win32.c', 'fp_x86.h', 'fma.c', 'totk.rc'}
X86_HEADERS = re.compile(r'^\s*#\s*include\s*[<"]((?:[a-z0-9]*intrin|[xepstn]mmintrin|cpuid)\.h)[>"]', re.M)
USES = [
    (re.compile(r'\b_mm(?:256|512)?_\w+'), 'x86 intrinsic'),
    (re.compile(r'\b__m(?:64|128|256|512)[id]?\b'), 'x86 vector type'),
    (re.compile(r'\b__builtin_ia32_\w+'), 'x86 builtin'),
    (re.compile(r'\b__(?:get_)?cpuid(?:_count|_max)?\b'), 'CPUID'),
    (re.compile(r'\bREG_(?:RIP|RSP|RAX|RBP|ERR|EFL|TRAPNO)\b'), 'x86 signal context'),
]
# x86 assembly inside __asm__ strings: AT&T register names and x86-only mnemonics.
X86_ASM = re.compile(r'%%?(?:r[a-ds]x|r[sd]i|r[sb]p|rip|r(?:8|9|1[0-5])\b|e[a-ds]x|[xyz]mm\d)|\b(?:pushq|popq|pushfq|popfq|'
                     r'movq|leaq|jmpq|callq|ldmxcsr|stmxcsr|fnstcw|fldcw|xgetbv|vzeroupper|vmovdqa|ud2|rdtscp?)\b')
X86_MACROS = re.compile(r'\b(?:__x86_64__|_M_X64|__i386__|_M_IX86|__amd64__|_WIN32)\b')
ARM_MACROS = re.compile(r'\b(?:__aarch64__|_M_ARM64|__arm64__)\b')
COND = re.compile(r'^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$')


def strip(text):
    """Blank comments, keeping line numbers. String literals stay: inline assembly lives in them."""
    def blank(m):
        return re.sub(r'[^\n]', ' ', m.group(0))
    text = re.sub(r'/\*.*?\*/', blank, text, flags=re.S)
    return re.sub(r'//[^\n]*', blank, text)


def positive_x86(cond):
    """A condition that holds only on x86: names an x86 macro, not negated."""
    if not X86_MACROS.search(cond):
        return False
    return not re.search(r'!\s*(?:defined\s*\(?\s*)?(?:__x86_64__|_M_X64|__i386__|_M_IX86|__amd64__|_WIN32)', cond)


def x86_lines(lines):
    """For each line, whether only x86-64 compiles it (inside a branch for x86)."""
    out, stack = [], []   # stack of [x86 branch now, an earlier branch of this #if named aarch64]
    for line in lines:
        m = COND.match(line)
        if m:
            kind, cond = m.group(1), m.group(2)
            if kind in ('if', 'ifdef'):
                stack.append([positive_x86(cond) if kind == 'if' else bool(X86_MACROS.search(cond)),
                              bool(ARM_MACROS.search(cond))])
            elif kind == 'ifndef':
                stack.append([False, bool(ARM_MACROS.search(cond))])
            elif kind == 'elif' and stack:
                stack[-1][0] = positive_x86(cond) or (stack[-1][1] and not ARM_MACROS.search(cond)
                                                      and not cond.strip().startswith('0'))
                stack[-1][1] |= bool(ARM_MACROS.search(cond))
            elif kind == 'else' and stack:
                stack[-1][0] = stack[-1][1]
            elif kind == 'endif' and stack:
                stack.pop()
            out.append(True)
            continue
        out.append(any(frame[0] for frame in stack))
    return out


def scan(path):
    raw = path.read_text(encoding='utf-8', errors='replace')
    lines = strip(raw).split('\n')
    guarded = x86_lines(lines)
    hits = []
    for m in X86_HEADERS.finditer(raw):
        n = raw.count('\n', 0, m.start())
        if not guarded[n]:
            hits.append((n + 1, f'#include <{m.group(1)}>'))
    for n, line in enumerate(lines):
        if guarded[n] or line.lstrip().startswith('#'):
            continue
        for rx, what in USES:
            for m in rx.finditer(line):
                hits.append((n + 1, f'{what} {m.group(0)}'))
        if '"' in line and X86_ASM.search(line):
            hits.append((n + 1, f'x86 assembly {X86_ASM.search(line).group(0)}'))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=str(Path(__file__).resolve().parent.parent))
    a = ap.parse_args()
    rt = Path(a.root) / 'runtime'
    pats = ('*.c', '*.h', '*.cpp', '*.inc', 'ui/*.cpp', 'ui/*.h', 'platform/*.c', 'platform/*.h')
    files = sorted({p for pat in pats for p in rt.glob(pat)})
    bad = 0
    for p in files:
        if p.name in SKIP_NAMES or p.stem.endswith('_win'):
            continue
        for line, what in scan(p):
            print(f'FAIL {p.relative_to(a.root).as_posix()}:{line}: {what} outside an x86-64 branch')
            bad += 1
    print(f'check_arch: {len(files)} files, {bad} x86-only uses outside an x86-64 branch')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
