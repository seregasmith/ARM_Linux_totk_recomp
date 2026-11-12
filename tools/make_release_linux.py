"""Assemble the Linux release folder of docs/installer.md ("Linux release") and pack it as an AppImage.

    .venv/bin/python tools/make_release_linux.py --out build/release/TotK-Setup-linux \\
        [--appimage build/release/TotK-Setup-x86_64.AppImage]
    .venv/bin/python tools/make_release_linux.py --out build/release/TotK-Setup-linux --prove
    .venv/bin/python tools/make_release_linux.py --arch aarch64 --sysroot DIR --deps DIR --no-gpu \\
        --out build/release/TotK-Setup-linux-aarch64 [--appimage build/release/TotK-Setup-aarch64.AppImage]

The same model as the Windows release (tools/make_release.py, whose download cache, unpacking and checks this reuses): the
folder carries the setup and a toolchain, never game code; the player's machine builds the game. Needs network access
on the first run, make, the .NET 10 SDK and Python 3.12+ with Pillow (.venv).

--arch picks the machines the release is for (default: this machine's). It is built on a machine of that architecture:
NativeAOT builds the shader translator for the machine it runs on, and the checks run the folder's own programs.

    x86_64   Everything that runs on the player's machine is built against the Steam Runtime 3 "sniper" SDK sysroot
             (glibc 2.31), so the AppImage and the game it builds run on SteamOS and on any distribution at least as
             new. The sysroot, Vulkan headers and zstd are pinned downloads.
    aarch64  ARM64 Linux handhelds (docs/ARM64_PORT_PLAN.md, Phase 5): the Ubuntu 22.04 sysroot (glibc 2.35) and the
             libraries of tools/nix (SDL3 built against it, Opus, libvpx and zstd static, the Vulkan headers, VMA),
             passed as --sysroot and --deps (nix-build tools/nix -A sysroot / -A deps; build_docker.sh setup does it).

The folder holds:

    python/    python-build-standalone 3.14 (with tkinter) + the packages of tools/requirements-installer.txt; a .pth
               file puts the folder, tools/ and recomp/ on sys.path
    llvm/      LLVM 23 (clang, lld, llvm-ar, the clang resource headers) and llvm/sysroot: the files of the sysroot the
               game's compile and link read, nothing else (recorded while building the runtime library)
    runtime/   libtotkrt.a and libtotkrt-<level>.a (build_game.py --make-runtime-lib --sysroot: x86-64-v3 and v2, or
               a64 and oryon), the runtime headers, romfs.c, patches.c and ov_zstd.c (compiled per install), totk.png
               (the window icon), deps/ (x86_64: Vulkan headers 1.4.363, VMA, zstd built static, with SDL3, Opus and
               libvpx from the sysroot; aarch64: --deps)
    bin/       totk_xlate (+ .id), hactool, ninja, libSDL3.so.0 (the game finds it beside itself)
    recomp/, tools/, data/, installer/, licenses/, THIRD_PARTY_NOTICES.md, totk.ini.example, VERSION
    AppRun, totk-setup.desktop, totk-setup.png   the AppImage entry: python/bin/python3 -m installer.gui, or with
               --cli as its first argument the command-line setup (python -m installer)

--prove, an optional check, compiles a lifted game version's C
(generated/v121, else v143, and its build/extracted/<version>) and links build/release-linux-check/totk with the
folder's tools only, then runs the probes beside it.

The folder contains nothing derived from the game: check_content runs over it before the script finishes, then the
folder's own python runs a check-only installer pass and the window's --selftest. --no-gpu is for a machine without a
Vulkan device (a container): the installer pass may then stop at its Vulkan check instead of at the missing game
files, and --prove runs only the probes that need no GPU.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_game  # noqa: E402
import make_release as mr  # noqa: E402

ROOT = mr.ROOT
MAIN_ROOT = mr.MAIN_ROOT
WORK = mr.WORK
CHECK = MAIN_ROOT / 'build' / 'release-linux-check'

LLVM_VERSION = '23.1.4'
LLVM_MAJOR = LLVM_VERSION.split('.')[0]
SNIPER = '3.0.20261021.266025'
SNIPER_URL = 'https://repo.steampowered.com/steamrt-images-sniper/snapshots'
PINS = {
    'llvm-linux': (f'https://github.com/llvm/llvm-project/releases/download/llvmorg-{LLVM_VERSION}/'
                   f'LLVM-{LLVM_VERSION}-Linux-X64.tar.xz',
                   '25d814fda50d5fb0084b42be09605d8b1595d49d5e2e03a3345d7e112ff6f9f3'),
    'llvm-license': (f'https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-{LLVM_VERSION}/llvm/LICENSE.TXT',
                     '8d85c1057d742e597985c7d4e6320b015a9139385cff4cbae06ffc0ebe89afee'),   # the binary release carries none
    'sniper-sdk': (f'{SNIPER_URL}/{SNIPER}/com.valvesoftware.SteamRuntime.Sdk-amd64%2Ci386-sniper-sysroot.tar.gz',
                   '8f71947035f97925a311b2ed753f22efcf2f13d6f1631945b8e537f8903ce2cf'),
    'python-linux': ('https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'
                     'cpython-3.14.10%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz',
                     '1b38024924eb7f4cf0cab45d7b871d1f1991354190fccd2625ac7529a43786e2'),
    'tcl-linux-src': ('https://github.com/tcltk/tcl/archive/refs/tags/core-9-0-4.tar.gz',
                      '8c01bac92a9a8ce271b63d3aa28f09cde2c6762d6719ca671cdc8aee25391234'),
    'tk-linux-src': ('https://github.com/tcltk/tk/archive/refs/tags/core-9-0-4.tar.gz',
                     'ab9bd89fcff2e1248b7a9c097035f817fa0725285028c95686fdb92db65bc148'),
    'ninja-linux': ('https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-linux.zip',
                    '5749cbc4e668273514150a80e387a957f933c6ed3f5f11e03fb30955e2bbead6'),
    'zstd-src': ('https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz',
                 'eb33e51f49a15e023950cd7825ca74a4a2b43db8354825ac24fc1b7ee09e6fa3'),
    'vulkan-headers-src': ('https://github.com/KhronosGroup/Vulkan-Headers/archive/refs/tags/v1.4.363.tar.gz',
                           'cbaf687d3c59b9666fe080e7f8396b6b4f4d344a768ee6b337c59852f4526b68'),
    'vma-header': build_game.VMA_FETCH,
    # appimagetool and the type-2 runtime from release tags, not "continuous", whose uploads change under the same
    # URL and would stop the build at the hash check
    'appimagetool': ('https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage',
                     'ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0'),
    'appimage-runtime': ('https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64',
                         '2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d'),
    # aarch64: the same releases built for ARM64 Linux
    'llvm-linux-arm64': (f'https://github.com/llvm/llvm-project/releases/download/llvmorg-{LLVM_VERSION}/'
                         f'LLVM-{LLVM_VERSION}-Linux-ARM64.tar.xz',
                         '9b27cf263d711aa6ebfd4045ab241d947bd09b3cd19ffa5728f48a80c9eb3a91'),
    'python-linux-arm64': ('https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'
                           'cpython-3.14.10%2B20261026-aarch64-unknown-linux-gnu-install_only.tar.gz',
                           'dcc31ce7b9ddbbffac160da7fff0cc069054f43eca52382a7d0eebf94e985b88'),
    'ninja-linux-arm64': ('https://github.com/ninja-build/ninja/releases/download/v1.13.2/ninja-linux-aarch64.zip',
                          'fd2cacc8050a7f12a16a2e48f9e06fca5c14fc4c2bee2babb67b58be17a607fc'),
    'appimagetool-arm64': ('https://github.com/AppImage/appimagetool/releases/download/1.9.1/'
                           'appimagetool-aarch64.AppImage',
                           'f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158'),
    'appimage-runtime-arm64': ('https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-aarch64',
                               '00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444'),
}
mr.PINS.update(PINS)   # one cache (toolchain/release-dl) and one fetch for both releases
# tools/ files only the Linux install uses: the session profiler (installer/games.py SYSMON_FILES, TOTK_SYSMON)
SYSMON_TOOLS = ('sysmon_launch.sh', 'odin_sysmon.sh', 'sysmon_report.py')

# What differs between the target architectures: the pinned downloads (PINS names), the top folder of the LLVM
# release, the glibc the folder may need at most (check_glibc) and how the sysroot comes (sniper: a pinned download;
# nix: --sysroot and --deps from tools/nix).
TARGETS = {
    'x86_64': {'llvm': 'llvm-linux', 'llvm_top': f'LLVM-{LLVM_VERSION}-Linux-X64', 'python': 'python-linux',
               'ninja': 'ninja-linux', 'appimagetool': 'appimagetool', 'runtime': 'appimage-runtime',
               'glibc': (2, 34), 'sysroot': 'sniper'},
    'aarch64': {'llvm': 'llvm-linux-arm64', 'llvm_top': f'LLVM-{LLVM_VERSION}-Linux-ARM64',
                'python': 'python-linux-arm64', 'ninja': 'ninja-linux-arm64', 'appimagetool': 'appimagetool-arm64',
                'runtime': 'appimage-runtime-arm64', 'glibc': (2, 35), 'sysroot': 'nix'},
}
ARCH = build_game.HOST_ARCH if build_game.HOST_ARCH in TARGETS else 'x86_64'   # set_arch replaces both
TRIPLE = build_game.TRIPLES[ARCH]


def set_arch(arch):
    global ARCH, TRIPLE
    ARCH, TRIPLE = arch, build_game.TRIPLES[arch]


def per_arch(key):
    """TARGETS[key] of the architecture the release is for (set_arch)."""
    return TARGETS[ARCH][key]


def rtlib_name(cpu):
    """The runtime library of a code level: libtotkrt.a for the architecture's default (build_game.py DEFAULT_CPU),
    libtotkrt-<level>.a for the others. installer/layout.py Layout.runtime_lib_for finds them by the same names."""
    return 'libtotkrt.a' if cpu == build_game.DEFAULT_CPU[ARCH] else f'libtotkrt-{cpu}.a'


def cpu_levels():
    """The code levels the release ships, the default first (build_game.py CPU_LEVELS)."""
    default = build_game.DEFAULT_CPU[ARCH]
    return [default] + [c for c in build_game.CPU_LEVELS[ARCH] if c != default]

# What clang, lld and the runtime build need from the LLVM release (the rest of its 8 GB stays packed).
LLVM_MEMBERS = [f'bin/clang-{LLVM_MAJOR}', 'bin/clang', 'bin/clang++', 'bin/lld', 'bin/ld.lld', 'bin/llvm-ar',
                'bin/llvm-ranlib', 'bin/llvm-objcopy', 'bin/llvm-strip', f'lib/clang/{LLVM_MAJOR}/include']
# The parts of the sniper SDK a C/C++ compile and link for x86_64 can read (x86_64 only).
SNIPER_MEMBERS = ['usr/include', 'usr/share/pkgconfig', 'usr/lib/x86_64-linux-gnu', 'usr/lib/gcc/x86_64-linux-gnu',
                  'lib/x86_64-linux-gnu', 'lib64', 'usr/share/doc/libsdl3-0', 'usr/share/doc/libopus0',
                  'usr/share/doc/libvpx6', 'usr/share/doc/libc6', 'usr/share/doc/gcc-10-base',
                  'usr/share/doc/libvulkan1',
                  'usr/share/doc/libxft2', 'usr/share/doc/libxrender1']
# glibc's link libraries and their scripts (lib<name>.so, .a, _nonshared.a), and the GCC runtime pieces clang links.
GLIBC_LIBS = ['c', 'm', 'mvec', 'dl', 'pthread', 'rt', 'util']
GCC_LIBS = {'libgcc.a', 'libgcc_eh.a', 'libgcc_s.so', 'libstdc++.a', 'libsupc++.a'}
# The libraries of a release link (build_game.py LINK_LIBS with --deps: Opus, libvpx and zstd static).
RELEASE_LIBS = ['-lvulkan', '-lSDL3', '-l:libopus.a', '-l:libvpx.a', '-l:libzstd.a', '-l:libstdc++.a', '-lm',
                '-lpthread', '-ldl']


def run(cmd, **kw):
    return mr.run(cmd, **kw)


def tar_extract(name, dest, members, strip=0):
    """Extract only members (path prefixes inside the archive) of a pinned tarball with the system tar, once."""
    stamp = dest / '.unpacked'
    if stamp.is_file():
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    path = mr.fetch(name)
    t0 = time.time()
    run(['tar', '-xf', path, '-C', dest, f'--strip-components={strip}', '--wildcards', '--no-wildcards-match-slash',
         *members])
    print(f'{name}: unpacked in {time.time() - t0:.0f} s')
    stamp.write_text('')
    return dest


# ---- llvm/ and the sysroot -------------------------------------------------------------------------------------------

def build_llvm(out):
    top = per_arch('llvm_top')
    src = tar_extract(per_arch('llvm'), WORK / per_arch('llvm'), [f'{top}/{m}' for m in LLVM_MEMBERS], strip=1)
    dst = out / 'llvm'
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns('.unpacked'))
    strip(dst / 'bin', dst / 'bin' / 'llvm-strip')
    r = subprocess.run([str(dst / 'bin' / 'clang'), '--version'], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f'{dst / "bin" / "clang"} does not start:\n{r.stderr}')
    print(r.stdout.splitlines()[0])
    return dst


def strip(where, tool):
    """Drop the debug symbols of the ELF files under where (the LLVM and Python releases ship them: clang 270 MB)."""
    for p in sorted(where.rglob('*')):
        if p.is_file() and not p.is_symlink() and p.read_bytes()[:4] == b'\x7fELF':
            subprocess.run([str(tool), '--strip-unneeded', str(p)], check=True)


def full_sysroot(given=None):
    """x86_64: the sniper SDK's compile and link files in toolchain/release-work/sniper, absolute symlinks made relative
    (the SDK points some at /lib and /usr/lib, which outside its container are the host's). aarch64: the --sysroot
    tools/nix built, whose links are relative already."""
    if per_arch('sysroot') == 'nix':
        if not given or not (given / 'usr' / 'include' / 'stdio.h').is_file():
            sys.exit(f'--arch {ARCH} needs --sysroot: the Ubuntu 22.04 sysroot of tools/nix '
                     f'(nix-build tools/nix -A sysroot --argstr arch {ARCH})')
        return given.resolve()
    root = WORK / 'sniper'
    if (root / '.relinked').is_file() and (root / 'usr/share/doc/libxft2/copyright').is_file():
        return root
    # Older cached SDK extractions omitted the font libraries' architecture-independent pkg-config files and the
    # libXft and libXrender copyright files.
    (root / '.unpacked').unlink(missing_ok=True)
    tar_extract('sniper-sdk', root, SNIPER_MEMBERS)
    for p in root.rglob('*'):
        if p.is_symlink() and os.readlink(p).startswith('/'):
            target = root / os.readlink(p).lstrip('/')
            p.unlink()
            p.symlink_to(os.path.relpath(target, p.parent))
    (root / '.relinked').write_text('')
    return root


def sysroot_cc(llvm, sysroot):
    return f'{llvm / "bin" / "clang"} --target={TRIPLE} --sysroot={sysroot} -fuse-ld=lld'


def copy_writable(src, dst):
    """copytree with symlinks kept, then every directory and file made writable by the owner: files from the Nix store
    are read-only, and a read-only folder would stop the next run's rmtree of the release folder."""
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    for p in [dst, *dst.rglob('*')]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | 0o200)


