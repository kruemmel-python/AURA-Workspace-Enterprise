# AURA Workspace Enterprise – Build- & Integrationsleitfaden mit der KR Compiler IDE

Dieser Leitfaden dokumentiert die vollständige Architektur, Werkzeugketten und Build-Schritte zur Erstellung von **AURA Workspace Enterprise** auf Basis der eigenentwickelten **KR Compiler IDE (v4.3.1)**. 

Das System zeichnet sich durch ein polyglottes Multi-Plattform-Design aus: Es vereint **C++20**, **Rust**, **Go** und **Java/Android** in einer gemeinsamen Codebasis und erzeugt native, releasefähige Auslieferungen für **Linux x64**, **Windows x64** und **Android ARM64/Mobile** – vollständig offline und ohne Abhängigkeit von externen Netzwerk-Downloads während des Builds.

---

## 1. Architektur & Philosophie der KR Compiler IDE

Die **KR Compiler IDE** ist ein autarkes Entwicklungswerkzeug und Kompilierungs-Ökosystem, das speziell für deterministische, hermetische Builds entwickelt wurde.

### Kernprinzipien:
1. **Hermetische Toolchain (Zero-Download):** Alle Compiler, Linker, Runtimes und Ziel-Sysroots befinden sich fest versioniert im Verzeichnis `toolchain/`. Es finden während der Build-Phase keine Downloads oder unkontrollierten Versionsaktualisierungen statt.
2. **Polyglotte Integration:** Native Koexistenz unterschiedlicher Programmiersprachen über standardisierte C-ABI-Grenzen:
   - **Rust (Edition 2021):** Strikte Geschäftsregel-, Buchungs-, Urlaubs- und Workflow-Validierung (`rules/src/lib.rs`).
   - **C++20:** Performante Datenbank-Engine (`core/enterprise_database.cpp`), nativer GUI-Kern (`aura_workspace.cpp`) und Linux-FFI-Bridge (`clients/linux/local_bridge.cpp`).
   - **Go (Local Toolchain, cgo):** Verteilte Synchronisationsdienste, SQLite-Journalisierung, REST-API und Multi-User-Server (`services/`).
   - **Java / Android SDK:** Universelle Touch-Anwendung für Mobilgeräte (`clients/android-ui/`).
3. **KR-Projektmanifest (`aura.krproject`):** Deklariert Modulquellen, Optimierungsstufen und Zielplattformen (`windows-x64`, `linux-x64`, `android-arm64`).
4. **KR-Pipeline-Verifikation (`tools/KrPipelineVerifier.java`):** Prüft vor dem Bau die Sprachgrenzen und semantische Integrität des Projekts über die KR-Frontend-Engine (`KR-Compiler-IDE.jar`).

---

## 2. Struktur der KR Compiler IDE Toolchain

Auf der Build-Workstation liegt die KR-Compiler-Suite unter:
```
$HOME/KR-Compiler/KR-Compiler-Linux-x64-v4.3.1/
```

### Komponenten-Übersicht:

| Pfad | Komponente & Version | Verwendung im Projekt |
| :--- | :--- | :--- |
| `toolchain/bin/clang.real`, `clang++.real` | Clang/LLVM 20.x, LLD | Nativer Linux-Build & Windows-Cross-Compiler |
| `toolchain/go/bin/go` | Go Compiler (`GOTOOLCHAIN=local`) | Synchronisations-Server & C-Shared DLL |
| `toolchain/rustup-home/toolchains/1.99.1-*/bin/rustc` | Rust Compiler 1.99.1 | Regelwerk für Linux, Windows & Android |
| `toolchain/targets/windows/` | MinGW-w64 Sysroot & CRT | Windows C++20 Header, Bibliotheken (`libbcrypt`, etc.) |
| `toolchain/android/ndk/` | Android NDK r28 | Native C++20 Bibliotheken für ARM64-Android |
| `toolchain/jdk/` | OpenJDK Runtime & Compiler | IDE-Laufzeit, AAPT2/D8-Orchestrierung |
| `dist/KR-Compiler-IDE.jar` | KR IDE & Pipeline-Engine | Frontend-Analyse und Projektverifikation |

