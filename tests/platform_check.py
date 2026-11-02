"""Build and run tests/platform_check.c with runtime/platform/linux.c and common.c: fiber switches, the write-fault hook
and, on aarch64, kern_ap_stub (runtime/kern_ap_stub.inc) under real interruptions. Linux only; no game files.

The compiler, the target and the code level come from $CC, $TOTK_TEST_ARCH and the defaults of tests/diff/common.py,
and $TOTK_TEST_RUN runs a cross build (qemu-aarch64 -L SYSROOT), as for the differential tests. The platform layer
includes <SDL3/SDL_clipboard.h>: --cflags '-I DIR' names its directory when SDL3's headers are elsewhere.

Usage: platform_check.py [--cflags FLAGS]
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests' / 'diff'))
from common import compiler_cmd, default_cflags, run_prefix  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cflags', default='', help='extra compiler flags, e.g. -I for the SDL3 headers')
    args = ap.parse_args()
    if os.name == 'nt':
        print('platform_check: the Linux platform layer only; skipped on Windows')
        return 0
    work = ROOT / 'build' / 'test' / 'platform'
    work.mkdir(parents=True, exist_ok=True)
    exe = work / 'platform_check'
    rt = ROOT / 'runtime'
    srcs = [ROOT / 'tests' / 'platform_check.c', rt / 'platform' / 'linux.c', rt / 'platform' / 'common.c']
    cmd = compiler_cmd() + ['-O2', '-g', '-fuse-ld=lld', '-fno-strict-aliasing', *default_cflags().split(),
                            *args.cflags.split(), '-I', str(rt), *map(str, srcs), '-o', str(exe), '-lpthread', '-lm',
                            '-ldl']
    subprocess.check_call(cmd)
    return subprocess.run(run_prefix() + [str(exe)], timeout=120).returncode


if __name__ == '__main__':
    sys.exit(main())
