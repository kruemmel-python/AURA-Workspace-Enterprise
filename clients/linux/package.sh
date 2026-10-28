#!/usr/bin/env bash
# Build a self-contained application layout for Linux x86-64. System GTK,
# Python and shared-library dependencies are intentionally not bundled.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
output=${1:-"$root/KR_BUILD/dist/aura-workspace-linux-x64.tar.gz"}
go_bin=${AURA_GO:-}
if [[ -z "$go_bin" ]]; then
  if [[ -x "$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain/go/bin/go" ]]; then
    go_bin="$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/toolchain/go/bin/go"
  elif command -v go >/dev/null 2>&1; then
    go_bin="go"
  fi
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'Dieses Paket muss unter Linux x86-64 gebaut werden.' >&2; exit 1
fi
if [[ -e "$output" || -L "$output" ]]; then
  echo 'Das gewählte Paket existiert bereits.' >&2; exit 1
fi
mkdir -p "$(dirname "$output")"
output=$(cd "$(dirname "$output")" && pwd)/$(basename "$output")
tmp=$(mktemp -d "$(dirname "$output")/.aura-package-XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
"$root/clients/linux/build.sh"
(cd "$root/services" && CGO_ENABLED=1 GOTOOLCHAIN=local "$go_bin" build -trimpath -o "$tmp/aura-sync-server" .)
bundle="$tmp/aura-workspace-linux-x64"
mkdir -p "$bundle/clients/linux" "$bundle/KR_BUILD/linux-x64" "$bundle/docs"
modules=(aura_gui.py local_store.py approval_sync.py attachment_sync.py booking_sync.py
  dependency_sync.py employee_sync.py facility_asset_sync.py leave_sync.py maintenance_sync.py
  organization_sync.py personal_presence_sync.py personal_comment_pull.py project_assignment_sync.py project_sync.py
  room_sync.py subtask_sync.py task_comment_sync.py task_sync.py ticket_comment_sync.py ticket_sync.py)
for module in "${modules[@]}"; do cp "$root/clients/linux/$module" "$bundle/clients/linux/"; done
cp "$root/clients/linux/start.sh" "$root/clients/linux/README.md" "$bundle/clients/linux/"
cp "$root/KR_BUILD/linux-x64/aura-workspace-enterprise" "$root/KR_BUILD/linux-x64/libaura_linux_local.so" "$bundle/KR_BUILD/linux-x64/"
cp "$tmp/aura-sync-server" "$bundle/KR_BUILD/linux-x64/aura-sync-server"
cp "$root/docs/sync-server.md" "$root/docs/administrator-handbook.md" "$bundle/docs/"
cat > "$bundle/START-LINUX.txt" <<'EOF'
AURA Workspace – Linux x86-64

Voraussetzungen: Ubuntu x86-64, Python 3, python3-gi, gir1.2-gtk-3.0,
SQLite 3 und OpenSSL 3. Das Paket enthält den Client und den Server;
es enthält keine Benutzerdaten, Serverkonfiguration oder Zugangsdaten.

Oberfläche starten: ./clients/linux/start.sh

Beispiel für einen lokalen Server:
  read -rs -p 'Server-Token: ' AURA_SYNC_TOKEN; echo; export AURA_SYNC_TOKEN
  export AURA_SYNC_JOURNAL="$HOME/.local/share/aura-workspace/server/events.ndjson"
  mkdir -p "$(dirname "$AURA_SYNC_JOURNAL")"
  export AURA_SYNC_LISTEN=127.0.0.1:8787
  ./KR_BUILD/linux-x64/aura-sync-server --server

Der Server benötigt eine eigene Konfiguration. Details und TLS-Hinweise
stehen in docs/sync-server.md und docs/administrator-handbook.md.
Funktionsumfang und Einschränkungen stehen in clients/linux/README.md.
EOF
tar -C "$tmp" -czf "$tmp/package.tar.gz" aura-workspace-linux-x64
# Hard-link publication prevents an existing output from being overwritten.
ln -- "$tmp/package.tar.gz" "$output"
echo "Linux-Paket erstellt: $output"
