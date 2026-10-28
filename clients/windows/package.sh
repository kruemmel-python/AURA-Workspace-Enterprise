#!/usr/bin/env bash
# Build a self-contained release package for Windows x64.
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
output=${1:-"$root/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-windows-x64.zip"}

if [[ -e "$output" || -L "$output" ]]; then
  echo "Das gewaehlte Paket existiert bereits: $output" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"
output=$(cd "$(dirname "$output")" && pwd)/$(basename "$output")
tmp=$(mktemp -d "$(dirname "$output")/.aura-win-pkg-XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT

"$root/clients/windows/build.sh"

bundle="$tmp/aura-workspace-windows-x64"
mkdir -p "$bundle" "$bundle/docs"

files=(
  "AURA-Workspace-Enterprise.exe"
  "aura_services.dll"
  "aura-sync-server.exe"
  "libc++.dll"
  "libunwind.dll"
  "libwinpthread-1.dll"
)

for file in "${files[@]}"; do
  if [[ ! -f "$root/KR_BUILD/windows-x64/$file" ]]; then
    echo "Fehlende Datei fuer Paketierung: $file" >&2
    exit 1
  fi
  cp "$root/KR_BUILD/windows-x64/$file" "$bundle/"
done

cp "$root/docs/sync-server.md" "$root/docs/administrator-handbook.md" "$bundle/docs/"

cat > "$bundle/START-WINDOWS.txt" <<'EOF'
AURA Workspace Enterprise – Windows x64

Ausfuehren:
  Doppelklick auf AURA-Workspace-Enterprise.exe

Synchronisationsserver lokal starten:
  set AURA_SYNC_TOKEN=<Ihr-Geheimtoken>
  set AURA_SYNC_LISTEN=127.0.0.1:8787
  aura-sync-server.exe --server

Hinweise zur Serverkonfiguration und TLS finden Sie in docs/sync-server.md
und docs/administrator-handbook.md.
EOF

# Erstelle package-manifest.json mit SHA256-Pruefsummen
manifest="$bundle/package-manifest.json"
{
  echo "{"
  echo '  "generatedAtUtc": "'$(date -u +"%Y-%m-%dT%H:%M:%SZ")'",'
  echo '  "client": "AURA-Workspace-Enterprise.exe",'
  echo '  "files": ['
  first=1
  for file in "${files[@]}"; do
    size=$(stat -c%s "$bundle/$file")
    sha=$(sha256sum "$bundle/$file" | awk '{print $1}')
    if [[ $first -eq 1 ]]; then
      first=0
    else
      echo ","
    fi
    printf '    {"name": "%s", "bytes": %d, "sha256": "%s"}' "$file" "$size" "$sha"
  done
  echo ""
  echo "  ]"
  echo "}"
} > "$manifest"

(cd "$tmp" && zip -q -r "$tmp/package.zip" aura-workspace-windows-x64)
mv "$tmp/package.zip" "$output"
echo "Windows-Paket erstellt: $output"