---

## 3. Polyglotte Schnittstellen & Artefakte

AURA Workspace Enterprise nutzt eine klare Schichtenarchitektur:

```
+--------------------------------------------------------------------------+
|                            Präsentationsschicht                          |
|  +--------------------+  +----------------------+  +------------------+  |
|  | Windows C++20 GUI  |  | Linux GTK-3 (Python) |  | Android Touch    |  |
|  | Win32 GDI / Events |  | ctypes FFI / Bridge  |  | Java / Material  |  |
|  +--------------------+  +----------------------+  +------------------+  |
+--------------------------------------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------+
|                         Kern- & Regelschicht                             |
|  +--------------------------------+  +--------------------------------+  |
|  | SQLite Enterprise Database     |  | Rust Rule Engine               |  |
|  | (core/enterprise_database.cpp) |  | (rules/src/lib.rs -> .a)       |  |
|  | C++20 / Verschlüsselung / CRUD |  | Revisionssichere Validierung   |  |
|  +--------------------------------+  +--------------------------------+  |
+--------------------------------------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------+
|                      Synchronisations- & Netzwerkdienst                  |
|  +--------------------------------------------------------------------+  |
|  | Go Multi-User Sync Daemon & C-Shared Service Engine                |  |
|  | (services/ -> aura_services.dll / aura-sync-server)                |  |
|  | NDJSON-Ereignisstrom, Outbox-Cursor, REST-Administration & TLS     |  |
|  +--------------------------------------------------------------------+  |
+--------------------------------------------------------------------------+
```

---

## 4. Detaillierte Plattform-Builds

Alle Ausgaben, Caches und Zwischenschritte verbleiben streng innerhalb des Workspace-Verzeichnisses unter `KR_BUILD/`.

### 4.1 Linux x64 (Native Build)

Der Linux-Build kombiniert den C++20-Kern, die Rust-Regelbibliothek, die SQLite3-Anbindung und die GTK-3-Oberfläche:

1. **Rust-Regeln kompilieren:**
   ```bash
   rustc --edition=2021 --crate-type=staticlib -C relocation-model=pic -C opt-level=2 \
     rules/src/lib.rs -o KR_BUILD/linux-native/libaura_rules.a
   ```
2. **C++ Kern & Shared Library (CMake):**
   ```bash
   cmake -S . -B KR_BUILD/linux-native -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
   cmake --build KR_BUILD/linux-native --target aura_linux_local --parallel
   ```
   Erzeugt `KR_BUILD/linux-x64/libaura_linux_local.so`.
3. **Go Synchronisationsserver & Portable Client:**
   ```bash
   (cd services && CGO_ENABLED=1 GOTOOLCHAIN=local "$AURA_GO" build -trimpath -o ../KR_BUILD/linux-x64/aura-sync-server .)
   (cd services && CGO_ENABLED=0 GOTOOLCHAIN=local "$AURA_GO" build -trimpath -o ../KR_BUILD/linux-x64/aura-workspace-enterprise ./cmd/aura-portable)
   ```
4. **Paketierung:**
   ```bash
   bash clients/linux/package.sh "$PWD/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-linux-x64.tar.gz"
   ```

---

### 4.2 Windows x64 (Cross-Compilation unter Linux)

Die KR Compiler IDE erlaubt die vollständige Cross-Kompilierung des Windows-Gesamtpakets direkt unter Linux:

1. **Rust-Regelbibliothek für Windows (Target `x86_64-pc-windows-gnu`):**
   ```bash
   RUSTUP_HOME="$toolchain/rustup-home" "$rustc_bin" \
     --target=x86_64-pc-windows-gnu --crate-type=staticlib --edition=2021 -C opt-level=2 \
     rules/src/lib.rs -o KR_BUILD/windows-cross-deps/libaura_rules.a
   ```
