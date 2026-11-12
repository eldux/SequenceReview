# SPDX-License-Identifier: AGPL-3.0-only
[CmdletBinding()]
param(
    [string]$DataFolder = (Join-Path $PSScriptRoot 'data'),
    [string]$ExportFolder = (Join-Path $PSScriptRoot 'exports'),
    [ValidateRange(1,65535)][int]$Port = 8765,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install Docker Desktop and start it before running this script.'
}
if (-not (Test-Path -LiteralPath $DataFolder -PathType Container)) {
    if ($PSBoundParameters.ContainsKey('DataFolder')) { throw "Input folder does not exist: $DataFolder" }
    New-Item -ItemType Directory -Path $DataFolder | Out-Null
}
if (-not (Test-Path -LiteralPath $ExportFolder -PathType Container)) {
    New-Item -ItemType Directory -Path $ExportFolder | Out-Null
}
$env:SEQUENCE_REVIEW_DATA = (Resolve-Path -LiteralPath $DataFolder).Path.Replace('\','/')
$env:SEQUENCE_REVIEW_EXPORTS = (Resolve-Path -LiteralPath $ExportFolder).Path.Replace('\','/')
$env:SEQUENCE_REVIEW_PORT = "$Port"
Push-Location -LiteralPath $PSScriptRoot
try {
    docker compose up --build --detach --wait
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not start Sequence Review.' }
} finally {
    Pop-Location
}
$url = "http://127.0.0.1:$Port/"
Write-Output "Sequence Review is ready at $url"
if (-not $NoBrowser) { Start-Process $url }
