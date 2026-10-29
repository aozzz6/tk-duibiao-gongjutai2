param(
    [string]$RuntimeSource = '',
    [switch]$SkipInstall
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# The public source tree intentionally does not contain the 5.9 GB runtime.
# A release checkout can pass -RuntimeSource pointing at resources/runtime,
# or place a runtime/ folder next to this script before building.
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'runtime'))) {
    if ($RuntimeSource -and (Test-Path -LiteralPath $RuntimeSource)) {
        Write-Host "Copying runtime from $RuntimeSource"
        Copy-Item -LiteralPath $RuntimeSource -Destination (Join-Path $PSScriptRoot 'runtime') -Recurse
    } else {
        throw 'Missing runtime/. Download the matching release package, or rerun with -RuntimeSource <path-to-resources/runtime>.'
    }
}
foreach ($required in @('runtime\python\python.exe', 'runtime\ffmpeg\ffmpeg.exe', 'runtime\models\medium.pt', 'payload\manifest.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $required))) { throw "Missing build input: $required. Copy resources/runtime from the complete portable release into this source folder as runtime/ first." }
}
if (-not $SkipInstall) {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
}
& npm.cmd run pack
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
Copy-Item -LiteralPath '使用说明.txt' -Destination 'dist\win-unpacked\使用说明.txt'
Write-Output 'Ready: dist/TikTok工作台 Setup *.exe and dist/win-unpacked/TikTok工作台.exe'
