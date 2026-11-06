# Building from source

Windows 10 or later, an x86-64 CPU with SSE4.2, and a GPU with Vulkan 1.3 and `VK_EXT_external_memory_host`. The
default build targets x86-64-v3 (AVX2/FMA); a CPU without them builds with `--cpu v2` instead and runs slower (see
"Build"). Allow about 60 GB for the work folder. Extraction wants about 55 GB free while it runs and keeps about 35 GB
afterwards.

Players who only want to play can use the [installer](installer.md) instead: it builds the game on their machine
without this toolchain.

## Toolchain

Versions are the ones the build was verified with; newer patch releases are fine.

| Tool | Version | Install | Used for |
|---|---|---|---|
| MSYS2 UCRT64 | clang/lld 23.1.3 | msys2.org installer to `C:\msys64`, packages below | compiling the generated C and the runtime |
| Visual Studio Build Tools | 2026, C++ workload, Windows SDK 10.0.26100 | winget `Microsoft.VisualStudio.BuildTools` with `--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended` | the linker .NET NativeAOT calls |
| .NET SDK | 10.0 | winget `Microsoft.DotNet.SDK.10` | the shader translator (`totk_xlate.exe`) |
| Vulkan SDK | 1.4.363 | winget `KhronosGroup.VulkanSDK` | validation layers, `vulkaninfo` |
| uv | 0.13 | standalone installer from astral.sh | the two Python environments |
| hactool | 1.4.0 @ `1d64a83` | `.venv\Scripts\python tools\build_hactool.py`, see below | NCA and update extraction |

MSYS2 packages, after `pacman -Syuu` (run it twice; the first pass updates the core and exits):

```sh
pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-{clang,lld,ninja,gcc,make,pkgconf,sdl3,opus,libvpx,zstd,vulkan-devel,\
vulkan-memory-allocator,vulkan-validation-layers,shaderc,spirv-tools} base-devel git unzip
```

`ffmpeg` from MSYS2 is a GPLv3 build and is never linked; the movies go through libvpx.

PATH:

- Add `C:\Program Files (x86)\Microsoft Visual Studio\Installer` to the user PATH: NativeAOT needs `vswhere.exe`, or
  the publish fails at the link step (MSB3073).
- Another `cmake` or `ninja` earlier on PATH (a 32-bit winlibs MinGW, for example) is picked up by accident. The build
  scripts call Ninja from `C:\msys64\ucrt64\bin` themselves; from a shell, put `C:\msys64\ucrt64\bin` first.

### hactool

`tools/build_hactool.py` fetches SciresM/hactool at `1d64a83` into `toolchain\src\hactool`, applies the four patches in
`tools/hactool/` and builds a static `toolchain\hactool\hactool.exe` with MSYS2 UCRT64 gcc (a few seconds):

- `portable.patch`: the parts of MacNativeGames' `build_hactool()` that run on Windows, the `MNG_TITLE_KEYS` variable and
  the two BKTR fixes in `nca.c` for the subsection table padding and the subsection layout of newer cumulative updates;
- `unicode.patch`: a manifest with `<activeCodePage>UTF-8</activeCodePage>`, so paths under a non-ASCII user profile
  reach it intact;
- `mbedtls-asm-clobbers.patch`: the `memory` and `cc` clobbers the bundled mbedtls 2.6.1's x86-64 multiply asm lacks.
  Without them a clang build (the Linux release) fails every RSA signature check and the update merge stops;
- `memory-errors.patch`: two memory errors AddressSanitizer finds in the update merge. `nca_process_bktr_section`
  spreads the BKTR buckets in place with an overlapping `memcpy`, which aarch64 glibc copies wrongly (the merge crashed
  with SIGSEGV there; x86-64 glibc, macOS and Windows copy like `memmove`), and `get_kv` wrote a byte past its line
  buffer on a key line over 1023 characters, such as a Lockpick `eticket_rsa_keypair`.

