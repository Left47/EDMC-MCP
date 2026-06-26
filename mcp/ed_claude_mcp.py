#!/usr/bin/env python3
"""
Elite Dangerous MCP server.

Reads the local JSON snapshot written by the Elite Dangerous MCP plugin for
EDMarketConnector and exposes it to an MCP client (Claude Desktop, Ollama, …) as
queryable tools — current ship loadout with engineering modifications,
engineering materials inventory (enriched with grade and category), the fleet,
and current location/credits.

Run over stdio (the default MCP transport):

    pip install -r requirements.txt
    python ed_claude_mcp.py

State file location resolution (first match wins):
    1. EDCLAUDE_STATE_FILE environment variable
    2. ~/.elite-dangerous-claude/state.json   (the plugin's default)
"""
from __future__ import annotations

import datetime
import json
import math
import os
import time
import uuid
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(os.path.expanduser("~"), ".elite-dangerous-claude", "state.json")
MATERIALS_REF_FILE = os.path.join(HERE, "materials_ref.json")
BLUEPRINTS_REF_FILE = os.path.join(HERE, "blueprints_ref.json")
ENGINEERS_REF_FILE = os.path.join(HERE, "engineers_ref.json")

mcp = FastMCP("elite-dangerous")


CAPI_REQUEST_FILE = "capi_request.json"


def _state_path() -> str:
    return os.environ.get("EDCLAUDE_STATE_FILE", DEFAULT_STATE_FILE)


def _request_path(state_path: str) -> str:
    """Sibling file (next to the snapshot) the plugin polls for refresh requests."""
    return os.path.join(os.path.dirname(state_path) or ".", CAPI_REQUEST_FILE)


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            f"Elite Dangerous MCP plugin enabled, and the game launched at least once "
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
    capi = snap.get("capi") or {}
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
        # Last live Frontier CAPI pull. Use request_capi_refresh() to fetch a new one.
        "capi": {"status": capi.get("status"),
                 "responded_at": capi.get("responded_at")} if capi else None,
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


