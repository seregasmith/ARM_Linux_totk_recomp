# ARM64 (aarch64 Linux) port plan

Status: written 2026-11-02 against commit `ab1dee5`; Phases 1 and 2 implemented since ("Status" below).

## Goal and decisions

- **Targets.** aarch64 Linux handhelds with Snapdragon SoCs and Mesa's Turnip Vulkan driver: first the Snapdragon 8
  Elite (Adreno 830), then 8 Gen 2 / Gen 3 (Adreno 740 / 750). They are the Bloodborne ARM port's targets too, so
  bbport's Turnip patches, its Docker + Nix build and its device tooling carry over.
- **CPU.** Keep the static recompiler. The lifted C is compiled for aarch64 instead of x86-64. The game is ARM64 code,
  so the guest and the host now share an instruction set and a memory model. That removes more problems than it adds.
- **One tree for both architectures.** x86-64 Windows and Linux keep building unchanged. aarch64 is selected by
  `--arch` and `#if defined(__aarch64__)`, so each step can go upstream as its own pull request.
- **Builds.** Docker + Nix, modelled on bbport's `build_docker.sh`. Phase 1 builds the playable folder on the Mac,
  in a native arm64 container, from the player's own files, and the folder is copied to the handheld. Phase 5 adds
  `TotK-Setup-aarch64.AppImage`, which builds the game on the handheld as the x86 release does.
- **Out of scope for now.** 16 KB page kernels (Asahi, Raspberry Pi 5's default kernel), Mali and V3D GPUs (no
  geometry shaders, no BCn), Windows on ARM, and native execution of the guest code (MacNativeGames style). See
  "Later".

| Phase | What | Size |
|---|---|---|
| 0 | Check the handheld: kernel, pages, address space, Vulkan | hours |
| 1 | Docker + Nix build environment, target sysroot, `build_docker.sh` | days |
| 2 | CPU and runtime compile and pass the tests on aarch64 | 1-2 weeks |
| 3 | Bring-up on the handheld: Turnip, window, input | open-ended (driver work) |
| 4 | Performance on ARM | ongoing |
| 5 | aarch64 setup AppImage; a setup window you can drive with a gamepad | 1-2 weeks |
| 6 | CI on arm64 runners, upstream pull requests | alongside |

## Status

Done in the working tree (uncommitted, 2026-11-02):

- **Phase 2, steps 1-8.**
  - Arch plumbing: `tools/build_game.py` `--arch` and `--cpu` (`CPU_LEVELS`, `ARCH_FLAGS`, `RT_ARCH_FLAGS`,
    `GEN_ARCH_FLAGS`).
  - FP control: `runtime/platform/fp_arm64.h`, `runtime/simd.h`, `runtime/cpu.h`.
  - Platform assembly in `runtime/platform/linux.c`: fibers, the signal context, write faults from the ESR.
  - `kern_ap_stub` for AArch64, moved with the x86 stubs into `runtime/kern_ap_stub.inc`; the generated code is
    compiled with `-ffixed-x18`.
  - Leaf files: `vk_rec.c`, `ov_movie.c` (NEON), `sha256.c` (Armv8 SHA-256), `sysinfo.c` (core names from MIDR),
    `main.c` (HWCAP check), `vk_hdr.c` (a missing include).
  - `tools/check_arch.py`.
- **Memory ordering (step 5).** The runtime is compiled with `-fms-volatile` on aarch64: every `volatile` access
  becomes an acquire load or a release store, the order x86-64 gives. A per-variable audit was not needed. The 149
  relaxed atomics were reviewed; they are counters, statistics and hints.
- **Phase 3, step 1.** `VK_EXT_external_memory_host` is required only when shadow storage is off
  (`vk_mem_shadow_wanted`).
- **Phase 1.**
  - `build_docker.sh` and `shell.nix`.
  - `tools/nix/`: toolchain, Ubuntu 22.04 sysroot, SDL3, deps, and the lock generator `sysroot_lock.py`.
  - `tools/build_xlate.py` builds `linux-arm64` on aarch64.
  - `tools/package_build.py`: shared fonts are optional, and `libSDL3.so.0` is copied.
  - Requirement markers for three packages unused on aarch64.
- **Tests.**
  - Cross-architecture runs: `CC`, `TOTK_TEST_ARCH` and `TOTK_TEST_RUN` in `tests/diff/common.py`.
  - `tests/platform_check.py`.
  - `.github/workflows/tests.yml`, on x86-64 and arm64 runners.

Verified without game files:

- **Runtime library.** It compiles for aarch64 with clang 23.1.4, and still for x86-64.
- **Under QEMU (aarch64):**
  - `tests/diff/simd.py`, the full default run: 159,960 cases, 0 failing.
  - `tests/diff/atomic.py`: 0 failing.
  - `tests/flags_check.py`: 0 mismatches.
- **On AArch64 hardware** (a Linux 6.8 VM on Apple silicon):
  - `tests/platform_check` passes: fibers; write faults from the ESR, atomics included; `kern_ap_stub` through 200
    interruptions.
  - The SIMD and atomics harnesses give output bit-identical to QEMU's.
  - The SHA-256 test vectors pass.
- **x86-64.** The same suites pass, with clang 18 and 23.

Added after the first game build on the Mac (2026-11-04):

- **hactool on aarch64 Linux.** `tools/hactool/memory-errors.patch`: the BKTR bucket moves used an overlapping
  `memcpy`, which aarch64 glibc copies wrongly (the update merge crashed with SIGSEGV), and a key line over 1023
  characters wrote past a buffer.
- **`TOTK_DATA_DIR`** in the runtime (`runtime/cfg.h` `cfg_data_dir`): everything the game writes can live outside its
  folder. It came with a game AppImage, since removed (Phase 5, "Two outputs, two modes").

- **Write tracking without userfaultfd.** The handheld's kernel (7.2.6) is built without `CONFIG_USERFAULTFD`, so
  nothing tracked guest writes: the back end copied every pool and re-uploaded every texture at each use, about
  190 MB a frame, and frame preparation took 88 ms of a 92 ms frame. `runtime/memory.c` now tracks writes per 2 MiB
  region there, with the frozen regions (read-only until the first write), and `totk.log` names the reason. Tested on
  aarch64 with a kernel lacking the feature (`userfaultfd: asynchronous write protection (Invalid argument)`), not yet
  in the game.

Known issue: `tests/excl_mt.py` fails its ABA cases on x86-64 and aarch64 alike. The CAS-based exclusive monitor in
`cpu.h` cannot detect ABA; this is upstream behaviour, not a port regression.

Not verified yet, because this needs your machine:

- **`build_docker.sh` and `tools/nix`.** They were evaluated against the pinned nixpkgs (`nix-instantiate`) but not
  built: the environment that wrote them could not reach cache.nixos.org or the Ubuntu archive. On the Mac, run
  `./build_docker.sh lock` (commit the lock file it writes), then `./build_docker.sh test`.
