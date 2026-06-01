#!/usr/bin/env python3
"""
Build / refresh the reference data the MCP server uses to enrich game data:

  * materials_ref.json  — material symbol -> {name, type, grade, category}
  * blueprints_ref.json — engineering blueprints & experimental effects with
                          per-grade material costs and resulting effects

Sources (community-maintained, kept current as the game adds content):
  * Materials:  EDCD/FDevIDs  material.csv
  * Blueprints: EDEngineer     blueprints.json

Run standalone to refresh the bundled files:

    python update_references.py

Or call update_references() from code (the MCP server exposes it as a tool).
Only the Python standard library is used, so no extra dependency is needed.
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MATERIALS_REF_FILE = os.path.join(HERE, "materials_ref.json")
BLUEPRINTS_REF_FILE = os.path.join(HERE, "blueprints_ref.json")
ENGINEERS_REF_FILE = os.path.join(HERE, "engineers_ref.json")

# Curated engineer location + unlock data. Engineer NAMES must match the
# journal exactly (they're the keys used by state['Engineers']). Specialisations
# and max grade are derived from the blueprint data at build time and merged in,
# so they stay accurate as blueprints update. Unlock/access details are
# community-sourced game facts (bubble engineers verified against the Elite Wiki;
# Colonia engineers from established references) and should be verified in-game.
ENGINEER_INFO = {
    # --- Bubble (Horizons) engineers ---
    "Felicity Farseer":    {"system": "Deciat",        "base": "Farseer Inc.",          "access": "Achieve exploration rank of Scout", "unlock": "Donate 1 Meta-Alloy"},
    "Elvira Martuuk":      {"system": "Khun",          "base": "Long Sight Base",       "access": "Travel 300 Ly from your career start", "unlock": "Donate 3 Soontill Relics"},
    "The Dweller":         {"system": "Wyrd",          "base": "Black Hide",            "access": "Use 5 different black markets", "unlock": "Donate 500,000 CR"},
    "Liz Ryder":           {"system": "Eurybia",       "base": "Demolition Unlimited",  "access": "Become Friendly with Eurybia Blue Mafia", "unlock": "Donate 200 Landmines"},
    "Tod McQuinn":         {"system": "Wolf 397",      "base": "Trophy Camp",           "access": "Earn 15 bounty vouchers", "unlock": "Donate 100,000 CR in bounty vouchers"},
    "Zacariah Nemo":       {"system": "Yoru",          "base": "Nemo Cyber Party Base", "access": "Get an invitation from the Party of Yoru", "unlock": "Donate 25 Xihe Companions"},
    "Lei Cheung":          {"system": "Laksak",        "base": "Trader's Rest",         "access": "Use 50 different commodity markets", "unlock": "Donate 200 Gold"},
    "Hera Tani":           {"system": "Kuwemaki",      "base": "The Jet's Hole",        "access": "Achieve Empire rank of Outsider", "unlock": "Donate 50 Kamitra Cigars"},
    "Juri Ishmaak":        {"system": "Giryak",        "base": "Pater's Memorial",      "access": "Earn 50 combat bonds", "unlock": "Donate 100,000 CR in combat bonds"},
    "Selene Jean":         {"system": "Kuk",           "base": "Prospector's Rest",     "access": "Mine 500 t of ore", "unlock": "Donate 10 Painite"},
    "Marco Qwent":         {"system": "Sirius",        "base": "Qwent Research Base",   "access": "Get an invitation from Sirius Corporation", "unlock": "Donate 25 Modular Terminals"},
    "Ram Tah":             {"system": "Meene",         "base": "Phoenix Base",          "access": "Achieve exploration rank of Surveyor", "unlock": "Donate 50 Classified Scan Databanks"},
    "Broo Tarquin":        {"system": "Muang",         "base": "Broo's Legacy",         "access": "Achieve combat rank of Competent", "unlock": "Donate 50 Fujin Tea"},
    "Colonel Bris Dekker": {"system": "Sol",           "base": "Dekker's Yard",         "access": "Become Friendly with the Federation", "unlock": "Donate 1,000,000 CR in combat bonds"},
    "Didi Vatermann":      {"system": "Leesti",        "base": "Vatermann LLC",         "access": "Achieve trading rank of Merchant", "unlock": "Donate 50 Lavian Brandy"},
    "Professor Palin":     {"system": "Arque",         "base": "Abel Laboratory",       "access": "Travel 5,000 Ly from your career start", "unlock": "Donate 25 Sensor Fragments"},
    "Lori Jameson":        {"system": "Shinrarta Dezhra", "base": "Jameson Base",       "access": "Achieve combat rank of Dangerous", "unlock": "Donate 25 Kongga Ale"},
    "Tiana Fortune":       {"system": "Achenar",       "base": "Fortune's Loss",        "access": "Become Friendly with the Empire", "unlock": "Donate 50 Decoded Emission Data"},
    "The Sarge":           {"system": "Beta-3 Tucani", "base": "The Beach",             "access": "Achieve Federal Navy rank of Midshipman", "unlock": "Donate 50 Aberrant Shield Pattern Analysis"},
    "Bill Turner":         {"system": "Alioth",        "base": "Turner Metallics Inc",  "access": "Become Friendly with the Alliance", "unlock": "Donate 50 Bromellite"},
    # --- Colonia region engineers ---
    "Mel Brandon":         {"system": "Luchtaine",     "base": "The Brig",              "access": "Become Friendly with the Colonia Council", "unlock": "Donate 100,000 CR in bounty vouchers"},
    "Etienne Dorn":        {"system": "Los",           "base": "Kaku Plant",            "access": "Achieve trading rank of Dealer", "unlock": "Donate 25 Occupied Escape Pods"},
    "Marsha Hicks":        {"system": "Tir",           "base": "The Watney Recreation Centre", "access": "Achieve trading/exploration rank", "unlock": "Donate 10 Osmium"},
    "Petra Olmanova":      {"system": "Asura",         "base": "Sanderling's Hideaway", "access": "Achieve combat rank", "unlock": "Donate 200 Progenitor Cells"},
    "Chloe Sedesi":        {"system": "Shenve",        "base": "Cinder Dock",           "access": "Travel 5,000 Ly from your career start", "unlock": "Donate 25 Sensor Fragments"},
}

MATERIAL_CSV_URL = "https://raw.githubusercontent.com/EDCD/FDevIDs/master/material.csv"
BLUEPRINTS_URL = (
    "https://raw.githubusercontent.com/msarilar/EDEngineer/master/"
    "EDEngineer/Resources/Data/blueprints.json"
)


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ED-Claude-Connector"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted URLs)
        return resp.read()


def build_materials_ref(csv_bytes: bytes) -> dict:
    """material symbol (lowercase, as used in the journal) -> attributes."""
    ref: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    for row in reader:
        symbol = row["symbol"].strip().lower()
        try:
            grade = int(row["rarity"])
        except (ValueError, KeyError):
            grade = None
        cat = (row.get("category") or "").strip()
        raw_cat = None
        if row.get("type") == "Raw" and cat.isdigit():
            raw_cat = int(cat)
            cat = None
        if cat in ("None", ""):
            cat = None
        entry = {"name": row["name"].strip(), "type": row["type"].strip(),
                 "grade": grade, "category": cat}
        if raw_cat is not None:
            entry["raw_category"] = raw_cat
        ref[symbol] = entry
    return ref


def build_blueprints_ref(blueprints: list, materials_ref: dict) -> tuple[list, list]:
    """EDEngineer blueprint list -> our compact form, plus unmatched ingredient names."""
    name_to_symbol = {v["name"].strip().lower(): sym for sym, v in materials_ref.items()}
    out: list[dict] = []
    unmatched: set[str] = set()
    for e in blueprints:
        grade = e.get("Grade")
        # Entries without a Grade are experimental ("special") effects, applied
        # at a workshop rather than rolled through grades. Engineer is "@Technology".
        experimental = grade is None
        ingredients = []
        for ing in e.get("Ingredients", []):
            nm = ing["Name"].strip()
            sym = name_to_symbol.get(nm.lower())
            if sym is None:
                unmatched.add(nm)
            ingredients.append({"symbol": sym, "name": nm, "count": ing.get("Size", 1)})
        out.append({
            "type": e.get("Type"),
            "name": e.get("Name"),
            "grade": grade,
            "experimental": experimental,
            "engineers": [x for x in e.get("Engineers", []) if not x.startswith("@")],
            "ingredients": ingredients,
            "effects": [
                {"property": x.get("Property"), "effect": x.get("Effect"),
                 "is_good": x.get("IsGood")}
                for x in e.get("Effects", [])
            ],
        })
    return out, sorted(unmatched)


def build_engineers_ref(blueprints: list) -> dict:
    """Merge curated location/unlock data with specialisations + max grade
    derived from the blueprint data. Keyed by engineer name (journal spelling)."""
    derived: dict[str, dict] = {}
    for b in blueprints:
        btype, grade = b.get("type"), (b.get("grade") or 0)
        if not btype or btype == "Unlock":  # 'Unlock' is a pseudo-type, not a module
            continue
        for name in b.get("engineers", []):
            d = derived.setdefault(name, {"modules": set(), "max_grade": 0})
            d["modules"].add(btype)
            d["max_grade"] = max(d["max_grade"], grade)

    ref: dict[str, dict] = {}
    for name in set(derived) | set(ENGINEER_INFO):
        d = derived.get(name, {"modules": set(), "max_grade": 0})
        info = ENGINEER_INFO.get(name, {})
        ref[name] = {
            "system": info.get("system"),
            "base": info.get("base"),
            "access": info.get("access"),
            "unlock": info.get("unlock"),
            "max_grade": d["max_grade"],
            "specialisations": sorted(d["modules"]),
            "domain": "odyssey" if d["max_grade"] == 0 else "ship",
        }
    return ref


def update_references(dest_dir: str = HERE) -> dict:
    """Fetch sources and (re)write all reference files. Returns a summary."""
    materials_ref = build_materials_ref(_fetch(MATERIAL_CSV_URL))
    blueprints = json.loads(_fetch(BLUEPRINTS_URL))
    blueprints_ref, unmatched = build_blueprints_ref(blueprints, materials_ref)
    engineers_ref = build_engineers_ref(blueprints_ref)

    mats_path = os.path.join(dest_dir, "materials_ref.json")
    bp_path = os.path.join(dest_dir, "blueprints_ref.json")
    eng_path = os.path.join(dest_dir, "engineers_ref.json")
    with open(mats_path, "w", encoding="utf-8") as fh:
        json.dump(materials_ref, fh, indent=1, sort_keys=True)
    with open(bp_path, "w", encoding="utf-8") as fh:
        json.dump(blueprints_ref, fh, separators=(",", ":"))
    with open(eng_path, "w", encoding="utf-8") as fh:
        json.dump(engineers_ref, fh, indent=1, sort_keys=True)

    return {
        "materials": len(materials_ref),
        "blueprint_entries": len(blueprints_ref),
        "distinct_blueprints": len({(b["type"], b["name"]) for b in blueprints_ref}),
        "engineers": len(engineers_ref),
        "unmatched_ingredients": unmatched,
        "materials_ref": mats_path,
        "blueprints_ref": bp_path,
        "engineers_ref": eng_path,
    }


if __name__ == "__main__":
    print(json.dumps(update_references(), indent=2))
