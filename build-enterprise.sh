#!/usr/bin/env bash
# Build all enterprise deliverables: Linux, Windows, and Android.
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)

if [[ -z "${AURA_GO:-}" ]]; then
  if [[ -x "$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain/go/bin/go" ]]; then
    export AURA_GO="$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain/go/bin/go"
  elif command -v go >/dev/null 2>&1; then
    export AURA_GO=$(command -v go)
  fi
fi

echo "=== 1/3: Linux Client & Server ==="
"$root/clients/linux/build.sh"

echo "=== 2/3: Windows Client & Server ==="
"$root/clients/windows/build.sh"

echo "=== 3/3: Android Mobile App ==="
"$root/clients/android/build.sh"

release_artifacts=(
  "KR_BUILD/windows-x64/AURA-Workspace-Enterprise.exe"
  "KR_BUILD/linux-x64/aura-workspace-enterprise"
  "KR_BUILD/android-arm64/aura-workspace-mobile.apk"
)

for rel in "${release_artifacts[@]}"; do
  if [[ ! -f "$root/$rel" ]]; then
    echo "Release-Artefakt fehlt: $root/$rel" >&2
    exit 1
  fi
done

manifest="$root/KR_BUILD/release-manifest.json"
{
  echo "{"
  echo '  "version": "2.4.5",'
  echo '  "generatedAtUtc": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'",'
  echo '  "artifacts": ['
  first=1
  for rel in "${release_artifacts[@]}"; do
    size=$(stat -c%s "$root/$rel")
    sha=$(sha256sum "$root/$rel" | awk '{print $1}')
    if [[ $first -eq 1 ]]; then
      first=0
    else
      echo ","
    fi
    printf '    {"path": "%s", "bytes": %d, "sha256": "%s"}' "$rel" "$size" "$sha"
  done
  echo ""
  echo "  ]"
  echo "}"
} > "$manifest"

echo "Enterprise-Pakete vollstaendig erstellt. Manifest aktualisiert: $manifest"
