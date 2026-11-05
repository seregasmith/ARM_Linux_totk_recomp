# The target side of build_docker.sh: what the game is compiled and linked against. The host tools (Python, .NET,
# Ninja, ...) are in shell.nix at the root.
#
#   nix-build tools/nix -A toolchain                       clang, clang++, lld, llvm-ar: build_game.py --toolchain
#   nix-build tools/nix -A sysroot --argstr arch aarch64   Ubuntu 22.04 (glibc 2.35) from sysroot-arm64.lock.json
#   nix-build tools/nix -A deps --argstr arch aarch64      build_game.py --deps: SDL3, static Opus, libvpx and zstd,
#                                                          the Vulkan headers and VMA
#   nix-build tools/nix -A appimageRuntime --argstr arch aarch64
#                                                          AppImage's runtime for tools/package_appimage.py
#
# What runs on the handheld (totk, libSDL3.so.0, totk_xlate) is linked against the sysroot, not against Nix's glibc, so
# it runs on any distribution with glibc 2.35 or newer, outside Nix. The sysroot's packages are pinned by
# sysroot-<deb arch>.lock.json (tools/nix/sysroot_lock.py writes it; ./build_docker.sh lock runs that).
{ pkgs ? import <nixpkgs> { }, arch ? "aarch64" }:
let
  lib = pkgs.lib;
  llvm = pkgs.llvmPackages_23 or pkgs.llvmPackages_latest;
  debArch = { aarch64 = "arm64"; x86_64 = "amd64"; }.${arch} or (throw "tools/nix: arch is aarch64 or x86_64, not ${arch}");
  triple = "${arch}-linux-gnu";
  lockFile = ./. + "/sysroot-${debArch}.lock.json";
  lock =
    if builtins.pathExists lockFile then builtins.fromJSON (builtins.readFile lockFile)
    else throw "tools/nix/sysroot-${debArch}.lock.json is missing: ./build_docker.sh --arch ${arch} lock writes it";
  # Store names allow neither '~' nor '%', which Debian file names may hold.
  storeName = url: builtins.replaceStrings [ "~" "%" ] [ "_" "_" ] (baseNameOf url);