# ---- runtime/deps, bin/ --------------------------------------------------------------------------------------------

def build_deps(out, llvm, sysroot, deps=None):
    d = out / 'runtime' / 'deps'
    if per_arch('sysroot') == 'nix':
        # tools/nix deps: include/ (Vulkan headers, VMA, SDL3) and lib/ (libSDL3.so*, libopus.a, libvpx.a, libzstd.a)
        if not deps or not (deps / 'include' / 'vma' / 'vk_mem_alloc.h').is_file():
            sys.exit(f'--arch {ARCH} needs --deps: the libraries of tools/nix '
                     f'(nix-build tools/nix -A deps --argstr arch {ARCH})')
        for sub in ('include', 'lib'):
            copy_writable(deps / sub, d / sub)
        return
    (d / 'include' / 'vma').mkdir(parents=True, exist_ok=True)
    (d / 'lib').mkdir(parents=True, exist_ok=True)
    vk = tar_extract('vulkan-headers-src', WORK / 'vulkan-headers', ['Vulkan-Headers-1.4.363/include',
                                                                     'Vulkan-Headers-1.4.363/LICENSE.md'], strip=1)
    for sub in ('vulkan', 'vk_video'):
        shutil.copytree(vk / 'include' / sub, d / 'include' / sub)
    shutil.copy2(mr.fetch('vma-header'), d / 'include' / 'vma' / 'vk_mem_alloc.h')
    zs = tar_extract('zstd-src', WORK / 'zstd', ['zstd-1.5.7'], strip=1)
    lib = zs / 'lib'
    if not (lib / 'libzstd.a').is_file():
        run(['make', '-C', lib, f'-j{os.cpu_count() or 4}', 'libzstd.a', f'CC={sysroot_cc(llvm, sysroot)}',
             f'AR={llvm / "bin" / "llvm-ar"}', 'ZSTD_LEGACY_SUPPORT=0', 'MOREFLAGS=-O2'], stdout=subprocess.DEVNULL)
    shutil.copy2(lib / 'libzstd.a', d / 'lib' / 'libzstd.a')
    for name in ('zstd.h', 'zstd_errors.h'):
        shutil.copy2(lib / name, d / 'include' / name)