hactool reads `-k <prod.keys>`; MSYS2 shells set `HOME`, which hides its default key location, so pass `-k` there.

## Build

From the repository root, in PowerShell. Every step that depends on the game version takes it as `<ver>`: `v143` for
1.4.3, `v121` for 1.2.1 or `v110` for 1.1.0. The tools default to `v121` when `--version` is left out. Saves load on any
of them.

1. Python environments (once). `.venv` runs the lifter and the build; `.venv-py314` runs the extraction scripts, which
   need the stdlib `compression.zstd` of Python 3.14:

   ```powershell
   uv venv --python 3.12 .venv
   uv pip install --python .venv/Scripts/python.exe -r tools/requirements.txt
   uv venv --python 3.14 .venv-py314
   uv pip install --python .venv-py314/Scripts/python.exe -r tools/requirements-py314.txt
   ```

2. Copy `config.example.toml` to `config.local.toml` and set the paths to your base game, the update and your console's
   keys file. `config.local.toml` is ignored by git. The tools read the keys file themselves.

3. Extract and verify. This writes `build\extracted\v0` (1.0.0) and `build\extracted\<ver>` for each update in
   `updates`, identified by its hash; a hash mismatch stops the run. With another `[work] dir` in `config.local.toml`,
   `extracted` goes there instead, and `build_game.py` reads the ExeFS and RomFS from there:

   ```powershell
   .venv-py314\Scripts\python tools\extract.py
   ```

4. Shader translator (no game files; again after a change to its sources, and `tools\make_release.py` stops on an older
   one). This publishes `build\xlate\totk_xlate.exe`:

   ```powershell
   .venv\Scripts\python tools\build_xlate.py
   ```