- **Everything that needs game files:** the lift, function and jump-table tests, the game build, and the real
  executable's self-test.
- **The device phases:** Phase 0 (device checks), Phase 3 (Turnip bring-up), Phase 4 and Phase 5 (installer, host
  check, AppImage, gamepad setup window).

## What makes the current tree x86-only

This is the complete list from a search of `runtime/`, `recomp/`, `tools/` and `installer/`. Everything else in
`runtime/` (51.5k lines of C) and `recomp/` is portable C and Python.

| Where | x86 dependency | aarch64 replacement | Phase |
|---|---|---|---|
| `tools/build_game.py` `GEN_FLAGS`, `RT_FLAGS`, `set_cpu`; `--sysroot` path | `-march=x86-64-v3` / `v2`, `--target=x86_64-linux-gnu` | `--arch aarch64`, aarch64 CPU levels (Phase 2), `--target=aarch64-linux-gnu` | 2 |
| `runtime/simd.h` | `immintrin.h`; FPCR.RMode through MXCSR (`_mm_getcsr` / `_mm_setcsr` in `simd_rm_*`) | `runtime/platform/fp_arm64.h`: FPCR.RMode (bits 22-23) through `mrs` / `msr fpcr` | 2 |
| `runtime/cpu.h` `rt_fpcr_sync`, `rt_get/set_fpcr` | The x86 branch mirrors FPCR.FZ into MXCSR. The `__aarch64__` branch assumes the asm-passthrough lifter (host FPCR = guest FPCR) and makes `rt_fpcr_sync` a no-op, but the TotK lifter emits `c->fpcr = ...; rt_fpcr_sync(c);` (`recomp/lifter.py`, `self.sc`). | Choose the branch by lifter mode, not by host. With `--simd=c` on aarch64, `rt_fpcr_sync` writes FZ into the host FPCR, as MXCSR FTZ\|DAZ does on x86. | 2 |
| `runtime/simd_fp.h:190` | `"+x"` asm constraint (an SSE register) | `"+w"` | 2 |
| `runtime/platform/platform.h`, `fp_x86.h` | `plat_pause` is `_mm_pause` (empty on other hosts); `plat_fp_get/set` are MXCSR | `yield` (or `isb`: measure); FPCR | 2 |
| `runtime/platform/linux.c` `plat_fiber_swap` / `plat_fiber_entry` | x86-64 asm (callee-saved GPRs, MXCSR, x87 control word) | aarch64 asm: x19-x30, sp, d8-d15, FPCR | 2 |
| `runtime/platform/linux.c` `intr_handler`, `plat_intr_stop` | `REG_RIP` / `REG_RSP` | `uc_mcontext.pc` / `uc_mcontext.sp` | 2 |
| `runtime/platform/linux.c` `fault_handler` | The x86 page-fault error code (`REG_ERR`, write bit 2) separates write faults (frozen-region thaw, crash report). | The ESR from the `esr_context` record in `uc_mcontext.__reserved` (`ESR_MAGIC`), WnR bit 6 | 2 |
| `runtime/kernel.c` `kern_ap_stub` (asynchronous preemption) | x86-64 asm: saves volatile GPRs, flags, YMM0-15 and MXCSR, then jumps back through a stack slot | aarch64 stub (Phase 2, step 8). Until then `TOTK_ASYNC_PREEMPT=0`. | 2 → 4 |
| `runtime/vk_rec.c` | `_mm_pause` in the recorder ring | `plat_pause()` | 2 |
| `runtime/ov_movie.c` | SSE2 U/V interleave | NEON `vst2q_u8`, or plain C | 2 |
| `runtime/sha256.c` | SHA-NI path under `__x86_64__` (a portable fallback exists) | optional ARMv8 SHA2 path (`vsha256hq_u32`) | 4 |
| `runtime/sysinfo.c` | `cpuid` brand string | `/proc/cpuinfo` / MIDR | 2 |
| `runtime/main.c` `check_cpu` | AVX2 / FMA / BMI2 self-test | HWCAP check of the level the build targets | 2 |
| `runtime/fma.c` | software fma for x86-64-v2 | not used: guarded by `__x86_64__`, and ARMv8 has fused multiply-add | - |
| `recomp/regs.py` `LOCALS = 6` | sized for x86-64's 16 GPRs | 10-16 on aarch64 (31 GPRs), measured | 4 |
| `installer/hostcheck.c`, `installer/steps.py` (`x86_64_v2/v3`, `ctx.cpu`), `installer/layout.py` (`libtotkrt-v2.a`) | CPUID levels | HWCAP levels, page size, VA bits, kernel write-watch support | 5 |
| `tools/make_release_linux.py` | LLVM `Linux-X64`, the sniper `amd64` sysroot, python-build-standalone `x86_64`, appimagetool and runtime `x86_64` | LLVM `Linux-ARM64`, an Ubuntu 22.04 arm64 sysroot, `aarch64` Python, `runtime-aarch64` | 5 |
| `tools/build_xlate.py` | `-r linux-x64` | `-r linux-arm64` (NativeAOT supports it) | 1 |
| `tools/requirements.txt` | Windows and x86 packages that no file in the repository imports (`pywin32-ctypes`, `pyinstaller`, `pefile`, `lief`, `keystone-engine`, `oead`, `evfl`, `quicktex`) | Environment markers, or a Linux list. Of the test packages only `unicorn` and `capstone` matter, and both exist for aarch64. | 1 |

These are not x86-specific, but they behave differently on aarch64:

- **`char` is unsigned on aarch64 Linux.** Build the runtime with `-fsigned-char`, which keeps the x86 behaviour,
  rather than audit 51k lines first. The lifted code uses fixed-width types and is unaffected.
- **Memory ordering.** The guest side is already correct:
  - `DMB` lowers to `__atomic_thread_fence(__ATOMIC_SEQ_CST)`, which is `dmb ish`.
  - `LDAR` / `STLR` lower to acquire / release.
  - Exclusives lower to compare-and-swap, which is `casal` with LSE.

  The host side has only ever run on x86, whose TSO ordering hides missing barriers. `runtime/` has 199 `volatile`
  declarations, many of them cross-thread flags (`gfx_present.c`, `fs_host.c`, `boot.c`, `audren.c`, `cheats.c`, ...).
  On ARM a `volatile` store publishes nothing else. Each one becomes `__atomic_*` with acquire / release, or gets a
  comment saying why not (Phase 2).
