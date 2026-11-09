#!/bin/bash
# build_docker.sh: build TotK Recomp from your own game files in a Linux Docker container with Nix, on any host, for
#   aarch64  ARM64 Linux handhelds (Snapdragon with Turnip; docs/ARM64_PORT_PLAN.md)  -> a folder to copy to the
#            device, or the setup AppImage that builds the game on the device
#   x86_64   x86-64 Linux PCs and the Steam Deck
# Hosts: macOS on Apple Silicon or Intel (Docker Desktop, OrbStack or colima), Linux on arm64 or x86-64 (Docker Engine).
# A target that matches Docker's own architecture builds natively; the other one runs emulated (Rosetta or QEMU, slow).
#
# Usage:  ./build_docker.sh [--arch aarch64|x86_64] [tools|test|game|setup|lock|shell|clean]
#   --arch  target architecture (or TOTK_ARCH; arm64 and amd64 work too). Default: Docker's own architecture
#   tools   (no game files) Python environments, refs/, hactool, the shader translator, and from Nix the target
#           toolchain, sysroot (Ubuntu 22.04, glibc 2.35) and libraries (SDL3, Opus, libvpx, zstd)
#   test    tools, then the checks that need no game files (the differential tests against Unicorn, flags, the platform
#           layer, check_arch/check_platform); after a game build also the lift, function and jump-table tests
#   game    (default) tools, then the game from your files: extract, lift, RomFS patches, compile and link, the shader
#           seeds, and a portable folder in TOTK_OUT (game files below). Rebuilds only what changed
#   setup   (no game files) the setup AppImage, TotK-Setup-<arch>.AppImage in TOTK_OUT (tools/make_release_linux.py):
#           it builds the game on the device itself from the player's own files, with the toolchain inside it, and
#           holds no game code. After a game run it also builds that game once with the setup's own tools, as a check
#   lock    write tools/nix/sysroot-<arm64|amd64>.lock.json: the pinned Ubuntu packages of the target sysroot (a run
#           does it when the file is missing or tools/nix/sysroot_lock.py lists other packages; commit it)
#   shell   interactive nix-shell in the build environment, in the source tree
#   clean   delete this target's image and volumes (the build, the generated C, venvs, toolchain caches, refs and its
#           own Nix store; not a volume named by TOTK_NIX_VOLUME). The source tree and TOTK_OUT are untouched
#
# Game files (game mode; each mounted read-only, the keys file never read by this script or printed):
#   TOTK_BASE      the base game, XCI or NSP
#   TOTK_UPDATE    the 1.4.3, 1.2.1 or 1.1.0 update NSP
#   TOTK_KEYS      prod.keys from your console
#   TOTK_FIRMWARE  optional: a firmware NCA folder, for the shared fonts (Chinese, Korean text)
# Example:
#   TOTK_BASE=~/Switch/totk.nsp TOTK_UPDATE=~/Switch/totk-143.nsp TOTK_KEYS=~/Switch/prod.keys ./build_docker.sh
# Then copy the folder to the handheld, run `./totk --prepare-shaders` there once (the GPU half of the shader step),
# and play `./totk`. The folder holds code and data of your game: for your own devices only. The setup AppImage holds
# none: it builds the game on the device, into ~/.local/share/totk-recomp, and can add it to Steam (docs/installer.md).
# For x86-64 PCs and the Steam Deck on Apple Silicon add --arch x86_64 (or TOTK_ARCH=x86_64): the container then runs
# under Rosetta or QEMU, with its own image, volumes and build/install-x86_64 (docs/building.md, "The other
# architecture").
#
# Settings (environment variables):
#   TOTK_ARCH       target, as --arch (the option wins)
#   TOTK_SRC        source tree (default: the directory this script is in)
#   TOTK_VERSION    v143, v121 or v110: the version the update file must be. Default: the update's own, told by
#                   tools/extract.py from its code
#   TOTK_CPU        code level: aarch64 a64 (default; Armv8.2 + LSE, RCpc, dot product) or oryon (Snapdragon 8
#                   Elite); x86_64 v3 (default) or v2. tools/build_game.py CPU_LEVELS
#   TOTK_GEN_OPT    O1 or O2: the generated C's optimisation level (default O2 on aarch64, O1 on x86_64;
#                   tools/build_game.py GEN_OPT_DEFAULT)
#   TOTK_OUT        where the portable folder, totk-<version>, and the setup AppImage go (default:
#                   build/install-<arch> in the source tree, which git ignores)
#   TOTK_ROMFS      0: leave the 16 GB RomFS image out of the portable folder (copy it yourself)
#   TOTK_NIX_VOLUME an existing Docker volume to use as the Nix store. The Bloodborne port's build_docker.sh pins the
#                   same nixpkgs, so its volume saves the downloads: TOTK_NIX_VOLUME=bbport-nix (aarch64) or
#                   bbport-nix-x86_64. It must exist and hold the target architecture's Nix
#   JOBS            parallel compiler processes (default: from Docker's RAM, about 2 GB each)
#   GITHUB_TOKEN    optional GitHub token for Nix downloads, if GitHub's API rate limit hits
#
# Logs: build/docker-logs-<arch>/ in the source tree: each step's output, and in tools/ the tools' own logs (the
# extraction's, hactool's merge), copied there when the container exits, so a failed run can be read. The work
# itself (extracted game, generated C, objects, venvs, caches) lives in Docker volumes: macOS shares folders slowly and
# without case sensitivity. Linux host with Docker Engine: files the container writes into the tree and TOTK_OUT are
# given back to you (ownership only). Written for macOS's stock bash 3.2 and any newer bash.
set -eo pipefail

