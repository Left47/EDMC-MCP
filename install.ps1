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

# --- 2. Locate Python and install the MCP dependency ------------------------
$py = $null
foreach ($cand in @('py', 'python', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd; break }
}
if (-not $py) {
    throw "No Python found on PATH. Install Python 3.10+ (python.org) and re-run."
}
if ($py.Name -like 'py*') { $pyExe = 'py'; $pyPre = @('-3') }
else { $pyExe = $py.Source; $pyPre = @() }

$req = Join-Path $repo 'mcp\requirements.txt'
Write-Host "Using Python: $pyExe $($pyPre -join ' ')"
& $pyExe @pyPre -m pip install --user -r $req
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
Write-Host "[2/3] MCP dependency installed" -ForegroundColor Green

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
    command = $pyExe
    args    = @($pyPre + $serverPath)
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