def build_bin(out, llvm, sysroot):
    b = out / 'bin'
    b.mkdir(parents=True, exist_ok=True)
    zip_dir = mr.unpack(per_arch('ninja'))
    shutil.copy2(zip_dir / 'ninja', b / 'ninja')
    (b / 'ninja').chmod(0o755)
    hac = WORK / 'hactool-linux'
    run([sys.executable, ROOT / 'tools' / 'build_hactool.py', '--src', WORK / 'hactool-src', '--out', hac,
         '--cc', sysroot_cc(llvm, sysroot)])
    shutil.copy2(hac / 'hactool', b / 'hactool')
    xl = WORK / 'xlate-linux'
    run([sys.executable, ROOT / 'tools' / 'build_xlate.py', '--out', xl, '--llvm', llvm, '--sysroot', sysroot])
    shutil.copy2(xl / 'totk_xlate', b / 'totk_xlate')
    shutil.copy2(xl / 'totk_xlate.id', b / 'totk_xlate.id')
    # SDL3: the sniper sysroot's on x86_64, tools/nix's (copied into runtime/deps by build_deps) on aarch64
    sdl_dir = sysroot / 'usr' / 'lib' / TRIPLE if per_arch('sysroot') == 'sniper' else out / 'runtime' / 'deps' / 'lib'
    shutil.copy2((sdl_dir / 'libSDL3.so.0').resolve(), b / 'libSDL3.so.0')