- **No red zone in AAPCS64.** The aarch64 `kern_ap_stub` needs no 128-byte skip below sp.
- **Branch target identification.** The generated code must not be built with BTI. Keep `-mbranch-protection=none`
  (clang's default) explicit, because `kern_ap_stub` jumps back to an arbitrary instruction.
- **Address space.** Guest addresses are host addresses below 2^39 (`recomp/layout.py` `FIRST_BASE = 0x7000000000`,
  `runtime/memory.c`). That is fine on 48-bit VA kernels. On a 39-bit VA kernel, which Android-derived kernels often
  use, user space ends at 2^39 and the host's own mappings share the range. Check on the device (Phase 0) and stop
  with a clear message otherwise.
- **4 KB pages** are assumed throughout: `runtime/memory.c` `PAGE`, guest permissions per page, the write watch. A
  kernel with 16 KB or 64 KB pages is refused with a message, not supported.
- **Write watch.** `runtime/platform/linux.c` uses userfaultfd asynchronous write protection with `PAGEMAP_SCAN`
  (Linux 6.7 on x86). arm64 gained userfaultfd write protection only in Linux 6.10, so the kernel floor on ARM is 6.10.

## Phase 0: check the handheld (hours)

Before changing any code, run the following on each target device. Save the output in `docs/arm64-devices.md`,
without personal data.

1. The kernel and memory:
   - `uname -r`: 6.10 or newer.
   - `getconf PAGESIZE`: 4096.
   - VA size and userfaultfd: `zcat /proc/config.gz | grep -E 'ARM64_VA_BITS|USERFAULTFD|HAVE_ARCH_USERFAULTFD_WP'`,
     or the same on `/boot/config-*`.
2. `ldd --version`: glibc 2.35 or newer, for the Ubuntu 22.04 sysroot below.
3. `vulkaninfo --summary`, plus the full lists of extensions and features.
   - What the runtime requires today (`runtime/vk_device.c`):
     - Vulkan 1.3.
     - Extensions: `VK_KHR_swapchain`, `VK_KHR_push_descriptor`, `VK_EXT_depth_clip_control` and
       `VK_EXT_external_memory_host`.
     - Features: timelineSemaphore, dynamicRendering, synchronization2, shaderStorageImageWriteWithoutFormat,
       depthClipControl, shaderInt64, samplerAnisotropy, independentBlend, geometryShader and textureCompressionBC.
   - Also record whether these are present:
     - Extensions: `VK_EXT_graphics_pipeline_library`, `VK_EXT_map_memory_placed` and
       `VK_EXT_external_memory_dma_buf`.
     - `textureCompressionASTC_LDR`.
     - The subgroup sizes.
     - Whether a memory type is HOST_CACHED and HOST_COHERENT.
4. The Mesa version, and whether it is the device's own Turnip or bbport's patched one. bbport needed its
   `packaging/mesa-sm8550/` patches to stop Turnip losing the device on the Adreno 740, plus Adreno 830 chip IDs.
5. RAM, free disk space, display resolution and compositor (gamescope, Sway, KDE, ...).

**Exit.** There is one table per device. Each "no" (VA bits, page size, kernel 6.10, a required Vulkan feature)
becomes a work item or a known limit before Phase 2 starts.

**Open question.** Turnip may not expose `VK_EXT_external_memory_host` at all. Linux already runs in shadow storage
mode (`docs/building.md`, "Memory"), which never imports guest memory, so the extension can become optional there
(Phase 3).

## Phase 1: Docker + Nix build environment (days)

### The script

`build_docker.sh` sits at the root of this tree and is written like bbport's:

- It runs on macOS's bash 3.2 with `set -eo pipefail`.
- `--help` prints the header comment.
- It runs no git commands on the repository.
- Each run writes a log into the work folder, which outlives the container.

Supported hosts:

- **macOS on Apple Silicon** (Docker Desktop, OrbStack or colima): the container is linux/arm64 and builds natively.
- **Linux arm64 with Docker Engine:** works the same way.
- **An x86-64 host** runs the aarch64 container emulated (QEMU), as bbport's script does: it works, slowly. Building
  for aarch64 there without emulation (clang `--target` plus the sysroot, the tests under qemu-user) comes later.
- **The reverse,** `--arch x86_64` on Apple Silicon, runs the x86-64 container under Rosetta or QEMU for x86-64 PCs
  and the Steam Deck (`docs/building.md`, "The other architecture"). Not tried yet with this tree.

```
./build_docker.sh [--arch aarch64|x86_64] [tools|test|game|lock|shell|clean]
  tools  no game files: the Python environments, refs/, hactool, totk_xlate for the target, and from Nix the target
         toolchain, sysroot and libraries (a test program is built and run against them)
  test   tools, then the checks that need no game files: tests/flags_check.py, tests/diff/simd.py,
         tests/diff/atomic.py, tests/platform_check.py, tools/check_arch.py, tools/check_platform.py,
         tests/installer_test.py (tests/excl_mt.py too, informational: its ABA cases fail upstream as well); after a
         game build also tests/diff/lift.py, tests/diff/func.py and tests/jumptables_check.py
  game   (default) tools, then the game from the player's files (below) into a portable folder in TOTK_OUT
  lock   write tools/nix/sysroot-<arm64|amd64>.lock.json from the Ubuntu archive (the first tools run does it when
         the file is missing)
  shell  nix-shell in the container, in the source tree
  clean  delete this target's image and its own volumes (never a shared Nix volume)
```

| Variable | Meaning |
|---|---|
| `TOTK_ARCH` | Target, as `--arch`. Default: Docker's own architecture. |
| `TOTK_SRC` | Source tree. Default: the script's directory. |
| `TOTK_BASE`, `TOTK_UPDATE`, `TOTK_KEYS`, `TOTK_FIRMWARE` | Host paths of the player's files, each mounted read-only on its own (game mode only). The keys file is passed by path; its contents never reach a command line, the log or the image. |
| `TOTK_VERSION` | `v143` (default), `v121` or `v110`. |
| `TOTK_OUT` | Where the portable folder goes. Default: `build/install-<arch>-<ver>/`, which git ignores; the folder holds game-derived files. |
| `TOTK_ROMFS` | `0`: leave the 16 GB RomFS image out of the folder. |
| `TOTK_CPU` | Code level: aarch64 `a64` (default) or `oryon`; x86_64 `v3` (default) or `v2`. |
| `TOTK_GEN_OPT` | Optimisation level of the generated C: `O2` (the aarch64 default) or `O1` (the x86_64 default). |
| `TOTK_NIX_VOLUME` | An existing Nix store volume to share. bbport's `bbport-nix` works when both scripts pin the same nixpkgs (below); clang, Python and .NET are then downloaded once. |
| `JOBS` | Compiler processes. Default: Docker's RAM divided by 2 GB, capped at its CPU count, as in bbport. |
| `GITHUB_TOKEN` | Optional. Raises Nix's GitHub API rate limit. |

### Image and Nix

- **Image.** `FROM nixos/nix:latest` plus nixpkgs pinned by commit in `NIX_PATH`, the same Dockerfile as bbport's.
  Pin the same revision as bbport (`6510408d147d8c5e6d76043e899e9251756f6a75`, 2026-10-10) so the store volume can be
  shared, and later move both pins together.
- **Nix guards.** The inner script copies bbport's verbatim, rather than rewriting them:
  - `NIX_CONFIG` with no sandbox, no build users and no seccomp filter;
  - `max-jobs = 1` and `cores = $JOBS`;
  - the `/homeless-shelter` post-build hook, its watcher and the three-attempt retry;
  - `HOME=/root`.
- **`shell.nix` (new): the host tools.**
  - Compiler: `llvmPackages_23` (clang-unwrapped, lld, and llvm-ar / objcopy / strip). Write it as
    `llvmPackages_23 or llvmPackages_latest` until the pin has LLVM 23.
  - Build tools: ninja, gnumake, cmake, pkg-config, git.
  - Python: python312, python314, uv.
  - .NET: `dotnet-sdk_10`.
  - Shader and Vulkan tools: spirv-tools, glslang, vulkan-headers, vulkan-tools.
  - Sysroot and archive tools: dpkg, patchelf, xz, zstd.
- **Python packages.** They come from the repository's pinned `tools/requirements*.txt` through uv, as on x86.
  manylinux wheels need `libstdc++.so.6` and zlib, so set `LD_LIBRARY_PATH` for the uv-run commands only. Set
  globally, it would leak into the test runs of the target binaries.
- **Prebuilt programs.** The NativeAOT compiler that NuGet brings for the shader translator is built for a usual
  Linux; the container links `/lib/ld-linux-*.so.*` to nix-ld, which gives it Nix's glibc and libraries.
- **Host versus target.** Host tools run on Nix's glibc. Everything that goes to the handheld (`totk`,
  `libSDL3.so.0`, `totk_xlate`) links against the target sysroot, never against `/nix/store`.
