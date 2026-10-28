#!/usr/bin/env bash
# Package Android Mobile Client APK
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
output=${1:-"$root/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-android.apk"}

if [[ -e "$output" || -L "$output" ]]; then
  echo "Das gewaehlte Paket existiert bereits: $output" >&2
  exit 1
fi

mkdir -p "$(dirname "$output")"

"$root/clients/android/build.sh"

apk="$root/KR_BUILD/android-arm64/aura-workspace-mobile.apk"
if [[ ! -f "$apk" ]]; then
  echo "Erwartetes APK nicht gefunden: $apk" >&2
  exit 1
fi

cp "$apk" "$output"
echo "Android-Paket erstellt: $output"