5. Lift the modules to C, write the RomFS overlay (the frame-rate fix), then compile and link
   `build\<ver>\totk.exe`:

   ```powershell
   .venv\Scripts\python recomp\gen.py --exefs build\extracted\<ver>\exefs --out generated\<ver> --patches recomp\patches-<ver>.txt
   .venv-py314\Scripts\python tools\make_romfs_patches.py --version <ver>
   .venv\Scripts\python tools\build_game.py --version <ver>
   ```

   `--cpu v2` targets x86-64-v2 (SSE4.2, no AVX2/FMA) for older CPUs; the default `v3` needs AVX2. The v2 build links
   `runtime/fma.c`, a correctly rounded software fma the FP lowering needs, and runs slower. Inline assembly,
   target attributes and intrinsics are not bound by `-march`, so a v2 build runs `tools/check_isa.py` over the runtime
   objects before it links: any AVX, BMI or other instruction above x86-64-v2 outside a function listed there as
   dispatched by a runtime CPU check stops the build.

   `gen.py` sizes its emit pool from the CPU count and the free memory; `--jobs N` sets it, and so does the environment variable `TOTK_LIFT_JOBS=N`, which the installer's lift step honours too (it has no `--jobs`). When a worker runs out of memory, the pool restarts with half the workers (`emit: out of memory with N workers, retrying with M`), down to one; only an out-of-memory error with one worker fails the step.

   Lifting takes about 15 s, a clean compile about 200 s on 24 threads; later builds recompile only what changed.
   A compiler cache in front of clang shortens it further: with `SCCACHE` set to an sccache executable, `build_game.py`
   writes it into the ninja file ahead of `cc`, and every lift rewrites the generated chunks with the same contents, so
   a second build of the same version hits all of their objects:

   ```powershell
   $env:SCCACHE = "path\to\sccache.exe"
   .venv\Scripts\python tools\build_game.py --version <ver>
   ```
   `--patches` builds in the frame-rate, resolution and other patch sets, switchable at run time. The link copies
   `SDL3.dll`, `libiconv-2.dll` and `libopus-0.dll` next to the exe.
   For Xbox button prompts, run `make_romfs_patches.py` twice more, with `--prompts xbox` and `--prompts xbox-label`:
   they write `build\<ver>\romfs-patches-xbox` and `romfs-patches-xbox-label` ([controls.md](controls.md), "Xbox
   button prompts"), which `build_game.py` copies next to `totk.exe` like `romfs-patches`. `--prompts pc` writes
   `romfs-patches-pc`, the keyboard and mouse prompts ([controls.md](controls.md), "PC button prompts").

6. Prepare the shaders (once per build folder and translator). It needs `refs\MacNativeGames` (`tools\fetch_refs.sh`)
   for the shared shader list:

   ```powershell
   .venv-py314\Scripts\python tools\prepare_shaders.py build\<ver> --version <ver>
   ```

   Without this step the first launch compiles shaders and pipelines during play and stutters until the caches fill.
   The AMD driver keeps its pipeline cache per exe path and holds a limited amount: preparing another build folder can
   evict this one's. `totk.exe --warm-shaders` rebuilds it from the shader cache.
   When the driver's shader compiler crashes on a variant (NVIDIA 615.78.08 on an RTX 5080 does, issue #30),
   `prepare_shaders.py` runs the driver step again with `--warm-shaders`, up to three times, without the variants the
   driver was building at the crash. `shader-prepare-skip.txt` next to the exe lists them with the GPU and driver
   version; they build at their first draw instead, another driver version builds them again, and so does deleting the
   file.

7. Run. The game keeps running when its window loses the focus (`assume_focus = 1` in `totk.ini`, the default; the
   Game tab switches it). With 0 it pauses, as it does on the Switch when Home is pressed.

   ```powershell
   build\<ver>\totk.exe
   ```

Settings live in `totk.ini` next to `totk.exe` ([totk.ini.example](../totk.ini.example) lists them). Saves go to
`build\profile` for `v121` and `build\profile-<ver>` for other versions (`build_game.py --profile` changes it).

### Portable folder

`.venv\Scripts\python tools\package_build.py --version <ver>` writes `build\<ver>-portable`, which runs on another of
your machines without MSYS2, Python or .NET. `romfs.bin` stays out unless you pass `--with-romfs`; otherwise put it in
`romfs\` next to the exe or point `TOTK_ROMFS` at its folder. Run `totk.exe --prepare-shaders` once in the copied
folder. The folder holds code and data derived from your game: for your own machines only.

### Profile-guided build

`build_game.py --pgo <file>.profdata` compiles the generated code at -O2 with an execution profile, and the runtime
with it too: hot paths laid out straight, hot calls inlined, code that never ran compiled for size. On 1.4.3 the fight
bench ran 12-15% faster per frame and a fight outside the training about 6%. The installer builds with
`data/pgo-<ver>.profdata` of the release when the update has one, else at -O1.

A profile only fits the generated C it was trained on: functions are matched by name and control-flow hash, so a
function whose C changes loses its counts without a warning. Retrain after a lifter or generator change, from a clean
`recomp/`. The instrumented build needs llvm-mingw, as MSYS2's clang has no profile runtime:

```powershell
$llvm = "toolchain\llvm-mingw-<date>\llvm-mingw-<date>-ucrt-x86_64"
.venv\Scripts\python tools\build_game.py --version v143 --out build\v143-pgogen --toolchain $llvm --pgo gen
```

Play `build\v143-pgogen\totk.exe` through varied scenes (fights, towns, riding, building, the Depths, menus) for 20-30
minutes; it is slower than a normal build. It writes `build\v143-pgogen\pgo\*.profraw` every minute and at exit.
Scripted runs from saves (`tools/run_game.py`) add to the same file. Then:

```powershell
.venv\Scripts\python tools\pgo.py merge build\v143-pgogen --version v143
```

writes `build\pgo\pgo-v143.profdata` and its record (`.json`: the `recomp/` tree it fits). `make_release.py` ships every
profile of `build\pgo` in `data\` and warns when a record names another `recomp/` tree than the one released.

## Linux (in progress)

The runtime builds and runs on x86-64 Linux with a window, input and menu on SDL3 (`*_sdl.c`) in place of the Win32
files (`*_win.c`); `runtime/platform/linux.c` implements `platform.h`. Status: the selftest and the probes pass; 1.2.1
plays at 60 fps on RADV (RX 7800 XT) and NVIDIA (RTX 3060); 1.4.3 builds, probes and plays on RADV (RX 6600, and the
Vega 7 of a Ryzen 5 4600G at about 34 fps). Long play sessions and the Steam Deck itself are not verified yet.

Requirements:

- Linux 6.7 or later: the write watch uses userfaultfd write protection with `PAGEMAP_SCAN`. SteamOS 3.8 qualifies.
  On a kernel without it (older, or built without `CONFIG_USERFAULTFD`), the game tracks writes per 2 MiB region
  instead (`runtime/memory.c`, "region write tracking") and says so in `totk.log`: it works, but costs frame rate.
  `TOTK_WRITE_WATCH=region` chooses that tracking anywhere, for comparisons.
- A Vulkan 1.3 driver (RADV, NVIDIA). `VK_EXT_external_memory_host` is needed only with `TOTK_SHADOW=0`: the default
  shadow storage (below) never imports host memory.
- Packages on Arch:

  ```sh
  pacman -S --needed clang lld ninja make sdl3 opus libvpx zstd vulkan-headers vulkan-icd-loader \
      vulkan-validation-layers shaderc spirv-tools dotnet-sdk uv git
  ```

  Vulkan Memory Allocator is a single header (AUR `vulkan-memory-allocator`). Without it, `build_game.py` fetches the
  version the Windows release pins (3.4.0) into `toolchain/vma/` on first use and checks its hash.

The steps are the Windows ones with POSIX paths: `.venv/bin/python` in place of `.venv\Scripts\python`, and `totk` in
place of `totk.exe`. `build_game.py` takes clang, lld and Ninja from PATH (or the ones in `<dir>/bin` with
`--toolchain <dir>`) and links against the system libraries, with `libstdc++` static and an `$ORIGIN` rpath;
`build_hactool.py` builds `hactool` with make. `tools/prepare_shaders.py` reads the shader list from
`refs/MacNativeGames` at `origin/HEAD`, which must be the pinned commit with its history (the list format changed later).

The probes open a window, so from an SSH session they need the desktop session's display, or they stop at `gpu 0
skipped: no graphics+compute+present queue family`: on Wayland `XDG_RUNTIME_DIR=/run/user/$(id -u)
WAYLAND_DISPLAY=wayland-1 SDL_VIDEODRIVER=wayland` (the socket name is in `$XDG_RUNTIME_DIR`), on X11 `DISPLAY=:0`.

Memory: on Windows the GPU imports guest memory and `GetWriteWatch` tracks the CPU's writes to it. On Linux,
re-protecting imported memory invalidates the driver's userptr buffer on every submit (about 1.3 ms per 16 MiB on
amdgpu), and importing pins the pages, which drops their write protection. Each CPU-visible pool therefore gets a
host-visible copy (shadow storage, `runtime/vk_memory.c`): the pages the CPU wrote go in before each submit, and the GPU's
writes come back at its fence. In play that costs about 0.2 ms a frame and doubles the memory of those pools (about
1.1 GB). `TOTK_SHADOW=1` turns it on under Windows too. The Steam Deck's RADV has `VK_EXT_external_memory_host` as
well: the shadow copies are chosen for the measured cost above, not for a missing extension.

The Deck's GPU (Van Gogh) has no native ASTC, so its textures go through the compute decoder (`runtime/vk_astc.c`,
`TOTK_ASTC`) as on desktop AMD GPUs without it; frame times measured there include it.

## ARM64 Linux and the Docker build

The runtime and the lifted code also build for aarch64 Linux (Snapdragon handhelds with Mesa's Turnip; the plan and
its status: [ARM64_PORT_PLAN.md](ARM64_PORT_PLAN.md)). `tools/build_game.py --arch aarch64` selects the target, and
`--cpu` its code level: `a64` (the default: Armv8.2 with the LSE atomics, RCpc and the dot product) or `oryon`
(Snapdragon 8 Elite). The FP/SIMD lowering is the same portable C as on x86-64, with FPCR in place of MXCSR
(`runtime/platform/fp_arm64.h`); the generated code keeps x18 free for `kern_ap_stub` (`runtime/kern_ap_stub.inc`), and
the runtime is compiled with `-fsigned-char` and `-fms-volatile` (`tools/build_game.py` `ARCH_FLAGS`).

Requirements beyond x86-64 Linux's: a 4 KB-page kernel with a 48-bit address space (the guest's modules sit at
0x7000000000) and Linux 6.10 or later for the write watch (arm64 gained userfaultfd write protection in 6.10), built
with `CONFIG_USERFAULTFD` and `CONFIG_PTE_MARKER_UFFD_WP`. Without them the game falls back to region write tracking
(above), slower but far faster than copying every pool at each use; `totk.log` names the reason.

`build_docker.sh` builds either architecture in a Docker container with Nix, on macOS (Apple Silicon builds aarch64
natively) or Linux, in the manner of the Bloodborne port's script; `./build_docker.sh --help` lists its modes and
settings. The host tools come from `shell.nix` (nixpkgs pinned in the script): Python 3.12 and 3.14 with the packages
of `tools/requirements*.txt` (uv), clang 23, .NET 10, Ninja. What runs on the handheld is compiled against an Ubuntu
22.04 sysroot (glibc 2.35) and links Opus, libvpx and zstd statically and SDL3 beside the exe (`tools/nix/default.nix`).
The sysroot's packages are pinned by `tools/nix/sysroot-<arm64|amd64>.lock.json`; `./build_docker.sh lock` writes it
from the Ubuntu archive (`tools/nix/sysroot_lock.py`), and every run writes it anew when it is missing or the script's
package list changed (commit it).

For a handheld it makes two things, each with its own mode. Both land in `build/install-<arch>` (`TOTK_OUT`):

| Mode | What it makes | Game files (`TOTK_BASE`, `TOTK_UPDATE`, `TOTK_KEYS`) |
|---|---|---|
| `game` (the default) | `totk-<version>/`: the game built on the computer, as a portable folder (`tools/package_build.py`), with the RomFS inside unless `TOTK_ROMFS=0` | always |
| `setup` | `TotK-Setup-<arch>.AppImage`: the setup, which builds the game on the handheld from the player's own files into the games folder and can add it to Steam | never |

```sh
./build_docker.sh test                       # the toolchain, then the checks that need no game files
TOTK_BASE=~/Switch/totk.nsp TOTK_UPDATE=~/Switch/totk-143.nsp TOTK_KEYS=~/Switch/prod.keys ./build_docker.sh
./build_docker.sh setup
```

Copy the `game` folder to the handheld and run `./totk --prepare-shaders` there once. The setup is what goes to
players: it carries the toolchain instead of the game's code, builds the game on the handheld from their own files and
adds it to Steam ([installer.md](installer.md), "Games folder and Steam (Linux)").

### The other architecture: x86-64 on an ARM machine

`--arch` picks the target (or `TOTK_ARCH`; `--arch` wins when both are set). Without either, the target is Docker's own
architecture: aarch64 on Apple Silicon, x86_64 on an Intel Mac or an x86-64 PC. For the other one the whole container
runs emulated: on Apple Silicon, `--arch x86_64` builds the game for x86-64 Linux PCs and the Steam Deck under Rosetta
or QEMU. It is the same build, only slower, several times so for the compile.

```sh
./build_docker.sh --arch x86_64 test                    # the toolchain and the checks, as a first try
TOTK_BASE=~/Switch/totk.nsp TOTK_UPDATE=~/Switch/totk-143.nsp TOTK_KEYS=~/Switch/prod.keys \
    ./build_docker.sh --arch x86_64 game                # the portable folder, build/install-x86_64/totk-<version>
TOTK_ARCH=x86_64 TOTK_CPU=v2 ./build_docker.sh game     # the same with TOTK_ARCH, for CPUs without AVX2
```

- **Docker has to run the other platform's containers.** The script checks that first (a `linux/amd64` container
  that prints `uname -m`) and, when it can't, stops and prints how to turn emulation on:
  - Docker Desktop: Settings > General > "Use Rosetta for x86_64/amd64 emulation on Apple Silicon";
  - OrbStack: built in;
  - colima: `colima start --vm-type vz --vz-rosetta`;
  - Linux on arm64: `docker run --privileged --rm tonistiigi/binfmt --install amd64` (lasts until the next reboot).
- **Each target has its own Docker image, volumes, logs and output.** The image is `totk-builder:<nixpkgs>-x86_64`, the
  Nix store `totk-nix-x86_64` (`TOTK_NIX_VOLUME=bbport-nix-x86_64` reuses the Bloodborne port's), the work volumes
  `totk-x86_64-*`, the logs `build/docker-logs-x86_64` and the output `build/install-x86_64`. Nothing is shared with
  the aarch64 build: the x86_64 build extracts the game files again (about 55 GB free at the peak, once more).
- **The sysroot lock.** The first x86_64 run writes `tools/nix/sysroot-amd64.lock.json` (Ubuntu 22.04 amd64 from
  `archive.ubuntu.com`); commit it with the arm64 one.
- **Code levels.** `TOTK_CPU=v3` (the default: AVX2, FMA, BMI2) or `v2` (SSE4.2, for older CPUs, slower). The build
  runs the game's self-test in the container, which checks that the CPU has the level's instructions, so the emulator
  must provide them: a v3 build stops there under an emulator without AVX2 or FMA, and `TOTK_CPU=v2` builds one that
  passes.
- **The outputs** are those of aarch64: `game` the portable folder (Steam Deck: from Desktop Mode, or as a non-Steam
  game), `setup` `TotK-Setup-x86_64.AppImage`. The x86-64 setup is built against Valve's Steam Runtime 3 "sniper" SDK,
  which `tools/make_release_linux.py` downloads itself, as upstream's Linux release is; the Nix sysroot is aarch64's.
- **On an x86-64 machine** the Docker build runs natively, and the Windows and Linux builds above ("Build", "Linux
  (in progress)") need no Docker at all.
- **Not tried yet with this tree:** an x86_64 build under Rosetta on Apple Silicon. NativeAOT (the shader translator)
  and the extraction's hactool run emulated there and are the likeliest to need work; the logs in
  `build/docker-logs-x86_64` say which step stopped.

The same works the other way: on an x86-64 host, `--arch aarch64` builds for the handheld under QEMU.

### The data folder

`TOTK_DATA_DIR` (`runtime/cfg.h` `cfg_data_dir`) names the folder for everything the game writes: `totk.ini`, `logs/`,
the profile (unless `TOTK_PROFILE_DIR` names one), the shader and pipeline caches (`shader-cache/`,
`pipeline-cache.bin` and the files beside it), `mesa-cache/`, `mods/` and `flight/`, for a launcher that keeps the
game's folder read-only. Unset, all of it stays next to `totk`. The files the build made stay next to `totk` either way: `totk_xlate`, `romfs-index.bin`, the RomFS
patches, `seed-*.bin`, `exefs/` and `build-cache/`.

### The setup AppImage

`./build_docker.sh setup` builds the setup AppImage, without game files: `TotK-Setup-<arch>.AppImage`, the Linux
release of `docs/installer.md` ("Linux release") with the toolchain inside it. It builds the game on the handheld
itself from the player's files, so it holds no game code and can be shared. Its runtime libraries are compiled against
a `layout.h` that holds only the module set (`recomp/layout.py` `write_runtime_only`), the one part of the generated
layout the runtime reads. When an earlier run left a lifted version in the build volumes, it also builds that game
once with the folder's own tools as a check (`tools/make_release_linux.py --prove`, the self-test and the kernel probe;
the probes that need a GPU run on the device). The window checks run under Xvfb in the container, and the setup's own
installer pass stops at its Vulkan check there, as the container has no GPU (`--no-gpu`).

### Checks on aarch64

The checks run on aarch64 too. A cross run names the compiler and an emulator: `CC='clang --target=aarch64-linux-gnu
--sysroot=DIR'` and `TOTK_TEST_RUN='qemu-aarch64 -L DIR'` (tests/diff/common.py). `tests/platform_check.py` checks
the fiber switch, the write-fault hook and, on aarch64, `kern_ap_stub` against a thread it interrupts; QEMU's user mode
leaves the ESR out of signal frames, so the write-fault part runs on a real kernel only. `tools/check_arch.py` fails on
x86-only code outside an x86-64 branch.

## Checks

Before a commit:

- `python tools/check_content.py --staged`: nothing game-derived is staged. CI runs the same check;
  `git config core.hooksPath .githooks` runs it before each local commit.
- `build\<ver>\totk.exe --selftest`: layout, dispatch table and host-function registry, without game files.
- `python tools/check_arch.py`: no x86-only code outside an x86-64 branch, so the aarch64 build keeps compiling.

A change to the button prompts (`tools/make_romfs_patches.py --prompts`, `tools/button_icons.py`) passes
`.venv-py314\Scripts\python tests\romfs_prompts_test.py`, which runs the tool on a RomFS it makes itself (no game
files); a change to the settings passes `totk.exe --probe-cfg`.

The lifter (`recomp/`) and the CPU headers (`runtime/cpu.h`, `runtime/simd.h`) are checked against Unicorn. Every lifter
change passes these, all run with `.venv`:

| Test | Checks | Game files |
|---|---|---|
| `tests\diff\lift.py` | instructions sampled from the extracted modules; `--coverage` lists every word the lifter rejects or traps | yes |
| `tests\diff\simd.py` | the whole FP/SIMD encoding space | no |
| `tests\diff\atomic.py` | exclusive load/store sequences and ARMv8.1 atomics | no |
| `tests\diff\func.py` | whole functions as the generator emits them, loops, jump tables and calls included (calls go to a stub that checks the registers at the call) | yes |
| `tests\flags_check.py` | the function-local flag helpers of `runtime\cpu.h` against its packed ones | no |
| `tests\excl_mt.py` | the exclusive monitor helpers under contention and ABA, on several threads | no |
| `tests\jumptables_check.py` | `recomp/jumptables.py` against a Unicorn oracle; must report 0 disagreements, on 1.4.3 too | yes |

A `--cpu v2` build passes the same differential tests with `--cflags=-march=x86-64-v2`; they compile `runtime/fma.c`
too, so they check the software fma the v2 build links rather than the C runtime's.

Runtime probes, each a mode of `totk.exe` (the list is at the top of `runtime/main.c`): `--probe-kernel`,
`--probe-vkexec`, `--probe-nvn-tex` and `--probe-shader` cover most runtime changes. `--probe-shader` and the draw
part of `--probe-vkexec` translate shaders of the census below: they look for `build/<ver>/shaders/jobs.json` of the
exe's own version first, then of the other one.

Shaders: `.venv-py314\Scripts\python tools\shader_census.py --version <ver> --translate` extracts every Maxwell shader
to `build\<ver>\shaders\` and runs it through the translator and `spirv-val`; failure groups go to `failures.txt`.
`tools\xlate_smoke.py` checks the translator alone, without game files.

## Reverse engineering (optional)

- Ghidra 12.1.2 with Adubbz's Ghidra-Switch-Loader in `toolchain\ghidra\` (JDK 21) opens the game modules (NSO).
- `tools\fetch_refs.sh` clones the upstream projects this port builds on into `refs\`, read-only.