# ---- runtime/ ----------------------------------------------------------------------------------------------------------

def runtime_layout():
    """(version, generated folder) the runtime libraries are compiled against, without game files. The runtime reads
    only MOD_COUNT and MOD_MAIN_BASE of the generated layout.h, which follow from the module set alone, the same in
    every supported update: recomp/layout.py write_runtime_only writes that layout.h from the module names of 1.2.1's
    expected hashes (tools/data). A runtime file that reads another base or a size fails to compile against it."""
    sys.path.insert(0, str(ROOT / 'recomp'))
    import json
    import layout
    names = json.loads((ROOT / 'tools' / 'data' / 'expected-v1.2.1.json').read_text(encoding='utf-8'))['exefs_sha256']
    gen = CHECK / 'layout-runtime-only'
    shutil.rmtree(gen, ignore_errors=True)
    mods = layout.write_runtime_only(names, gen)
    print(f'runtime layout: {", ".join(mods)}, from the expected hashes ({gen})')
    return 'v121', gen


def runtime_lib(out, llvm, sysroot):
    """One runtime library per code level (cpu_levels): libtotkrt.a (x86-64-v3) and libtotkrt-v2.a (x86-64-v2), so a
    machine without AVX2 can install too; on aarch64 libtotkrt.a (a64) and libtotkrt-oryon.a (the Snapdragon 8 Elite's
    cores). Returns the last level's build folder, whose dependency files used_sysroot_files reads."""
    version, gen = runtime_layout()
    rt = out / 'runtime'
    for cpu in cpu_levels():
        build = CHECK / f'rtlib-{cpu}'
        shutil.rmtree(build, ignore_errors=True)
        run([sys.executable, ROOT / 'tools' / 'build_game.py', '--make-runtime-lib', '--arch', ARCH,
             '--toolchain', llvm, '--sysroot', sysroot, '--cpu', cpu, '--deps', out / 'runtime' / 'deps',
             '--ninja', out / 'bin' / 'ninja', '--version', version, '--gen', gen, '--out', build])
        shutil.copy2(build / 'libtotkrt.a', rt / rtlib_name(cpu))
    for p in sorted((ROOT / 'runtime').rglob('*')):
        if p.suffix in mr.RUNTIME_SUFFIXES and p.is_file():
            (rt / p.relative_to(ROOT / 'runtime')).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, rt / p.relative_to(ROOT / 'runtime'))
    for name in build_game.INSTALL_SIDE:
        shutil.copy2(ROOT / 'runtime' / name, rt / name)
    from PIL import Image   # Tk loads no .ico off Windows: the window and the AppImage take a PNG
    with Image.open(ROOT / 'runtime' / 'totk.ico') as ico:
        ico.size = max(ico.ico.sizes())
        ico.convert('RGBA').save(rt / 'totk.png')
    return build


def used_sysroot_files(out, llvm, sysroot, rtlib_build):
    """The sysroot files a game build reads: the headers of the runtime's compile (its .d files), of the install-side
    sources and of hostcheck, and every library and object of a link (lld --trace) of the runtime library with the
    flags and libraries build_game.py uses, its references to the generated code left unresolved."""
    used = set()
    for dep in rtlib_build.rglob('*.d'):
        used.update(re.findall(r'\S+', dep.read_text(encoding='utf-8', errors='replace').replace('\\\n', ' ')))
    probe = CHECK / 'sysroot-probe'
    shutil.rmtree(probe, ignore_errors=True)
    probe.mkdir(parents=True)
    cc = [llvm / 'bin' / 'clang', f'--target={TRIPLE}', f'--sysroot={sysroot}', '-I', out / 'runtime',
          '-I', out / 'runtime' / 'deps' / 'include', '-I', CHECK / 'rtlib']
    for i, src in enumerate([*(out / 'runtime' / n for n in sorted(build_game.INSTALL_SIDE)),
                             ROOT / 'installer' / 'hostcheck.c']):
        # romfs.c needs the per-install romfs_config.h: whatever it reads up to there still lands in the .d file
        subprocess.run([str(c) for c in [*cc, '-fsyntax-only', '-MD', '-MF', probe / f'{i}.d', src]],
                       capture_output=True)
        if (probe / f'{i}.d').is_file():
            used.update(re.findall(r'\S+', (probe / f'{i}.d').read_text().replace('\\\n', ' ')))
    trace = subprocess.run([str(c) for c in [
        llvm / 'bin' / 'clang', f'--target={TRIPLE}', f'--sysroot={sysroot}', *build_game.LINK_FLAGS[:3],
        '-Wl,--trace', '-Wl,--unresolved-symbols=ignore-all', '-Wl,--whole-archive', out / 'runtime' / 'libtotkrt.a',
        '-Wl,--no-whole-archive', '-L', out / 'runtime' / 'deps' / 'lib',
        *RELEASE_LIBS, '-o', probe / 'trace.out']],
        capture_output=True, text=True)
    if trace.returncode:
        sys.exit(f'sysroot trace link failed:\n{trace.stderr[-3000:]}')
    used.update(line.split('(')[0] for line in trace.stdout.splitlines())
    root = str(sysroot.resolve())
    files = set()
    for p in used:
        path = Path(p)
        if not path.is_absolute():
            continue
        for q in (path, path.resolve()):   # the name read and, through symlinks, the file
            if str(q).startswith(root + '/') and q.exists():
                files.add(q)
    # The C library and kernel headers whole: which of them a compile reads depends on its flags (at -O1 glibc's inline
    # variants pull in bits/stdio.h and others), and the setup compiles the generated code, hostcheck and the
    # install-side sources with flags of its own
    inc = sysroot / 'usr' / 'include'
    arch = inc / TRIPLE
    files.update(p for p in [*inc.glob('*.h'), *arch.glob('*.h')] if p.is_file())
    for d in (arch / 'bits', arch / 'sys', arch / 'gnu', arch / 'asm', inc / 'linux', inc / 'asm-generic'):
        files.update(p for p in d.rglob('*') if p.is_file() or p.is_symlink())
    # The C runtime whole, for PIE links (hostcheck) as well as the game's: lld's trace names the libraries a link
    # script loads but not the script (libc.so, libm.so, libgcc_s.so), and clang's GCC detection needs the crt objects
    lib = sysroot / 'usr' / 'lib' / TRIPLE
    for name in GLIBC_LIBS:
        files.update(p for p in lib.glob(f'lib{name}[._]*') if p.suffix in ('.so', '.a') or '.so' in p.name)
        files.update(p for p in (sysroot / 'lib' / TRIPLE).glob(f'lib{name}.so*'))
    files.update(lib.glob('*crt*.o'))
    # the dynamic loader: /lib64/ld-linux-x86-64.so.2 on x86_64, /lib/ld-linux-aarch64.so.1 on aarch64
    files.update((sysroot / 'lib64').glob('ld-linux*'))
    files.update((sysroot / 'lib').glob('ld-linux*'))
    files.update((sysroot / 'lib' / TRIPLE).glob('ld-linux*'))
    files.update((sysroot / 'lib' / TRIPLE).glob('libgcc_s.so*'))
    gcc = gcc_dir(sysroot)
    files.update(p for p in gcc.iterdir() if p.name.startswith('crt') or p.name in GCC_LIBS)
    return files


