#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
go_bin=${AURA_GO:-}
if [[ -z "$go_bin" ]]; then
  if [[ -x "$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain/go/bin/go" ]]; then
    go_bin="$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain/go/bin/go"
  elif command -v go >/dev/null 2>&1; then
    go_bin="go"
  fi
fi
if [[ -z "$go_bin" ]] || ! command -v "$go_bin" >/dev/null 2>&1; then
  echo 'Go nicht gefunden. AURA_GO auf den Go-Compiler setzen.' >&2
  exit 1
fi
mkdir -p "$root/KR_BUILD/linux-x64"
(cd "$root/services" && CGO_ENABLED=0 GOTOOLCHAIN=local "$go_bin" build -trimpath -o "$root/KR_BUILD/linux-x64/aura-workspace-enterprise" ./cmd/aura-portable)
cmake -S "$root" -B "$root/KR_BUILD/linux-native" -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$root/KR_BUILD/linux-native" --target aura_linux_local --parallel
cp "$root/KR_BUILD/linux-native/libaura_linux_local.so" "$root/KR_BUILD/linux-x64/"
echo "Client gebaut. GUI starten: $root/clients/linux/start.sh"
