# Build environment of build_docker.sh (Docker + Nix, as the Bloodborne port's): the tools that run in the container.
# What runs on the handheld is compiled against the target sysroot instead (tools/nix/default.nix), never against
# Nix's glibc.
#
#   nix-shell shell.nix --run 'python3.12 --version'
#
# The repository's own tools keep their versions: Python 3.12 for the lifter and the build (.venv) and 3.14 for the
# extraction and shader scripts (.venv-py314), their packages from tools/requirements*.txt through uv, clang 23,
# .NET 10 for the shader translator. Programs built for a usual Linux (Python wheels, the NativeAOT compiler NuGet
# brings) run through nix-ld: build_docker.sh links the container's /lib/ld-linux-*.so.* to TOTK_NIX_LD_STUB.
{ pkgs ? import <nixpkgs> { } }:
let
  lib = pkgs.lib;
  llvm = pkgs.llvmPackages_23 or pkgs.llvmPackages_latest;
  target = import ./tools/nix { inherit pkgs; };
  # What prebuilt Linux programs and Python's binary wheels load from the system besides glibc.
  fhsLibs = with pkgs; [ stdenv.cc.cc zlib icu openssl zstd ];
  # What the setup AppImage's Tk (rebuilt with Xft against the sysroot, tools/make_release_linux.py) loads when
  # build_docker.sh setup runs its window check under Xvfb
  guiLibs = with pkgs; [ libx11 libxext libxrender libxft fontconfig.lib freetype ];
in
pkgs.mkShell {
  packages = with pkgs; [
    llvm.lld llvm.llvm ninja gnumake cmake pkg-config git curl xz zstd file which
    python312 python314 uv dotnet-sdk_10
    spirv-tools glslang vulkan-tools dpkg patchelf
    squashfs-tools   # mksquashfs: tools/package_appimage.py (build_docker.sh appimage)
    xvfb-run         # a display for the setup window's check (build_docker.sh setup)
  ];
  # clang, clang++, lld and llvm-ar for the target (build_game.py --toolchain, build_xlate.py --llvm, the tests' CC)
  TOTK_LLVM = target.toolchain;
  # nix-ld: the dynamic linker that prebuilt programs name, and the libraries it gives them
  TOTK_NIX_LD_STUB = "${pkgs.nix-ld}/libexec/nix-ld";
  NIX_LD = pkgs.stdenv.cc.bintools.dynamicLinker;
  NIX_LD_LIBRARY_PATH = lib.makeLibraryPath (fhsLibs ++ guiLibs);
  # Fonts for the setup window's check (DejaVu: the Cyrillic glyph test of tools/make_release_linux.py check_bundle)
  TOTK_FONTS_CONF = pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; };
  # Python's binary wheels (numpy, unicorn, capstone, ...) under Nix's Python: their libstdc++ and zlib
  TOTK_WHEEL_LIBS = lib.makeLibraryPath fhsLibs;
  DOTNET_CLI_TELEMETRY_OPTOUT = "1";
  DOTNET_NOLOGO = "1";
}