def gcc_dir(sysroot):
    """The GCC version folder clang links with (crtbegin.o, libgcc.a, libstdc++): the newest one holding crtbegin.o, as
    clang's own GCC detection picks it. A sysroot may hold more version folders than the one with the C runtime."""
    root = sysroot / 'usr' / 'lib' / 'gcc' / TRIPLE
    found = [d for d in root.iterdir() if (d / 'crtbegin.o').is_file()] if root.is_dir() else []
    if not found:
        sys.exit(f'{root}: no GCC folder with crtbegin.o')
    return max(found, key=lambda d: [int(x) if x.isdigit() else 0 for x in d.name.split('.')])


def link_chain(path):
    """path and every link on the way to its file: libvulkan.so -> libvulkan.so.1 -> libvulkan.so.1.4.309."""
    hops = [path]
    while hops[-1].is_symlink() and len(hops) < 16:
        hops.append(hops[-1].parent / os.readlink(hops[-1]))
    return hops


def build_sysroot(out, sysroot, files):
    dst = out / 'llvm' / 'sysroot'
    files = {Path(os.path.normpath(h)) for f in files for h in link_chain(f)}
    for f in sorted(files):
        rel = f.relative_to(sysroot)
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        if f.is_symlink():
            if not (dst / rel).is_symlink():
                (dst / rel).symlink_to(os.readlink(f))
        elif not (dst / rel).exists():
            shutil.copy2(f, dst / rel)
    # a symlink copied above may name a file the build reads only through it
    for link in [p for p in dst.rglob('*') if p.is_symlink()]:
        target = (sysroot / link.relative_to(dst)).resolve()
        mirrored = dst / target.relative_to(sysroot.resolve())
        if not mirrored.exists():
            mirrored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, mirrored)
    print(f'sysroot: {sum(1 for p in dst.rglob("*") if p.is_file())} files, {mr.mib(mr.dir_size(dst))}')


# ---- python/ ---------------------------------------------------------------------------------------------------------

def build_tk_xft(out, llvm, sysroot):
    """Replace standalone Python's no-Xft Tcl/Tk with the same version using system TrueType fonts (#227)."""
    prefix = WORK / 'tcltk-xft'
    env = dict(os.environ, CC=sysroot_cc(llvm, sysroot),
               PKG_CONFIG_SYSROOT_DIR=str(sysroot),
               PKG_CONFIG_LIBDIR=os.pathsep.join(str(sysroot / p) for p in
                   (f'usr/lib/{TRIPLE}/pkgconfig', 'usr/share/pkgconfig')),
               PKG_CONFIG_PATH='')
    sources = {}
    for name in ('tcl', 'tk'):
        src = mr.unpack(f'{name}-linux-src') / f'{name}-core-9-0-4'
        sources[name] = src
        build = src / 'unix'
        options = [f'--prefix={prefix}', '--enable-shared', '--disable-zipfs', '--disable-rpath']
        if name == 'tk':
            options += [f'--with-tcl={prefix / "lib"}', '--enable-xft', '--disable-libcups', '--disable-xss']
        run([build / 'configure', *options], cwd=build, env=env)
        run(['make', f'-j{os.cpu_count() or 4}'], cwd=build, env=env)
        run(['make', 'install'], cwd=build, env=env)
    lib = prefix / 'lib'
    # A successful GUI selftest alone did not catch missing glyphs. Reject a no-Xft build before packaging it.
    symbols = subprocess.run(['nm', '-D', str(lib / 'libtcl9tk9.0.so')],
                             capture_output=True, text=True, check=True).stdout
    if not re.search(r'\bU Xft', symbols):
        sys.exit('Tcl/Tk build has no Xft font rendering')
    dst = out / 'python' / 'lib'
    for name in ('libtcl9.0.so', 'libtcl9tk9.0.so'):
        shutil.copy2(lib / name, dst / name)
    # libXft is not on every desktop (the old Tk did not need it) and without it _tkinter fails to import, so the
    # window would not open at all. python's RPATH ($ORIGIN/../lib) finds these beside the Tk library.
    sys_lib = sysroot / 'usr' / 'lib' / TRIPLE
    for pattern in ('libXft.so.2*', 'libXrender.so.1*'):
        for f in sys_lib.glob(pattern):
            shutil.copy2(f, dst / f.name, follow_symlinks=False)
    for name in ('tcl9.0', 'tk9.0'):
        shutil.rmtree(dst / name)
        shutil.copytree(lib / name, dst / name, symlinks=True)
    licenses = out / 'licenses'
    licenses.mkdir(exist_ok=True)
    for name, src in sources.items():
        shutil.copy2(src / 'license.terms', licenses / f'{name}-license.terms')


