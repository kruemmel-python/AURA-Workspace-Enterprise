param([switch]$WithServices, [switch]$SmokeTest)

$ErrorActionPreference = 'Stop'
$toolchain = 'H:\KR-Compiler\KR-Compiler-Windows-x64-v4.3.1\toolchain'
$krCompilerRoot = 'H:\KR-Compiler\KR-Compiler-Windows-x64-v4.3.1'
$output = Join-Path $PSScriptRoot 'KR_BUILD\windows-x64'
$cacheRoot = Join-Path $PSScriptRoot '.cache'
$tempRoot = Join-Path $cacheRoot 'temp'
$goCache = Join-Path $cacheRoot 'go-build'
$cargoHome = Join-Path $cacheRoot 'cargo-home'
$cargoTarget = Join-Path $cacheRoot 'cargo-target'
$testOutput = Join-Path $cacheRoot 'test-bin'
New-Item -ItemType Directory -Force -Path $output, $tempRoot, $goCache, $cargoHome, $cargoTarget, $testOutput | Out-Null

# Keep the release directory unambiguous: stale fallback executables and the
# C header emitted by Go's c-shared build are not runtime deliverables.
# Only exact, build-owned artifact names are removed here.
Get-ChildItem -LiteralPath $output -File -Filter 'AURA-Workspace-Enterprise-*.exe' -ErrorAction SilentlyContinue | Remove-Item -Force
Remove-Item -LiteralPath (Join-Path $output 'AURA-Workspace-Enterprise-buildcheck.exe'), (Join-Path $output 'enterprise_database_tests.exe'), (Join-Path $output 'aura_services.h') -Force -ErrorAction SilentlyContinue

# The project must never consume space on C:.  These variables cover Go/cgo,
# Rust/Cargo and child compiler temporary files.
$env:TMP = $tempRoot
$env:TEMP = $tempRoot
$env:TMPDIR = $tempRoot
$env:GOTMPDIR = $tempRoot
$env:GOCACHE = $goCache
$env:CARGO_HOME = $cargoHome
$env:CARGO_TARGET_DIR = $cargoTarget
$env:Path = "$toolchain\bin;$env:Path"
$env:CC = "$toolchain\bin\gcc.exe"

# Available in both Windows PowerShell 5.1 installations with reduced module
# sets and PowerShell 7.  Do not depend on Get-FileHash for release packaging.
function Get-AuraSha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($stream)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '')
    }
    finally { $stream.Dispose() }
}

# Execute the installed KR compiler pipeline non-interactively on every
# product build. Its generated verification output stays inside this workspace.
$krJar = Join-Path $krCompilerRoot 'dist\KR-Compiler-IDE.jar'
$krJava = Join-Path $krCompilerRoot 'runtime\jdk\bin\java.exe'
$krJavac = Join-Path $krCompilerRoot 'runtime\jdk\bin\javac.exe'
$krVerifierOutput = Join-Path $cacheRoot 'kr-verifier'
if (!(Test-Path -LiteralPath $krJar) -or !(Test-Path -LiteralPath $krJava) -or !(Test-Path -LiteralPath $krJavac)) { throw 'KR-Compiler-Pipeline ist nicht vollständig installiert.' }
New-Item -ItemType Directory -Force -Path $krVerifierOutput | Out-Null
& $krJavac -cp $krJar -d $krVerifierOutput (Join-Path $PSScriptRoot 'tools\KrPipelineVerifier.java')
if ($LASTEXITCODE -ne 0) { throw 'KR-Pipeline-Verifizierer konnte nicht kompiliert werden.' }
& $krJava -cp "$krVerifierOutput;$krJar" KrPipelineVerifier $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'KR-Pipeline-Validierung fehlgeschlagen.' }

& "$toolchain\rust\bin\cargo.exe" test --manifest-path "$PSScriptRoot\rules\Cargo.toml"
if ($LASTEXITCODE -ne 0) { throw 'Rust-Regeltests fehlgeschlagen.' }

