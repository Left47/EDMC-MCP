# Elite Dangerous MCP

An **MCP server for Elite Dangerous**: it pushes real-time **ship loadouts** (with
engineering modifications) and your **engineering materials inventory** to any MCP
client — **Claude Desktop, Ollama, and others** — so you can ask things like
*"what grade-5 dirty drag drives can I roll right now, and what am I short on?"*

Two pieces, both running locally on the same machine — **no data leaves your PC,
no network, no API keys**:

```
 Elite Dangerous ──journal──> EDMarketConnector
                                     │  (Elite Dangerous MCP plugin)
                                     ▼
                       ~/.elite-dangerous-claude/state.json   ← live snapshot
                                     ▲
                                     │  (reads the file)
    MCP client (Claude, Ollama) ──MCP── ed_claude_mcp.py
```

1. **`plugin/EDClaudeConnector/`** — an EDMarketConnector (EDMC) plugin (shown as
   **Elite Dangerous MCP** in EDMC) that writes a JSON snapshot whenever your
   loadout or materials change.
2. **`mcp/ed_claude_mcp.py`** — an MCP server that reads that snapshot and gives
   the connected client tools to query it.

## What the assistant can see

The MCP server exposes these tools:

| Tool | What it returns |
|------|-----------------|
| `get_status` | Commander, current ship, location, credits, data freshness |
| `get_materials` | Materials enriched with friendly name, grade (1–5), category, count. Filter by type / min grade / category / search |
| `get_current_loadout` | Every fitted module on your active ship, with engineering blueprint, grade, experimental effect, and per-stat modifiers |
| `get_ship_loadout` | The full cached loadout of **any** ship in your fleet (matched by name, type, ident, or ID) — not just the one you're in. Each ship is cached the last time you boarded it or changed its outfitting, and survives restarts |
| `get_blueprint_requirements` | Engineering blueprints & experimental effects with per-grade material costs, compared against your inventory (need / have / short, and whether you can afford the roll now) |
| `plan_material_trades` | How to get a material you need by trading others at a Material Trader, using in-game rates (trade up 6:1/grade, down 1:3/grade, ×6 across sub-categories; Raw/Manufactured/Encoded can't cross). Cheapest options from your current inventory first |
| `get_engineer_status` | Engineers with live unlock status (Unlocked + rank, Invited, Known, Unknown) merged with location, access & unlock requirements, specialisations, and max grade |
| `get_fleet` | Your ships (current + stored + any with a cached loadout), flagging which have a detailed loadout available |
| `request_capi_refresh` | Ask EDMC to pull a fresh live update from Frontier's Companion API (the same as EDMC's **Update** button) — the authoritative current loadout, fleet, and credits/location, including details the game doesn't write to the journal. If Frontier's global cooldown is active it returns a `cooldown` status with `retry_after_seconds` |
| `get_full_snapshot` | The raw snapshot (includes the last captured live CAPI data under `capi`) |
| `refresh_reference_data` | Re-download the materials & blueprint reference data (run if the game adds new content the tools don't recognise) |

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
3. Restart EDMarketConnector. You should see a green **"Elite Dangerous MCP:
   Running"** line on the main window, and an **Elite Dangerous MCP** tab under
   Settings.

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

Point your client's `command` at that venv's Python (paths below). Keep
`ed_claude_mcp.py` and `materials_ref.json` together — the server loads the
reference from its own directory.

### 3. Point your client at the server

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
>
> **Microsoft Store version of Claude Desktop?** It's sandboxed and reads its
> config from a different place:
> `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json`.
> Use Claude's **Settings → Developer → Edit Config** to open the exact file it
> reads. (The `install.bat` installer writes to both locations automatically.)

**Claude Code** (CLI):

```bash
claude mcp add elite-dangerous -- python /path/to/mcp/ed_claude_mcp.py
```

If you changed the snapshot path in the plugin settings, set the same path for
the server via the `EDCLAUDE_STATE_FILE` environment variable (add an `"env"`
block in the Desktop config, or `-e EDCLAUDE_STATE_FILE=...` with `claude mcp add`).

## Try it

With Elite Dangerous **and** EDMarketConnector running, ask your assistant:

- *"What's my current loadout and which modules are engineered?"*
- *"List my grade-5 manufactured materials."*
- *"I want to roll Dirty Drive Tuning grade 5 — do I have the materials?"* (uses `get_blueprint_requirements`)
- *"Which Power Distributor blueprints can I fully afford right now?"*
- *"For my current FSD, what would the next grade of Increased Range cost me?"*
- *"Which engineers do Power Distributor mods, and which have I unlocked?"* (uses `get_engineer_status`)
- *"What do I still need to unlock the engineers I haven't got yet?"*
- *"What shield-related encoded data am I low on?"*
- *"I'm short 8 Conductive Polymers for this roll — what can I trade for them?"* (uses `plan_material_trades`)
- *"Show me the engineering on my stored Anaconda."* (uses `get_ship_loadout`)
- *"Pull a fresh live update from Frontier and tell me my exact current loadout."* (uses `request_capi_refresh`)

## Use with Ollama (optional)

The MCP server isn't Claude-specific — any MCP client can use it. To drive it
with a **local model via [Ollama](https://ollama.com/)**, the simplest route is
[**ollmcp**](https://github.com/jonigl/mcp-client-for-ollama), a terminal MCP
client for Ollama. No changes to this project are needed — it runs the *same*
server with the *same* config shape Claude Desktop uses.

1. Install Ollama and pull a **tool-capable** model (e.g. a recent Llama 3.x,
   Qwen, or Mistral — the model must support tool calling, or none of the tools
   will fire).
2. Run `ollmcp` (no install needed with [uv](https://docs.astral.sh/uv/)) and
   register this server. Point `command` at the venv Python the installer
   created:

   ```bash
   # Windows
   uvx ollmcp mcp add elite-dangerous -- "C:\Users\you\EDMC-MCP\.venv\Scripts\python.exe" "C:\Users\you\EDMC-MCP\mcp\ed_claude_mcp.py"

   # Linux
   uvx ollmcp mcp add elite-dangerous -- /path/to/EDMC-MCP/.venv/bin/python /path/to/EDMC-MCP/mcp/ed_claude_mcp.py
   ```

   Or add it to `ollmcp`'s JSON config (same `mcpServers` format as Claude
   Desktop — copy the block the installer already wrote, including any
   `EDCLAUDE_STATE_FILE`):

   ```json
   {
     "mcpServers": {
       "elite-dangerous": {
         "command": "C:\\Users\\you\\EDMC-MCP\\.venv\\Scripts\\python.exe",
         "args": ["C:\\Users\\you\\EDMC-MCP\\mcp\\ed_claude_mcp.py"],
         "env": { "EDCLAUDE_STATE_FILE": "C:\\Users\\you\\.elite-dangerous-claude\\state.json" }
       }
     }
   }
   ```

3. Start `ollmcp`, pick your model, and ask the same questions as above.

Notes:
- Tool-calling reliability depends on the **model**, not the client. Small local
  models call tools far less reliably than Claude — favour a strong tool-capable
  model and lean on the focused tools (avoid `get_full_snapshot`, which can swamp
  a small context window).
- This gives you a terminal UI. For a browser GUI instead, an MCP→OpenAPI proxy
  such as `mcpo` in front of [Open WebUI](https://openwebui.com/) is an
  alternative — but `ollmcp` is the least-effort path.

## Updating

**Code (plugin + server):** the plugin checks GitHub for a newer release on
startup and, if one exists, appends `(update vX — click to update)` to the
status line on the EDMC main window. **Click that label** to run the updater
automatically; then restart EDMC and your MCP client when it finishes. You can
still update by hand by running **`update.bat`** (Windows) or **`./update.sh`**
(Linux) from your installed folder — both pull the latest (git *or* ZIP
download) and re-run the installer.

> The clickable label needs to know where you installed from, which the
> installer records (in `install_info.json` next to the plugin). The very first
> time you update onto this feature, run `update.bat` by hand once; after that
> the label is clickable.

**Reference data (materials & blueprints):** kept in `materials_ref.json` /
`blueprints_ref.json`, generated from community sources. If the game adds new
materials or blueprints, just ask your assistant to run **`refresh_reference_data`**, or
run `python mcp/update_references.py` yourself. (The installer also refreshes
these automatically.)

## Notes & limitations

- Materials are always current (the journal `state` tracks running totals).
- Detailed loadout only updates when the game emits a `Loadout` event (ship
  swap, outfitting changes, login). Visit outfitting once to populate it — or
  ask your assistant to run `request_capi_refresh` to pull the live loadout from Frontier.
- Each ship's loadout is cached the last time you boarded it, so `get_ship_loadout`
  can show any ship even when you're not in it. A ship you've never boarded while
  EDMC was running won't be cached yet — board it once to capture it.
- `request_capi_refresh` is subject to Frontier's global ~60s cooldown (shared
  with EDMC's **Update** button and its automatic pulls). If it's active the tool
  returns a `cooldown` status with `retry_after_seconds` rather than firing. EDMC
  must be signed in to Frontier.
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