def build_python(out, llvm, sysroot):
    src = mr.unpack(per_arch('python')) / 'python'
    py = out / 'python'
    shutil.copytree(src, py, symlinks=True,
                    ignore=shutil.ignore_patterns('test', 'idlelib', 'turtledemo', '__pycache__', '*.a'))
    exe = py / 'bin' / 'python3'
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK='1')
    run([exe, '-m', 'pip', 'install', '--no-compile', '--only-binary', ':all:', '-r',
         ROOT / 'tools' / 'requirements-installer.txt'], env=env, stdout=subprocess.DEVNULL)
    run([exe, '-m', 'pip', 'uninstall', '-y', 'pip'], stdout=subprocess.DEVNULL)   # the setup installs nothing
    build_tk_xft(out, llvm, sysroot)
    strip(py, out / 'llvm' / 'bin' / 'llvm-strip')
    site = next(py.glob('lib/python3.*/site-packages'))
    # The release folder, tools/ and recomp/ on sys.path, as the Windows python's ._pth does: `python -m installer` and
    # the tools importing their siblings work from any directory
    (site / 'totk-release.pth').write_text('../../../..\n../../../../tools\n../../../../recomp\n', encoding='utf-8')
    check = subprocess.run([str(exe), '-c', 'import tkinter, numpy, lz4.block, cryptography, cffi, pycparser; '
                            'print("python ok: Tcl", tkinter.Tcl().eval("info patchlevel"), "numpy", numpy.__version__)'],
                           cwd='/', capture_output=True, text=True)
    if check.returncode:
        sys.exit('bundled python check failed:\n' + check.stderr)
    print(check.stdout.strip())


# ---- licences, AppImage entry ------------------------------------------------------------------------------------------

def collect_licenses(out, sysroot, deps=None):
    lic = out / 'licenses'
    lic.mkdir(parents=True, exist_ok=True)
    doc = sysroot / 'usr' / 'share' / 'doc'
    # libstdc++ is linked static from the sysroot's GCC (10 in sniper, 11 in Ubuntu 22.04); libvpx is libvpx6 in sniper,
    # libvpx7 in Ubuntu 22.04
    gcc_base = f'gcc-{gcc_dir(sysroot).name.split(".")[0]}-base'
    vpx = sorted(doc.glob('libvpx[0-9]*'))
    if per_arch('sysroot') == 'sniper':
        own = [(doc / 'libsdl3-0' / 'copyright', 'SDL3-copyright.txt'),
               (WORK / 'vulkan-headers' / 'LICENSE.md', 'vulkan-headers-LICENSE.txt'),
               (WORK / 'zstd' / 'LICENSE', 'zstd-LICENSE.txt')]
    else:   # tools/nix deps: SDL3 and the Vulkan headers from their sources, zstd from Ubuntu's package
        own = [(deps / 'share' / 'licenses' / 'SDL3-LICENSE.txt', 'SDL3-LICENSE.txt'),
               (deps / 'share' / 'licenses' / 'vulkan-headers-LICENSE.md', 'vulkan-headers-LICENSE.txt'),
               (doc / 'libzstd1' / 'copyright', 'zstd-copyright.txt')]
    pairs = [
        (mr.fetch('llvm-license'), 'llvm-LICENSE.txt'),
        *own,
        (doc / 'libopus0' / 'copyright', 'opus-copyright.txt'),
        ((vpx[-1] if vpx else doc / 'libvpx') / 'copyright', 'libvpx-copyright.txt'),
        (doc / 'libc6' / 'copyright', 'glibc-copyright.txt'),
        (doc / gcc_base / 'copyright', 'libstdc++-copyright.txt'),
        (doc / 'libvulkan1' / 'copyright', 'vulkan-loader-copyright.txt'),
        (doc / 'libxft2' / 'copyright', 'libXft-copyright.txt'),
        (doc / 'libxrender1' / 'copyright', 'libXrender-copyright.txt'),
        (out / 'python' / 'lib' / f'python{sys_version(out)}' / 'LICENSE.txt', 'python-LICENSE.txt'),
        (ROOT / 'runtime' / 'shaders' / 'ffx' / 'LICENSE.txt', 'FidelityFX-FSR-LICENSE.txt'),
        (WORK / 'hactool-linux' / 'LICENSE', 'hactool-LICENSE.txt'),
        (MAIN_ROOT / 'refs' / 'MacNativeGames' / 'LICENSE', 'MacNativeGames-LICENSE.txt'),
    ]
    for src, name in pairs:
        shutil.copy2(src, lic / name)
    import tarfile   # Debian's libvpx copyright carries no patent grant: the WebM PATENTS text from the pinned source tag
    with tarfile.open(mr.fetch('libvpx-src')) as t:
        (lic / 'libvpx-PATENTS.txt').write_bytes(t.extractfile('libvpx-1.17.0/PATENTS').read())
    head = (out / 'runtime' / 'deps' / 'include' / 'vma' / 'vk_mem_alloc.h').read_text(encoding='utf-8').split('*/')[0]
    (lic / 'vulkan-memory-allocator-LICENSE.txt').write_text(head.lstrip('/*') + '\n', encoding='utf-8')


def sys_version(out):
    return next((out / 'python' / 'lib').glob('python3.*')).name.removeprefix('python')


APPRUN = '''#!/bin/sh
# The AppImage entry: the setup window, run by the bundled Python from the read-only mount (no bytecode written there).
# With --cli as the first argument, the command-line setup instead (installer/cli.py: python -m installer --help).
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONDONTWRITEBYTECODE=1
# Ignore ~/.local/lib/pythonX.Y/site-packages: a user-installed package such as PyPI's `installer` would shadow ours
export PYTHONNOUSERSITE=1
export TCL_LIBRARY="$HERE/python/lib/tcl9.0"
export TK_LIBRARY="$HERE/python/lib/tk9.0"
if [ "${1:-}" = --cli ]; then
    shift
    exec "$HERE/python/bin/python3" -m installer --root "$HERE" "$@"
fi
exec "$HERE/python/bin/python3" -m installer.gui "$@"
'''

DESKTOP = '''[Desktop Entry]
Type=Application
Name=TotK Recomp Setup
Comment=Builds Tears of the Kingdom for this PC from your own game files
Exec=AppRun
Icon=totk-setup
Categories=Game;
Terminal=false
'''


def build_entry(out):
    (out / 'AppRun').write_text(APPRUN, encoding='utf-8')
    (out / 'AppRun').chmod(0o755)
    (out / 'totk-setup.desktop').write_text(DESKTOP, encoding='utf-8')
    shutil.copy2(out / 'runtime' / 'totk.png', out / 'totk-setup.png')
    (out / '.DirIcon').symlink_to('totk-setup.png')