NIXPKGS_REV=6510408d147d8c5e6d76043e899e9251756f6a75   # nixos-unstable, 2026-10-10 (the Bloodborne port's pin)
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
SRC=${TOTK_SRC:-$HERE}
ARCH=${TOTK_ARCH:-}
MODE=game
VERSION=${TOTK_VERSION:-}

die() { echo "ERROR: $*" >&2; exit 1; }
usage() {
    echo "Usage: $0 [--arch aarch64|x86_64] [tools|test|game|setup|lock|shell|clean]" >&2
    exit 2
}
norm_arch() {
    case $1 in
        aarch64|arm64|armv8*) echo aarch64 ;;
        x86_64|amd64|x64|x86-64|x86) echo x86_64 ;;
        *) echo "" ;;
    esac
}

while [ $# -gt 0 ]; do
    case $1 in
        --arch) [ $# -ge 2 ] || usage; ARCH=$2; shift 2 ;;
        --arch=*) ARCH=${1#--arch=}; shift ;;
        -h|--help) awk 'NR > 1 && /^set -eo/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
        tools|test|game|setup|lock|shell|clean) MODE=$1; shift ;;
        *) echo "ERROR: unknown argument '$1'" >&2; usage ;;
    esac
done

command -v docker >/dev/null || die "docker not found. Install Docker Desktop, OrbStack, colima or Docker Engine."
docker info >/dev/null 2>&1 || die "Docker is installed but not running (or this user may not use it)."
DOCKER_ARCH=$(norm_arch "$(docker info --format '{{.Architecture}}' 2>/dev/null)")
[ -n "$DOCKER_ARCH" ] || die "cannot tell Docker's CPU architecture (docker info --format '{{.Architecture}}')"
if [ -z "$ARCH" ]; then
    ARCH=$DOCKER_ARCH
else
    wanted=$(norm_arch "$ARCH")
    [ -n "$wanted" ] || die "unknown target architecture '$ARCH' (aarch64 or x86_64)"
    ARCH=$wanted
fi
case $ARCH in
    aarch64) PLATFORM=linux/arm64 ;;
    x86_64)  PLATFORM=linux/amd64 ;;
esac
DOCKER_PLATFORM_ARCH=${PLATFORM#linux/}
case $VERSION in ''|v143|v121|v110) ;; *) die "TOTK_VERSION=$VERSION: v143, v121 or v110 (or unset: the update's)" ;; esac

# Per target: image, Nix store volume (a store holds one architecture's Nix) and the work volumes.
IMAGE="totk-builder:${NIXPKGS_REV:0:8}-$ARCH"
OWN_NIX=totk-nix-$ARCH
NIX_VOLUME=${TOTK_NIX_VOLUME:-$OWN_NIX}
WORK_VOLUMES="build generated toolchain refs venv venv314"   # totk-<arch>-<name>
work_mounts() {
    echo "-v totk-$ARCH-build:/src/build -v totk-$ARCH-generated:/src/generated -v totk-$ARCH-toolchain:/src/toolchain"
    echo "-v totk-$ARCH-refs:/src/refs -v totk-$ARCH-venv:/src/.venv -v totk-$ARCH-venv314:/src/.venv-py314"
}

