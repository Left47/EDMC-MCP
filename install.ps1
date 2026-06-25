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
# Record where we installed from so the plugin's "click to update" can find
# update.bat (the plugin folder otherwise has no idea where the repo lives).
[System.IO.File]::WriteAllText(
    (Join-Path $pluginDest 'install_info.json'),
    (@{ repo = $repo; version_installed_from = 'install.ps1' } | ConvertTo-Json))
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
# Best-effort: refresh reference data from the community sources. Non-fatal —
# the repo already ships generated copies if this can't reach the network.
try { & $venvPy (Join-Path $repo 'mcp\update_references.py') | Out-Null; Write-Host "        reference data refreshed" }
catch { Write-Warning "Could not refresh reference data (using bundled copies): $_" }
Write-Host "[2/3] MCP dependency installed into $venv" -ForegroundColor Green

# --- 3. Wire up Claude Desktop config ---------------------------------------
# Claude Desktop comes in two flavours that read DIFFERENT config locations:
#   * Standalone (.exe) installer:  %APPDATA%\Claude\
#   * Microsoft Store (MSIX) build: %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\
# We write to every Claude config dir we find so it works regardless.
$serverPath = Join-Path $repo 'mcp\ed_claude_mcp.py'

$configDirs = New-Object System.Collections.Generic.List[string]
$configDirs.Add((Join-Path $env:APPDATA 'Claude'))                       # standalone .exe build
Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages') -Filter 'Claude_*' -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { $configDirs.Add((Join-Path $_.FullName 'LocalCache\Roaming\Claude')) }  # Store build(s)

# Determine the snapshot path to pin via EDCLAUDE_STATE_FILE, in priority order:
#   1. -StateFile argument (explicit override)
#   2. a path already pinned in an existing Claude config (preserve on update)
#   3. the default under the user profile
# This keeps a custom snapshot path working across re-installs/updates.
$default = Join-Path $env:USERPROFILE '.elite-dangerous-claude\state.json'
$existing = $null
foreach ($dir in $configDirs) {
    $cp = Join-Path $dir 'claude_desktop_config.json'
    if (-not (Test-Path $cp)) { continue }
    try {
        $prev = (Get-Content -Raw $cp | ConvertFrom-Json).mcpServers.'elite-dangerous'.env.EDCLAUDE_STATE_FILE
        if ($prev) { $existing = $prev; break }
    } catch { }
}
$statePinned = if ($StateFile) { $StateFile } elseif ($existing) { $existing } else { $default }
if ($existing -and -not $StateFile -and $existing -ne $default) {
    Write-Host "        preserving custom snapshot path: $statePinned"
}

# Pin an absolute path: the Microsoft Store Claude is sandboxed, so the server's
# `~` expansion can resolve to a virtualised home rather than the real profile.
$server = [PSCustomObject]@{
    command = $venvPy
    args    = @($serverPath)
    env     = [PSCustomObject]@{ EDCLAUDE_STATE_FILE = $statePinned }
}

$written = @()
foreach ($dir in $configDirs) {
    # Only target a Store dir if its package actually exists; always allow the
    # standalone dir (create it so a not-yet-launched Claude still picks it up).
    $isStore = $dir -like '*\Packages\*'
    if ($isStore -and -not (Test-Path (Split-Path (Split-Path (Split-Path $dir))))) { continue }

    $configPath = Join-Path $dir 'claude_desktop_config.json'
    if (Test-Path $configPath) {
        $json = Get-Content -Raw $configPath | ConvertFrom-Json
    } else {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $json = [PSCustomObject]@{}
    }
    if (-not ($json.PSObject.Properties.Name -contains 'mcpServers')) {
        $json | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([PSCustomObject]@{})
    }
    if ($json.mcpServers.PSObject.Properties.Name -contains 'elite-dangerous') {
        $json.mcpServers.'elite-dangerous' = $server
    } else {
        $json.mcpServers | Add-Member -NotePropertyName 'elite-dangerous' -NotePropertyValue $server
    }
    [System.IO.File]::WriteAllText($configPath, ($json | ConvertTo-Json -Depth 10))
    $written += $configPath
}
Write-Host "[3/3] Claude Desktop config updated:" -ForegroundColor Green
$written | ForEach-Object { Write-Host "        $_" }

Write-Host "`nDone." -ForegroundColor Cyan
Write-Host "Next:"
Write-Host "  1. Restart EDMarketConnector (look for 'ED Claude Connector: Running' on the main window)."
Write-Host "  2. Restart Claude Desktop."
Write-Host "  3. Launch Elite Dangerous, then ask Claude about your loadout or materials."