- **Test compiles.** The unit and differential tests compile and run inside the container, so they use the
  Nix-wrapped clang. Shipped binaries use clang-unwrapped with the sysroot.

### Target sysroot

Nix is what makes this part reproducible. The x86 release links the game against Valve's sniper SDK (glibc 2.31).
There is no arm64 sniper SDK, so aarch64 uses an Ubuntu 22.04 arm64 sysroot (glibc 2.35). bbport's ahead-of-time CPU
(`build_aot_docker.sh`) uses the same floor.

- **`tools/nix/default.nix` `sysroot`:** the `.deb` files `tools/nix/sysroot-<arm64|amd64>.lock.json` pins, each a
  fixed-output download, unpacked with absolute symlinks made relative.
  - The lock: `tools/nix/sysroot_lock.py` (`./build_docker.sh lock`) reads the archive's `Packages.xz` for jammy,
    jammy-updates and jammy-security and writes the URL and SHA-256 of the newest version of each package. The default
    archive is ports.ubuntu.com (arm64) or archive.ubuntu.com (amd64); `--mirror` takes a dated snapshot.ubuntu.com
    URL to pin the index as well. When a pinned version leaves the archive, run it again.
  - The roots, closed over their dependencies: glibc, kernel headers and GCC 11's libgcc and libstdc++; the libraries
    the game links (libvulkan-dev, libopus-dev, libvpx-dev, libzstd-dev); the headers and sonames SDL3 builds against
    (X11, Wayland, xkbcommon, libdecor, EGL/GL/GLES, libdrm, GBM, ALSA, PulseAudio, PipeWire, udev, D-Bus).
- **`sdl3`:** nixpkgs' SDL3 release (3.4.16) built with the Nix clang against that sysroot (a CMake toolchain file:
  `--target=aarch64-linux-gnu`, the sysroot). It produces the `libSDL3.so.0` that goes beside `totk`; Ubuntu 22.04 has
  no SDL3 package.
- **`deps`:** what `tools/build_game.py --deps` takes: Vulkan headers newer than Ubuntu 22.04's and VMA from nixpkgs,
  SDL3, and Opus, libvpx and zstd as the static `.a` files of the `-dev` packages.
- **`toolchain`:** Nix's clang 23 without Nix's cc-wrapper (`tools/nix/clang-wrapper.sh`; the wrapper would add Nix's
  glibc), with lld and the LLVM binutils. It adds the target's dynamic linker to a link, which nixpkgs' clang leaves
  out.
- **Running target binaries in the container.** The nixos/nix image has no `/lib/ld-linux-aarch64.so.1`, so call the
  sysroot's loader: `$SYSROOT/lib/ld-linux-aarch64.so.1 --library-path ... ./totk --selftest`. The build does not
  depend on the page size, but these test runs do: check `getconf PAGESIZE` inside the Docker VM.

### Volumes and paths

- **Work volume.** macOS bind mounts are slow for many small files and case-insensitive (the APFS default). The heavy,
  file-heavy parts therefore stay in a Docker volume, `totk-work-<arch>`:
  - the generated C (about 1.3 GB in thousands of chunks) and the objects;
  - the 117,000 loose shader-cache files;
  - the 55 GB extraction peak.

  The volume is mounted over `/src/build`, `/src/generated`, `/src/toolchain`, `/src/refs`, `/src/.venv` and
  `/src/.venv-py314`, as bbport mounts `build-x86_64/out` over `out/`. Only the finished install folder is copied out
  to `TOTK_OUT`.
- **Docker Desktop.** Give the VM about 100 GB of disk and at least 8 GB of RAM. The script warns below that, as
  bbport's does.
- **Config file.** The tools read `config.local.toml` from the repository root, and the container must not overwrite
  the player's own. It writes `/tmp/config.docker.toml` instead (container paths `/game/base.nsp`, ...;
  `[work] dir = 'build'`) and passes it on. `tools/extract.py` and `tools/extract_fonts.py` already take `--config`.
  `tools/build_game.py` needs the same option, because its work-folder lookup only reads the root file today.

### The `game` mode

`game` runs the developer pipeline on the player's files, mounted read-only:

1. `tools/extract.py --config /tmp/config.docker.toml`, a settings file with the container's paths (the player's
   `config.local.toml` is left alone).
2. `recomp/gen.py` lifts the version to C.
3. `tools/make_romfs_patches.py` writes the RomFS patches: the game's, then both Xbox prompt layouts.
4. `tools/build_game.py --arch --toolchain --sysroot --deps` compiles and links.
5. `totk --selftest` runs through the sysroot's loader.
6. `tools/prepare_shaders.py --no-run` writes the shader seeds.
7. `tools/package_build.py` writes the portable folder to `TOTK_OUT`, with `libSDL3.so.0` and, unless `TOTK_ROMFS=0`,
   the RomFS image.

Ninja keeps the build incremental, so a later run recompiles only what changed.

On the handheld, run `./totk --prepare-shaders` once in the copied folder. It translates the seeds and builds the
pipelines for that GPU, a step a container cannot do. Then play. For later builds, `rsync` sends only what changed,
usually just `totk`.

