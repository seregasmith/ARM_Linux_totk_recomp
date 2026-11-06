"""Pack a portable game folder (tools/package_build.py) into an AppImage for Linux: TotK-Recomp-<version>-<arch>.AppImage.

The AppImage holds the folder without its RomFS and profile: totk, libSDL3.so.0, the shader translator, the ExeFS, the
RomFS index and patches, the shared fonts and the shader seeds, about 0.5 GB. AppRun, a desktop entry and the icon
(runtime/totk.ico) come with it. The files sit in a squashfs image behind AppImage's static type-2 runtime, which mounts
it with FUSE when the system has /dev/fuse and fusermount, and else runs with --appimage-extract-and-run (or
APPIMAGE_EXTRACT_AND_RUN=1).

An AppImage is read-only, so AppRun points TOTK_DATA_DIR at ${XDG_DATA_HOME:-~/.local/share}/totk-recomp/<version>: the
settings (totk.ini), logs, shader and pipeline caches, Mesa's cache and the mods folder go there (runtime/cfg.h
cfg_data_dir). The saves go to totk-recomp/profile, one for every version, as saves load on any of them. A build's
prepared shader and pipeline files are copied there on the first start. The RomFS (16 GB) stays outside: AppRun takes
TOTK_ROMFS, else the first folder that holds romfs.bin of: romfs-<version>/ and romfs/ beside the AppImage, the
AppImage's own folder, romfs/ in the data folder, the data folder. Code mods (ExeFS patches, code cheats) need a rebuild of the game, which a read-only image cannot
do; RomFS mods work.

It contains game code: for your own devices only, never publish or share it.

    python tools/package_appimage.py --game build/install-aarch64/totk-v121 --version v121 --runtime RUNTIME
                                     [--arch aarch64] [--out FILE.AppImage] [--mksquashfs mksquashfs]

RUNTIME: AppImage's runtime-<arch> (tools/nix/default.nix appimageRuntime, pinned by hash). build_docker.sh appimage
runs this.
"""
import argparse
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / 'runtime' / 'totk.ico'
NAME = 'totk-recomp'
# Left out of the image: the RomFS (beside the AppImage) and what the game writes, which goes to TOTK_DATA_DIR. A
# folder that already ran holds some of these.
SKIP = {'romfs', 'profile', 'logs', 'totk.ini', 'mods', 'mesa-cache', 'flight'}
# Prepared state package_build.py may put beside totk (its STATE and shader-cache): the runtime reads and writes it in
# TOTK_DATA_DIR, so AppRun copies what the image has on the first start, when the data folder has none of it.
SEED_DATA = ('pipeline-cache.bin', 'pipeline-keys.bin', 'shader-answers.bin', 'shader-programs.bin',
             'shader-prepared.bin', 'shader-cache')
ARCHES = ('aarch64', 'x86_64')

APPRUN = r'''#!/bin/sh
# AppRun of the TotK Recomp AppImage (tools/package_appimage.py). The image is read-only: TOTK_DATA_DIR takes what the
# game writes (runtime/cfg.h cfg_data_dir), and the RomFS stays outside the image.
version=@VERSION@
here=$(dirname "$(readlink -f "$0")")
game=$here/game
base=${XDG_DATA_HOME:-$HOME/.local/share}/totk-recomp
if [ -z "${TOTK_DATA_DIR:-}" ]; then
    TOTK_DATA_DIR=$base/$version
    # saves load on every version: one profile for all of them
    [ -n "${TOTK_PROFILE_DIR:-}" ] || export TOTK_PROFILE_DIR="$base/profile"
fi
export TOTK_DATA_DIR
mkdir -p "$TOTK_DATA_DIR" || exit 1
# the build's prepared shader and pipeline files, on the first start
for f in @SEED_DATA@; do
    if [ -e "$game/$f" ] && [ ! -e "$TOTK_DATA_DIR/$f" ]; then cp -R "$game/$f" "$TOTK_DATA_DIR/$f"; fi
done
# The RomFS: TOTK_ROMFS, else the first of these folders that holds romfs.bin (the game checks its size at start).
romfs=${TOTK_ROMFS:-}
looked=
look_in() {
    [ -n "$romfs" ] && return 0
    looked="$looked
  $1/romfs.bin"
    if [ -f "$1/romfs.bin" ]; then romfs=$1; fi
}
if [ -z "$romfs" ]; then
    if [ -n "${APPIMAGE:-}" ]; then
        beside=$(dirname "$APPIMAGE")
        look_in "$beside/romfs-$version"
        look_in "$beside/romfs"
        look_in "$beside"
    fi
    look_in "$TOTK_DATA_DIR/romfs"
    look_in "$TOTK_DATA_DIR"
fi
if [ -z "$romfs" ]; then
    msg="TotK Recomp $version: romfs.bin not found. Put it in one of these places, or set TOTK_ROMFS to the folder that holds it:$looked"
    echo "$msg" >&2
    if command -v notify-send >/dev/null 2>&1; then notify-send "TotK Recomp" "$msg"; fi
    exit 1
fi
export TOTK_ROMFS="$romfs"
exec "$game/totk" "$@"
'''

DESKTOP = '''[Desktop Entry]
Type=Application
Name=TotK Recomp @VERSION@
Comment=The recompiled game (runs the build in this AppImage)
Exec=totk
Icon=@NAME@
Terminal=false
Categories=Game;
'''