if [ "$MODE" = clean ]; then
    docker image rm "$IMAGE" 2>/dev/null || true
    for v in $WORK_VOLUMES; do docker volume rm "totk-$ARCH-$v" 2>/dev/null || true; done
    echo "Removed the $ARCH image $IMAGE and the work volumes totk-$ARCH-*."
    if [ "$NIX_VOLUME" = "$OWN_NIX" ]; then
        docker volume rm "$NIX_VOLUME" 2>/dev/null || true
        echo "Removed the Nix store volume $NIX_VOLUME (the next build downloads it again)."
    else
        echo "Kept the Nix store volume $NIX_VOLUME: it is not this script's own ($OWN_NIX)."
    fi
    exit 0
fi
if [ "$NIX_VOLUME" != "$OWN_NIX" ] && ! docker volume inspect "$NIX_VOLUME" >/dev/null 2>&1; then
    echo "ERROR: TOTK_NIX_VOLUME=$NIX_VOLUME: no such Docker volume. Volumes here:" >&2
    docker volume ls --format '  {{.Name}}' >&2 || true
    die "name an existing one, or unset TOTK_NIX_VOLUME to use this script's own ($OWN_NIX)"
fi

SRC=$(cd -- "$SRC" 2>/dev/null && pwd) || die "TOTK_SRC directory not found"
[ -f "$SRC/tools/build_game.py" ] && [ -f "$SRC/shell.nix" ] && [ -d "$SRC/recomp" ] ||
    die "$SRC is not the TotK Recomp source tree (no tools/build_game.py, shell.nix, recomp/). Put this script in its root or set TOTK_SRC."

# Game files: each one mounted read-only on its own, under its own name (tools/extract.py tells NSP from XCI by it).
game_flags=()
if [ "$MODE" = game ]; then
    for var in TOTK_BASE TOTK_UPDATE TOTK_KEYS; do
        eval "f=\${$var:-}"
        [ -n "$f" ] || die "$var is not set: $MODE needs TOTK_BASE (XCI/NSP), TOTK_UPDATE (NSP) and TOTK_KEYS (prod.keys)"
        [ -f "$f" ] || die "$var: no such file: $f"
    done
    abs() { (cd -- "$(dirname -- "$1")" && echo "$(pwd)/$(basename -- "$1")"); }
    base=$(abs "$TOTK_BASE") update=$(abs "$TOTK_UPDATE") keys=$(abs "$TOTK_KEYS")
    game_flags=(-v "$base:/game/base/$(basename "$base"):ro" -v "$update:/game/update/$(basename "$update"):ro"
                -v "$keys:/game/keys/$(basename "$keys"):ro"
                -e "TOTK_GAME_BASE=/game/base/$(basename "$base")" -e "TOTK_GAME_UPDATE=/game/update/$(basename "$update")"
                -e "TOTK_GAME_KEYS=/game/keys/$(basename "$keys")")
    if [ -n "${TOTK_FIRMWARE:-}" ]; then
        fw=$(cd -- "$TOTK_FIRMWARE" 2>/dev/null && pwd) || die "TOTK_FIRMWARE: no such folder: $TOTK_FIRMWARE"
        game_flags+=(-v "$fw:/game/firmware:ro" -e TOTK_GAME_FIRMWARE=/game/firmware)
    fi
fi
if [ "$MODE" = game ] || [ "$MODE" = setup ]; then
    OUT=${TOTK_OUT:-$SRC/build/install-$ARCH}
    mkdir -p "$OUT"
    OUT=$(cd -- "$OUT" && pwd)
    game_flags+=(-v "$OUT:/out")
fi
LOGS=$SRC/build/docker-logs-$ARCH
mkdir -p "$LOGS"

emulation_help() {
    echo "How to enable it:" >&2
    if [ "$(uname -s)" = Darwin ]; then
        echo "  Docker Desktop: Settings > General > \"Use Rosetta for x86_64/amd64 emulation on Apple Silicon\"" >&2
        echo "  OrbStack:       built in" >&2
        echo "  colima:         colima start --vm-type vz --vz-rosetta" >&2
    else
        echo "  Register QEMU for $PLATFORM (lasts until reboot):" >&2
        echo "    docker run --privileged --rm tonistiigi/binfmt --install $DOCKER_PLATFORM_ARCH" >&2
    fi
}

echo "==> Target: $ARCH ($PLATFORM); Docker runs on $DOCKER_ARCH"
if [ "$ARCH" != "$DOCKER_ARCH" ]; then
    echo "==> Emulated build (Rosetta or QEMU): slower than a native $ARCH build. Checking that $PLATFORM containers run"
    got=$(docker run --rm --platform "$PLATFORM" nixos/nix:latest uname -m 2>&1 | tail -n 1) || true
    if [ "$(norm_arch "$got")" != "$ARCH" ]; then
        echo "ERROR: this Docker can't run $PLATFORM containers (test output: ${got:-nothing})." >&2
        emulation_help
        exit 1
    fi
