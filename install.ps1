<#
.SYNOPSIS
    Installs the Elite Dangerous -> Claude connector on Windows:
      1. Copies the EDClaudeConnector plugin into the EDMarketConnector plugins folder
      2. Installs the MCP server's Python dependency (mcp)
      3. Wires up the "elite-dangerous" MCP server in Claude Desktop's config
         (merges into existing config; does not clobber other servers)

.NOTES
    Run from the repo root. If PowerShell blocks the script, use install.bat
    (double-click) or:  powershell -ExecutionPolicy Bypass -File install.ps1
#>
[CmdletBinding()]
param(
    # Override the snapshot file location (must match the plugin's setting).
    [string]$StateFile
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
Write-Host "ED Claude Connector installer" -ForegroundColor Cyan
Write-Host "Repo: $repo`n"

# Refuse to run from a temporary/zip-preview location. Windows lets you "open"
# a .zip and run files straight from a Temp mount that is later deleted -- the
# venv and server we set up would vanish and Claude would fail to launch them.
if ($repo -match '\\Temp\\' -or $repo -match '\.zip') {
    throw "It looks like you're running this from inside a zipped/temporary folder:`n  $repo`n`n" +
          "Please EXTRACT the ZIP to a permanent location first (e.g. " +
          "$HOME\Documents\EDMC-MCP), then run install.bat from there."
}

# --- 1. Install the EDMC plugin ---------------------------------------------
$pluginSrc  = Join-Path $repo 'plugin\EDClaudeConnector'
$pluginRoot = Join-Path $env:LOCALAPPDATA 'EDMarketConnector\plugins'
$pluginDest = Join-Path $pluginRoot 'EDClaudeConnector'

if (-not (Test-Path $pluginSrc)) { throw "Plugin source not found at $pluginSrc" }
if (-not (Test-Path $pluginRoot)) {
    Write-Warning "EDMC plugins folder not found at $pluginRoot."
    Write-Warning "Is EDMarketConnector installed? Creating the folder anyway."
    New-Item -ItemType Directory -Force -Path $pluginRoot | Out-Null
}
New-Item -ItemType Directory -Force -Path $pluginDest | Out-Null
Copy-Item -Path (Join-Path $pluginSrc '*') -Destination $pluginDest -Recurse -Force
Write-Host "[1/3] Plugin installed -> $pluginDest" -ForegroundColor Green

# --- 2. Locate a WORKING Python and install the MCP dependency --------------
# Get-Command can match a Microsoft Store alias that isn't really runnable, so
# each candidate is verified by actually invoking `--version`.
function Test-Python($exe, $pre) {
    try {
        & $exe @pre --version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$pyExe = $null; $pyPre = @()
$candidates = @(
    @{ exe = 'py';      pre = @('-3') },
    @{ exe = 'python';  pre = @() },
    @{ exe = 'python3'; pre = @() }
)
foreach ($c in $candidates) {
    $cmd = Get-Command $c.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    # Prefer the resolved full path; bare names can hit PATH/alias quirks.
    $exe = if ($cmd.Source) { $cmd.Source } else { $c.exe }
    if (Test-Python $exe $c.pre) { $pyExe = $exe; $pyPre = $c.pre; break }
}
if (-not $pyExe) {
    throw "No working Python 3.10+ found. Install it from https://python.org " +
          "(tick 'Add python.exe to PATH' during setup), then re-run install.bat."
}

# A dedicated venv isolates the dependency and avoids polluting system Python.
$req   = Join-Path $repo 'mcp\requirements.txt'
$venv  = Join-Path $repo '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
Write-Host "Using Python: $pyExe $($pyPre -join ' ')"
& $pyExe @pyPre -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r $req
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
Write-Host "[2/3] MCP dependency installed into $venv" -ForegroundColor Green

# --- 3. Wire up Claude Desktop config ---------------------------------------
$serverPath = Join-Path $repo 'mcp\ed_claude_mcp.py'
$configPath = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'

if (Test-Path $configPath) {
    $json = Get-Content -Raw $configPath | ConvertFrom-Json
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $configPath) | Out-Null
    $json = [PSCustomObject]@{}
}
if (-not ($json.PSObject.Properties.Name -contains 'mcpServers')) {
    $json | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([PSCustomObject]@{})
}

$server = [PSCustomObject]@{
    command = $venvPy
    args    = @($serverPath)
}
if ($StateFile) {
    $server | Add-Member -NotePropertyName 'env' -NotePropertyValue ([PSCustomObject]@{ EDCLAUDE_STATE_FILE = $StateFile })
}

if ($json.mcpServers.PSObject.Properties.Name -contains 'elite-dangerous') {
    $json.mcpServers.'elite-dangerous' = $server
} else {
    $json.mcpServers | Add-Member -NotePropertyName 'elite-dangerous' -NotePropertyValue $server
}

$json | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 $configPath
Write-Host "[3/3] Claude Desktop config updated -> $configPath" -ForegroundColor Green

Write-Host "`nDone." -ForegroundColor Cyan
Write-Host "Next:"
Write-Host "  1. Restart EDMarketConnector (look for 'Claude: ready' on the main window)."
Write-Host "  2. Restart Claude Desktop."
Write-Host "  3. Launch Elite Dangerous, then ask Claude about your loadout or materials."