The installer's own path (`python -m installer`) stays for Phase 5. It needs a `--no-gpu` option and an aarch64 layout
in `installer/layout.py` (the sysroot, `libSDL3.so.0`, `runtime_lib_for('a64')`).

### Exit criteria

- `./build_docker.sh test` passes in a clean clone on an Apple Silicon Mac, and setup takes under a minute when the
  volumes are already populated.
- `./build_docker.sh game` with the player's files runs extraction, lifting and the RomFS and shader steps in the
  container. The compile and link of `totk` go through once Phase 2 steps 1-4 are in; until then the x86 asm and
  intrinsics stop them. Phases 1 and 2 are therefore developed together, with `TOTK_ASYNC_PREEMPT=0` until step 8.
- `tools/check_content.py --staged` stays clean. Nothing game-derived is written outside `build/`, `generated/` or
  `TOTK_OUT`.

## Phase 2: the CPU and runtime on aarch64 (1-2 weeks)

The steps are ordered so that each one has a test that can fail.

1. **Arch plumbing** (`tools/build_game.py` and the helpers `make_release_linux.py` uses).
   - `--arch {x86_64,aarch64}`, defaulting to the host.
   - aarch64 levels for `--cpu`:
     - `a64` (the default): `-march=armv8.2-a+lse+rcpc+dotprod`. Snapdragon 8 Gen 2, 8 Gen 3 and 8 Elite, Apple M1
       and newer, and Cortex-A76-class cores all have it. LSE turns the exclusive-monitor compare-and-swap into a
       single `casal`.
     - `oryon`: `-mcpu=oryon-1` for the 8 Elite, if the pinned clang knows it. Measure before shipping it.
   - Add `-fsigned-char`, and keep `-mbranch-protection=none` explicit for the generated code.
   - `RT_CPU_LEVEL` gets an aarch64 counterpart, and `main.c` `check_cpu` reads `getauxval(AT_HWCAP)`
     (`HWCAP_ATOMICS`, `HWCAP_LRCPC`, `HWCAP_ASIMDDP`).
2. **FP control for `--simd=c` on aarch64.**
   - Add `runtime/platform/fp_arm64.h` for FPCR access.
   - Move `simd.h` `simd_rm_*` to FPCR.RMode, using the ARM encoding directly (no lookup table).
   - Make `cpu.h` `rt_fpcr_sync` write FPCR.FZ.
   - Fix the `simd_fp.h` constraint.

   The lowering's ARM semantics (NaN rules, FZ, saturation) stay in software and the host only supplies IEEE
   operations, as on x86.

   **Tests:** `tests/diff/simd.py` (the whole FP/SIMD encoding space against Unicorn), `tests/flags_check.py`,
   `tests/diff/atomic.py` and `tests/excl_mt.py`, all on aarch64. They need no game files and run in
   `build_docker.sh test`.
3. **Platform assembly in `runtime/platform/linux.c`.**
   - `plat_fiber_swap` and `plat_fiber_entry` for aarch64.
   - `intr_handler` reads and writes `pc` / `sp`.
   - `fault_handler` takes the write bit from the ESR record.
   - `plat_pause` becomes `yield`.

   **Tests:** `totk --selftest`; `--probe-kernel` (fibers, threads, the virtual cores); the frozen-region thaw in
   `gfx_abi_probe.c`, which exercises the write-fault path.
4. **Leaf files:** `vk_rec.c`, `ov_movie.c`, `sysinfo.c`, `main.c`.

   Add `tools/check_arch.py` next to `tools/check_platform.py`, so review catches new x86-only code. It fails on any
   of these outside `runtime/platform/` and an allowlist:
   - x86 intrinsics;
   - `__x86_64__`-only code paths with no aarch64 branch;
   - inline x86 asm.
5. **Host memory ordering: the `volatile` flags above.** Done with `-fms-volatile` for the runtime
   (`tools/build_game.py` `RT_ARCH_FLAGS`): every volatile load is an acquire and every volatile store a release, as
   on x86-64, without editing the 199 declarations. The relaxed atomics were reviewed as well (counters, statistics,
   hints). Converting the flags that publish data to explicit `__atomic` acquire/release remains a cleanup.

   Afterwards, run `--probe-kernel`, `--probe-vkexec` and a long soak on the handheld. ThreadSanitizer is unlikely to
   work with the fixed guest layout at `0x7000000000`: try it once, then rely on review and soak runs.
6. **Tests that need game files.** Run `tests/diff/lift.py`, `tests/diff/func.py` and `tests/jumptables_check.py` on
   aarch64. The last must report 0 disagreements on 1.2.1 and 1.4.3.
7. **Address space checks.** `memory.c` already re-executes or fails when its reservations below 2^39 fail. On
   aarch64, name the likely cause in that message (a 39-bit VA kernel, or 16 KB pages) and add both checks to the
   self-test.
8. **Asynchronous preemption.** This step can slip into Phase 4: without it the game runs, but a better-priority
   thread waits longer.
   - **The problem.** AArch64 cannot jump without a register, and the interrupted code may have any of x0-x30 live.
   - **The fix.** Compile the generated code with `-ffixed-x18` (Linux leaves x18 to applications); the stub then
     returns with `br x18`.
   - **What the stub saves:** x0-x17, x29, x30, NZCV, FPCR, FPSR and q0-q31. All of q0-q31 is needed because AAPCS64
     preserves only the low halves of v8-v15.
   - **The rest of the stub:**
     - It builds a frame record whose return address is the interrupted pc, so stack walks still show the interrupted
       function.
     - It calls `kern_ap_rip` and `kern_ap_yield`, restores everything and branches back.
   - **The handler.** `intr_handler` redirects `uc_mcontext.pc` instead of `REG_RIP`.

**Exit.** Every test that needs no game files, and the self-test, pass in the container. With the player's files, the
lift tests pass and `totk --probe-kernel` runs on the handheld.

## Phase 3: on the handheld (open-ended: driver work)

1. **Vulkan requirements (`runtime/vk_device.c`).**
   - `VK_EXT_external_memory_host` becomes required only when shadow storage is off. Linux runs with it on
     (`vk_memory.c` `shadow_on`).
   - The `VkPhysicalDeviceExternalMemoryHostPropertiesEXT` query is skipped when the extension is absent.
   - The integrated Adreno is picked (score 2). `vk_memory.c` `mirror_mode` already chooses "auto, uma", which means
     no device-local mirrors.
2. **Bring-up order.**
   1. `--probe-vkexec`, `--probe-nvn-tex` and `--probe-shader` on the device.
   2. The title screen.
   3. Play.

   `tools/shader_census.py --translate` runs in Docker (CPU only). Compile failures in Turnip's ir3 compiler show up
   on the device through `--probe-shader` and `totk --prepare-shaders`.
