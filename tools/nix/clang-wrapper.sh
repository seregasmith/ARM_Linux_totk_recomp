#!@shell@
# clang and clang++ for target builds (tools/nix/default.nix toolchain: build_game.py --toolchain, build_xlate.py --llvm,
# the tests' CC). Nix's clang without Nix's cc-wrapper, which would add Nix's glibc and its dynamic linker: the sysroot
# (--sysroot) provides the C library instead. For a dynamic link it names the target's dynamic linker, which nixpkgs
# leaves out of clang's link command (its purity patch) and its cc-wrapper would otherwise add.
link=1 static=0 target=
for a in "$@"; do
    case $a in
        -c | -S | -E | -M | -MM | -fsyntax-only | --version | -dumpmachine | -dumpversion | -print-*) link=0 ;;
        -static | -shared | -r | -nostdlib) static=1 ;;
        --target=*) target=${a#--target=} ;;
    esac
done
case ${target:-$(uname -m)} in
    aarch64*) dl=/lib/ld-linux-aarch64.so.1 ;;
    x86_64*) dl=/lib64/ld-linux-x86-64.so.2 ;;
    *) dl= ;;
esac
tool=@clang@/bin/$(basename "$0")
if [ "$link" = 1 ] && [ "$static" = 0 ] && [ -n "$dl" ]; then
    exec "$tool" "$@" -Wl,--dynamic-linker="$dl"
fi
exec "$tool" "$@"