def pack_appimage(appdir, runtime, out, mksquashfs, work):
    """The AppImage out: the type-2 runtime, then appdir as a squashfs image, which is made in the folder work first
    (the output folder may be a slow share: build_docker.sh writes to the host's folder). zstd is what the runtime
    reads; root-owned files and no extended attributes, as appimagetool makes them. out is written under a .part name
    until it is complete."""
    image = Path(work) / 'image.squashfs'
    subprocess.run([mksquashfs, str(appdir), str(image), '-noappend', '-root-owned', '-no-xattrs', '-comp', 'zstd',
                    '-quiet', '-no-progress'], check=True)
    part = out.with_name(out.name + '.part')
    with part.open('wb') as f:
        f.write(Path(runtime).read_bytes())
        with image.open('rb') as src:
            shutil.copyfileobj(src, f, 8 << 20)
    image.unlink()
    part.chmod(0o755)
    os.replace(part, out)


def write_appimage(out, path):
    """The folder (an AppDir: AppRun, the desktop entry, the icon) behind the pinned type-2 runtime. With mksquashfs on
    PATH (build_docker.sh's shell.nix has it) pack_appimage packs it, as appimagetool does with --runtime-file. An
    emulated container needs that: an AppImage cannot start under binfmt emulation (its ELF header carries the AppImage
    magic in e_ident's padding, which the emulator's binfmt_misc entry does not match), so the appimagetool AppImage
    does not run there. Without mksquashfs, appimagetool."""
    runtime = mr.fetch(per_arch('runtime'))
    path.parent.mkdir(parents=True, exist_ok=True)
    mksquashfs = shutil.which('mksquashfs')
    if mksquashfs:
        import tempfile
        with tempfile.TemporaryDirectory(prefix='setup-appimage-') as work:
            pack_appimage(out, runtime, path, mksquashfs, work)
    else:
        tool = mr.fetch(per_arch('appimagetool'))
        tool.chmod(0o755)
        env = dict(os.environ, ARCH=ARCH, APPIMAGE_EXTRACT_AND_RUN='1')
        run([tool, '--no-appstream', '--runtime-file', runtime, out, path], env=env)
    print(f'{path}: {mr.mib(path.stat().st_size)}')


# ---- checks -------------------------------------------------------------------------------------------------------------

# The newest glibc any program or library in the folder may need (TARGETS glibc). x86_64: the official LLVM binaries
# and the cryptography wheel are built against 2.34 (Ubuntu 22.04, Debian 12, SteamOS 3 have it); the game itself links
# the sniper sysroot's 2.31. aarch64: the LLVM binaries need 2.34 too, and what is built against the Ubuntu 22.04
# sysroot (the game, hactool, totk_xlate, SDL3) may use its 2.35 (installer/steps.py GLIBC_NEEDED).


def check_glibc(out):
    """Every ELF file in the folder (llvm/sysroot aside: glibc itself, read by the link) needs the glibc of TARGETS or
    older; prints which files set the floor."""
    limit = per_arch('glibc')
    newest, by = (0, 0), []
    for p in sorted(out.rglob('*')):
        if not p.is_file() or p.is_symlink() or out / 'llvm' / 'sysroot' in p.parents:
            continue
        with open(p, 'rb') as f:
            if f.read(4) != b'\x7fELF':
                continue
        text = subprocess.run(['readelf', '-V', str(p)], capture_output=True, text=True).stdout
        need = max((tuple(map(int, v.split('.'))) for v in re.findall(r'GLIBC_(\d+\.\d+(?:\.\d+)?)', text)),
                   default=(0, 0))
        if need > newest:
            newest, by = need, []
        if need == newest:
            by.append(str(p.relative_to(out)))
    floor = '.'.join(map(str, limit))
    if newest > limit:
        sys.exit(f'glibc check: {", ".join(by[:8])} need glibc {".".join(map(str, newest))}, above {floor}')
    print(f'glibc check: the folder needs glibc {".".join(map(str, newest))} (floor {floor}), set by '
          f'{", ".join(by[:6])}{" and others" if len(by) > 6 else ""}')


# Where the check-only installer pass of a machine without a Vulkan device (--no-gpu) stops: its host check, after the
# toolchain compiled and ran installer/hostcheck.c
NO_GPU_ERRORS = {'check.vulkan_missing', 'check.vulkan_no_device', 'check.vulkan_too_old'}