fi

mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
ncpu=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)
case $mem in ''|*[!0-9]*) mem=0 ;; esac
case $ncpu in ''|*[!0-9]*) ncpu=0 ;; esac
mem_mib=$((mem / 1048576))
if [ -z "${JOBS:-}" ]; then
    JOBS=$((mem_mib / 2048))
    if [ "$ncpu" -gt 0 ] && [ "$JOBS" -gt "$ncpu" ]; then JOBS=$ncpu; fi
    if [ "$JOBS" -lt 1 ]; then JOBS=1; fi
fi
case $JOBS in ''|*[!0-9]*|0) die "JOBS must be a positive number" ;; esac
echo "==> Source tree: $SRC; logs: $LOGS"
echo "==> Docker VM: ${ncpu} CPUs, ${mem_mib} MiB RAM; compiling with JOBS=${JOBS}"
if [ "$mem_mib" -lt 7500 ]; then
    echo "WARNING: little RAM for this build. Give Docker 8 GB or more" \
         "(Docker Desktop: Settings > Resources > Memory), or expect a slow build." >&2
fi
if [ "$MODE" = game ]; then
    shown=${VERSION:-the update\'s version}
    echo "==> Game ($shown) into $OUT; the extraction needs about 55 GB free in Docker's disk at its peak"
fi

# Linux host, rootful Docker Engine: files the container writes into the tree belong to root on the host; the
# container gives them back to this user at the end. Not with rootless Docker, Podman or Docker Desktop.
OWNER=""
if [ "$(uname -s)" = Linux ]; then
    case "$(docker --version 2>/dev/null) $(docker info --format '{{.OperatingSystem}} {{.SecurityOptions}}' 2>/dev/null)" in
        *odman*|*rootless*|*"Docker Desktop"*) ;;
        *) OWNER="${SUDO_UID:-$(id -u)}:${SUDO_GID:-$(id -g)}" ;;
    esac
fi

# Image: the official Nix image plus a pinned nixpkgs tree (NIX_PATH), as the Bloodborne port's, so shell.nix's
# `import <nixpkgs>` always resolves to the same package set. git is there for the refs and the translator's patches.
image_arch=$(docker image inspect --format '{{.Architecture}}' "$IMAGE" 2>/dev/null || true)
if [ "$image_arch" != "$DOCKER_PLATFORM_ARCH" ]; then
    echo "==> Building image $IMAGE ($PLATFORM)"
    docker build --platform "$PLATFORM" -t "$IMAGE" --build-arg NIXPKGS_REV="$NIXPKGS_REV" - <<'DOCKERFILE'
FROM nixos/nix:latest
ARG NIXPKGS_REV
RUN mkdir -p /opt/nixpkgs \
 && curl -fsSL "https://github.com/NixOS/nixpkgs/archive/${NIXPKGS_REV}.tar.gz" \
    | tar -xz --strip-components=1 -C /opt/nixpkgs \
 && (command -v git >/dev/null \
     || nix-env --option sandbox false --option filter-syscalls false --option build-users-group '' \
            -f /opt/nixpkgs -iA git) \
 && git config --global --add safe.directory '*'
ENV NIX_PATH=nixpkgs=/opt/nixpkgs
WORKDIR /src
DOCKERFILE
fi

if [ "$NIX_VOLUME" != "$OWN_NIX" ]; then
    vol_system=$(docker run --rm --platform "$PLATFORM" -v "$NIX_VOLUME":/nix "$IMAGE" \
        nix-instantiate --eval --expr builtins.currentSystem 2>/dev/null | tr -d '"' | tail -n 1) || true
    if [ "$vol_system" != "$ARCH-linux" ]; then
        die "TOTK_NIX_VOLUME=$NIX_VOLUME holds ${vol_system:-no working} Nix ($ARCH-linux needed)"
    fi
fi

