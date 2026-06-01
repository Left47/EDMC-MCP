#!/usr/bin/env python3
"""
ED Claude MCP server.

Reads the local JSON snapshot written by the EDClaudeConnector EDMarketConnector
plugin and exposes it to Claude as queryable tools — current ship loadout with
engineering modifications, engineering materials inventory (enriched with grade
and category), the fleet, and current location/credits.

Run over stdio (the default MCP transport):

    pip install -r requirements.txt
    python ed_claude_mcp.py

State file location resolution (first match wins):
    1. EDCLAUDE_STATE_FILE environment variable
    2. ~/.elite-dangerous-claude/state.json   (the plugin's default)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(os.path.expanduser("~"), ".elite-dangerous-claude", "state.json")
MATERIALS_REF_FILE = os.path.join(HERE, "materials_ref.json")

mcp = FastMCP("elite-dangerous")


def _state_path() -> str:
    return os.environ.get("EDCLAUDE_STATE_FILE", DEFAULT_STATE_FILE)


def _load_materials_ref() -> dict[str, Any]:
    try:
        with open(MATERIALS_REF_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {}


_MAT_REF = _load_materials_ref()


def _load_snapshot() -> dict[str, Any]:
    """Read the snapshot, raising a friendly error if it's missing."""
    path = _state_path()
    try:
        with open(path, encoding="utf-8") as fh:
            snap = json.load(fh)
    except FileNotFoundError:
        raise RuntimeError(
            f"No snapshot found at {path}. Is EDMarketConnector running with the "
            f"EDClaudeConnector plugin enabled, and the game launched at least once "
            f"since? You can also set EDCLAUDE_STATE_FILE to point at the file."
        )
    except json.JSONDecodeError:
        # The plugin writes atomically, but tolerate a torn read by retrying.
        time.sleep(0.2)
        with open(path, encoding="utf-8") as fh:
            snap = json.load(fh)
    snap["_age_seconds"] = _age_seconds(path)
    return snap


def _age_seconds(path: str) -> Optional[float]:
    try:
        return round(time.time() - os.path.getmtime(path), 1)
    except OSError:
        return None


def _enrich_material(symbol: str, count: int) -> dict[str, Any]:
    ref = _MAT_REF.get(symbol.lower(), {})
    return {
        "symbol": symbol,
        "name": ref.get("name", symbol),
        "type": ref.get("type"),
        "grade": ref.get("grade"),
        "category": ref.get("category"),
        "count": count,
    }


def _engineering_summary(module: dict[str, Any]) -> Optional[dict[str, Any]]:
    eng = module.get("Engineering")
    if not eng:
        return None
    return {
        "blueprint": eng.get("BlueprintName"),
        "grade": eng.get("Level"),
        "quality": eng.get("Quality"),
        "engineer": eng.get("Engineer"),
        "experimental_effect": eng.get("ExperimentalEffect_Localised") or eng.get("ExperimentalEffect"),
        "modifiers": [
            {"label": m.get("Label"), "value": m.get("Value"),
             "original": m.get("OriginalValue"), "less_is_good": m.get("LessIsGood")}
            for m in eng.get("Modifiers", [])
        ],
    }


# === Tools ===================================================================

@mcp.tool()
def get_status() -> dict[str, Any]:
    """Quick overview: commander, current ship, location, credits, and how fresh
    the data is. Call this first to confirm the game is running and data is live."""
    snap = _load_snapshot()
    ship = snap.get("current_ship", {})
    age = snap.get("_age_seconds")
    return {
        "commander": snap.get("cmdr"),
        "current_ship": {"type": ship.get("type"), "name": ship.get("name"),
                         "ident": ship.get("ident")},
        "location": snap.get("location"),
        "credits": snap.get("credits"),
        "material_totals": snap.get("material_totals"),
        "last_event": snap.get("last_event"),
        "updated": snap.get("updated"),
        "data_age_seconds": age,
        "data_is_fresh": age is not None and age < 120,
        "game_version": (snap.get("game") or {}).get("version"),
    }