3. **Turnip.** Adreno 830 support in Mesa is young. bbport patches nixpkgs' Mesa (`packaging/default.nix`,
   `packaging/mesa-sm8550/`: Adreno 830 chip IDs, bindless UBO constant lowering off, a freedreno Vulkan fix), or
   builds a turnip/gen8 tree through `BB_TURNIP_SRC`.
   - Try the device's own Mesa first. If it fails, use a Turnip with bbport's patches.
   - Getting that Turnip into a `totk` built outside Nix takes one of two routes: build it against the same Ubuntu
     sysroot (a Mesa derivation in `tools/nix/`), or run `totk` inside bbport-style packaging. Phase 5 decides.
4. **Features that are likely to differ.**
   - **ASTC** is native on Adreno, so `vk_astc.c` uploads it as is, with no compute decoder.
   - **Other features to verify** against the Phase 0 lists:
     - BCn support;
     - `VK_EXT_graphics_pipeline_library` (the fallback path exists);
     - the subgroup size, 64 or 128 (`vk_caps.c` passes it to the translator).
   - **`TOTK_VK_RECORDER`:** off by default on AMD; measure on Turnip.
   - **`TOTK_REPLAY_ASYNC`:** measure again, since the handheld has more, slower cores. Measured on the Odin 3
     (2026-11-06): no change in frame rate ([arm64-performance.md](arm64-performance.md)).
5. **Window and input.**
   - SDL3 runs on Wayland or X11 under the handheld's compositor (gamescope, Sway, KDE).
   - The built-in controller goes through SDL3's gamepad API.
   - Output is 1920x1080, or the panel's size, with `render_scale` and FSR 1 (`vk_fsr.c`).
6. **Write watch.** Confirm that `totk.log` does not contain `platform: no asynchronous userfaultfd write
   protection`. If a device lacks the feature, shadow sync needs an mprotect plus write-fault fallback, using the ESR
   path from Phase 2.

**Exit.** The game boots from the copied folder, plays through the Great Sky Island tutorial and saves. `totk.log`
shows no validation errors with the layers on.

## Phase 4: performance on ARM

Measure first: `TOTK_CORE_DIAG`, the F10 overlay, and perf or simpleperf on the device. The FP/SIMD, register-local
and code-level items below are planned step by step in [arm64-guest-code.md](arm64-guest-code.md). bbport's
`odin_profile_remote.sh` is a template for collecting the data. The setup's Steam entry profiles a whole session
with `TOTK_SYSMON=1` (`tools/sysmon_launch.sh`, `tools/odin_sysmon.sh`, `tools/sysmon_report.py`);
[arm64-performance.md](arm64-performance.md) holds the measurements on the Odin 3 and the suspects still to check. Then
work through these:

- **FP/SIMD.** The portable lowering emulates ARM semantics with extra NaN and FZ work that the hardware does for
  free. There are two options; measure and pick one.
  - **A NEON backend of `runtime/simd.h` for aarch64.** With the host FPCR equal to the guest's (FZ, DN, RMode), each
    helper becomes the NEON instruction itself (`vaddq_f32`, `vfmaq_f32`, `vrecpeq_f32`, ...). The result is
    bit-exact by hardware and still visible to the optimizer. `tests/diff/simd.py` is the gate.
  - **The lifter's `simd='asm'` passthrough** (from LAS Recomp; `recomp/lifter.py` `emit_asm_batch`). It is bit-exact
    by construction, but `recomp/gen.py` rejects it today. Each batch goes through `c->v[]` in memory and interrupts
    the register locals (`recomp/regs.py`, `recomp/flags.py`), which must be spilled around it.
- **Register locals.** On aarch64, raise `recomp/regs.py` `LOCALS` from 6 toward 12-16 (`gen.py --reg-locals`).
  Check correctness with `tests/diff/func.py` and the gain with frame times.
- **Code level.** Try `-mcpu=oryon-1` builds, and `-O2` for the hottest chunks (from `tools/prof_report.py`) if the
  compile time allows.
- **Zero-copy guest memory on unified memory.** Shadow storage copies the CPU-written pages at each submit and
  doubles about 1.1 GB of memory. On the handheld the GPU reads the same RAM, so there are three ways to avoid the
  copy:
  - map Vulkan memory at the guest's addresses (`VK_EXT_map_memory_placed`);
  - map dma-buf chunks in place, as bbport's `docs/unified_memory.md` describes for Turnip;
  - import guest memory directly, if Turnip has `VK_EXT_external_memory_host`.

  The memory types must be HOST_CACHED: guest code does atomics on pool memory, and ARM does not guarantee atomics on
  non-cacheable mappings.
- **Cores.** The 8 Elite has two prime cores and six performance cores, and no little cores. The 8 Gen 2 has a 1+4+3
  layout, with three Cortex-A510 little cores.
  - Pin the host threads of the three virtual cores to big cores.
  - Keep the shader-compile and recorder threads off those cores.
  - First step: `TOTK_PIN_RENDER` (`runtime/perf.c` `perf_pin_frame`) keeps the thread that replays, and with `2` the
    recorder, on the fastest cores. On by default on ARM64 Linux since the Odin 3 runs (`docs/arm64-performance.md`,
    "Defaults on ARM64 Linux").
- **Spin waits.** Compare `yield`, `isb` and `wfe` in `rt_spin_gate` and `plat_pause`.
- **Asynchronous preemption.** Finish the stub from Phase 2, step 8, if it is not done yet.

## Phase 5: aarch64 setup AppImage (days)

`TotK-Setup-aarch64.AppImage` is the x86-64 Linux release (`tools/make_release_linux.py`, `TotK-Setup-x86_64.AppImage`,
which the upstream merge of 2026-11-05 brought in) built for aarch64. The player starts it on the handheld and picks
the base game, the update and the keys. It builds the game into a folder they choose (`totk` with `libSDL3.so.0`
beside it) and prepares the shaders: no Docker, no root, no Mac. Like the x86-64 release it holds a toolchain and the
setup, never game code, so it can be shared.

**State (2026-11-05): implemented as below, not yet built.** `./build_docker.sh setup` is the next step on the Mac,
then a full setup run on the handheld. Checked so far: the host check on aarch64 hardware (an Apple silicon VM: levels
oryon and a64, a64 preferred, as it has no Oryon cores), the installer's tests with the new level choice
(`tests/installer_test.py` `CpuLevelTest`) and its window test, the Nix expressions by evaluation, and the aarch64
runtime library compiled against `write_runtime_only`'s `layout.h` (no game files).

