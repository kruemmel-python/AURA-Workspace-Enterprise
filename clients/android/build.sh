#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)

# 1. Android SDK lokalisieren
sdk=""
for cand in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "/home/ralf/Android/Sdk" "$HOME/Android/Sdk"; do
  if [[ -n "$cand" && -d "$cand" ]]; then
    sdk="$cand"
    break
  fi
done

if [[ -z "$sdk" ]]; then
  echo "Android SDK nicht gefunden. Bitte ANDROID_HOME setzen." >&2
  exit 1
fi

# 2. Build-Tools und Plattform lokalisieren
bt=""
for cand in "$sdk/build-tools/36.0.0" "$sdk/build-tools/34.0.0" "$sdk/build-tools"/*; do
  if [[ -x "$cand/aapt2" && -x "$cand/d8" && -x "$cand/apksigner" ]]; then
    bt="$cand"
    break
  fi
done

if [[ -z "$bt" ]]; then
  echo "Android Build-Tools (aapt2, d8, apksigner) nicht gefunden in $sdk/build-tools" >&2
  exit 1
fi

platform=""
for cand in "$sdk/platforms/android-35/android.jar" "$sdk/platforms/android-34/android.jar" "$sdk/platforms"/*/android.jar; do
  if [[ -f "$cand" ]]; then
    platform="$cand"
    break
  fi
done

if [[ -z "$platform" ]]; then
  echo "android.jar nicht gefunden in $sdk/platforms" >&2
  exit 1
fi

# 3. Java-Tools pruefen
for tool in javac jar keytool; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Benoetigtes Java-Tool nicht gefunden: $tool" >&2
    exit 1
  fi
done

work="$root/KR_BUILD/android-ui-work"
out="$root/KR_BUILD/android-arm64"
source="$root/clients/android-ui"
sign="$root/KR_BUILD/android-signing"

mkdir -p "$sign" "$out"
rm -rf "$work"
mkdir -p "$work/classes" "$work/dex" "$work/generated"

# 4. Signaturschluessel erstellen, falls noch nicht vorhanden
keystore="$sign/aura-enterprise.p12"
passfile="$sign/aura-enterprise.password"
if [[ ! -f "$keystore" ]]; then
  echo "Erzeuge Android-Signaturschluessel..."
  pass=$(openssl rand -hex 32)
  printf "%s\n%s\n" "$pass" "$pass" > "$passfile"
  keytool -genkeypair -noprompt -alias aura-enterprise -keyalg RSA -keysize 3072 \
    -sigalg SHA256withRSA -validity 3650 -dname "CN=AURA Workspace Enterprise, O=AURA, C=DE" \
    -storetype PKCS12 -keystore "$keystore" \
    -storepass "$pass" -keypass "$pass"
fi

# 5. Ressourcen kompilieren & Basis-APK linken
echo "Kompiliere Android-Ressourcen..."
"$bt/aapt2" compile --dir "$source/res" -o "$work/res.zip"
"$bt/aapt2" link -I "$platform" --manifest "$source/AndroidManifest.xml" \
  --min-sdk-version 23 --target-sdk-version 35 --auto-add-overlay \
  --java "$work/generated" -o "$work/base.apk" -R "$work/res.zip"

# 6. Java Quellcode kompilieren
echo "Kompiliere Java-Quellen..."
javac -source 8 -target 8 -classpath "$platform" -d "$work/classes" \
  "$work/generated/com/aura/workspace/R.java" \
  "$source/src/com/aura/workspace/MainActivity.java"

# 7. Dex Bytecode erzeugen
echo "Erzeuge DEX..."
class_files=$(find "$work/classes" -type f -name "*.class")
"$bt/d8" --lib "$platform" --output "$work/dex" $class_files

cp "$work/base.apk" "$work/unsigned.apk"
(cd "$work/dex" && jar uf "$work/unsigned.apk" classes.dex)

# 8. Signieren & Verifizieren
echo "Signiere APK..."
pass=$(head -n 1 "$passfile")
"$bt/apksigner" sign --ks "$keystore" \
  --ks-key-alias aura-enterprise --ks-pass "pass:$pass" --key-pass "pass:$pass" \
  --out "$out/aura-workspace-mobile.apk" "$work/unsigned.apk"

"$bt/apksigner" verify "$out/aura-workspace-mobile.apk"

echo "Android APK erfolgreich erstellt: $out/aura-workspace-mobile.apk"
