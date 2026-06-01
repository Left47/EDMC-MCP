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


def update_references(dest_dir: str = HERE) -> dict:
    """Fetch sources and (re)write both reference files. Returns a summary."""
    materials_ref = build_materials_ref(_fetch(MATERIAL_CSV_URL))
    blueprints_ref, unmatched = build_blueprints_ref(
        json.loads(_fetch(BLUEPRINTS_URL)), materials_ref)

    mats_path = os.path.join(dest_dir, "materials_ref.json")
    bp_path = os.path.join(dest_dir, "blueprints_ref.json")
    with open(mats_path, "w", encoding="utf-8") as fh:
        json.dump(materials_ref, fh, indent=1, sort_keys=True)
    with open(bp_path, "w", encoding="utf-8") as fh:
        json.dump(blueprints_ref, fh, separators=(",", ":"))

    return {
        "materials": len(materials_ref),
        "blueprint_entries": len(blueprints_ref),
        "distinct_blueprints": len({(b["type"], b["name"]) for b in blueprints_ref}),
        "unmatched_ingredients": unmatched,
        "materials_ref": mats_path,
        "blueprints_ref": bp_path,
    }


if __name__ == "__main__":
    print(json.dumps(update_references(), indent=2))
