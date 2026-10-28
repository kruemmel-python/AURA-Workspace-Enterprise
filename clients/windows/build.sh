#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
toolchain=${AURA_KR_TOOLCHAIN:-/home/ralf/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain}
deps="$root/KR_BUILD/windows-cross-deps"
out="$root/KR_BUILD/windows-x64"
win="$toolchain/targets/windows"

if [[ ! -d "$toolchain" ]]; then
  echo "KR-Compiler Toolchain nicht gefunden unter: $toolchain" >&2
  echo "Bitte Umgebungsvariable AURA_KR_TOOLCHAIN setzen." >&2
  exit 1
fi

go_bin=${AURA_GO:-"$toolchain/go/bin/go"}
if [[ ! -x "$go_bin" ]]; then
  if command -v go >/dev/null 2>&1; then
    go_bin=$(command -v go)
  else
    echo "Go-Compiler nicht gefunden: $go_bin" >&2
    exit 1
  fi
fi

# 1. Rust-Regelbibliothek verifizieren/kompilieren
rustup_home="$toolchain/rustup-home"
rustc_bin="$rustup_home/toolchains/1.99.1-x86_64-unknown-linux-gnu/bin/rustc"
if [[ ! -f "$deps/libaura_rules.a" || "$root/rules/src/lib.rs" -nt "$deps/libaura_rules.a" ]]; then
  echo "Kompiliere Rust-Regelwerk fuer Windows (x86_64-pc-windows-gnu)..."
  if [[ -x "$rustc_bin" ]]; then
    RUSTUP_HOME="$rustup_home" "$rustc_bin" --target=x86_64-pc-windows-gnu --crate-type=staticlib --edition=2021 -C opt-level=2 "$root/rules/src/lib.rs" -o "$deps/libaura_rules.a"
  else
    rustc --target=x86_64-pc-windows-gnu --crate-type=staticlib --edition=2021 -C opt-level=2 "$root/rules/src/lib.rs" -o "$deps/libaura_rules.a"
  fi
fi

# Erforderliche Windows-Abhaengigkeiten pruefen
for required in "$deps/libsqlite3.a" "$deps/sqlite-amalgamation-3540100/sqlite3.h" "$deps/clang-resource/lib/windows/libclang_rt.builtins-x86_64.a" "$deps/libc++.dll" "$deps/libunwind.dll" "$deps/libwinpthread-1.dll"; do
  if [[ ! -f "$required" ]]; then
    echo "Abhaengigkeit fehlt: $required" >&2
    exit 1
  fi
done

mkdir -p "$out"
tmp_cc="$out/.cc_cross"
printf '#!/usr/bin/env bash\nexec %q --target=x86_64-w64-windows-gnu --sysroot=%q -fuse-ld=lld --rtlib=compiler-rt -Qunused-arguments -resource-dir=%q "$@"\n' "$toolchain/bin/clang.real" "$win" "$deps/clang-resource" > "$tmp_cc"
chmod +x "$tmp_cc"

# 2. Go Synchronisationsdienst und Server kompilieren
echo "Kompiliere Go-Dienste fuer Windows..."
(
  cd "$root/services"
  GOOS=windows GOARCH=amd64 CGO_ENABLED=1 CC="$tmp_cc" \
    CGO_CFLAGS="-I$deps/sqlite-amalgamation-3540100" CGO_LDFLAGS="-L$deps" \
    GOTOOLCHAIN=local "$go_bin" build -buildmode=c-shared -o "$out/aura_services.dll" .
  GOOS=windows GOARCH=amd64 CGO_ENABLED=1 CC="$tmp_cc" \
    CGO_CFLAGS="-I$deps/sqlite-amalgamation-3540100" CGO_LDFLAGS="-L$deps" \
    GOTOOLCHAIN=local "$go_bin" build -o "$out/aura-sync-server.exe" .
)

# 3. Native C++20 Windows GUI-Anwendung kompilieren
echo "Kompiliere nativen C++20 Windows-Client..."
"$toolchain/bin/clang++.real" --target=x86_64-w64-windows-gnu --sysroot="$win" \
  -resource-dir="$deps/clang-resource" -isystem "$win/include/c++/v1" \
  -I "$deps/sqlite-amalgamation-3540100" -I "$root" -std=c++20 -fuse-ld=lld --rtlib=compiler-rt \
  -unwindlib=libunwind -Qunused-arguments -municode -mwindows \
  "$root/aura_workspace.cpp" "$root/core/enterprise_database.cpp" "$deps/libsqlite3.a" "$deps/libaura_rules.a" \
  "$win/x86_64-w64-mingw32/lib/libc++.dll.a" "$win/x86_64-w64-mingw32/lib/libunwind.dll.a" \
  -lbcrypt -luser32 -lgdi32 -lcomdlg32 -lshell32 -o "$out/AURA-Workspace-Enterprise.exe"

# 4. Laufzeit-DLLs bereitstellen
cp "$deps/libc++.dll" "$deps/libunwind.dll" "$deps/libwinpthread-1.dll" "$out/"
rm -f "$out/aura_services.h" "$tmp_cc"

echo "Windows-Client und Server gebaut in: $out"