# Runs inside the container, in /src (the mounted tree; its build/, generated/, toolchain/, refs/ and venvs are
# volumes). $1 = mode. Only the Nix image's own tools outside nix-shell.
read -r -d '' INNER <<'INNER_SCRIPT' || true
set -euo pipefail
mode=$1; shift
cd /src
log=/logs/build_docker.log
: > "$log"
# When the container exits, also after a failure: the tools' own logs (build/logs: the extraction's, hactool's merge)
# are in the build volume, so a copy goes to the host's log folder; and on a Linux host with Docker Engine, root-owned
# files in what the build writes into the tree go back to the host user (ownership only).
on_exit() {
    if [ -d /src/build/logs ] && mkdir -p /logs/tools; then cp -p /src/build/logs/* /logs/tools/ 2>/dev/null || true; fi
    if [ -n "${TOTK_OWNER:-}" ]; then
        local p
        for p in /logs /out tools/nix; do
            if [ -e "$p" ]; then find "$p" -user 0 -exec chown -h "$TOTK_OWNER" {} + 2>/dev/null || true; fi
        done
    fi
}
trap on_exit EXIT
# Nix in a throwaway root container: the Bloodborne port's settings and guards, unchanged (see its build_docker.sh):
# no sandbox, no build users, no seccomp filter (Rosetta, QEMU), builds one at a time with $TOTK_JOBS cores, and
# /homeless-shelter removed whenever it appears between builds, since Nix refuses to start a build while it exists.
export HOME=/root XDG_CACHE_HOME=/root/.cache
mkdir -p /tmp/nixhooks /tmp/nix-builds "$XDG_CACHE_HOME"
chmod 700 /tmp/nix-builds
printf '#!/bin/sh\nrm -rf /homeless-shelter\n' > /tmp/nixhooks/post-build
chmod +x /tmp/nixhooks/post-build
clear_homeless() {
    if [ -e /homeless-shelter ]; then
        echo "==> Removing /homeless-shelter left by $1"
        rm -rf /homeless-shelter
    fi
}
clear_homeless "the image"
(
    while sleep 0.2; do
        [ -e /homeless-shelter ] || continue
        [ -z "$(ls -A /tmp/nix-builds 2>/dev/null)" ] || continue
        rm -rf /homeless-shelter
    done
) &
export NIX_CONFIG="experimental-features = nix-command flakes
sandbox = false
build-users-group =
filter-syscalls = false
max-jobs = 1
build-dir = /tmp/nix-builds
cores = ${TOTK_JOBS:-2}
post-build-hook = /tmp/nixhooks/post-build"
if [ -n "${GITHUB_TOKEN:-}" ]; then
    NIX_CONFIG="$NIX_CONFIG
access-tokens = github.com=${GITHUB_TOKEN}"
fi

# What every step runs first, inside nix-shell: caches in the toolchain volume, and nix-ld as the dynamic linker that
# prebuilt Linux programs name (uv's wheels, the NativeAOT compiler NuGet brings).
cat > /tmp/totk-env.sh <<'ENV'
export HOME=/root XDG_CACHE_HOME=/root/.cache
export UV_CACHE_DIR=/src/toolchain/uv-cache UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
export NUGET_PACKAGES=/src/toolchain/nuget DOTNET_CLI_HOME=/src/toolchain/dotnet-home
# glibc's fatal messages (a failed fortify check, a corrupted heap) go to stderr, which the tools capture into their
# logs, instead of the terminal
export LIBC_FATAL_STDERR_=1
case $(uname -m) in
    aarch64) mkdir -p /lib && ln -sf "$TOTK_NIX_LD_STUB" /lib/ld-linux-aarch64.so.1 ;;
    x86_64) mkdir -p /lib64 && ln -sf "$TOTK_NIX_LD_STUB" /lib64/ld-linux-x86-64.so.2 ;;
esac
# The repository's Python environments, with the libraries binary wheels expect beside glibc
py() { LD_LIBRARY_PATH="$TOTK_WHEEL_LIBS" /src/.venv/bin/python "$@"; }
py314() { LD_LIBRARY_PATH="$TOTK_WHEEL_LIBS" /src/.venv-py314/bin/python "$@"; }
ENV

# in_shell LABEL COMMAND: COMMAND in the build environment (shell.nix); runs again (up to 3 times) when Nix stopped on
# /homeless-shelter, keeping what it built.
in_shell() {
    local status attempt
    for attempt in 1 2 3; do
        status=0
        clear_homeless "an earlier step"
        echo "==> $1" | tee -a "$log"
        nix-shell shell.nix --run ". /tmp/totk-env.sh; $2" 2>&1 | tee -a "$log" | tee /tmp/step.log || status=$?
        clear_homeless "$1"
        if [ "$status" -ne 0 ] && [ "$attempt" -lt 3 ] &&
           grep -q 'home directory "/homeless-shelter" exists' /tmp/step.log; then
            echo "==> Nix stopped on /homeless-shelter during $1: running it again (attempt $((attempt + 1)) of 3)"
            continue
        fi
        return "$status"
    done
}

case $TOTK_ARCH in aarch64) debarch=arm64 ;; x86_64) debarch=amd64 ;; esac
triple=$TOTK_ARCH-linux-gnu
echo "==> Container: $(uname -m), page size $(getconf PAGESIZE)"
if [ "$mode" = shell ]; then
    nix-shell shell.nix --command ". /tmp/totk-env.sh; return"
    exit $?
fi
# The lock is written anew when it is missing or tools/nix/sysroot_lock.py's package list changed (its --check)
if [ "$mode" = lock ] ||
   ! in_shell "the sysroot lock is current" "python3 tools/nix/sysroot_lock.py --arch $debarch --check"; then
    in_shell "the sysroot lock (tools/nix/sysroot-$debarch.lock.json; commit it)" \
        "python3 tools/nix/sysroot_lock.py --arch $debarch"
    [ "$mode" = lock ] && exit 0
fi

# ---- tools: everything that needs no game files
nixroots=/src/build/nix-$TOTK_ARCH
mkdir -p "$nixroots"
in_shell "the target toolchain, sysroot and libraries (Nix; the first run builds SDL3)" \
    "nix-build tools/nix -A sysroot --argstr arch $TOTK_ARCH -o $nixroots/sysroot >/dev/null &&
     nix-build tools/nix -A deps --argstr arch $TOTK_ARCH -o $nixroots/deps >/dev/null &&
     ls -l $nixroots"
sysroot=$(readlink -f "$nixroots/sysroot")
deps=$(readlink -f "$nixroots/deps")
case $TOTK_ARCH in
    aarch64) loader=$sysroot/lib/ld-linux-aarch64.so.1 ;;
    x86_64) loader=$sysroot/lib64/ld-linux-x86-64.so.2 ;;
esac
# Target programs run in the container through the sysroot's own loader and libraries (as on the handheld).
run_target="$loader --library-path $sysroot/lib/$triple:$sysroot/usr/lib/$triple"
in_shell "a test program for the target (compiled, linked and run against the sysroot)" \
    "printf '#include <stdio.h>\nint main(void){puts(\"target ok\");return 0;}\n' > /tmp/t.c &&
     \$TOTK_LLVM/bin/clang --target=$triple --sysroot=$sysroot -fuse-ld=lld -O2 /tmp/t.c -o /tmp/t && $run_target /tmp/t"
in_shell "the Python environments (.venv 3.12, .venv-py314)" \
    "[ -x .venv/bin/python ] || uv venv --allow-existing --python \$(command -v python3.12) .venv;
     [ -x .venv-py314/bin/python ] || uv venv --allow-existing --python \$(command -v python3.14) .venv-py314;
     LD_LIBRARY_PATH=\$TOTK_WHEEL_LIBS uv pip install -q --python .venv/bin/python -r tools/requirements.txt &&
     LD_LIBRARY_PATH=\$TOTK_WHEEL_LIBS uv pip install -q --python .venv-py314/bin/python -r tools/requirements-py314.txt"
in_shell "refs/ (the upstream projects at their pinned commits)" "bash tools/fetch_refs.sh"
# hactool as the Linux release builds it (tools/make_release_linux.py): clang against the sysroot, here static, not
# with the shell's GCC and Nix's hardening flags. toolchain/hactool/.recipe names the build with a hash of
# tools/build_hactool.py and tools/hactool/*.patch, so a changed patch rebuilds it.
hactool_cc="\$TOTK_LLVM/bin/clang --target=$triple --sysroot=$sysroot -fuse-ld=lld -static"
hactool_recipe="clang-sysroot-static-$(cat /src/tools/build_hactool.py /src/tools/hactool/*.patch | sha256sum | cut -c1-16)"
in_shell "hactool (clang, static, against the sysroot)" \
    "if [ -x toolchain/hactool/hactool ] && [ \"\$(cat toolchain/hactool/.recipe 2>/dev/null)\" = $hactool_recipe ]; then
         echo 'up to date'
     else
         py tools/build_hactool.py --cc \"$hactool_cc\" && echo $hactool_recipe > toolchain/hactool/.recipe
     fi"
in_shell "the shader translator (build/xlate/totk_xlate, NativeAOT for $TOTK_ARCH)" \
    "if [ -x build/xlate/totk_xlate ] && [ \"\$(cat build/xlate/totk_xlate.id 2>/dev/null)\" = \
          \"\$(cd tools && py -c 'import build_xlate; print(build_xlate.identity())')\" ]; then
         echo 'up to date'
     else
         py tools/build_xlate.py --llvm \$TOTK_LLVM --sysroot $sysroot
     fi"

# ---- test
if [ "$mode" = test ]; then
    cc="\$TOTK_LLVM/bin/clang --target=$triple --sysroot=$sysroot"
    env="CC=\"$cc\" TOTK_TEST_RUN=\"$run_target\""
    failed=""
    for t in "tests/flags_check.py" "tests/diff/simd.py --jobs $TOTK_JOBS" "tests/diff/atomic.py --jobs $TOTK_JOBS" \
             "tests/platform_check.py --cflags=-I$deps/include" "tools/check_arch.py" "tools/check_platform.py" \
             "tests/installer_test.py"; do
        # the installer's tests import tools/ncz.py, which needs Python 3.14 (compression.zstd)
        run=py
        case $t in tests/installer_test.py) run=py314 ;; esac
        in_shell "test: $t" "$env $run $t" || failed="$failed $t"
    done
    # A known failure upstream as well: the CAS-based exclusive monitor does not detect ABA (cpu.h ldx/stx).
    in_shell "test (known to fail its ABA cases): tests/excl_mt.py" "$env py tests/excl_mt.py --iters 50000" || true
    tv=${TOTK_VERSION:-$(ls -td build/extracted/v1*/exefs 2>/dev/null | head -n 1 | cut -d/ -f3)}
    if [ -n "$tv" ] && [ -d "build/extracted/$tv/exefs" ]; then
        exefs=build/extracted/$tv/exefs
        for t in "tests/diff/lift.py --exefs $exefs --jobs $TOTK_JOBS" "tests/diff/func.py --exefs $exefs --jobs $TOTK_JOBS" \
                 "tests/jumptables_check.py --exefs $exefs"; do
            in_shell "test: $t" "$env py $t" || failed="$failed $t"
        done
    else
        echo "==> The tests that need game files run after a game build"
    fi
    if [ -n "$failed" ]; then echo "==> FAILED:$failed"; exit 3; fi
    echo "==> Tests passed"
    exit 0