in
rec {
  inherit triple;

  # Nix's clang for the target, without Nix's cc-wrapper (clang-wrapper.sh), next to lld and the LLVM binutils.
  toolchain = pkgs.runCommand "totk-llvm-${llvm.clang-unwrapped.version}" { } ''
    mkdir -p $out/bin
    for t in clang clang++; do
      substitute ${./clang-wrapper.sh} $out/bin/$t --subst-var-by shell ${pkgs.runtimeShell} \
        --subst-var-by clang ${llvm.clang-unwrapped}
      chmod +x $out/bin/$t
    done
    for t in lld ld.lld llvm-ar llvm-ranlib llvm-nm llvm-objcopy llvm-strip llvm-readelf llvm-readobj llvm-objdump; do
      for d in ${llvm.lld}/bin ${llvm.llvm}/bin; do
        if [ -e $d/$t ]; then ln -s $d/$t $out/bin/$t; break; fi
      done
    done
  '';

  # Ubuntu 22.04's files unpacked as the packages ship them. Absolute symlinks are made relative, so they point into the
  # sysroot; the C library's linker scripts name /lib/... paths, which lld looks up inside the sysroot.
  sysroot = pkgs.runCommand "totk-sysroot-ubuntu2204-${debArch}"
    {
      nativeBuildInputs = [ pkgs.dpkg ];
      debs = map (p: pkgs.fetchurl { inherit (p) url sha256; name = storeName p.url; }) lock.packages;
    } ''
    mkdir -p $out
    for d in $debs; do dpkg-deb -x "$d" $out; done
    find $out -type l | while IFS= read -r l; do
      t=$(readlink "$l")
      case $t in
        /*) ln -sfn "$(realpath -m --relative-to="$(dirname "$l")" "$out$t")" "$l" ;;
      esac
    done
    chmod -R u+w $out
  '';

  # SDL3 (nixpkgs' release and sources) built against the sysroot; Ubuntu 22.04 has none. Its window, audio and input
  # back ends load their libraries at run time, so the library needs only glibc from the system.
  sdl3 = pkgs.stdenvNoCC.mkDerivation {
    pname = "totk-sdl3-${arch}";
    inherit (pkgs.sdl3) version src;
    nativeBuildInputs = [ pkgs.cmake pkgs.ninja pkgs.pkg-config pkgs.wayland-scanner toolchain ];
    dontFixup = true;   # a target library: no Nix patchelf or strip
    preConfigure = ''
      export PKG_CONFIG_SYSROOT_DIR=${sysroot}
      export PKG_CONFIG_LIBDIR=${sysroot}/usr/lib/${triple}/pkgconfig:${sysroot}/usr/share/pkgconfig
      unset PKG_CONFIG_PATH
    '';
    cmakeFlags = [
      "-DCMAKE_TOOLCHAIN_FILE=${pkgs.writeText "totk-${arch}.cmake" ''
        set(CMAKE_SYSTEM_NAME Linux)
        set(CMAKE_SYSTEM_PROCESSOR ${arch})
        set(CMAKE_SYSROOT ${sysroot})
        set(CMAKE_C_COMPILER ${toolchain}/bin/clang)
        set(CMAKE_C_COMPILER_TARGET ${triple})
        set(CMAKE_CXX_COMPILER ${toolchain}/bin/clang++)
        set(CMAKE_CXX_COMPILER_TARGET ${triple})
        set(CMAKE_AR ${toolchain}/bin/llvm-ar)
        set(CMAKE_RANLIB ${toolchain}/bin/llvm-ranlib)
        set(CMAKE_EXE_LINKER_FLAGS_INIT -fuse-ld=lld)
        set(CMAKE_SHARED_LINKER_FLAGS_INIT -fuse-ld=lld)
        set(CMAKE_FIND_ROOT_PATH ${sysroot})
        set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
        set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
        set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
        set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
      ''}"
      # stdenvNoCC passes empty CMAKE_AR/CMAKE_RANLIB cache entries; override them explicitly for the cross build.
      "-DCMAKE_AR=${toolchain}/bin/llvm-ar" "-DCMAKE_RANLIB=${toolchain}/bin/llvm-ranlib"
      "-DCMAKE_BUILD_TYPE=Release"
      "-DSDL_SHARED=ON" "-DSDL_STATIC=OFF" "-DSDL_TEST_LIBRARY=OFF" "-DSDL_TESTS=OFF" "-DSDL_EXAMPLES=OFF"
      "-DSDL_INSTALL_DOCS=OFF" "-DSDL_RPATH=OFF" "-DSDL_DEPS_SHARED=ON"
      "-DSDL_X11=ON" "-DSDL_WAYLAND=ON" "-DSDL_WAYLAND_LIBDECOR=ON" "-DSDL_KMSDRM=ON" "-DSDL_VULKAN=ON"
      "-DSDL_PIPEWIRE=ON" "-DSDL_PULSEAUDIO=ON" "-DSDL_ALSA=ON" "-DSDL_JACK=OFF" "-DSDL_SNDIO=OFF"
      "-DSDL_HIDAPI=ON" "-DSDL_HIDAPI_LIBUSB=OFF" "-DSDL_LIBUDEV=ON" "-DSDL_DBUS=ON" "-DSDL_IBUS=OFF"
    ];
  };

  # build_game.py --deps for a Linux target: include/ (Vulkan headers newer than Ubuntu 22.04's, VMA, SDL3) and lib/
  # (libSDL3.so*, which the game finds beside itself through its $ORIGIN rpath; Opus, libvpx and zstd static, so the
  # game needs no more than glibc, the system's Vulkan loader and that SDL3). share/licenses/ holds the licences of
  # SDL3 and the Vulkan headers for the setup AppImage (tools/make_release_linux.py collect_licenses); the Ubuntu
  # packages' are in the sysroot's usr/share/doc.
  deps = pkgs.runCommand "totk-deps-${arch}" { } ''
    mkdir -p $out/include/vma $out/lib $out/share/licenses
    cp -r ${pkgs.vulkan-headers}/include/vulkan ${pkgs.vulkan-headers}/include/vk_video $out/include/
    cp ${pkgs.vulkan-memory-allocator}/include/vk_mem_alloc.h $out/include/vma/
    cp -r ${sdl3}/include/SDL3 $out/include/
    cp -P ${sdl3}/lib/libSDL3.so* $out/lib/
    for a in libopus libvpx libzstd; do cp ${sysroot}/usr/lib/${triple}/$a.a $out/lib/; done
    cp ${sdl3.src}/LICENSE.txt $out/share/licenses/SDL3-LICENSE.txt
    cp ${pkgs.vulkan-headers.src}/LICENSE.md $out/share/licenses/vulkan-headers-LICENSE.md
  '';

  # The front of an AppImage (tools/package_appimage.py, build_docker.sh appimage): AppImage's type-2 runtime, release
  # 20251108 of github.com/AppImage/type2-runtime (MIT), pinned by hash. It is static, with libfuse3 and squashfuse
  # inside, so the device needs no FUSE library (only /dev/fuse and fusermount to mount; else
  # --appimage-extract-and-run), and it reads zstd squashfs.
  appimageRuntime = pkgs.fetchurl {
    url = "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-${arch}";
    hash = {
      aarch64 = "sha256-AMvfz5F8xsD/bTNH1Z4Moff0Wm3xpCig1tinhmTYdEQ=";
      x86_64 = "sha256-L8qLRDySUQ8Ug6iD9gBhrQm0a5eLJjHIB82HOkfsJg0=";
    }.${arch};
  };
}
