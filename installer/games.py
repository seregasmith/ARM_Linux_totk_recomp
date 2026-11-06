"""The games folder of the Linux setup: one install folder per update under it, and the saves they share.

    <games>/v121/, <games>/v143/   install folders (installer/cli.py --games), each a normal install with its own
                                   totk-install.json, named by the update's version tag (layout.version_tag)
    <games>/profile/               the saves; each install's totk.ini sets profile_dir = ../profile (runtime/main.c
                                   portable_paths reads it), so 1.2.1 and 1.4.3 load each other's saves

The default is ~/.local/share/totk-recomp ($XDG_DATA_HOME/totk-recomp), where the game AppImage of earlier builds kept
its per-version data folders (TOTK_DATA_DIR) and the same shared profile: an install there takes over those settings,
caches and saves.

Steam: add_to_steam asks the running Steam client to add a game as a non-Steam game (steam://addnonsteamgame), as
SteamOS's own "Add to Steam" does. Steam names the entry after the file, so the setup adds a two-line script named after
the game and its version that starts totk beside it.
"""
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from . import state as statefile
from .layout import SUPPORTED, version_tag

PROFILE = 'profile'
# totk.ini's key for the saves folder (runtime/cfg.c, TOTK_PROFILE_DIR), relative to the folder of totk
PROFILE_KEY, PROFILE_VALUE = 'profile_dir', '../profile'
# What the game writes into a data folder (runtime/cfg.h cfg_data_dir; the earlier game AppImage's): a folder
# holding only these may become an install folder, which keeps them
DATA_NAMES = {'totk.ini', 'logs', 'shader-cache', 'pipeline-cache.bin', 'pipeline-keys.bin', 'shader-answers.bin',
              'shader-programs.bin', 'shader-prepared.bin', 'mesa-cache', 'mods', 'flight', PROFILE}
# The script add_to_steam adds: Steam shows the file name
STEAM_NAME = 'TotK Recomp {version}'
STEAM_SCRIPT = ('#!/bin/sh\n'
                '# Starts totk beside this file; Steam names its library entry after it (installer/games.py).\n'
                'exec "$(dirname "$(readlink -f "$0")")/totk" "$@"\n')
# The file that lets a steam://addnonsteamgame request through (SteamOS's steamos-add-to-steam creates it the same way)
STEAM_ALLOW = Path('/tmp/addnonsteamgamefile')


def default_folder():
    data = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(data) / 'totk-recomp'


def version_key(version):
    """'1.4.3' -> (1, 4, 3), for picking the highest."""
    return tuple(int(p) if p.isdigit() else 0 for p in version.split('.'))


def folder_for(games, version):
    return Path(games) / version_tag(version)


def installs(games):
    """[(version, folder)] of the finished installs in games, highest version first. A folder counts when its
    totk-install.json reads, records a supported version and the game exe is there."""
    found = []
    for version in SUPPORTED:
        folder = folder_for(games, version)
        try:
            data = statefile.load(folder)
        except statefile.StateError:
            continue
        exe = folder / ('totk.exe' if os.name == 'nt' else 'totk')
        if data and data.get('version', '1.2.1') == version and exe.is_file():
            found.append((version, folder))
    return sorted(found, key=lambda vf: version_key(vf[0]), reverse=True)


def highest(games):
    """(version, folder) of the highest installed version, or None."""
    found = installs(games)
    return found[0] if found else None


def has_saves(folder):
    folder = Path(folder)
    return folder.is_dir() and any(folder.iterdir())


def share_profile(dest, games):
    """Point the install at games/profile: moves its own profile/ there when that holds saves and the shared one does
    not. Returns None when it shares the folder, else the path of the install's own saves that stay where they are
    (both held saves)."""
    own, shared = Path(dest) / PROFILE, Path(games) / PROFILE
    if own.is_symlink():
        own.unlink()
    if has_saves(own):
        if has_saves(shared):
            return own
        if shared.is_dir():
            shared.rmdir()
        shutil.move(str(own), str(shared))
    elif own.is_dir():
        own.rmdir()
    shared.mkdir(parents=True, exist_ok=True)
    return None


def steam_link(dest, version):
    """dest/<STEAM_NAME>: the script that starts totk, named for Steam's library."""
    path = Path(dest) / STEAM_NAME.format(version=version)
    if not path.is_file() or path.read_text(encoding='utf-8', errors='replace') != STEAM_SCRIPT:
        path.unlink(missing_ok=True)
        path.write_text(STEAM_SCRIPT, encoding='utf-8', newline='\n')
    path.chmod(0o755)
    return path


# Where Steam's launcher script writes the process id of the running client: the native install, the Flatpak's
STEAM_PIDS = ('.steam/steam.pid', '.var/app/com.valvesoftware.Steam/.steam/steam.pid')


def steam_running():
    """False when a Steam pid file exists and none names a live process, else True (no pid file: nothing to tell)."""
    pids = [Path.home() / p for p in STEAM_PIDS]
    found = [p for p in pids if p.is_file()]
    if not found:
        return True
    for p in found:
        try:
            pid = int(p.read_text().strip())
        except (OSError, ValueError):
            continue
        if Path(f'/proc/{pid}').exists():
            return True
    return False


def add_to_steam(path):
    """Ask the running Steam client to add path as a non-Steam game. Returns (ok, detail). Steam must be running; it
    takes the request from its steam:// handler (the `steam` command passes it to the running client, xdg-open
    otherwise). Whether Steam added it is not reported back: ok means the request went out."""
    if not steam_running():
        return False, 'Steam is not running'
    url = 'steam://addnonsteamgame/' + urllib.parse.quote(str(path), safe='')
    try:
        STEAM_ALLOW.touch()
    except OSError as e:
        return False, f'{STEAM_ALLOW}: {e}'
    tool = shutil.which('steam') or shutil.which('xdg-open')
    if not tool:
        return False, 'neither steam nor xdg-open is on PATH'
    try:
        subprocess.Popen([tool, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return False, f'{tool}: {e}'
    return True, tool
