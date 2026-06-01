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
BLUEPRINTS_REF_FILE = os.path.join(HERE, "blueprints_ref.json")
ENGINEERS_REF_FILE = os.path.join(HERE, "engineers_ref.json")

mcp = FastMCP("elite-dangerous")


def _state_path() -> str:
    return os.environ.get("EDCLAUDE_STATE_FILE", DEFAULT_STATE_FILE)


def _load_json_file(path: str, fallback: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return fallback


_MAT_REF = _load_json_file(MATERIALS_REF_FILE, {})
_BP_REF = _load_json_file(BLUEPRINTS_REF_FILE, [])
_ENG_REF = _load_json_file(ENGINEERS_REF_FILE, {})


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


def _inventory_by_symbol(snap: dict[str, Any]) -> dict[str, int]:
    """Flatten raw/manufactured/encoded into one symbol -> count map."""
    inv: dict[str, int] = {}
    for bucket in (snap.get("materials") or {}).values():
        for symbol, count in (bucket or {}).items():
            inv[symbol.lower()] = inv.get(symbol.lower(), 0) + count
    return inv


@mcp.tool()
def get_blueprint_requirements(
    query: str,
    grade: Optional[int] = None,
    module_type: Optional[str] = None,
    experimental_only: bool = False,
    only_affordable: bool = False,
) -> dict[str, Any]:
    """Look up engineering blueprints (and experimental effects) with their
    material costs, compared against what's currently in inventory — so you can
    tell exactly what a roll needs and whether it's affordable right now.

    Args:
        query: substring matched (case-insensitive) against the blueprint name
            AND module type, e.g. 'dirty drive', 'engine focused', 'long range'.
        grade: filter to a single grade 1-5 (omit for all grades; experimental
            effects have no grade).
        module_type: extra filter on module type, e.g. 'Power Distributor',
            'Frame Shift Drive', 'Thrusters'.
        experimental_only: only return experimental ("special") effects.
        only_affordable: only return blueprints you can fully afford now.

    Each ingredient is annotated with need/have/short. `tracked` is false for
    ingredients that aren't ship engineering materials (Odyssey suit mats,
    tech-broker commodities) — those aren't in the materials inventory, so
    affordability ignores them and `can_afford` is null when any are present.
    """
    snap = _load_snapshot()
    inv = _inventory_by_symbol(snap)
    q = query.lower().strip()

    results: list[dict[str, Any]] = []
    for bp in _BP_REF:
        name, btype = bp.get("name") or "", bp.get("type") or ""
        if q and q not in name.lower() and q not in btype.lower():
            continue
        if grade is not None and bp.get("grade") != grade:
            continue
        if module_type and module_type.lower() not in btype.lower():
            continue
        if experimental_only and not bp.get("experimental"):
            continue

        ingredients, untracked, affordable = [], False, True
        for ing in bp.get("ingredients", []):
            sym = ing.get("symbol")
            need = ing.get("count", 0)
            tracked = sym is not None
            have = inv.get(sym, 0) if tracked else None
            if not tracked:
                untracked = True
            elif have < need:
                affordable = False
            ingredients.append({
                "name": ing.get("name"), "symbol": sym, "need": need,
                "have": have, "short": (max(0, need - have) if tracked else None),
                "tracked": tracked,
            })
        can_afford = None if untracked else affordable
        if only_affordable and can_afford is not True:
            continue
        results.append({
            "module_type": btype, "blueprint": name, "grade": bp.get("grade"),
            "experimental": bp.get("experimental"), "engineers": bp.get("engineers"),
            "can_afford": can_afford, "ingredients": ingredients,
            "effects": bp.get("effects"),
        })

    results.sort(key=lambda b: (b["module_type"] or "", b["blueprint"] or "", b["grade"] or 0))
    return {"query": query, "count": len(results), "blueprints": results,
            "data_age_seconds": snap.get("_age_seconds")}


@mcp.tool()
def get_engineer_status(
    module_type: Optional[str] = None,
    status: Optional[str] = None,
    domain: Optional[str] = None,
) -> dict[str, Any]:
    """Engineers, combining live unlock status (from the journal) with reference
    data: location (system + base), how to gain access, the unlock requirement,
    what they specialise in, and the max blueprint grade they offer. Use this to
    plan which engineers to unlock or rank up for a given module.

    Live status is one of: Unlocked (with rank 1-5 + rank_progress), Invited,
    Known, or Unknown (not yet discovered). Location/unlock details are reference
    data — verify in-game, as requirements can change.

    Args:
        module_type: only engineers who work on this module, e.g. 'Power
            Distributor', 'Frame Shift Drive', 'Thrusters' (substring match).
        status: filter by live status. Use 'unlocked', 'locked' (anything not
            yet unlocked), 'invited', 'known', or 'unknown'.
        domain: 'ship' for module engineers, 'odyssey' for suit/weapon engineers.
    """
    snap = _load_snapshot()
    live = snap.get("engineers", {})  # name -> {status, rank?, rank_progress?}
    want = (status or "").lower()

    results = []
    for name in sorted(set(_ENG_REF) | set(live)):
        ref = _ENG_REF.get(name, {})
        st = live.get(name) or {"status": "Unknown"}
        eff = st.get("status", "Unknown")

        if module_type and not any(
                module_type.lower() in (m or "").lower()
                for m in ref.get("specialisations", [])):
            continue
        if domain and ref.get("domain") != domain.lower():
            continue
        if want:
            is_unlocked = eff == "Unlocked"
            if want == "locked" and is_unlocked:
                continue
            if want != "locked" and eff.lower() != want:
                continue

        results.append({
            "engineer": name,
            "status": eff,
            "rank": st.get("rank"),
            "rank_progress": st.get("rank_progress"),
            "system": ref.get("system"),
            "base": ref.get("base"),
            "access": ref.get("access"),
            "unlock": ref.get("unlock"),
            "max_grade": ref.get("max_grade"),
            "specialisations": ref.get("specialisations", []),
            "domain": ref.get("domain"),
        })

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"count": len(results), "status_counts": counts, "engineers": results,
            "note": "Location/unlock data is reference-only; verify in-game.",
            "data_age_seconds": snap.get("_age_seconds")}


@mcp.tool()
def refresh_reference_data() -> dict[str, Any]:
    """Re-download the materials and engineering blueprint reference data from
    the community sources and rebuild the bundled reference files. Use this if
    the game has added new materials, blueprints, or experimental effects that
    the other tools don't yet recognise. Requires internet access."""
    global _MAT_REF, _BP_REF, _ENG_REF
    import update_references  # local module; stdlib-only, network at call time
    summary = update_references.update_references(HERE)
    _MAT_REF = _load_json_file(MATERIALS_REF_FILE, {})
    _BP_REF = _load_json_file(BLUEPRINTS_REF_FILE, [])
    _ENG_REF = _load_json_file(ENGINEERS_REF_FILE, {})
    return {"status": "refreshed", **summary}


if __name__ == "__main__":
    mcp.run()