- **`tools/make_release_linux.py --arch aarch64`.** x86_64 stays the default and keeps its current output.
  - **Pins per architecture.** All of these were published on 2026-11-05:
    - LLVM 23.1.4 `Linux-ARM64`;
    - python-build-standalone 3.14.10 `aarch64-unknown-linux-gnu` (20261026);
    - `appimagetool-aarch64` 1.9.1;
    - `ninja-linux-aarch64` 1.13.2.

    The type-2 `runtime-aarch64` is already pinned in `tools/nix/default.nix`.
  - **Sysroot.** The Ubuntu 22.04 arm64 one from Phase 1, in place of sniper (x86-64 only). SDL3, static Opus,
    libvpx and zstd, the Vulkan headers and VMA come from the Nix `deps` output that `build_docker.sh` already builds,
    so `build_deps` is skipped.
  - **Paths.** The target's triple replaces the hard-coded `x86_64-linux-gnu` in `used_sysroot_files`,
    `build_tk_xft` and the SDL3 copy. The dynamic loader is in `/lib`, not `/lib64`.
  - **Runtime libraries.** `libtotkrt.a` (a64, the default level) and `libtotkrt-oryon.a`
    (`build_game.py --make-runtime-lib --cpu`), named as x86-64's `libtotkrt.a` (v3) and `libtotkrt-v2.a`.
  - **Checks.** `check_glibc` gets a 2.35 floor, the sysroot's.
  - **Licences per architecture.** Ubuntu 22.04's libvpx7, zstd and the GCC that libstdc++ comes from; SDL3's and
    the Vulkan headers' own licence files, which the Nix `deps` output now carries in `share/licenses`.
- **Sysroot packages.** `libxft-dev`, `libfontconfig-dev` and `libfreetype-dev` join the roots of
  `tools/nix/sysroot_lock.py`. They are for Tk rebuilt with Xft, so Russian text uses system fonts, as on x86-64.
  `build_docker.sh` writes the lock anew whenever its roots differ from the script's (`sysroot_lock.py --check`); the
  new lock then needs a commit.
- **`installer/hostcheck.c`.** It includes `<cpuid.h>` unconditionally, so it does not compile on aarch64.
  - An aarch64 branch reports the code levels the CPU runs from the HWCAP bits `runtime/main.c` `check_cpu` tests:
    `a64` (LSE atomics, RCpc, dot product) and `oryon` (the Armv8.6 features on top). It prefers oryon only when
    `/proc/cpuinfo` names Qualcomm's Oryon cores (implementer 0x51, part 0x001 or 0x002).
  - It also reports the page size (the check stops on anything but 4 KiB) and whether userfaultfd's asynchronous
    write protection works. A test reservation of the guest's address window was left out: its addresses belong to
    `recomp/layout.py`, and the game reports a taken window itself.
  - New message ids in `installer/text/{en,ru,pt-BR,zh-Hans}.json`: `check.cpu_too_old_arm`, `check.cpu_level_unknown`,
    `check.cpu_level_unsupported`, `check.page_size`.
- **`installer/steps.py` and `installer/layout.py`.** `Check.cpu_level` takes the host check's preferred level, else
  "CPU too old". `Layout.runtime_lib_for` finds a level's library by name, in place of `runtime_lib_v2`. The glibc
  floor is 2.35 on aarch64.
- **CPU override.** `TOTK_CPU=oryon` (or `a64`) when starting the AppImage, or `--cpu` in the command-line setup, as
  `build_docker.sh` takes it. A level the CPU cannot run stops the check with a message, rather than building a game
  that dies of an illegal instruction.
- **Write tracking.** The kernel version warning (`warn.kernel_old`, below 6.7, now gone from the catalogs) became the
  host check's test of userfaultfd's asynchronous write protection, noted in the setup's log only. Without the feature
  the game tracks writes per 2 MiB region (`runtime/memory.c`), as on the handheld's 7.2.6 kernel built without
  `CONFIG_USERFAULTFD`.
- **`./build_docker.sh setup`.**
  - It takes no game files. The runtime libraries are compiled against a `layout.h` with only the module set
    (`recomp/layout.py` `write_runtime_only`), the part of the generated layout the runtime reads; only the check
    below needs a lifted version, which an earlier `game` run leaves in the build volumes, and is skipped without one.
  - It runs `make_release_linux.py --arch aarch64 --sysroot ... --deps ... --no-gpu --appimage` under Xvfb (with
    DejaVu fonts and the X libraries for nix-ld, from `shell.nix`) and writes `TotK-Setup-aarch64.AppImage` to
    `TOTK_OUT`. The container has no GPU, so the setup's own check-only pass stops at its Vulkan check (`--no-gpu`).
  - Then, after a game run, it runs `--prove` for the default level (a64), which builds and links the game with the
    bundle's own tools only, and runs the self-test and the kernel probe through the sysroot's loader (`--run-with`).
    The oryon library is built but not linked into a game there; the GPU probes run on the handheld.
- **GPU driver.** The game uses the device's Mesa, as on x86-64. Stock Turnip (Mesa 26.2.4)
  runs the game on the handheld.
- **On the handheld.**
  - The AppImage mounts with FUSE, else it runs with `--appimage-extract-and-run`.
  - The Tk window needs X11 (XWayland under a Wayland session).
  - It is used by touch until gamepad navigation (below) lands. `--cli` as the AppImage's first argument starts the
    command-line setup (`python -m installer`) instead.
- **Resources.**
  - The compile takes about 3 minutes on 24 x86 cores; expect 10-20 minutes on eight phone cores, with JOBS sized by
    free RAM.
  - The setup checks free space before it starts; the extraction is the peak (about 55 GB in the Docker build).
  - The installer resumes after a failure and recompiles only the chunks that changed.
- **Risks to check first.**
  - Python 3.14 aarch64 wheels for every pin in `tools/requirements-installer.txt`: the bundle installs binaries
    only.
  - XWayland in the handheld's session.
  - The window's size and Tk's scaling on the handheld's screen.
- **Decisions.**
  - Both code levels ship, so the setup also serves Snapdragon 8 Gen 2 and 8 Gen 3 handhelds.
  - Touch first; gamepad navigation next.
  - Written to go upstream (tests, four languages), since it holds no game code.

**Exit.** On the handheld, the AppImage builds the game from the NSPs and the keys, prepares the shaders, and the game
plays.

### Games folder, Steam and the code level box (done 2026-11-06, not yet run on the device)