fi
[ "$mode" = tools ] && { echo "==> Tools ready"; exit 0; }

# ---- setup: the setup AppImage (tools/make_release_linux.py), without game files
if [ "$mode" = setup ]; then
    rel=build/release/TotK-Setup-linux-$TOTK_ARCH
    image=TotK-Setup-$TOTK_ARCH.AppImage
    # Under Xvfb, with fonts, for the window checks; the folder's own programs (Python, LLVM, the hostcheck) run
    # through nix-ld. The container has no GPU: the installer pass stops at its Vulkan check (--no-gpu).
    in_shell "the setup release ($rel; $image in TOTK_OUT)" \
        "FONTCONFIG_FILE=\$TOTK_FONTS_CONF xvfb-run -a -s '-screen 0 1280x800x24' \
             env LD_LIBRARY_PATH=\$TOTK_WHEEL_LIBS /src/.venv/bin/python tools/make_release_linux.py \
             --arch $TOTK_ARCH --sysroot $sysroot --deps $deps --no-gpu --out $rel --appimage /out/$image"
    # The check that builds a game with the folder's own tools needs a lifted version and its extraction, which only
    # an earlier game run leaves in the build volumes (tools/make_release.py lifted_version: v121, else v143)
    v=
    for c in v121 v143; do
        if [ -f "generated/$c/layout.json" ] && [ -d "build/extracted/$c/exefs" ]; then v=$c; break; fi
    done
    if [ -n "$v" ]; then
        in_shell "$v built with the setup's own tools (tools/make_release_linux.py --prove, the default level)" \
            "py tools/make_release_linux.py --arch $TOTK_ARCH --out $rel --prove --no-gpu --run-with '$run_target'"
    else
        echo "==> No lifted game in the build volumes, so the check that builds one with the setup's own tools" \
             "is skipped (a game run first makes it possible)" | tee -a "$log"
    fi
    echo "==> The setup: $image in TOTK_OUT" | tee -a "$log"
    exit 0
