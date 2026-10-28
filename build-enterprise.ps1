param([switch]$SmokeTest)

# Single release entry point: all supported client artifacts are built from the
# same workspace and every compiler/cache temporary remains on H:.
$ErrorActionPreference = 'Stop'
$arguments = @{ WithServices = $true }
if ($SmokeTest) { $arguments.SmokeTest = $true }
& (Join-Path $PSScriptRoot 'build-windows.ps1') @arguments
if ($LASTEXITCODE -ne 0) { throw 'Windows-Enterprise-Build fehlgeschlagen.' }
& (Join-Path $PSScriptRoot 'build-platforms.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Linux-/Android-Enterprise-Build fehlgeschlagen.' }
& (Join-Path $PSScriptRoot 'build-android-ui.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Native Android-UI-Build fehlgeschlagen.' }
$releaseArtifacts = @(
    (Join-Path $PSScriptRoot 'KR_BUILD\windows-x64\AURA-Workspace-Enterprise.exe'),
    (Join-Path $PSScriptRoot 'KR_BUILD\linux-x64\aura-workspace-enterprise'),
    (Join-Path $PSScriptRoot 'KR_BUILD\android-arm64\aura-workspace-mobile.apk')
)
foreach ($releaseArtifact in $releaseArtifacts) { if (!(Test-Path -LiteralPath $releaseArtifact)) { throw "Release-Artefakt fehlt: $releaseArtifact" } }
$releaseManifest = [ordered]@{
    version = '2.4.5'
    generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    artifacts = @($releaseArtifacts | ForEach-Object { $item = Get-Item -LiteralPath $_; [ordered]@{ path = $item.FullName.Substring($PSScriptRoot.Length + 1).Replace('\','/'); bytes = $item.Length; sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() } })
}
$releaseManifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'KR_BUILD\release-manifest.json') -Encoding utf8
Write-Host "Enterprise-Paket vollständig erstellt unter $PSScriptRoot\KR_BUILD"