@mcp.tool()
def get_materials(
    material_type: Optional[str] = None,
    min_grade: Optional[int] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
) -> dict[str, Any]:
    """List engineering materials in inventory, enriched with friendly name,
    grade (1-5), category, and count. Sorted by type then grade then name.

    Args:
        material_type: filter to 'raw', 'manufactured', or 'encoded'.
        min_grade: only materials at this grade or higher (1-5).
        category: case-insensitive substring match on category (e.g. 'shield',
            'capacitors', 'wake').
        search: case-insensitive substring match on the material name.
    """
    snap = _load_snapshot()
    materials = snap.get("materials", {})
    buckets = [material_type.lower()] if material_type else ["raw", "manufactured", "encoded"]

    results: list[dict[str, Any]] = []
    for bucket in buckets:
        for symbol, count in (materials.get(bucket) or {}).items():
            item = _enrich_material(symbol, count)
            if min_grade is not None and (item["grade"] or 0) < min_grade:
                continue
            if category and category.lower() not in (item["category"] or "").lower():
                continue
            if search and search.lower() not in item["name"].lower():
                continue
            results.append(item)

    results.sort(key=lambda m: (m["type"] or "", -(m["grade"] or 0), m["name"]))
    return {
        "count": len(results),
        "totals": snap.get("material_totals"),
        "materials": results,
        "data_age_seconds": snap.get("_age_seconds"),
    }


@mcp.tool()
def get_current_loadout() -> dict[str, Any]:
    """Full loadout of the ship the commander is currently in: every fitted
    module with its slot, item, power priority, health, and any engineering
    blueprint + experimental effect + per-stat modifiers. Use this to advise on
    engineering rolls for the active ship."""
    snap = _load_snapshot()
    ship = snap.get("current_ship", {})
    loadout = ship.get("loadout")
    if not loadout:
        return {
            "ship": {"type": ship.get("type"), "name": ship.get("name")},
            "note": "No detailed Loadout captured yet. Switch ships or visit "
                    "outfitting in-game to make the game emit a Loadout event.",
        }

    modules = []
    for m in loadout.get("Modules", []):
        modules.append({
            "slot": m.get("Slot"),
            "item": m.get("Item"),
            "on": m.get("On"),
            "priority": m.get("Priority"),
            "health": m.get("Health"),
            "engineering": _engineering_summary(m),
        })
    engineered = [m for m in modules if m["engineering"]]
    return {
        "ship": {
            "type": loadout.get("Ship"), "name": loadout.get("ShipName"),
            "ident": loadout.get("ShipIdent"), "ship_id": loadout.get("ShipID"),
            "hull_value": loadout.get("HullValue"),
            "modules_value": loadout.get("ModulesValue"),
            "rebuy": loadout.get("Rebuy"),
            "max_jump_range": loadout.get("MaxJumpRange"),
            "unladen_mass": loadout.get("UnladenMass"),
            "cargo_capacity": loadout.get("CargoCapacity"),
            "fuel_capacity": loadout.get("FuelCapacity"),
        },
        "module_count": len(modules),
        "engineered_module_count": len(engineered),
        "modules": modules,
        "data_age_seconds": snap.get("_age_seconds"),
    }


@mcp.tool()
def get_fleet() -> dict[str, Any]:
    """List known ships in the fleet (current ship plus stored ships, when last
    seen at a shipyard) with type, name, value, and location."""
    snap = _load_snapshot()
    ships = snap.get("ships", {})
    fleet = []
    for ship_id, info in ships.items():
        entry = {"ship_id": ship_id}
        entry.update(info)
        fleet.append(entry)
    fleet.sort(key=lambda s: (not s.get("current", False), s.get("type") or ""))
    return {"count": len(fleet), "ships": fleet,
            "note": "Stored ships only refresh when you dock at a shipyard.",
            "data_age_seconds": snap.get("_age_seconds")}


@mcp.tool()
def get_full_snapshot() -> dict[str, Any]:
    """Return the entire raw snapshot (everything the plugin tracks). Use the
    more specific tools when possible; this is for when you need raw fields."""
    return _load_snapshot()


if __name__ == "__main__":
    mcp.run()