fi

# ---- game: the player's files (read-only mounts), the settings file the tools read, then the pipeline
cat > /tmp/config.docker.toml <<CONFIG
[input]
base = '$TOTK_GAME_BASE'
updates = ['$TOTK_GAME_UPDATE']
keys = '$TOTK_GAME_KEYS'
${TOTK_GAME_FIRMWARE:+firmware = '$TOTK_GAME_FIRMWARE'}

[work]
dir = 'build'
CONFIG
in_shell "extract and verify the game (build/extracted)" "py314 tools/extract.py --config /tmp/config.docker.toml"
# The version: the folder tools/extract.py made for this update file (its extraction-manifest.json names the source).
in_shell "the update's version" \
    "py314 -c 'import json, pathlib, sys
src = str(pathlib.Path(sys.argv[1]).resolve())
for d in sorted(pathlib.Path(\"build/extracted\").glob(\"v*\")):
    m = d / \"extraction-manifest.json\"
    if d.name != \"v0\" and m.is_file() and json.loads(m.read_text())[\"source\"] == src:
        print(d.name)' \"$TOTK_GAME_UPDATE\" > /tmp/version && cat /tmp/version"
v=$(head -n 1 /tmp/version)
if [ -z "$v" ] || [ ! -d "build/extracted/$v/exefs" ]; then
    echo "ERROR: no extracted update for $TOTK_GAME_UPDATE in build/extracted" | tee -a "$log"
    exit 1
fi
if [ -n "${TOTK_VERSION:-}" ] && [ "$TOTK_VERSION" != "$v" ]; then
    echo "ERROR: the update file is $v, not TOTK_VERSION=$TOTK_VERSION (unset TOTK_VERSION to build $v)" | tee -a "$log"
    exit 1
fi
if [ -n "${TOTK_GAME_FIRMWARE:-}" ]; then
    in_shell "the shared fonts" "py314 tools/extract_fonts.py --config /tmp/config.docker.toml"
fi
in_shell "lift $v to C (generated/$v)" \
    "py recomp/gen.py --exefs build/extracted/$v/exefs --out generated/$v --patches recomp/patches-$v.txt"
in_shell "RomFS patches (game, Xbox prompts both layouts)" \
    "py314 tools/make_romfs_patches.py --version $v &&
     py314 tools/make_romfs_patches.py --version $v --prompts xbox &&
     py314 tools/make_romfs_patches.py --version $v --prompts xbox-label"
in_shell "compile and link (build/$v/totk, $TOTK_ARCH ${TOTK_CPU:-default level}, ${TOTK_GEN_OPT:-default optimisation})" \
    "py tools/build_game.py --version $v --arch $TOTK_ARCH ${TOTK_CPU:+--cpu $TOTK_CPU} \
         ${TOTK_GEN_OPT:+--gen-opt $TOTK_GEN_OPT} --toolchain \$TOTK_LLVM \
         --sysroot $sysroot --deps $deps --config /tmp/config.docker.toml --jobs $TOTK_JOBS"
in_shell "the self-test (layout, dispatch table, host functions)" \
    "cp -L $deps/lib/libSDL3.so.0 build/$v/ && $run_target build/$v/totk --selftest"
in_shell "shader seeds (translated and built on the device: totk --prepare-shaders)" \
    "py314 tools/prepare_shaders.py build/$v --version $v --no-run"
romfs=--with-romfs
[ "${TOTK_ROMFS:-1}" = 0 ] && romfs=
in_shell "the portable folder" \
    "py tools/package_build.py --version $v --build build/$v --xlate build/xlate/totk_xlate --out /out/totk-$v $romfs &&
     du -sh /out/totk-$v && echo \"==> The game: totk-$v in TOTK_OUT\""
INNER_SCRIPT

tty_flags=()
if [ -t 0 ] && [ -t 1 ]; then tty_flags=(-it); fi

# shellcheck disable=SC2046  # work_mounts prints the volume options to split
docker run --rm ${tty_flags[@]+"${tty_flags[@]}"} \
    --platform "$PLATFORM" \
    -v "$NIX_VOLUME":/nix -v "$SRC":/src $(work_mounts) -v "$LOGS":/logs \
    ${game_flags[@]+"${game_flags[@]}"} \
    -e TOTK_ARCH="$ARCH" -e TOTK_VERSION="$VERSION" -e TOTK_CPU="${TOTK_CPU:-}" -e TOTK_ROMFS="${TOTK_ROMFS:-1}" \
    -e TOTK_GEN_OPT="${TOTK_GEN_OPT:-}" \
    -e TOTK_OWNER="$OWNER" -e TOTK_JOBS="$JOBS" -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
    -e SDL_VIDEO_DRIVER=dummy -e SDL_AUDIO_DRIVER=dummy \
    "$IMAGE" bash -c "$INNER" inner "$MODE"

case $MODE in
    game) echo "==> Done: the folder totk-<version> in $OUT. Copy it to the device, run ./totk --prepare-shaders there once, then ./totk." ;;
    setup) echo "==> Done: TotK-Setup-$ARCH.AppImage in $OUT. Copy it to the device with your game files and start" \
                "it there: it builds the game into ~/.local/share/totk-recomp and can add it to Steam." ;;
    tools) echo "==> Done: the $ARCH build tools are ready in the Docker volumes." ;;
esac