# Build the static Rust rule engine as a production dependency of the native
# client.  The UI therefore cannot bypass capacity/interval integrity rules.
& "$toolchain\rust\bin\cargo.exe" build --release --manifest-path "$PSScriptRoot\rules\Cargo.toml"
if ($LASTEXITCODE -ne 0) { throw 'Rust-Regelwerk konnte nicht gebaut werden.' }

& "$toolchain\bin\g++.exe" -std=c++20 "$PSScriptRoot\core\enterprise_database.cpp" "$PSScriptRoot\tests\enterprise_database_tests.cpp" -I "$toolchain\include" -L "$toolchain\lib" -lsqlite3 -lbcrypt -o "$testOutput\enterprise_database_tests.exe"
if ($LASTEXITCODE -ne 0) { throw 'Datenbanktests konnten nicht kompiliert werden.' }
& "$testOutput\enterprise_database_tests.exe"
if ($LASTEXITCODE -ne 0) { throw 'Datenbanktests fehlgeschlagen.' }

if ($WithServices) {
    Push-Location "$PSScriptRoot\services"
    & "$toolchain\go\bin\go.exe" test ./...
    Pop-Location
    if ($LASTEXITCODE -ne 0) { throw 'Go-Service-Tests fehlgeschlagen.' }
    Push-Location "$PSScriptRoot\services"
    & "$toolchain\go\bin\go.exe" build -buildmode=c-shared -o "$output\aura_services.dll" .
    Pop-Location
    if ($LASTEXITCODE -ne 0) { throw 'Go-Service-Build fehlgeschlagen.' }
    Push-Location "$PSScriptRoot\services"
    & "$toolchain\go\bin\go.exe" build -o "$output\aura-sync-server.exe" .
    Pop-Location
    if ($LASTEXITCODE -ne 0) { throw 'Go-Synchronisationsserver konnte nicht gebaut werden.' }
}
Remove-Item -LiteralPath (Join-Path $output 'aura_services.h') -Force -ErrorAction SilentlyContinue

$client = Join-Path $output 'AURA-Workspace-Enterprise.exe'
$rustStaticLibrary = Join-Path $cargoTarget 'release\libaura_rules.a'
$rustDynamicLibrary = Join-Path $cargoTarget 'release\aura_rules.dll'
if (!(Test-Path -LiteralPath $rustStaticLibrary)) { throw "Statische Rust-Bibliothek fehlt: $rustStaticLibrary" }
if (!(Test-Path -LiteralPath $rustDynamicLibrary)) { throw "Rust-Laufzeitbibliothek fehlt: $rustDynamicLibrary" }
# Pass the archive by full path.  `-laura_rules` can prefer the generated
# import library (`libaura_rules.dll.a`) and would create an accidental DLL
# dependency in the product executable.
& "$toolchain\bin\g++.exe" -std=c++20 -mwindows "$PSScriptRoot\aura_workspace.cpp" "$PSScriptRoot\core\enterprise_database.cpp" -I "$PSScriptRoot" -I "$toolchain\include" "$rustStaticLibrary" -L "$toolchain\lib" -lsqlite3 -lgdi32 -luser32 -lcomdlg32 -lshell32 -lws2_32 -lbcrypt -ladvapi32 -luserenv -lntdll -o $client
if ($LASTEXITCODE -ne 0) {
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $client = Join-Path $output "AURA-Workspace-Enterprise-$timestamp.exe"
    & "$toolchain\bin\g++.exe" -std=c++20 -mwindows "$PSScriptRoot\aura_workspace.cpp" "$PSScriptRoot\core\enterprise_database.cpp" -I "$PSScriptRoot" -I "$toolchain\include" "$rustStaticLibrary" -L "$toolchain\lib" -lsqlite3 -lgdi32 -luser32 -lcomdlg32 -lshell32 -lws2_32 -lbcrypt -ladvapi32 -luserenv -lntdll -o $client
}
if ($LASTEXITCODE -ne 0) { throw 'C++20-Client-Build fehlgeschlagen.' }

