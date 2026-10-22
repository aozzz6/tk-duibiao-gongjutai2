$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
foreach ($required in @('runtime\python\python.exe', 'runtime\ffmpeg\ffmpeg.exe', 'runtime\models\medium.pt', 'payload\manifest.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $required))) { throw "Missing build input: $required. Copy resources/runtime from the complete portable release into this source folder as runtime/ first." }
}
& npm.cmd ci
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& npm.cmd run pack
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
Copy-Item -LiteralPath '使用说明.txt' -Destination 'dist\win-unpacked\使用说明.txt'
Write-Output 'Ready: dist/win-unpacked/TikTok工作台.exe'
