"""Command line: python -m installer --base FILE --update FILE --keys FILE --lang CODE --dest DIR [--firmware DIR]
                                     [--root DIR] [--temp DIR] [--verify] [--dlss] [--cpu LEVEL] [--gen-opt O1|O2]
              python -m installer ... --games DIR [--add-to-steam]   (in place of --dest)

A first install needs every option. --firmware names the Switch firmware folder (an unpacked firmware or an emulator's
Contents/registered); the setup extracts the system shared fonts from it when the language is Chinese, Korean or
Traditional Chinese. On a folder with totk-install.json (resume after a cancel or failure, or update with a newer
release) only --dest is needed: --lang defaults to the recorded language, and the game files are needed only when the
extraction has to run again. Prints JSON events on stdout (events.py). Exit code: 0 done, 1 failed, 2 bad command
line, 3 cancelled (Ctrl+C or Ctrl+Break).

--dlss downloads NVIDIA's DLSS library (nvngx_dlss.dll) from NVIDIA's DLSS SDK repository into the install folder on
Windows with an NVIDIA RTX GPU (steps.py, Dlss).

--cpu (or the TOTK_CPU environment variable, which the window passes on) builds for that code level instead of the
one the host check picks: v3 or v2 on x86-64, a64 or oryon on aarch64 (tools/build_game.py CPU_LEVELS). The check stops
on a level this CPU cannot run (steps.py, Check.cpu_level).

--gen-opt O1|O2 (or TOTK_GEN_OPT) compiles the generated C at that optimisation level instead of the default: -O2 on
aarch64, -O1 on x86-64 (tools/build_game.py GEN_OPT_DEFAULT). Changing it recompiles the game; a release's execution
profile compiles at -O2.

--games DIR (the Linux window's default, installer/games.py) names a games folder instead of an install folder: the
install goes into <games>/<version tag>, v121 or v143, and the saves into <games>/profile, shared by every version
there. With --update the setup first identifies that file's version (steps.py identify_update_file); without it, it
continues or updates the highest version installed there. --add-to-steam then only asks the running Steam client to
add that install's game as a non-Steam game (or --dest's) and exits.
"""
import argparse
import hashlib
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from . import games as gamesdir
from . import layout as layouts
from . import state as statefile
from .events import Reporter
from .runner import Context, InstallError, run_steps
from .steps import all_steps, identify_update_file, migrate_state

EXIT = {'ok': 0, 'failed': 1, 'cancelled': 3}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        Reporter().error('cli.usage', {'detail': message})
        sys.exit(2)


# The temp folder inside the install folder when the layout names none (the Linux release); the check step does not
# count it when it requires an empty install folder
TEMP_IN_DEST = '.setup-temp'


def temp_folder(base, dest):
    """One temp folder per install folder, so two installs never share generated files."""
    tag = hashlib.sha256(str(dest).lower().encode()).hexdigest()[:8]
    return base / f'{dest.name or "root"}-{tag}'


def games_dest(args, layout):
    """The install folder of a --games run: <games>/<version tag> of --update's version, else the highest version
    installed there. None after an error event (the setup then ends failed)."""
    games = args.games.resolve()
    work = games / '.setup-identify'   # the identification's own temp folder: no install folder is known yet
    rep = Reporter(log_path=work / 'logs' / time.strftime('identify-%Y%m%d-%H%M%S.log'))
    try:
        if not args.update:
            found = gamesdir.highest(games)
            if found is None:
                rep.error('cli.no_install', {'path': str(games)})
                rep.emit('end', result='failed', seconds=0)
                return None
            rep.log(f'games: {games}, highest installed {found[0]} in {found[1]}')
            return found[1]
        try:
            if not args.keys:
                raise InstallError('check.inputs_needed')
            version = identify_update_file(args.update, args.keys, work, rep)
        except InstallError as e:
            rep.error(e.id, e.params)
            rep.emit('end', result='failed', seconds=round(rep.elapsed(), 1))
            return None
        dest = gamesdir.folder_for(games, version)
        rep.log(f'games: {games}, the update is {version}: {dest}')
        # an NSZ the identification unpacked goes where Check.unpack looks for it
        unpacked = work / 'unpacked'
        if unpacked.is_dir():
            base = args.temp or layout.temp   # as main() picks the temp folder
            target = (temp_folder(base.resolve(), dest) if base else dest / TEMP_IN_DEST) / 'unpacked'
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(unpacked), str(target))
        shutil.rmtree(work / 'update', ignore_errors=True)
        return dest
    finally:
        rep.close()