# Ship every non-system runtime next to the executable.  The Rust DLL is
# included deliberately as a compatibility fallback; the client is linked to
# the static archive above and therefore does not require it at runtime.
Copy-Item -LiteralPath $rustDynamicLibrary -Destination (Join-Path $output 'aura_rules.dll') -Force
$runtimeDlls = @('libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll')
foreach ($runtimeDll in $runtimeDlls) {
    $source = Join-Path $toolchain "bin\$runtimeDll"
    if (!(Test-Path -LiteralPath $source)) { throw "Benötigte Runtime-DLL fehlt: $source" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $output $runtimeDll) -Force
}
$sqliteRuntime = $null
foreach ($pathEntry in ($env:PATH -split ';')) {
    if ([string]::IsNullOrWhiteSpace($pathEntry)) { continue }
    $candidate = Join-Path $pathEntry 'libsqlite3-0.dll'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $sqliteRuntime = $candidate; break }
}
if ($null -eq $sqliteRuntime) { throw 'Benötigte SQLite-Runtime libsqlite3-0.dll wurde nicht im Toolchain-Pfad gefunden.' }
Copy-Item -LiteralPath $sqliteRuntime -Destination (Join-Path $output 'libsqlite3-0.dll') -Force

if (!(Test-Path -LiteralPath (Join-Path $output 'aura_services.dll'))) {
    Write-Warning 'aura_services.dll fehlt: Build mit -WithServices erzeugt den Mehrbenutzer-Synchronisationsdienst.'
}
$packageNames = @((Split-Path -Leaf $client), 'aura_services.dll', 'aura-sync-server.exe', 'aura_rules.dll', 'libsqlite3-0.dll') + $runtimeDlls
$requiredPackageNames = @((Split-Path -Leaf $client), 'aura_services.dll', 'aura-sync-server.exe', 'aura_rules.dll', 'libsqlite3-0.dll') + $runtimeDlls
foreach ($requiredPackageName in $requiredPackageNames) {
    $requiredPackagePath = Join-Path $output $requiredPackageName
    if (!(Test-Path -LiteralPath $requiredPackagePath)) { throw "Unvollständiges Produktpaket: $requiredPackageName fehlt in $output" }
}
$packageFiles = Get-ChildItem -LiteralPath $output -File | Where-Object { $_.Name -in $packageNames } | Sort-Object Name
$manifest = [ordered]@{
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    client = (Split-Path -Leaf $client)
    files = @($packageFiles | ForEach-Object { [ordered]@{ name = $_.Name; bytes = $_.Length; sha256 = (Get-AuraSha256 $_.FullName) } })
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $output 'package-manifest.json') -Encoding utf8
if ($SmokeTest) {
    # A real process launch catches missing DLLs that file manifests cannot.
    # The process is stopped immediately; all application state remains under
    # KR_BUILD because the executable resolves the workspace from its package.
    $previousBootstrap = $env:AURA_BOOTSTRAP_PASSWORD
    $env:AURA_BOOTSTRAP_PASSWORD = 'Build-Smoke-Password-2026'
    $process = Start-Process -FilePath $client -WorkingDirectory $output -WindowStyle Hidden -PassThru
    try {
        Start-Sleep -Seconds 2
        if ($process.HasExited) { throw "Paket-Laufzeittest fehlgeschlagen (ExitCode $($process.ExitCode))." }
        Write-Host "Paket-Laufzeittest erfolgreich: PID $($process.Id)"
    }
    finally {
        if (!$process.HasExited) { Stop-Process -Id $process.Id -Force }
        $env:AURA_BOOTSTRAP_PASSWORD = $previousBootstrap
    }
}
Write-Host "Erstellt: $client"
