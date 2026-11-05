"""Guest address layout of the loaded modules, computed from their sizes.

Guest addresses equal host addresses, and the lifter writes absolute addresses into the generated C, so the
generator and the runtime must agree on every module base. This is the single place that decides them: the generator
calls layout() and writes the result into generated/<version>/layout.h and layout.json, which the runtime and the
build read.

Modules are placed in Horizon's load order (rtld is dropped; the host links the modules):
main, subsdk0..9, sdk. Each base is aligned to 2 MiB. Everything must stay below 2^39, the limit of the addresses
IPC buffers can encode.
"""
import json
from pathlib import Path

from nso import Nso

FIRST_BASE = 0x7000000000
ALIGN = 0x200000
ADDRESS_LIMIT = 1 << 39
ORDER = ['main'] + [f'subsdk{i}' for i in range(10)] + ['sdk']


def align_up(v, a=ALIGN):
    return (v + a - 1) & ~(a - 1)


def layout(exefs):
    """[(name, base, size)] for the modules present in the ExeFS directory, in load order."""
    exefs = Path(exefs)
    out, base = [], FIRST_BASE
    for name in ORDER:
        path = exefs / name
        if not path.exists():
            continue
        size = Nso(str(path)).size
        out.append((name, base, size))
        base = align_up(base + size)
    if base > ADDRESS_LIMIT:
        raise ValueError(f'modules end at {base:#x}, above 2^39')
    return out


def write(mods, outdir):
    """layout.json for Python tools and layout.h for the runtime."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / 'layout.json').write_text(json.dumps(
        [{'name': n, 'base': f'{b:#x}', 'size': f'{s:#x}'} for n, b, s in mods], indent=1) + '\n')
    lines = ['// Module layout chosen by recomp/layout.py. Generated; do not edit.', '#pragma once', '']
    for n, b, s in mods:
        lines.append(f'#define MOD_{n.upper()}_BASE {b:#x}ull')
        lines.append(f'#define MOD_{n.upper()}_SIZE {s:#x}ull')
    lines += ['', f'#define MOD_COUNT {len(mods)}',
              '#define MOD_LIST(X) ' + ' '.join(f'X({n}, {n.upper()})' for n, _, _ in mods), '']
    (outdir / 'layout.h').write_text('\n'.join(lines))


def write_runtime_only(names, outdir):
    """layout.h and layout.json for compiling the runtime library without a game's ExeFS (tools/make_release_linux.py
    builds the setup's prebuilt runtime libraries this way). The runtime reads only MOD_COUNT and MOD_MAIN_BASE, and
    both follow from which modules there are (names, in any order; ORDER decides) and FIRST_BASE, not from the module
    sizes. layout.h leaves every other base and size out, so a runtime file that starts to read one fails to compile
    here instead of baking in a wrong address; layout.json gives them as 0, and nothing may link a game against it."""
    mods = [n for n in ORDER if n in names]
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / 'layout.json').write_text(json.dumps(
        [{'name': n, 'base': '0x0', 'size': '0x0'} for n in mods], indent=1) + '\n')
    lines = ['// Module layout for the runtime library alone (recomp/layout.py write_runtime_only): the module set,',
             '// no sizes. Generated; do not edit.', '#pragma once', '',
             f'#define MOD_MAIN_BASE {FIRST_BASE:#x}ull', f'#define MOD_COUNT {len(mods)}',
             '#define MOD_LIST(X) ' + ' '.join(f'X({n}, {n.upper()})' for n in mods), '']
    (outdir / 'layout.h').write_text('\n'.join(lines))
    return mods


def read(outdir):
    """[(name, base, size)] from a layout.json written by write()."""
    data = json.loads((Path(outdir) / 'layout.json').read_text())
    return [(m['name'], int(m['base'], 16), int(m['size'], 16)) for m in data]
