param([switch]$Linux, [switch]$Android)

$ErrorActionPreference = 'Stop'
if (!$Linux -and !$Android) { $Linux = $true; $Android = $true }
$root = $PSScriptRoot
$toolchain = 'H:\KR-Compiler\KR-Compiler-Windows-x64-v4.3.1\toolchain'
$cache = Join-Path $root '.cache'
$temp = Join-Path $cache 'temp'
$env:TMP = $temp; $env:TEMP = $temp; $env:TMPDIR = $temp
$env:GOCACHE = Join-Path $cache 'go-build'; $env:GOTMPDIR = $temp
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $cache 'zig-global'; $env:ZIG_LOCAL_CACHE_DIR = Join-Path $cache 'zig-local'
New-Item -ItemType Directory -Force -Path $temp,$env:GOCACHE,$env:ZIG_GLOBAL_CACHE_DIR,$env:ZIG_LOCAL_CACHE_DIR | Out-Null

if ($Linux) {
    $linuxOut = Join-Path $root 'KR_BUILD\linux-x64'
    New-Item -ItemType Directory -Force -Path $linuxOut | Out-Null
    Push-Location (Join-Path $root 'services')
    try { $env:GOOS='linux'; $env:GOARCH='amd64'; $env:CGO_ENABLED='0'; & "$toolchain\go\bin\go.exe" build -trimpath -o (Join-Path $linuxOut 'aura-workspace-enterprise') './cmd/aura-portable' }
    finally { Pop-Location; Remove-Item Env:GOOS -ErrorAction SilentlyContinue; Remove-Item Env:GOARCH -ErrorAction SilentlyContinue; Remove-Item Env:CGO_ENABLED -ErrorAction SilentlyContinue }
    if ($LASTEXITCODE -ne 0) { throw 'Linux-ELF konnte nicht gebaut werden.' }
    $magic = [System.IO.File]::ReadAllBytes((Join-Path $linuxOut 'aura-workspace-enterprise'))[0..3]
    if (($magic -join ',') -ne '127,69,76,70') { throw 'Linux-Artefakt ist kein ELF-Programm.' }
}

if ($Android) {
    $androidOut = Join-Path $root 'KR_BUILD\android-arm64'
    New-Item -ItemType Directory -Force -Path $androidOut | Out-Null
    $ndk = 'C:\Users\ralfk\AppData\Local\Android\Sdk\ndk\28.2.13676358'
    $clang = Join-Path $ndk 'toolchains\llvm\prebuilt\windows-x86_64\bin\aarch64-linux-android26-clang++.cmd'
    if (!(Test-Path -LiteralPath $clang)) { throw 'Android-NDK-Compiler fehlt.' }
    $userSo = Join-Path $androidOut 'libuser.so'
    & $clang -std=c++20 -shared -fPIC (Join-Path $root 'clients\android\aura_android_entry.cpp') -o $userSo
    if ($LASTEXITCODE -ne 0) { throw 'Android-ARM64-Bibliothek konnte nicht gebaut werden.' }
    $unsigned = Join-Path $androidOut 'aura-workspace-enterprise-unsigned.apk'
    $artifact = Join-Path $androidOut 'aura-workspace-enterprise.apk'
    $template = Join-Path $toolchain 'android\runner-template.apk'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $source = [System.IO.Compression.ZipFile]::OpenRead($template)
    $targetStream = [System.IO.File]::Create($unsigned)
    $target = [System.IO.Compression.ZipArchive]::new($targetStream, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($entry in $source.Entries) {
            if ($entry.FullName -like 'META-INF/*' -or $entry.FullName -eq 'lib/arm64-v8a/libuser.so') { continue }
            $out = $target.CreateEntry($entry.FullName, [System.IO.Compression.CompressionLevel]::Optimal)
            $inStream = $entry.Open(); $outStream = $out.Open()
            try { $inStream.CopyTo($outStream) } finally { $outStream.Dispose(); $inStream.Dispose() }
        }
        $native = $target.CreateEntry('lib/arm64-v8a/libuser.so', [System.IO.Compression.CompressionLevel]::NoCompression)
        $nativeStream = [System.IO.File]::OpenRead($userSo); $apkStream = $native.Open()
        try { $nativeStream.CopyTo($apkStream) } finally { $apkStream.Dispose(); $nativeStream.Dispose() }
    } finally { $target.Dispose(); $targetStream.Dispose(); $source.Dispose() }
    $security = Join-Path $root 'KR_BUILD\android-signing'; New-Item -ItemType Directory -Force -Path $security | Out-Null
    $keystore = Join-Path $security 'aura-enterprise.p12'; $passwordFile = Join-Path $security 'aura-enterprise.password'
    if (!(Test-Path -LiteralPath $keystore)) {
        $pass = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
        Set-Content -LiteralPath $passwordFile -Value @($pass,$pass) -Encoding ascii
        & 'H:\KR-Compiler\KR-Compiler-Windows-x64-v4.3.1\runtime\jdk\bin\keytool.exe' -genkeypair -noprompt -alias aura-enterprise -keyalg RSA -keysize 3072 -sigalg SHA256withRSA -validity 3650 -dname 'CN=AURA Workspace Enterprise, O=AURA, C=DE' -storetype PKCS12 -keystore $keystore -storepass $pass -keypass $pass
        if ($LASTEXITCODE -ne 0) { throw 'Android-Signaturschlüssel konnte nicht erstellt werden.' }
    }
    $signer = 'C:\Users\ralfk\AppData\Local\Android\Sdk\build-tools\36.0.0\apksigner.bat'
    & $signer sign --ks $keystore --ks-key-alias aura-enterprise --ks-pass "file:$passwordFile" --key-pass "file:$passwordFile" --out $artifact $unsigned
    if ($LASTEXITCODE -ne 0) { throw 'APK-Signatur fehlgeschlagen.' }
    & $signer verify --verbose $artifact
    if ($LASTEXITCODE -ne 0) { throw 'APK-Prüfung fehlgeschlagen.' }
    Remove-Item -LiteralPath $unsigned -Force
}