def _format_loadout(loadout: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw journal Loadout event into the module summary the tools return."""
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
    }


@mcp.tool()
def get_current_loadout() -> dict[str, Any]:
    """Full loadout of the ship the commander is currently in: every fitted
    module with its slot, item, power priority, health, and any engineering
    blueprint + experimental effect + per-stat modifiers. Use this to advise on
    engineering rolls for the active ship. For a different ship in the fleet, use
    get_ship_loadout."""
    snap = _load_snapshot()
    ship = snap.get("current_ship", {})
    loadout = ship.get("loadout")
    if not loadout:
        return {
            "ship": {"type": ship.get("type"), "name": ship.get("name")},
            "note": "No detailed Loadout captured yet. Switch ships or visit "
                    "outfitting in-game to make the game emit a Loadout event.",
        }
    return {**_format_loadout(loadout), "data_age_seconds": snap.get("_age_seconds")}


@mcp.tool()
def get_ship_loadout(query: str) -> dict[str, Any]:
    """Full cached loadout of ANY ship in the fleet — not just the one you're
    currently in — with every module and its engineering, exactly like
    get_current_loadout. The plugin caches each ship's last-known loadout every
    time you board it or change its outfitting, and the cache survives restarts.

    Use this to compare ships or plan engineering for a stored ship without
    having to switch to it in-game.

    Args:
        query: match a ship by its name, type (e.g. 'anaconda',
            'federation_corvette'), ident, or numeric ShipID. Case-insensitive
            substring match.

    If a ship hasn't been boarded while EDMC was running, it won't be cached yet
    — board it (or open its outfitting) once to capture it.
    """
    snap = _load_snapshot()
    cache = snap.get("ship_loadouts") or {}
    q = query.strip().lower()
    matches = []
    for sid, lo in cache.items():
        haystack = [str(sid), lo.get("ShipName") or "", lo.get("Ship") or "",
                    lo.get("ShipIdent") or ""]
        if any(q == h.lower() or (q and q in h.lower()) for h in haystack):
            matches.append((sid, lo))

    if not matches:
        known = sorted(
            f"{lo.get('Ship')} \"{lo.get('ShipName')}\" (id {sid})"
            for sid, lo in cache.items())
        return {"query": query, "matches": 0,
                "cached_ships": known,
                "note": "No cached ship matched. Board the ship (or open its "
                        "outfitting) once while EDMC is running to cache it."}

    ships = [{**_format_loadout(lo), "ship_id": sid} for sid, lo in matches]
    return {"query": query, "matches": len(ships), "ships": ships,
            "data_age_seconds": snap.get("_age_seconds")}


@mcp.tool()
def get_fleet() -> dict[str, Any]:
    """List known ships in the fleet (current ship plus stored ships, when last
    seen at a shipyard) with type, name, value, and location."""
    snap = _load_snapshot()
    ships = dict(snap.get("ships", {}))
    cache = snap.get("ship_loadouts") or {}
    # Include ships we've cached a loadout for even if they're not in the last
    # StoredShips list (those only refresh at a shipyard).
    for sid, lo in cache.items():
        if sid not in ships:
            ships[sid] = {"type": lo.get("Ship"), "name": lo.get("ShipName"),
                          "ident": lo.get("ShipIdent")}
    fleet = []
    for ship_id, info in ships.items():
        entry = {"ship_id": ship_id}
        entry.update(info)
        loadout = cache.get(str(ship_id))
        entry["has_detailed_loadout"] = loadout is not None
        if loadout is not None:
            entry["engineered_module_count"] = sum(
                1 for m in loadout.get("Modules", []) if m.get("Engineering"))
        fleet.append(entry)
    fleet.sort(key=lambda s: (not s.get("current", False), s.get("type") or ""))
    return {"count": len(fleet), "ships": fleet,
            "note": "Stored ships only refresh when you dock at a shipyard; "
                    "use get_ship_loadout for any ship's full cached loadout "
                    "(has_detailed_loadout=true).",
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


def _resolve_material(query: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Resolve a material symbol or friendly name to (symbol, ref)."""
    q = query.strip().lower()
    if q in _MAT_REF:
        return q, _MAT_REF[q]
    for sym, ref in _MAT_REF.items():
        if (ref.get("name") or "").lower() == q:
            return sym, ref
    for sym, ref in _MAT_REF.items():  # substring fallback
        if q and q in (ref.get("name") or "").lower():
            return sym, ref
    return None, None


def _mat_subcat(ref: dict[str, Any]) -> tuple:
    """The trader sub-category a material belongs to (raw group number, or the
    named manufactured/encoded sub-category)."""
    if ref.get("type") == "Raw":
        return ("raw", ref.get("raw_category"))
    return ("sub", ref.get("category"))


def _trade_cost(src: dict[str, Any], tgt: dict[str, Any]) -> Optional[float]:
    """Source units consumed per 1 target unit at a Material Trader, or None if
    the trade is impossible (different material type — traders are type-locked).

    In-game rates: +1 grade costs x6 (multiplicative per grade), -1 grade gives
    x3 (so 1/3 the cost per grade), and a different sub-category costs an extra x6.
    """
    if not src.get("type") or src.get("type") != tgt.get("type"):
        return None
    sg, tg = src.get("grade"), tgt.get("grade")
    if sg is None or tg is None:
        return None
    diff = tg - sg  # > 0: target is a higher grade (trading up)
    cost = 6.0 ** diff if diff >= 0 else 1.0 / (3.0 ** (-diff))
    if _mat_subcat(src) != _mat_subcat(tgt):
        cost *= 6.0
    return cost


def _fmt_rate(per_target: float) -> str:
    if per_target > 1:
        return f"{round(per_target)} : 1 (trade up)"
    if per_target < 1:
        return f"1 : {round(1 / per_target)} (trade down)"
    return "1 : 1"


@mcp.tool()
def plan_material_trades(target: str, quantity: int = 1, max_options: int = 10) -> dict[str, Any]:
    """Work out how to obtain a material you need by trading materials you
    already have at a Material Trader, using the in-game exchange rates.

    Rates apply WITHIN one material type only — Raw, Manufactured, and Encoded
    cannot be traded for each other. Trading UP one grade costs 6:1 and stacks
    per grade (36:1 across two grades); trading DOWN gives 1:3 per grade (1:9
    across two); swapping to a different sub-category costs an extra x6. So
    trading DOWN within the same sub-category is the most efficient source.

    Args:
        target: the material you want (symbol or friendly name).
        quantity: how many units you need (default 1).
        max_options: cap on trade sources returned (cheapest first).

    Returns the viable trades from your CURRENT inventory, cheapest first (fewest
    source units consumed), each with the source material, the exchange rate, how
    many units it would consume, and whether you hold enough to fully cover it.
    """
    snap = _load_snapshot()
    tgt_sym, tgt_ref = _resolve_material(target)
    if tgt_ref is None:
        return {"target": target,
                "error": "Unknown material — use a symbol or name from get_materials."}

    inv = _inventory_by_symbol(snap)
    have_target = inv.get(tgt_sym, 0)
    still_short = max(0, quantity - have_target)

    options: list[dict[str, Any]] = []
    for sym, count in inv.items():
        if count <= 0 or sym == tgt_sym:
            continue
        src_ref = _MAT_REF.get(sym)
        if not src_ref:
            continue
        per_target = _trade_cost(src_ref, tgt_ref)
        if per_target is None:
            continue
        options.append({
            "source": src_ref.get("name", sym),
            "source_symbol": sym,
            "source_grade": src_ref.get("grade"),
            "source_subcategory": src_ref.get("category") or src_ref.get("raw_category"),
            "rate": _fmt_rate(per_target),
            "source_per_target": round(per_target, 3),
            "have": count,
            "need_to_trade": math.ceil(still_short * per_target) if still_short else 0,
            "can_cover_shortfall": count >= math.ceil(still_short * per_target) if still_short else True,
            "max_obtainable": int(count / per_target),
        })

    options.sort(key=lambda o: (not o["can_cover_shortfall"], o["source_per_target"], -o["have"]))
    return {
        "target": tgt_ref.get("name", tgt_sym),
        "target_symbol": tgt_sym,
        "target_type": tgt_ref.get("type"),
        "target_grade": tgt_ref.get("grade"),
        "quantity_needed": quantity,
        "already_have": have_target,
        "still_short": still_short,
        "trade_options": options[:max_options],
        "note": "Material Traders are type-locked (no Raw/Manufactured/Encoded "
                "cross-trades). You trade in whole units, so amounts are rounded "
                "up. 'need_to_trade' covers only the shortfall.",
        "data_age_seconds": snap.get("_age_seconds"),
    }


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
def request_capi_refresh(wait_seconds: float = 15.0) -> dict[str, Any]:
    """Ask EDMarketConnector to pull a fresh live update from Frontier's
    Companion API (CAPI) — the same thing EDMC's "Update" button does.

    This fetches authoritative data straight from Frontier's servers that the
    game does NOT always write to the journal on its own: the current ship's
    live loadout (including engineering), the full fleet, and credits/location.
    Use it when you need the very latest state — e.g. right after the commander
    re-rolls engineering, swaps modules, or buys/sells a ship — rather than
    relying on whatever the journal happened to emit.

    CAPI engineering vs the journal (get_current_loadout) — by design:
      - CAPI is the fresher source for roll-volatile fields (grade, quality,
        modifiers); after a remote-workshop re-roll the journal can lag.
      - `engineer` is the originating engineer (merged from the journal when the
        same mod is still fitted); `engineer_last_roll` is who CAPI says rolled
        it last — these differ after a re-roll at a different engineer.
      - `quality` is ESTIMATED (CAPI omits it), derived from how far the roll sits
        into the blueprint grade's range; `quality_estimated` is True only for the
        quality field and only when a value was actually derived (else quality is
        null and the flag is False).
      - `modifiers` carry CAPI's per-stat `multiplier` + `display` string (e.g.
        1.49 / "49.00%"); the journal path instead gives absolute `value`/
        `original`. Both shapes are intentional — CAPI doesn't provide absolutes.
      - CAPI omits the cockpit and cargo hatch, so its module_count is lower than
        the journal's; don't compare raw counts (engineered_module_count matches).

    Frontier enforces a global 60s cooldown (shared with EDMC's own pulls, e.g.
    on docking). If it's still active the refresh is skipped and this returns
    status 'cooldown' with 'retry_after_seconds' telling you when to try again.
    Requires EDMC to be running with this plugin enabled and signed in to Frontier.

    Args:
        wait_seconds: how long to wait for fresh data to arrive (1-60, default 15).

    Returns one of:
      - status 'refreshed': the captured live CAPI data (also under the 'capi'
        key of get_full_snapshot()).
      - status 'cooldown': with 'retry_after_seconds' / 'cooldown_until'.
      - status 'no_data' / 'not_acknowledged': nothing arrived in time (see note).
    """
    path = _state_path()
    if not os.path.exists(path):
        raise RuntimeError(
            f"No snapshot found at {path}. Is EDMarketConnector running with the "
            f"Elite Dangerous MCP plugin enabled? You can also set EDCLAUDE_STATE_FILE "
            f"to point at the file."
        )

    nonce = uuid.uuid4().hex
    req_path = _request_path(path)
    try:
        os.makedirs(os.path.dirname(req_path) or ".", exist_ok=True)
        tmp = req_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"nonce": nonce, "requested_at": _utcnow_iso()}, fh)
        os.replace(tmp, req_path)  # atomic on the same filesystem
    except OSError as exc:
        raise RuntimeError(f"Could not write CAPI request file at {req_path}: {exc}")

    deadline = time.time() + max(1.0, min(wait_seconds, 60.0))
    acknowledged = False
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with open(path, encoding="utf-8") as fh:
                capi = (json.load(fh).get("capi") or {})
        except (OSError, json.JSONDecodeError):
            continue
        if capi.get("request_nonce") == nonce:
            acknowledged = True
        if capi.get("response_nonce") == nonce:
            if capi.get("status") == "cooldown":
                remaining = capi.get("cooldown_remaining_seconds")
                retry_after = int(remaining + 0.999) if isinstance(remaining, (int, float)) else None
                note = "Frontier's global CAPI cooldown is active, so EDMC skipped " \
                       "the live update (its 'Update' button is greyed out too)."
                if retry_after is not None:
                    note += f" Retry in about {retry_after}s (cooldown ends {capi.get('cooldown_until')})."
                return {"status": "cooldown", "retry_after_seconds": retry_after,
                        "cooldown_until": capi.get("cooldown_until"), "note": note}
            return {"status": "refreshed", "capi": capi,
                    "note": "Fresh live data captured from Frontier's CAPI."}

    if acknowledged:
        return {"status": "no_data", "request_acknowledged": True,
                "note": "EDMC fired the request but no fresh CAPI data arrived in "
                        "time. Likely not signed in to Frontier, the CAPI servers "
                        "are slow/down, or the wait window was too short — try a "
                        "larger wait_seconds."}
    return {"status": "not_acknowledged", "request_acknowledged": False,
            "note": "The request was written but the EDMC plugin didn't pick it up "
                    "within the wait window. Is EDMarketConnector running with the "
                    "Elite Dangerous MCP plugin enabled?"}


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