def add_to_steam(dest):
    """--add-to-steam: the install's game, under a link named for Steam's library (installer/games.py), handed to the
    running Steam client. Prints a steam event, or an error."""
    rep = Reporter()
    exe = dest / ('totk.exe' if os.name == 'nt' else 'totk')
    try:
        data = statefile.load(dest)
    except statefile.StateError as e:
        rep.error(e.id, {'path': str(e.path)})
        return EXIT['failed']
    if data is None or not exe.is_file():
        rep.error('steam.not_installed', {'path': str(dest)})
        return EXIT['failed']
    try:
        link = gamesdir.steam_link(dest, data.get('version', '1.2.1'))
    except OSError as e:
        rep.error('steam.failed', {'detail': str(e)})
        return EXIT['failed']
    ok, detail = gamesdir.add_to_steam(link)
    if not ok:
        rep.error('steam.failed', {'detail': detail})
        return EXIT['failed']
    rep.emit('steam', added=str(link), tool=detail)
    return EXIT['ok']


def cancel(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    ap = Parser(prog='python -m installer', description=__doc__.splitlines()[0])
    ap.add_argument('--base', type=Path, help='base game, XCI or NSP (or NSZ, XCZ: unpacked first)')
    ap.add_argument('--update', type=Path, help='the update NSP or NSZ: 1.1.0, 1.2.1 or 1.4.3')
    ap.add_argument('--keys', type=Path, help='prod.keys')
    ap.add_argument('--firmware', type=Path, help='Switch firmware folder (Contents/registered) for the shared fonts '
                                                   'of Chinese, Korean and Traditional Chinese text')
    ap.add_argument('--lang', help='game language, a Switch language code (en-US, en-GB, ja, de, ru, ...)')
    ap.add_argument('--dest', type=Path, help='install folder (or --games)')
    ap.add_argument('--games', type=Path, help='games folder: the install goes into <games>/<version tag> and the '
                                               'saves into <games>/profile (installer/games.py)')
    ap.add_argument('--add-to-steam', action='store_true', help='only add the installed game (--dest, or the highest '
                                                                 'version in --games) to the running Steam client')
    ap.add_argument('--root', type=Path, help='release folder or repository (default: the folder above installer/)')
    ap.add_argument('--temp', type=Path, help='temp folder (default: temp/ of the release on Windows, .setup-temp in '
                                              'the install folder on Linux, build/install-temp in a repository)')
    ap.add_argument('--verify', action='store_true', help='hash every kept file, also those this release made')
    ap.add_argument('--dlss', action='store_true', help="download NVIDIA's nvngx_dlss.dll (NVIDIA RTX SDK license) next "
                                                      'to the game, for dlaa and the DLSS modes')
    ap.add_argument('--cpu', help='code level to build for instead of the one the host check picks: v3 or v2 on '
                                  'x86-64, a64 or oryon on aarch64 (TOTK_CPU does the same)')
    ap.add_argument('--gen-opt', choices=('O1', 'O2'),
                    help='optimisation level of the generated C instead of the default (O2 on aarch64, O1 on x86-64): '
                         'O2 optimises more and compiles longer (TOTK_GEN_OPT does the same)')
    args = ap.parse_args(argv)
    if bool(args.dest) == bool(args.games):
        ap.error('one of --dest and --games is required')

    # The window cancels with Ctrl+Break to the installer's process group (CREATE_NEW_PROCESS_GROUP), which also reaches
    # the tools it runs; Python's default for it ends the process without recording anything.
    for name in ('SIGBREAK', 'SIGTERM'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), cancel)
    layout = layouts.find(args.root)
    dest = args.dest.resolve() if args.dest else games_dest(args, layout)
    if dest is None:
        return EXIT['failed']
    if args.add_to_steam:
        return add_to_steam(dest)
    base = args.temp or layout.temp
    temp = temp_folder(base.resolve(), dest) if base else dest / TEMP_IN_DEST
    log = temp / 'logs' / time.strftime('install-%Y%m%d-%H%M%S.log')
    rep = Reporter(log_path=log)
    t0 = time.monotonic()
    try:
        state = statefile.load(dest)
    except statefile.StateError as e:
        rep.error(e.id, {'path': str(e.path)})
        rep.emit('end', result='failed', seconds=0)
        return 1
    ctx = Context(layout, dest, temp, rep, args, state=state, verify=args.verify)
    migrate_state(ctx)
    steps = all_steps()
    rep.emit('start', release=layout.release, mode=ctx.mode, dev=layout.dev, dest=str(dest), log=str(log),
             steps=[s.id for s in steps])
    result = run_steps(steps, ctx)
    rep.emit('end', result=result, seconds=round(time.monotonic() - t0, 1))
    rep.close()
    return EXIT[result]
