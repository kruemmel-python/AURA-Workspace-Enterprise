#!/usr/bin/env bash
# Experimental cross-build for compatibility checks; not a Windows release.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
toolchain=${AURA_KR_TOOLCHAIN:-/home/ralf/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain}
deps="$root/KR_BUILD/windows-cross-deps"
out="$root/KR_BUILD/windows-cross-current"
win="$toolchain/targets/windows"
if [[ ! -f "$deps/libaura_rules.a" || "$root/rules/src/lib.rs" -nt "$deps/libaura_rules.a" ]]; then
    echo 'Die Windows-Rust-Regelbibliothek fehlt oder ist älter als der Quelltext.' >&2
    exit 1
fi
for required in "$deps/libsqlite3.a" "$deps/sqlite-amalgamation-3540100/sqlite3.h" "$deps/clang-resource/lib/windows/libclang_rt.builtins-x86_64.a" "$deps/libc++.dll" "$deps/libunwind.dll" "$deps/libwinpthread-1.dll"; do
    if [[ ! -f "$required" ]]; then echo "Abhängigkeit fehlt: $required" >&2; exit 1; fi
done
mkdir -p "$out"
printf '#!/usr/bin/env bash\nexec %q --target=x86_64-w64-windows-gnu --sysroot=%q -fuse-ld=lld --rtlib=compiler-rt -Qunused-arguments -resource-dir=%q "$@"\n' "$toolchain/bin/clang.real" "$win" "$deps/clang-resource" > "$out/cc"
chmod +x "$out/cc"
(
    cd "$root/services"
    GOOS=windows GOARCH=amd64 CGO_ENABLED=1 CC="$out/cc" \
      CGO_CFLAGS="-I$deps/sqlite-amalgamation-3540100" CGO_LDFLAGS="-L$deps" \
      GOTOOLCHAIN=local "$toolchain/go/bin/go" build -buildmode=c-shared -o "$out/aura_services.dll" .
)
"$toolchain/bin/clang++.real" --target=x86_64-w64-windows-gnu --sysroot="$win" \
    -resource-dir="$deps/clang-resource" -isystem "$win/include/c++/v1" \
    -I "$deps/sqlite-amalgamation-3540100" -std=c++20 -fuse-ld=lld --rtlib=compiler-rt \
    -unwindlib=libunwind -Qunused-arguments -municode -mwindows \
    "$root/aura_workspace.cpp" "$root/core/enterprise_database.cpp" "$deps/libsqlite3.a" "$deps/libaura_rules.a" \
    "$win/x86_64-w64-mingw32/lib/libc++.dll.a" "$win/x86_64-w64-mingw32/lib/libunwind.dll.a" \
    -lbcrypt -luser32 -lgdi32 -lcomdlg32 -lshell32 -o "$out/AURA-Workspace-Enterprise.exe"
cp "$deps/libc++.dll" "$deps/libunwind.dll" "$deps/libwinpthread-1.dll" "$out/"
echo "Experimenteller Windows-Testbuild: $out"