def icon_png(ico):
    """The largest PNG image in a .ico file (runtime/totk.ico holds PNGs up to 256 pixels)."""
    data = ico.read_bytes()
    _, kind, count = struct.unpack_from('<HHH', data, 0)
    best = None
    for i in range(count if kind == 1 else 0):
        w, _, _, _, _, _, size, offset = struct.unpack_from('<BBBBHHII', data, 6 + 16 * i)
        image = data[offset:offset + size]
        if image.startswith(b'\x89PNG\r\n\x1a\n') and (best is None or (w or 256) > best[0]):
            best = (w or 256, image)
    if best is None:
        sys.exit(f'package_appimage: {ico} holds no PNG image')
    return best[1]


def fill(appdir, game, version):
    """The AppDir: the folder's files under game/ (but SKIP), AppRun, the desktop entry, the icon. Returns the file
    count."""
    count = 0
    for entry in sorted(game.iterdir()):
        if entry.name in SKIP:
            continue
        target = appdir / 'game' / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
            count += sum(1 for p in target.rglob('*') if p.is_file())
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            count += 1
    apprun = appdir / 'AppRun'
    apprun.write_text(APPRUN.replace('@VERSION@', version).replace('@SEED_DATA@', ' '.join(SEED_DATA)),
                      encoding='utf-8', newline='\n')
    apprun.chmod(0o755)
    (appdir / f'{NAME}.desktop').write_text(DESKTOP.replace('@VERSION@', version).replace('@NAME@', NAME),
                                            encoding='utf-8', newline='\n')
    (appdir / f'{NAME}.png').write_bytes(icon_png(ICON))
    (appdir / '.DirIcon').symlink_to(f'{NAME}.png')
    return count


def pack(appdir, runtime, out, mksquashfs, work):
    """The AppImage out: the type-2 runtime, then appdir as a squashfs image, which is made in the folder work first.
    zstd is what the runtime reads; root-owned files and no extended attributes, as appimagetool makes them. out is
    written under a .part name until it is complete. tools/make_release_linux.py packs the setup the same way: an
    AppImage cannot start under binfmt emulation (its ELF header carries the AppImage magic in e_ident's padding, which
    the emulator's binfmt_misc entry does not match), so appimagetool does not run in an emulated container."""
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


def check_runtime(runtime, arch):
    """runtime is an AppImage type-2 runtime ELF for arch."""
    head = runtime.read_bytes()[:20]
    machine = struct.unpack_from('<H', head, 18)[0] if len(head) >= 20 else 0
    want = {'aarch64': 183, 'x86_64': 62}[arch]   # e_machine: EM_AARCH64, EM_X86_64
    if head[:4] != b'\x7fELF' or head[8:11] != b'AI\x02' or machine != want:
        sys.exit(f'package_appimage: {runtime} is not the AppImage type-2 runtime for {arch}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--game', type=Path, required=True, help='the portable folder (tools/package_build.py --out)')
    ap.add_argument('--version', required=True, help='game version of the folder (v121, v143): names the AppImage, '
                                                     'its data folder and the RomFS folder it looks for')
    ap.add_argument('--runtime', type=Path, required=True, help="AppImage's runtime-<arch>")
    ap.add_argument('--arch', choices=ARCHES, default=platform.machine() if platform.machine() in ARCHES else 'aarch64')
    ap.add_argument('--out', type=Path, help='default: TotK-Recomp-<version>-<arch>.AppImage beside the folder')
    ap.add_argument('--mksquashfs', default='mksquashfs', help='mksquashfs with zstd (squashfs-tools)')
    a = ap.parse_args()

    game = a.game.resolve()
    for need in ('totk', 'totk_xlate', 'romfs-index.bin', 'exefs'):
        if not (game / need).exists():
            sys.exit(f'package_appimage: {game} has no {need}: not a portable folder (tools/package_build.py)')
    check_runtime(a.runtime, a.arch)
    mksquashfs = shutil.which(a.mksquashfs)
    if not mksquashfs:
        sys.exit(f'package_appimage: {a.mksquashfs} not found (squashfs-tools)')
    out = (a.out or game.parent / f'TotK-Recomp-{a.version}-{a.arch}.AppImage').resolve()
    if out.is_relative_to(ROOT) and not (out.is_relative_to(ROOT / 'build') or out.is_relative_to(ROOT / 'generated')):
        print('package_appimage: warning: the AppImage holds game code; keep it out of the repository')

    out.parent.mkdir(parents=True, exist_ok=True)
    # The AppDir and the image in the temporary folder (TMPDIR): the output folder may be a slow share (build_docker.sh
    # writes to the host's folder). Only the AppImage goes there, under a .part name until it is complete.
    with tempfile.TemporaryDirectory(prefix='appimage-') as tmp:
        tmp = Path(tmp)
        appdir = tmp / 'AppDir'
        appdir.mkdir()
        count = fill(appdir, game, a.version)
        pack(appdir, a.runtime, out, mksquashfs, tmp)
    print(f'package_appimage: {out}: {count} files, {out.stat().st_size / 2**20:.0f} MiB')
    print(f'package_appimage: put the RomFS beside it as romfs-{a.version}/romfs.bin '
          f'(or into ~/.local/share/{NAME}/{a.version}/); data goes to ~/.local/share/{NAME}/{a.version}')


if __name__ == '__main__':
    main()