The setup AppImage installs into a games folder, `~/.local/share/totk-recomp` by default: one install folder per update
(`v121/`, `v143/`) and one shared `profile/`, which every install's `totk.ini` points at (`profile_dir = ../profile`).
That is where the game AppImage of earlier builds kept its data and saves, so an install there takes them over. With
game files the setup identifies the update first and installs into its folder; without them it continues or rebuilds
the highest version there. Once a version is installed, "Add to Steam" asks the running Steam client to add the
highest one (`steam://addnonsteamgame/`, as SteamOS's "Add to Steam" does), through a script named
`TotK Recomp <version>` beside `totk`. The window also gets a code level box (Automatic, a64, oryon; `TOTK_CPU` preselects one). Details:
`docs/installer.md`, "Games folder and Steam (Linux)"; code in `installer/games.py`, `cli.py` (`--games`,
`--add-to-steam`), `steps.py`, `gui.py` and `guilogic.py`; tests in `tests/installer_test.py` `GamesFolderTest` and
`tests/installer_gui_test.py`.

### Two outputs, two modes (done 2026-11-06)

`build_docker.sh` makes two things for a handheld, one mode each (`docs/building.md`, "ARM64 Linux and the Docker
build"):

- **`game`**, the default: the portable folder, RomFS included, built on the computer from the game files.
- **`setup`**, the setup AppImage: no game files, ever. It is what players get: it builds the game on the handheld and
  adds it to Steam (above).

Between 2026-11-04 and 2026-11-06 there was a third way: `appimage` packed the game's code read-only as
`TotK-Recomp-<version>-<arch>.AppImage` (`tools/package_appimage.py`), with `TOTK_DATA_DIR` pointing at
`~/.local/share/totk-recomp/<version>`, and `romfs` wrote its RomFS beside it. The games folder and "Add to Steam"
replaced both, so the two modes, the packer and the pinned AppImage runtime in `tools/nix/default.nix` are gone. The
runtime keeps `TOTK_DATA_DIR`, and the games folder takes over that AppImage's data folders (above).

### Gamepad navigation in the setup window

On the handheld, the setup window may be the only thing on screen, in a game-mode session with no mouse or keyboard.
The same goes for the mods rebuild window (`installer/gui.py --rebuild`), which `totk` opens when an enabled mod needs
a rebuild. Today both windows take only mouse and keyboard, and Tkinter cannot read a gamepad.

- **Input helper.** `bin/totk_pad` is a small C program built with the release against the bundled `libSDL3.so.0`.
  - It opens the gamepads and prints one line per event on stdout: the button or stick direction, pressed or
    released, and the button's label (`SDL_GetGamepadButtonLabel`).
  - Through SDL, the handheld's built-in controller and Steam Input's virtual pad behave the same.
  - `installer/gui.py` starts it, reads its output on a thread and passes the events to Tk with `after()`.
  - The window stops the helper before it starts the game, so only one of them holds the pad.
  - If the helper is missing or exits, the window still works with mouse and keyboard.
- **Navigation.** Focus moves between the window's controls by their position on screen, not by Tab order.

  | Input | Action |
  |---|---|
  | D-pad or left stick | Move focus. Held, it repeats. |
  | A | Press the focused button, open a list or tick a box. |
  | B | Close a list or a dialog, or go back. |
  | Start | Start the setup, or the rebuild. |
  | Right stick, LT, RT | Scroll the log. |

  A and B follow the button's label, so they stay correct on Nintendo layouts, where A is the east button. The focused
  control gets a clear focus ring (a ttk style), because the default focus is hard to see at a distance.
- **File and folder pickers.** `filedialog.askopenfilename` and `askdirectory` can't be driven with a pad, and with no
  keyboard you can't type a path.
  - **A picker inside the window.** It shows one list of folders and matching files (`.nsp`, `.xci`, `prod.keys`,
    firmware folders). A opens a folder and B goes up.
    - It starts from the home folder, removable media (`/run/media/*`, `/media/*`) and the SD card.
    - The install folder gets a default name inside the folder you pick, so no typing is needed.
  - **A scan for candidates.** It looks in the same places for files with the right names and sizes and lists them
    first, so a typical setup is A, A, A, Start.
- **Dialogs.** The `messagebox` confirmations become dialogs inside the window, whose buttons take focus.
- **Text.** The hint line ("A select · B back · Start begin") and the picker's strings go into
  `installer/text/{en,ru,pt-BR,zh-Hans}.json`.
- **Off switch.** Navigation is on whenever a gamepad is connected; `TOTK_SETUP_PAD=0` turns it off.
- **Tests.**
  - `--pad-script FILE` replays recorded events in place of the helper, so `tests/installer_gui_test.py` can drive a
    whole setup and the rebuild screen without hardware.
  - The window's `--selftest` also visits every control of every screen in every language.
- **Prior art and license.** bbport's launcher already does this (`launcher/bbport_launcher_gamepad.py`): an SDL
  helper prints the events, and focus moves by position.
  - That file is GPL-2.0-or-later and TotK is MIT. Following `AGENTS.md`, read it for behaviour and write the TotK
    version from scratch, unless every author of that file agrees to license it as MIT.
- **Not only ARM.** The same window ships in the x86-64 release, so this also helps the Steam Deck in game mode, and
  it can go upstream on its own.

**Exit.** On the handheld, a full setup and a mods rebuild can be done with only the built-in controller.

## Phase 6: CI and upstream

- **CI.** GitHub Actions on `ubuntu-24.04-arm` (hosted arm64 runners) runs `build_docker.sh test` on every pull
  request, next to an x86-64 job, so neither architecture breaks silently. CI uses no game files, as today
  (`content-guard.yml`).
- **Pull requests**, in this order, each one small and testable:
  1. arch plumbing and `check_arch.py`;
  2. FP control and the aarch64 differential tests;
  3. platform assembly;
  4. leaf files;
  5. the `volatile` audit;
  6. the Vulkan requirement change;
  7. the Docker + Nix build;
  8. asynchronous preemption;
  9. the installer and the release;
  10. gamepad navigation in the setup window. It doesn't depend on the ARM work, so it can go upstream at any time.

  None of them changes the x86-64 Windows or Linux builds or their tests.
- **House rules.** The rules in `AGENTS.md` still apply:
  - generated C is never edited by hand;
  - every lifter change passes the differential tests;
  - `tools/check_content.py --staged` runs before every commit.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Turnip on the Adreno 830 hangs or loses the device on TotK's shaders | high | bbport's Mesa patches; Adreno 740 / 750 devices, where Turnip is more mature; Mesa reports with `TOTK_DUMP` / `TOTK_FLIGHT` captures |
| Host-side races that x86's TSO ordering hid | medium | the Phase 2 audit; soak runs on the device |
| A kernel without arm64 userfaultfd write protection (before 6.10, or built without it), or with 39-bit VA | medium, per device | Phase 0 checks; write tracking per 2 MiB region (`runtime/memory.c`, done); clear stop messages |
| Shadow copies cost too much LPDDR bandwidth | medium | Phase 4 zero-copy |
| FP differences in the C lowering on a new host | low | the full-encoding-space Unicorn test runs on aarch64 |
| Docker Desktop disk or RAM limits on the Mac | low | the work volume; JOBS from RAM; warnings |

## Later

- **Native execution of the guest code**, as MacNativeGames, nxnative and d2r-mobile do on ARM. It removes the lift
  and compile step and gives the fastest CPU path. But it needs a new CPU half for everything built on lifting:
  patches, cheats, spin gates and preemption. Revisit once the lifted build plays.
- **16 KB pages** (Asahi, Raspberry Pi 5): guest permissions per 4 KB page in software.
- **Mali / PanVK:** geometry shader emulation and a BCn decoder.
- **Windows on ARM:** the Win32 platform layer with ARM64 asm.