2. **Cross-Compiler-Wrapper für Go CGO (`cc`):**
   Go benötigt für `c-shared` und SQLite-Header einen passenden C-Cross-Compiler:
   ```bash
   exec "$toolchain/bin/clang.real" \
     --target=x86_64-w64-windows-gnu \
     --sysroot="$toolchain/targets/windows" \
     -fuse-ld=lld --rtlib=compiler-rt \
     -resource-dir="$deps/clang-resource" "$@"
   ```
3. **Go Windows DLL & Server:**
   ```bash
   (cd services && \
     GOOS=windows GOARCH=amd64 CGO_ENABLED=1 CC="$tmp_cc" \
     CGO_CFLAGS="-I$deps/sqlite-amalgamation-3540100" CGO_LDFLAGS="-L$deps" \
     GOTOOLCHAIN=local "$AURA_GO" build -buildmode=c-shared -o KR_BUILD/windows-x64/aura_services.dll .)

   (cd services && \
     GOOS=windows GOARCH=amd64 CGO_ENABLED=1 CC="$tmp_cc" \
     CGO_CFLAGS="-I$deps/sqlite-amalgamation-3540100" CGO_LDFLAGS="-L$deps" \
     GOTOOLCHAIN=local "$AURA_GO" build -o KR_BUILD/windows-x64/aura-sync-server.exe .)
   ```
4. **C++20 Windows GUI Client:**
   Kompiliert mit `clang++.real`, bindet statisches SQLite, die statische Rust-Regelbibliothek sowie Windows-Systembibliotheken ein:
   ```bash
   "$toolchain/bin/clang++.real" --target=x86_64-w64-windows-gnu \
     --sysroot="$toolchain/targets/windows" \
     -resource-dir="$deps/clang-resource" \
     -isystem "$toolchain/targets/windows/include/c++/v1" \
     -I "$deps/sqlite-amalgamation-3540100" -I "." -std=c++20 \
     -fuse-ld=lld --rtlib=compiler-rt -unwindlib=libunwind -municode -mwindows \
     aura_workspace.cpp core/enterprise_database.cpp \
     KR_BUILD/windows-cross-deps/libsqlite3.a \
     KR_BUILD/windows-cross-deps/libaura_rules.a \
     "$toolchain/targets/windows/x86_64-w64-mingw32/lib/libc++.dll.a" \
     "$toolchain/targets/windows/x86_64-w64-mingw32/lib/libunwind.dll.a" \
     -lbcrypt -luser32 -lgdi32 -lcomdlg32 -lshell32 \
     -o KR_BUILD/windows-x64/AURA-Workspace-Enterprise.exe
   ```
5. **Laufzeit-Bündelung & Paketierung:**
   DLLs (`libc++.dll`, `libunwind.dll`, `libwinpthread-1.dll`, `aura_services.dll`), Handbücher und `package-manifest.json` werden zu einem fertigen ZIP-Release archiviert:
   ```bash
   bash clients/windows/package.sh "$PWD/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-windows-x64.zip"
   ```

---

### 4.3 Android Mobile App (API 23–35)

Die Android-Anwendung bietet eine vollständige Touch-Oberfläche für Raum-, Urlaubs-, Ticket- und Aufgabenverwaltung:

1. **Signaturschlüssel (`aura-enterprise.p12`):**
   Wird bei Erstausführung über `keytool` im Arbeitsbereich erzeugt (`RSA 3072 Bit, SHA256withRSA`).
2. **Ressourcen-Kompilierung (AAPT2):**
   ```bash
   aapt2 compile --dir clients/android-ui/res -o KR_BUILD/android-ui-work/res.zip
   aapt2 link -I $platform --manifest clients/android-ui/AndroidManifest.xml \
     --min-sdk-version 23 --target-sdk-version 35 --auto-add-overlay \
     --java KR_BUILD/android-ui-work/generated -o KR_BUILD/android-ui-work/base.apk -R KR_BUILD/android-ui-work/res.zip
   ```
