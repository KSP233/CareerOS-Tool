param([switch]$RefreshDependencies)
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runtimePython = Join-Path $projectRoot "runtime\python.exe"
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) { throw "Missing runtime\python.exe" }

if ($RefreshDependencies) {
    & $runtimePython -m pip install --upgrade -r (Join-Path $projectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
}

& $runtimePython -B -m unittest discover -s (Join-Path $projectRoot "tests") -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }

$buildRoot = Join-Path $projectRoot "build\portable-v020"
$distRoot = Join-Path $projectRoot "release-v2\portable-v020"
foreach ($candidate in @($buildRoot, $distRoot)) {
    $full = [IO.Path]::GetFullPath($candidate)
    if (-not $full.StartsWith([IO.Path]::GetFullPath($projectRoot), [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe build path: $full" }
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

& $runtimePython -m PyInstaller --noconfirm --clean --workpath $buildRoot --distpath $distRoot (Join-Path $projectRoot "CareerOS.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$appFolder = Join-Path $distRoot "CareerOS"
Copy-Item -LiteralPath (Join-Path $projectRoot "README-PORTABLE.txt") -Destination $appFolder
$forbidden = Get-ChildItem -LiteralPath $appFolder -Recurse -Force | Where-Object { $_.Name -in @("careeros.db", "applypilot.db", "settings.json", ".api-key-encryption", "data-location.json") -or $_.FullName -match "CareerOS-data" }
if ($forbidden) { throw "Privacy check failed: local data was included in the portable build" }

$selfTest = Start-Process -FilePath (Join-Path $appFolder "CareerOS.exe") -ArgumentList "--self-test" -Wait -PassThru -WindowStyle Hidden
if ($selfTest.ExitCode -ne 0) { throw "Packaged self-test failed with exit code $($selfTest.ExitCode)" }
if (Test-Path -LiteralPath (Join-Path $appFolder "CareerOS-data")) { throw "Self-test created user data inside the clean package" }

$manifest = Join-Path $appFolder "SHA256SUMS.txt"
$lines = Get-ChildItem -LiteralPath $appFolder -Recurse -File | Where-Object { $_.FullName -ne $manifest } | Sort-Object FullName | ForEach-Object {
    $relative = [IO.Path]::GetRelativePath($appFolder, $_.FullName); $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
    "$hash  $relative"
}
[IO.File]::WriteAllLines($manifest, $lines, [Text.UTF8Encoding]::new($false))

$zip = Join-Path (Join-Path $projectRoot "release-v2") "CareerOS-Portable-unsigned-v0.2.0.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -LiteralPath $appFolder -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Portable package: $zip"
