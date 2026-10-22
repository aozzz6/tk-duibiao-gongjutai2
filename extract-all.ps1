$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$sumFile = Join-Path $PSScriptRoot 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $sumFile)) { throw 'Download SHA256SUMS.txt and every ZIP part into this folder first.' }
$parts = @()
foreach ($line in Get-Content -LiteralPath $sumFile) {
    if (-not $line.Trim()) { continue }
    if ($line -notmatch '^([a-f0-9]{64})  (tk-workbench-win-x64-part[0-9]+\.zip)$') { throw 'Invalid checksum entry.' }
    $expected = $matches[1]
    $part = Join-Path $PSScriptRoot $matches[2]
    if (-not (Test-Path -LiteralPath $part)) { throw "Missing ZIP part: $part" }
    Write-Host "Checking $part"
    if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw "Checksum mismatch: $part" }
    $parts += $part
}
if ($parts.Count -eq 0) { throw 'No ZIP parts found in checksum list.' }
$destination = Join-Path $PSScriptRoot 'extracted'
foreach ($part in $parts) {
    Write-Host "Extracting $part"
    Expand-Archive -LiteralPath $part -DestinationPath $destination -Force
}
Write-Host "Done. Open TikTok工作台.exe in the extracted folder: $destination"
