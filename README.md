# Elite Dangerous → Claude Connector

Push real-time **ship loadouts** (with engineering modifications) and your
**engineering materials inventory** from Elite Dangerous into Claude, so you can
ask things like *"what grade-5 dirty drag drives can I roll right now, and what
am I short on?"*

Two pieces, both running locally on the same machine — **no data leaves your PC,
no network, no API keys**:

```
 Elite Dangerous ──journal──> EDMarketConnector
                                     │  (EDClaudeConnector plugin)
                                     ▼
                       ~/.elite-dangerous-claude/state.json   ← live snapshot
                                     ▲
                                     │  (reads the file)
       Claude Desktop / Code ──MCP── ed_claude_mcp.py
```

1. **`plugin/EDClaudeConnector/`** — an EDMarketConnector (EDMC) plugin that
   writes a JSON snapshot whenever your loadout or materials change.
2. **`mcp/ed_claude_mcp.py`** — an MCP server that reads that snapshot and gives
   Claude tools to query it.

## What Claude can see

The MCP server exposes these tools:

| Tool | What it returns |
|------|-----------------|
| `get_status` | Commander, current ship, location, credits, data freshness |
| `get_materials` | Materials enriched with friendly name, grade (1–5), category, count. Filter by type / min grade / category / search |
| `get_current_loadout` | Every fitted module on your active ship, with engineering blueprint, grade, experimental effect, and per-stat modifiers |
| `get_fleet` | Your ships (current + stored, as last seen at a shipyard) |
| `get_full_snapshot` | The raw snapshot |

## Prerequisites

- **[Elite Dangerous](https://www.elitedangerous.com/)** and
  **[EDMarketConnector](https://github.com/EDCD/EDMarketConnector/releases)**
  installed (Windows, or Linux via Proton/Steam Play).
- **[Claude Desktop](https://claude.ai/download)** (or Claude Code).
- **Python 3.10 or newer**, for the MCP server. Download it from
  [python.org/downloads](https://www.python.org/downloads/) and, on Windows,
  **tick "Add python.exe to PATH"** in the installer. Verify with `python --version`
  (or `py -3 --version`). This is separate from the Python that EDMC bundles —
  the plugin itself needs no Python install.

## Quick install (Windows)

The fastest path — does all three steps below automatically:

1. Make sure the **prerequisites above** are installed (especially Python, with
   "Add to PATH" ticked).
2. Download this repo (green **Code → Download ZIP**) and **extract it to a
   permanent folder** — e.g. `Documents\EDMC-MCP`. Don't run it from inside the
   Windows zip preview; that's a temporary folder that gets deleted (the
   installer will refuse if you try).
3. Open the extracted folder and double-click **`install.bat`**.

It copies the plugin into EDMC, creates a `.venv` and installs the MCP
dependency into it, and adds the `elite-dangerous` server to your Claude Desktop
config (merging — it won't touch other MCP servers). Then restart
EDMarketConnector and Claude Desktop.

> Keep the extracted folder where it is after installing — Claude launches the
> server from there.

> Just want the EDMC plugin (no Claude Desktop wiring)? Grab
> **`EDClaudeConnector.zip`** from the
> [latest release](https://github.com/Left47/EDMC-MCP/releases/latest) and
> extract the `EDClaudeConnector` folder into your EDMC plugins directory.

On Linux (running the game via Proton/Steam Play), run `./install.sh` instead.
To understand what the installers do, follow the manual steps below.

## Install (manual)

### 1. The EDMC plugin

EDMC bundles its own Python, so the plugin needs **no pip installs**.

1. Find your EDMC plugins folder: in EDMarketConnector, **File → Settings →
   Plugins → "Open"**. (Defaults: Windows `%LOCALAPPDATA%\EDMarketConnector\plugins`,
   Linux `~/.config/EDMarketConnector/plugins`.)
2. Copy the **`EDClaudeConnector`** folder (the one containing `load.py`) into
   that plugins folder.
3. Restart EDMarketConnector. You should see a green **"Claude: ready"** line on
   the main window, and an **ED Claude Connector** tab under Settings.

The snapshot is written to `~/.elite-dangerous-claude/state.json` by default
(configurable in the plugin's settings tab).

### 2. The MCP server

Needs your own Python **3.10+** (separate from EDMC's bundled one). Use a venv
so you don't hit PEP 668 "externally-managed-environment" errors on Linux:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r mcp/requirements.txt   # Windows
# Linux:  .venv/bin/python -m pip install -r mcp/requirements.txt
```

Point Claude's `command` at that venv's Python (paths below). Keep
`ed_claude_mcp.py` and `materials_ref.json` together — the server loads the
reference from its own directory.

### 3. Point Claude at the server

**Claude Desktop** — edit `claude_desktop_config.json` (Windows:
`%APPDATA%\Claude\claude_desktop_config.json`, Linux:
`~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "elite-dangerous": {
      "command": "python",
      "args": ["C:\\Users\\you\\EDMC-MCP\\mcp\\ed_claude_mcp.py"]
    }
  }
}
```

> On Windows use `"py"` or the full path to `python.exe` as `command`, and
> **double-backslash** paths (`\\`) or use forward slashes. Restart Claude
> Desktop after editing.

**Claude Code** (CLI):

```bash
claude mcp add elite-dangerous -- python /path/to/mcp/ed_claude_mcp.py
```

If you changed the snapshot path in the plugin settings, set the same path for
the server via the `EDCLAUDE_STATE_FILE` environment variable (add an `"env"`
block in the Desktop config, or `-e EDCLAUDE_STATE_FILE=...` with `claude mcp add`).

## Try it

With Elite Dangerous **and** EDMarketConnector running, ask Claude:

- *"What's my current loadout and which modules are engineered?"*
- *"List my grade-5 manufactured materials."*
- *"I want to roll Dirty Drive Tuning grade 5 — do I have the materials?"*
- *"What shield-related encoded data am I low on?"*

## Notes & limitations

- Materials are always current (the journal `state` tracks running totals).
- Detailed loadout only updates when the game emits a `Loadout` event (ship
  swap, outfitting changes, login). Visit outfitting once to populate it.
- Stored-ship details only refresh when you dock at a shipyard.
- Data freshness is reported as `data_age_seconds` — if it's large, the game or
  EDMC probably isn't running.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install mcp
.venv/bin/python tests/test_smoke.py   # end-to-end test with synthetic journal data
```

`materials_ref.json` is generated from
[EDCD/FDevIDs `material.csv`](https://github.com/EDCD/FDevIDs).
