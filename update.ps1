<#
.SYNOPSIS
    Updates Elite Dangerous MCP to the latest version, then re-runs the
    installer (which is idempotent: refreshes the EDMC plugin, the venv
    dependency, the reference data, and the Claude Desktop config).

    Works whether you cloned with git or downloaded the ZIP.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
Write-Host "Updating Elite Dangerous MCP in $repo" -ForegroundColor Cyan

if (Test-Path (Join-Path $repo '.git')) {
    Write-Host "git checkout detected - pulling latest..."
    git -C $repo pull --ff-only
} else {
    Write-Host "Downloading latest from GitHub..."
    $tmp = Join-Path $env:TEMP ("edmcmcp_" + [System.Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'main.zip'
    Invoke-WebRequest -Uri 'https://github.com/Left47/EDMC-MCP/archive/refs/heads/main.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Join-Path $tmp 'EDMC-MCP-main'
    # Refresh code/docs in place; .venv and your config are left untouched.
    foreach ($sub in 'plugin', 'mcp') {
        Copy-Item -Path (Join-Path (Join-Path $src $sub) '*') -Destination (Join-Path $repo $sub) -Recurse -Force
    }
    foreach ($f in 'install.ps1', 'install.bat', 'install.sh', 'update.ps1', 'update.bat', 'update.sh', 'README.md') {
        $sf = Join-Path $src $f
        if (Test-Path $sf) { Copy-Item $sf (Join-Path $repo $f) -Force }
    }
    Remove-Item -Recurse -Force $tmp
}

Write-Host "Re-running installer..." -ForegroundColor Cyan
& (Join-Path $repo 'install.ps1')
Write-Host "`nUpdate complete. Restart EDMarketConnector and Claude Desktop." -ForegroundColor Green
