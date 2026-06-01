"""End-to-end smoke test: drive the plugin's snapshot logic with stubbed EDMC
modules, then read it back through the MCP server's tools. Run with the repo
venv:  .venv/bin/python tests/test_smoke.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "plugin", "EDClaudeConnector")
MCP_DIR = os.path.join(ROOT, "mcp")

# --- Stub the EDMC-provided modules so load.py imports outside the app -------
_cfg_store = {}
config_mod = types.ModuleType("config")
class _Config:
    def get_str(self, k, default=None): return _cfg_store.get(k, default)
    def get_bool(self, k, default=False): return bool(_cfg_store.get(k, default))
    def getint(self, k, default=0): return int(_cfg_store.get(k, default) or 0)
    def get(self, k, default=None): return _cfg_store.get(k, default)
    def set(self, k, v): _cfg_store[k] = v
config_mod.config = _Config()
sys.modules["config"] = config_mod
sys.modules["myNotebook"] = types.ModuleType("myNotebook")
# Stub tkinter — EDMC ships its own Tk-enabled Python; the test never touches UI.
if "tkinter" not in sys.modules:
    try:
        import tkinter  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["tkinter"] = types.ModuleType("tkinter")
edmclog = types.ModuleType("EDMCLogging")
import logging
edmclog.get_main_logger = lambda: logging.getLogger("test")
sys.modules["EDMCLogging"] = edmclog

# Point the plugin at a temp snapshot, then import it.
state_file = os.path.join(tempfile.mkdtemp(), "state.json")
_cfg_store["edclaude_state_path"] = state_file
sys.path.insert(0, PLUGIN_DIR)
import load  # noqa: E402

# --- Sample journal data resembling real Elite Dangerous events --------------
LOADOUT_EVENT = {
    "timestamp": "2026-06-01T12:00:00Z", "event": "Loadout",
    "Ship": "federation_corvette", "ShipID": 7, "ShipName": "Bishop",
    "ShipIdent": "JT-01", "HullValue": 18000000, "ModulesValue": 90000000,
    "Rebuy": 5400000, "MaxJumpRange": 18.5, "UnladenMass": 1100.0,
    "CargoCapacity": 64, "FuelCapacity": {"Main": 32, "Reserve": 1.07},
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size8_class5", "On": True,
         "Priority": 0, "Health": 1.0,
         "Engineering": {"Engineer": "Hera Tani", "BlueprintName": "PowerPlant_Armoured",
                         "Level": 5, "Quality": 1.0,
                         "ExperimentalEffect_Localised": "Thermal Spread",
                         "Modifiers": [{"Label": "Integrity", "Value": 220.0,
                                        "OriginalValue": 158.0, "LessIsGood": 0}]}},
        {"Slot": "Slot01_Size7", "Item": "int_shieldgenerator_size7_class5_strong",
         "On": True, "Priority": 1, "Health": 1.0,
         "Engineering": {"Engineer": "Lei Cheung", "BlueprintName": "ShieldGenerator_Reinforced",
                         "Level": 5, "Quality": 0.8,
                         "ExperimentalEffect_Localised": "Hi-Cap",
                         "Modifiers": [{"Label": "ShieldGenStrength", "Value": 175.0,
                                        "OriginalValue": 130.0, "LessIsGood": 0}]}},
        {"Slot": "MainEngines", "Item": "int_engine_size7_class5", "On": True,
         "Priority": 0, "Health": 1.0},  # no engineering
    ],
}
STATE = {
    "GameVersion": "4.0.0", "GameBuild": "r300/Live", "Horizons": True, "Odyssey": True,
    "Credits": 1234567890, "ShipID": 7, "ShipType": "federation_corvette",
    "ShipName": "Bishop", "ShipIdent": "JT-01", "HullValue": 18000000,
    "ModulesValue": 90000000, "Rebuy": 5400000, "MaxJumpRange": 18.5,
    "CargoCapacity": 64, "FuelCapacity": {"Main": 32, "Reserve": 1.07},
    # Last three (cadmium / militarysupercapacitors / scandatabanks) are exactly
    # the mats for Power Distributor 'Engine Focused' grade 5 — used to verify
    # the blueprint affordability math below.
    "Raw": {"iron": 300, "zinc": 120, "tin": 24, "antimony": 6, "cadmium": 5},
    "Manufactured": {"shieldemitters": 200, "fedcorecomposites": 12,
                     "gridresistors": 250, "militarysupercapacitors": 5},
    "Encoded": {"shielddensityreports": 150, "shieldcyclerecordings": 300,
                "scandatabanks": 5},
    "Cargo": {}, "SystemName": "Shinrarta Dezhra", "StationName": "Jameson Memorial",
    "StationType": "Coriolis", "IsDocked": True, "OnFoot": False, "Body": None,
}

failures = []
def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        failures.append(name)

# --- Exercise the plugin -----------------------------------------------------
load.CONNECTOR.start()
load.journal_entry("CMDR Jim", False, "Shinrarta Dezhra", "Jameson Memorial", LOADOUT_EVENT, STATE)
# A material collected mid-session — state reflects new totals.
STATE2 = dict(STATE); STATE2["Raw"] = dict(STATE["Raw"]); STATE2["Raw"]["antimony"] = 10
load.journal_entry("CMDR Jim", False, "Shinrarta Dezhra", "Jameson Memorial",
                   {"event": "MaterialCollected", "timestamp": "2026-06-01T12:05:00Z"}, STATE2)
load.CONNECTOR.stop()  # flushes synchronously

with open(state_file) as fh:
    snap = json.load(fh)
check("snapshot written", os.path.exists(state_file))
check("current ship captured", snap["current_ship"]["type"] == "federation_corvette")
check("loadout attached to current ship", snap["current_ship"]["loadout"] is not None)
check("material update reflected", snap["materials"]["raw"]["antimony"] == 10,
      f'antimony={snap["materials"]["raw"]["antimony"]}')
check("material totals computed",
      snap["material_totals"]["raw"] == sum(STATE2["Raw"].values()),
      str(snap["material_totals"]))

# --- Read it back through the MCP server -------------------------------------
os.environ["EDCLAUDE_STATE_FILE"] = state_file
sys.path.insert(0, MCP_DIR)
import ed_claude_mcp as srv  # noqa: E402

status = srv.get_status()
check("MCP status commander", status["commander"] == "CMDR Jim")
check("MCP status credits", status["credits"] == 1234567890)

mats = srv.get_materials()
by_name = {m["name"]: m for m in mats["materials"]}
check("MCP enriches grade", by_name["Antimony"]["grade"] == 4, str(by_name.get("Antimony")))
check("MCP enriches type", by_name["Iron"]["type"] == "Raw")
check("MCP enriches category", by_name["Grid Resistors"]["category"] == "Capacitors",
      str(by_name.get("Grid Resistors")))

g5 = srv.get_materials(min_grade=5)
check("MCP min_grade filter", all(m["grade"] >= 5 for m in g5["materials"]),
      f'{len(g5["materials"])} g5 mats')
shield = srv.get_materials(category="shield")
check("MCP category filter", len(shield["materials"]) >= 1 and
      all("shield" in (m["category"] or "").lower() for m in shield["materials"]))

lo = srv.get_current_loadout()
check("MCP loadout module count", lo["module_count"] == 3, str(lo.get("module_count")))
check("MCP loadout engineered count", lo["engineered_module_count"] == 2,
      str(lo.get("engineered_module_count")))
pp = next(m for m in lo["modules"] if m["slot"] == "PowerPlant")
check("MCP engineering blueprint", pp["engineering"]["blueprint"] == "PowerPlant_Armoured")
check("MCP engineering experimental",
      pp["engineering"]["experimental_effect"] == "Thermal Spread")

fleet = srv.get_fleet()
check("MCP fleet has current ship", any(s.get("current") for s in fleet["ships"]))

# --- Blueprint requirements --------------------------------------------------
bp = srv.get_blueprint_requirements("engine focused", grade=5, module_type="Power Distributor")
check("blueprint lookup finds PD Engine Focused g5", bp["count"] >= 1, str(bp["count"]))
pd5 = bp["blueprints"][0]
check("blueprint ingredients tracked", all(i["tracked"] for i in pd5["ingredients"]))
check("blueprint affordable with stocked mats", pd5["can_afford"] is True,
      str(pd5["can_afford"]))
check("blueprint short is zero when affordable",
      all(i["short"] == 0 for i in pd5["ingredients"]))

# Same blueprint, but only_affordable should drop it once we can't pay.
empty_state = dict(STATE2)
empty_state["Raw"] = {}; empty_state["Manufactured"] = {}; empty_state["Encoded"] = {}
load.CONNECTOR.snapshot["materials"] = {"raw": {}, "manufactured": {}, "encoded": {}}
load.CONNECTOR._flush()
bp_none = srv.get_blueprint_requirements("engine focused", grade=5, only_affordable=True)
check("only_affordable filters out unaffordable", bp_none["count"] == 0, str(bp_none["count"]))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
