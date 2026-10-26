param()
$ErrorActionPreference='Stop'
$root=$PSScriptRoot;$sdk='C:\Users\ralfk\AppData\Local\Android\Sdk';$bt=Join-Path $sdk 'build-tools\36.0.0';$platform=Join-Path $sdk 'platforms\android-35\android.jar';$jdk='H:\KR-Compiler\KR-Compiler-Windows-x64-v4.3.1\runtime\jdk\bin';$source=Join-Path $root 'clients\android-ui';$work=Join-Path $root 'KR_BUILD\android-ui-work';$out=Join-Path $root 'KR_BUILD\android-arm64';$aapt2=Join-Path $bt 'aapt2.exe';$d8=Join-Path $bt 'd8.bat';$signer=Join-Path $bt 'apksigner.bat';
Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue;New-Item -ItemType Directory -Force -Path $work,$out | Out-Null
& $aapt2 compile --dir (Join-Path $source 'res') -o (Join-Path $work 'res.zip')
& $aapt2 link -I $platform --manifest (Join-Path $source 'AndroidManifest.xml') --auto-add-overlay --java (Join-Path $work 'generated') -o (Join-Path $work 'base.apk') -R (Join-Path $work 'res.zip')
& (Join-Path $jdk 'javac.exe') -source 8 -target 8 -classpath $platform -d (Join-Path $work 'classes') (Join-Path $work 'generated\com\aura\workspace\R.java') (Join-Path $source 'src\com\aura\workspace\MainActivity.java')
New-Item -ItemType Directory -Force -Path (Join-Path $work 'dex') | Out-Null
$classFiles=Get-ChildItem -LiteralPath (Join-Path $work 'classes') -Recurse -Filter *.class | Select-Object -ExpandProperty FullName
& $d8 --lib $platform --output (Join-Path $work 'dex') $classFiles
Copy-Item (Join-Path $work 'base.apk') (Join-Path $work 'unsigned.apk') -Force
Push-Location (Join-Path $work 'dex');try{& (Join-Path $jdk 'jar.exe') uf (Join-Path $work 'unsigned.apk') classes.dex}finally{Pop-Location}
$sign=Join-Path $root 'KR_BUILD\android-signing';$keystore=Join-Path $sign 'aura-enterprise.p12';$password=Join-Path $sign 'aura-enterprise.password';if(!(Test-Path $keystore)){throw 'Erst build-platforms.ps1 ausführen, um den Workspace-Signaturschlüssel zu erzeugen.'}
& $signer sign --ks $keystore --ks-key-alias aura-enterprise --ks-pass "file:$password" --key-pass "file:$password" --out (Join-Path $out 'aura-workspace-mobile.apk') (Join-Path $work 'unsigned.apk');if($LASTEXITCODE -ne 0){throw 'APK-Signatur fehlgeschlagen'}
& $signer verify --verbose (Join-Path $out 'aura-workspace-mobile.apk');if($LASTEXITCODE -ne 0){throw 'APK-Prüfung fehlgeschlagen'}
Write-Output "Erstellt: $(Join-Path $out 'aura-workspace-mobile.apk')"
