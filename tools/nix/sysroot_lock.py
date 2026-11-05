#!/usr/bin/env python3
"""Write tools/nix/sysroot-<arch>.lock.json: the Ubuntu 22.04 (jammy) .deb files tools/nix/default.nix unpacks into the
target sysroot, each with the URL and SHA-256 the archive's Packages index gives.

It starts from ROOTS (glibc, libstdc++, the libraries the game links, the Vulkan loader and what SDL3 builds against)
and adds their Depends and Pre-Depends, taking the first alternative the index has (a virtual package: its first
provider), and leaves out what only an installed system needs (SKIP). Each package is the newest version in jammy,
jammy-updates and jammy-security. build_docker.sh lock runs it; commit the file it writes.

Standard library only. Usage:
    python3 tools/nix/sysroot_lock.py --arch arm64|amd64 [--mirror URL] [--out FILE] [--packages FILE...]
    python3 tools/nix/sysroot_lock.py --arch arm64|amd64 --check
  --mirror    the archive: ports.ubuntu.com/ubuntu-ports for arm64, archive.ubuntu.com/ubuntu for amd64 by default; a
              dated snapshot.ubuntu.com URL pins the index too
  --packages  read these (uncompressed) Packages files instead of the archive's, for a test
  --check     exit 1 when the lock file is missing or was written for other ROOTS (build_docker.sh then writes it anew)
"""
import argparse
import json
import lzma
import re
import sys
import urllib.request
from pathlib import Path

SUITES = ('jammy', 'jammy-updates', 'jammy-security')
COMPONENTS = ('main', 'universe')
MIRRORS = {'arm64': 'https://ports.ubuntu.com/ubuntu-ports/', 'amd64': 'https://archive.ubuntu.com/ubuntu/'}
ROOTS = [
    # the C and C++ runtime the game and SDL3 link against
    'libc6-dev', 'linux-libc-dev', 'libgcc-11-dev', 'libstdc++-11-dev',
    # the game: the Vulkan loader (linked), Opus, libvpx and zstd (static)
    'libvulkan-dev', 'libopus-dev', 'libvpx-dev', 'libzstd-dev',
    # SDL3's back ends (their libraries are loaded at run time; the build needs headers and sonames)
    'libx11-dev', 'libxext-dev', 'libxrandr-dev', 'libxcursor-dev', 'libxfixes-dev', 'libxi-dev', 'libxss-dev',
    'libxtst-dev', 'libxkbcommon-dev', 'libwayland-dev', 'libdecor-0-dev', 'libegl-dev', 'libgles-dev', 'libgl-dev',
    'libdrm-dev', 'libgbm-dev', 'libasound2-dev', 'libpulse-dev', 'libpipewire-0.3-dev', 'libudev-dev', 'libdbus-1-dev',
    # the setup AppImage (tools/make_release_linux.py build_tk_xft): Tk rebuilt with Xft, so text uses system fonts
    'libxft-dev', 'libfontconfig-dev', 'libfreetype-dev',
]
# Packages a sysroot does without: interpreters, package-system and configuration machinery, build tools.
SKIP = re.compile(r'^(perl|perl-base|perl-modules-.*|python3.*|libpython3.*|debconf.*|dpkg|dpkg-dev|tzdata|adduser|'
                  r'passwd|login|init-system-helpers|lsb-base|sensible-utils|install-info|pkg-config|pkgconf.*|'
                  r'libglib2\.0-dev-bin|libglib2\.0-bin|shared-mime-info|dbus|dbus-.*|systemd.*|udev|'
                  r'mesa-.*-drivers|libgl1-mesa-dri|libllvm\d+|gcc-\d+|cpp-\d+|binutils.*|ucf|.*-doc)$')


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def parse(text):
    """Packages stanzas as dicts of field -> value (continuation lines joined)."""
    out = []
    for block in re.split(r'\n\s*\n', text):
        fields, last = {}, None
        for line in block.splitlines():
            if line[:1] in (' ', '\t') and last:
                fields[last] += '\n' + line.strip()
            elif ':' in line:
                last, _, value = line.partition(':')
                fields[last] = value.strip()
        if 'Package' in fields:
            out.append(fields)
    return out


def _order(c):
    if c == '~':
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _cmp_part(a, b):
    """Debian's comparison of an upstream version or revision: alternating non-digit and digit runs."""
    while a or b:
        na = re.match(r'[^0-9]*', a).group(0)
        nb = re.match(r'[^0-9]*', b).group(0)
        a, b = a[len(na):], b[len(nb):]
        for i in range(max(len(na), len(nb))):
            ca = _order(na[i]) if i < len(na) else 0
            cb = _order(nb[i]) if i < len(nb) else 0
            if ca != cb:
                return -1 if ca < cb else 1
        da = re.match(r'[0-9]*', a).group(0)
        db = re.match(r'[0-9]*', b).group(0)
        a, b = a[len(da):], b[len(db):]
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
    return 0