def check_bundle(out, gpu=True):
    """The folder's own python runs the installer without game files and the window's selftest. Without a GPU
    (gpu False) the installer may stop at its Vulkan check instead of at the game files."""
    import json
    chk = CHECK / 'bundle'
    shutil.rmtree(chk, ignore_errors=True)
    py = out / 'python' / 'bin' / 'python3'
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env.pop('TOTK_CPU', None)   # a build's code level (build_docker.sh passes it on) is no request to the setup
    r = subprocess.run([str(py), '-m', 'installer', '--lang', 'en-US', '--dest', str(chk / 'install'), '--temp',
                        str(chk / 'temp'), '--root', str(out)], cwd='/', env=env, capture_output=True, text=True,
                       timeout=600)
    events = [json.loads(line) for line in r.stdout.splitlines() if line.startswith('{')]
    errors = [e['id'] for e in events if e['type'] == 'error']
    started = any(e['type'] == 'start' and e.get('release') == (out / 'VERSION').read_text().strip() for e in events)
    steps = [e['phase'] for e in events if e['type'] == 'progress']
    at_inputs = errors == ['check.inputs_needed'] and 'inputs' in steps
    at_vulkan = not gpu and len(errors) == 1 and errors[0] in NO_GPU_ERRORS and 'host' in steps
    if not (at_inputs or at_vulkan) or not started:
        sys.exit(f'bundle check: exit {r.returncode}, errors {errors}, release read {started}\n{r.stdout[-2000:]}\n'
                 f'{r.stderr[-2000:]}')
    if at_inputs:
        print(f'bundle check: the installer ran the host check from the folder and stopped at the game files '
              f'(exit {r.returncode})')
    else:
        print(f'bundle check: the installer compiled and ran the host check from the folder and stopped at '
              f'{errors[0]}, as on a machine without a Vulkan device (--no-gpu; exit {r.returncode})')
    run([py, '-c', 'import tkinter as tk; from tkinter import font; '
         'root = tk.Tk(); root.withdraw(); '
         'fonts = [font.nametofont("TkDefaultFont"), font.Font(family="DejaVu Sans"), '
         'font.Font(family="DejaVu Sans Mono")]; '
         'missing = [(f.actual("family"), c) for f in fonts for c in "УстановкаЁёЙй" if f.measure(c) <= 0]; '
         'root.destroy(); assert not missing, f"Cyrillic glyphs missing: {missing}"'], cwd='/', env=env)
    r = subprocess.run([str(py), '-m', 'installer.gui', '--selftest', '--root', str(out)], cwd='/', env=env,
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        sys.exit(f'bundle check: window selftest exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
    print('bundle check: the window built every screen in both languages and closed')


# The probes that need no Vulkan device (--no-gpu): the self-test and the kernel probe
CPU_PROBES = ['--selftest', '--probe-kernel']


def prove(out, cpu=None, gpu=True, run_probes=True, run_with=()):
    """Compile the generated C and link totk with the folder's tools only, then run the probes beside it: all of them,
    the CPU ones without a GPU (gpu False), none with run_probes False (a code level this machine cannot run). run_with
    goes before the exe, as a loader that gives it the sysroot's libraries in a container without them."""
    cpu = cpu or build_game.DEFAULT_CPU[ARCH]
    out = out.resolve()
    version = mr.lifted_version()
    gen = MAIN_ROOT / 'generated' / version
    ext = MAIN_ROOT / 'build' / 'extracted' / version
    chk = CHECK / 'game'
    env = dict(os.environ, PATH=os.pathsep.join([str(out / 'llvm' / 'bin'), str(out / 'bin'), '/usr/bin', '/bin']))
    t0 = time.time()
    run([out / 'python' / 'bin' / 'python3', out / 'tools' / 'build_game.py', '--toolchain', out / 'llvm',
         '--arch', ARCH, '--cpu', cpu,
         '--sysroot', out / 'llvm' / 'sysroot', '--deps', out / 'runtime' / 'deps', '--runtime', out / 'runtime',
         '--runtime-lib', out / 'runtime' / rtlib_name(cpu),
         '--ninja', out / 'bin' / 'ninja', '--version', version,
         '--gen', gen, '--out', chk, '--exefs', ext / 'exefs', '--romfs', ext / 'romfs', '--profile', chk / 'profile'],
        env=env)
    print(f'prove: build {time.time() - t0:.0f} s')
    shutil.copy2(out / 'bin' / 'libSDL3.so.0', chk / 'libSDL3.so.0')
    needed = subprocess.run(['readelf', '-d', str(chk / 'totk')], capture_output=True, text=True).stdout
    print('prove: needs', ', '.join(re.findall(r'\[(lib[^\]]+)\]', needed)))
    glibc = sorted(set(re.findall(r'GLIBC_(\d+\.\d+)', subprocess.run(['readelf', '-V', str(chk / 'totk')],
                                                                         capture_output=True, text=True).stdout)),
                   key=lambda v: tuple(map(int, v.split('.'))))
    print(f'prove: newest glibc symbol version {glibc[-1] if glibc else "none"}')
    if not run_probes:
        print(f'prove: {cpu} built and linked; its probes are not run here')
        return
    failed = []
    for probe in ['--selftest', *mr.PROBES] if gpu else CPU_PROBES:
        r = subprocess.run([*run_with, str(chk / 'totk'), probe], cwd=chk, capture_output=True, text=True, timeout=900)
        tail = (r.stdout.strip().splitlines() or [''])[-1]
        print(f'{probe}: exit {r.returncode}: {tail}')
        if r.returncode:
            failed.append(probe)
    if failed:
        sys.exit(f'probes failed: {failed}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', type=Path, required=True, help='release folder (replaced)')
    ap.add_argument('--appimage', type=Path, help='also pack the folder as this AppImage')
    ap.add_argument('--prove', action='store_true', help='only run the compile-and-probe check on an existing folder')
    ap.add_argument('--arch', choices=sorted(TARGETS), default=ARCH,
                    help=f'the machines the release is for (default: this one, {build_game.HOST_ARCH}); it is built on '
                         'one of them')
    ap.add_argument('--sysroot', type=Path, help='aarch64: the Ubuntu 22.04 sysroot of tools/nix (-A sysroot)')
    ap.add_argument('--deps', type=Path, help='aarch64: the libraries of tools/nix (-A deps)')
    ap.add_argument('--cpu', help='--prove: build and run the check for this code level (default: the architecture\'s '
                                  'default, v3 or a64)')
    ap.add_argument('--no-gpu', action='store_true',
                    help='no Vulkan device here (a container): the installer pass may stop at its Vulkan check, and '
                         '--prove runs only the probes that need no GPU')
    ap.add_argument('--no-run', action='store_true', help='--prove: build and link only (a level this CPU cannot run)')
    ap.add_argument('--run-with', default='', help='--prove: a command line put before the exe when a probe runs, such '
                                                   'as the sysroot\'s loader with --library-path')
    args = ap.parse_args()
    if args.arch != build_game.HOST_ARCH:
        sys.exit(f'make_release_linux: a release for {args.arch} is built on {args.arch} (this machine is '
                 f'{build_game.HOST_ARCH}): NativeAOT and the checks run the target\'s programs')
    set_arch(args.arch)
    if args.cpu and args.cpu not in build_game.CPU_LEVELS[ARCH]:
        sys.exit(f'--cpu {args.cpu}: the code levels of {ARCH} are {", ".join(cpu_levels())}')
    out = args.out.resolve()
    if args.prove:
        import shlex
        prove(out, args.cpu, gpu=not args.no_gpu, run_probes=not args.no_run, run_with=shlex.split(args.run_with))
        return
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    deps = args.deps.resolve() if args.deps else None
    llvm = build_llvm(out)
    sysroot = full_sysroot(args.sysroot)
    build_deps(out, llvm, sysroot, deps)
    build_bin(out, llvm, sysroot)
    rtlib = runtime_lib(out, llvm, sysroot)
    build_sysroot(out, sysroot, used_sysroot_files(out, llvm, sysroot, rtlib))
    build_python(out, llvm, sysroot)
    mr.build_sources(out, {})   # no profiles: the Linux setup compiles without one (installer/layout.py, pgo_dir)
    for name in SYSMON_TOOLS:   # the session profiler, which the install copies beside the game (installer/games.py)
        shutil.copy2(ROOT / 'tools' / name, out / 'tools' / name)
    collect_licenses(out, sysroot, deps)
    build_entry(out)
    mr.check_folder(out)
    check_glibc(out)
    check_bundle(out, gpu=not args.no_gpu)
    mr.sizes(out)
    if args.appimage:
        write_appimage(out, args.appimage.resolve())


if __name__ == '__main__':
    main()