3. **Java-Quellcode-Kompilierung:**
   ```bash
   javac -source 8 -target 8 -classpath $platform -d KR_BUILD/android-ui-work/classes \
     KR_BUILD/android-ui-work/generated/com/aura/workspace/R.java \
     clients/android-ui/src/com/aura/workspace/MainActivity.java
   ```
4. **DEX-Bytecode-Generierung (D8):**
   ```bash
   d8 --lib $platform --output KR_BUILD/android-ui-work/dex $(find KR_BUILD/android-ui-work/classes -name "*.class")
   (cd KR_BUILD/android-ui-work/dex && jar uf KR_BUILD/android-ui-work/unsigned.apk classes.dex)
   ```
5. **APK-Signatur & Prüfung (`apksigner`):**
   ```bash
   apksigner sign --ks KR_BUILD/android-signing/aura-enterprise.p12 \
     --ks-key-alias aura-enterprise --out KR_BUILD/android-arm64/aura-workspace-mobile.apk \
     KR_BUILD/android-ui-work/unsigned.apk
   apksigner verify KR_BUILD/android-arm64/aura-workspace-mobile.apk
   ```
6. **Paketierung:**
   ```bash
   bash clients/android/package.sh "$PWD/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-android.apk"
   ```

---

## 5. Einheitlicher Gesamtbuild (`build-enterprise.sh`)

Um alle drei Zielplattformen in einem Durchgang zu kompilieren, zu verifizieren und das offizielle Release-Manifest zu erzeugen:

```bash
./build-enterprise.sh
```

### Automatische Aktionen:
1. Prüft und setzt die KR-Compiler-Umgebungsvariablen (`AURA_GO`, `AURA_KR_TOOLCHAIN`, `ANDROID_HOME`).
2. Führt sequentiell die Builds für **Linux**, **Windows** und **Android** aus.
3. Berechnet kryptographische **SHA-256-Prüfsummen** aller Release-Binärdateien.
4. Generiert das standardisierte Integritätsmanifest `KR_BUILD/release-manifest.json`:
   ```json
   {
     "version": "2.4.5",
     "generatedAtUtc": "2026-10-28T12:20:52Z",
     "artifacts": [
       {"path": "KR_BUILD/windows-x64/AURA-Workspace-Enterprise.exe", "bytes": 3094528, "sha256": "..."},
       {"path": "KR_BUILD/linux-x64/aura-workspace-enterprise", "bytes": 10196564, "sha256": "..."},
       {"path": "KR_BUILD/android-arm64/aura-workspace-mobile.apk", "bytes": 29080, "sha256": "..."}
     ]
   }
   ```

---

## 6. Schnellanleitung für Entwickler

```bash
# 1. In den Workspace wechseln
cd ~/Aurora_Workspace/AURA-Workspace-Enterprise

# 2. Ausführungsrechte für Build- und Paketskripte sicherstellen
chmod +x build-enterprise.sh clients/*/build.sh clients/*/package.sh

# 3. Gesamt-Release bauen
./build-enterprise.sh

# 4. Einzelne Pakete nach KR_BUILD/dist/ schnüren:
bash clients/linux/package.sh   "$PWD/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-linux-x64.tar.gz"
bash clients/windows/package.sh "$PWD/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-windows-x64.zip"
bash clients/android/package.sh "$PWD/KR_BUILD/dist/AURA-Workspace-Enterprise-v2.4.5-android.apk"
```

---

## 7. Zusammenfassung der KR-Compiler-Vorteile im Projekt

- **Deterministische Builds:** Keine Versionsdrift durch externe Paketmanager (`npm`, `pip`, `apt`, `cargo update`), da alle Bibliotheken und Toolchain-Binaries im Workspace bzw. der KR-Toolchain fixiert sind.
- **Echte Cross-Plattform-Entwicklung:** Windows-Binärdateien und Android-Pakete können auf Linux-Entwicklungsrechnern ohne virtuelle Maschinen erzeugt werden.
- **Hohe Ausführungsgeschwindigkeit:** C++20 und Rust garantieren minimale Latenz und minimalen Speicherverbrauch im Vergleich zu Electron- oder Web-basierten Unternehmensanwendungen.