def vcmp(x, y):
    """Debian version comparison: -1, 0 or 1."""
    def split(v):
        epoch, _, rest = v.rpartition(':') if ':' in v else ('0', '', v)
        up, _, rev = rest.rpartition('-') if '-' in rest else (rest, '', '0')
        return int(epoch or 0), up, rev
    ex, ux, rx = split(x)
    ey, uy, ry = split(y)
    if ex != ey:
        return -1 if ex < ey else 1
    return _cmp_part(ux, uy) or _cmp_part(rx, ry)


def index(stanzas, arch):
    """name -> newest stanza for arch (or all), and virtual name -> providers."""
    best, provides = {}, {}
    for s in stanzas:
        if s.get('Architecture') not in (arch, 'all'):
            continue
        name = s['Package']
        if name not in best or vcmp(s['Version'], best[name]['Version']) > 0:
            best[name] = s
        for p in s.get('Provides', '').split(','):
            p = p.strip().split(' ')[0]
            if p:
                provides.setdefault(p, []).append(name)
    return best, provides


def resolve(best, provides, roots):
    """The closure of roots over Depends and Pre-Depends, without SKIP."""
    want, todo, missing = set(), list(roots), []
    while todo:
        name = todo.pop()
        if name in want or SKIP.match(name):
            continue
        if name not in best:
            missing.append(name)
            continue
        want.add(name)
        s = best[name]
        deps = ', '.join(x for x in (s.get('Pre-Depends'), s.get('Depends')) if x)
        for dep in filter(None, (d.strip() for d in deps.split(','))):
            alts = [re.sub(r'\s*\(.*?\)|:any|:native|\s*\[.*?\]', '', a).strip() for a in dep.split('|')]
            pick = next((a for a in alts if a in best), None)
            if pick is None:
                pick = next((provides[a][0] for a in alts if a in provides), None)
            if pick is None:
                if not any(SKIP.match(a) for a in alts):
                    missing.append(dep)
                continue
            todo.append(pick)
    return sorted(want), missing


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--arch', choices=('arm64', 'amd64'), required=True)
    ap.add_argument('--mirror', help='archive URL (default: Ubuntu\'s ports archive for arm64, main archive for amd64)')
    ap.add_argument('--out', type=Path, help='default tools/nix/sysroot-<arch>.lock.json')
    ap.add_argument('--packages', type=Path, nargs='*', help='local Packages files instead of the archive\'s (tests)')
    ap.add_argument('--check', action='store_true', help='only check that the lock file was written for ROOTS')
    a = ap.parse_args()
    out = a.out or Path(__file__).resolve().parent / f'sysroot-{a.arch}.lock.json'
    if a.check:
        try:
            lock = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            sys.exit(f'sysroot_lock: no lock file {out}')
        if lock.get('roots') != ROOTS:
            added = sorted(set(ROOTS) - set(lock.get('roots') or ()))
            sys.exit(f'sysroot_lock: {out.name} was written for other roots (new: {", ".join(added) or "none"})')
        print(f'sysroot_lock: {out.name} is current')
        return
    mirror = (a.mirror or MIRRORS[a.arch]).rstrip('/') + '/'
    stanzas = []
    if a.packages:
        for f in a.packages:
            stanzas += parse(f.read_text(encoding='utf-8', errors='replace'))
    else:
        for suite in SUITES:
            for comp in COMPONENTS:
                url = f'{mirror}dists/{suite}/{comp}/binary-{a.arch}/Packages.xz'
                print(f'sysroot_lock: {url}', file=sys.stderr)
                stanzas += parse(lzma.decompress(fetch(url)).decode('utf-8', errors='replace'))
    best, provides = index(stanzas, a.arch)
    names, missing = resolve(best, provides, ROOTS)
    if missing:
        sys.exit('sysroot_lock: not in the index: ' + '; '.join(sorted(set(missing))))
    packages = [{'name': n, 'version': best[n]['Version'], 'url': mirror + best[n]['Filename'],
                 'sha256': best[n]['SHA256']} for n in names]
    out.write_text(json.dumps({'release': 'Ubuntu 22.04 (jammy)', 'arch': a.arch, 'mirror': mirror,
                               'roots': ROOTS, 'packages': packages}, indent=1) + '\n', encoding='utf-8')
    print(f'sysroot_lock: {len(packages)} packages -> {out}')


if __name__ == '__main__':
    main()
