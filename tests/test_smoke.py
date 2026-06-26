"""End-to-end smoke test: drive the plugin's snapshot logic with stubbed EDMC
modules, then read it back through the MCP server's tools. Run with the repo
venv:  .venv/bin/python tests/test_smoke.py
"""
import json
import os
import sys
import tempfile
import time
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
    # EDMC stores unlocked engineers as (Rank, RankProgress) tuples, others as
    # a status string.
    "Engineers": {"The Dweller": (5, 0), "Etienne Dorn": (3, 45),
                  "Felicity Farseer": "Known"},
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

# --- Engineer status ---------------------------------------------------------
# Restore materials so the snapshot reflects the engineer data we set.
load.CONNECTOR.update("CMDR Jim", "Shinrarta Dezhra", "Jameson Memorial",
                      {"event": "EngineerProgress", "timestamp": "2026-06-01T12:10:00Z"}, STATE)
load.CONNECTOR._flush()
eng = srv.get_engineer_status()
by_eng = {e["engineer"]: e for e in eng["engineers"]}
check("engineer unlocked status + rank", by_eng["The Dweller"]["status"] == "Unlocked"
      and by_eng["The Dweller"]["rank"] == 5, str(by_eng.get("The Dweller")))
check("engineer rank progress", by_eng["Etienne Dorn"]["rank_progress"] == 45)
check("engineer known status", by_eng["Felicity Farseer"]["status"] == "Known")
check("engineer reference merged (location)", by_eng["The Dweller"]["system"] == "Wyrd"
      and by_eng["The Dweller"]["unlock"] == "Donate 500,000 CR")
check("undiscovered engineer is Unknown", by_eng["Lori Jameson"]["status"] == "Unknown")

pd_eng = srv.get_engineer_status(module_type="Power Distributor")
pd_names = {e["engineer"] for e in pd_eng["engineers"]}
check("engineer module filter", {"The Dweller", "Etienne Dorn"} <= pd_names
      and "Liz Ryder" not in pd_names, str(sorted(pd_names)))
unlocked = srv.get_engineer_status(status="unlocked")
check("engineer status filter (unlocked)",
      all(e["status"] == "Unlocked" for e in unlocked["engineers"])
      and len(unlocked["engineers"]) == 2, str(len(unlocked["engineers"])))

# --- Live CAPI refresh round-trip --------------------------------------------
# The MCP tool writes a request file; the plugin (main thread) picks it up and
# fires a CAPI query; the cmdr_data hook captures the response. Simulate the
# plugin side in a background thread while the MCP tool waits. The writer thread
# was stopped earlier, so the simulated plugin flushes explicitly.
import threading  # noqa: E402

# Mirrors the real Frontier CAPI /profile shape: engineering hangs off the SLOT
# entry (sibling of "module"), the engineer block carries the name + recipe,
# specialModifications is {codename: codename} (or [] when none), and
# WorkInProgress_modifications gives per-stat multipliers. Item names are
# PascalCase and health is 0..1000000 — the plugin normalises both.
SAMPLE_CAPI = {
    "commander": {"name": "CMDR Jim", "credits": 1234567890, "docked": True},
    "lastSystem": {"name": "Shinrarta Dezhra"},
    "lastStarport": {"name": "Jameson Memorial"},
    "ship": {
        "id": 7, "name": "federation_corvette", "shipName": "Bishop", "shipID": "JT-01",
        "value": {"total": 108000000},
        "modules": {
            "FrameShiftDrive": {
                "module": {"name": "Int_Hyperdrive_Size5_Class5", "on": True,
                           "priority": 0, "health": 1000000, "value": 5103953},
                "engineer": {"engineerName": "Mel Brandon", "engineerId": 300280,
                             "recipeName": "FSD_LongRange", "recipeLevel": 5},
                "WorkInProgress_modifications": {
                    "OutfittingFieldType_FSDOptimalMass": {
                        "value": 1.55, "LessIsGood": False, "displayValue": "55.00%"},
                    "OutfittingFieldType_Mass": {
                        "value": 1.3, "LessIsGood": True, "displayValue": "-30.00%"}},
                "specialModifications": {"special_fsd_heavy": "special_fsd_heavy"}},
            "PowerPlant": {
                "module": {"name": "Int_Powerplant_Size8_Class5", "on": True,
                           "priority": 0, "health": 1000000, "value": 12971097},
                "engineer": {"engineerName": "Etienne Dorn", "engineerId": 300290,
                             "recipeName": "PowerPlant_Boosted", "recipeLevel": 5},
                "WorkInProgress_modifications": {
                    # +36.5% lands halfway through G5's 33%..40% range -> quality ~0.5
                    "OutfittingFieldType_PowerCapacity": {
                        "value": 1.365, "LessIsGood": False, "displayValue": "36.50%"}},
                "specialModifications": []},  # engineered, but no experimental effect
            # Same mod as the journal's Slot01_Size7 (item + blueprint match) but
            # re-rolled at a remote workshop by Mel Brandon -> engineer should come
            # from the journal (Lei Cheung), engineer_last_roll from CAPI.
            "Slot01_Size7": {
                "module": {"name": "Int_ShieldGenerator_Size7_Class5_Strong", "on": True,
                           "priority": 1, "health": 1000000, "value": 8000000},
                "engineer": {"engineerName": "Mel Brandon", "engineerId": 300280,
                             "recipeName": "ShieldGenerator_Reinforced", "recipeLevel": 5},
                "WorkInProgress_modifications": {
                    # +35% optimal strength = halfway through G5's 32%..38% -> quality 0.5
                    "OutfittingFieldType_ShieldGenStrength": {
                        "value": 1.35, "LessIsGood": False, "displayValue": "35.00%"},
                    # A resistance whose naive q (0.9) would skew the median if not skipped.
                    "OutfittingFieldType_ThermicResistance": {
                        "value": 1.162, "LessIsGood": False, "displayValue": "16.20%"}},
                "specialModifications": {"special_shield_regenerative": "special_shield_regenerative"}},
            # Engineered with a blueprint not in the quality table -> quality can't
            # be estimated: quality null AND quality_estimated false (not contradictory).
            "Slot09_Size1": {
                "module": {"name": "Int_DetailedSurfaceScanner_Tiny", "on": True,
                           "priority": 0, "health": 1000000, "value": 250000},
                "engineer": {"engineerName": "Lori Jameson", "engineerId": 300100,
                             "recipeName": "Sensor_Expanded", "recipeLevel": 5},
                "WorkInProgress_modifications": {
                    "OutfittingFieldType_DSS_PatchRadius": {
                        "value": 1.2, "LessIsGood": False, "displayValue": "20.00%"}},
                "specialModifications": []},
            # Known blueprint whose only varying stat is a resistance -> all stats
            # skipped -> quality null AND quality_estimated false (not contradictory).
            "Armour": {
                "module": {"name": "Federation_Corvette_Armour_Grade2", "on": True,
                           "priority": 1, "health": 1000000, "value": 3000000},
                "engineer": {"engineerName": "Selene Jean", "engineerId": 300210,
                             "recipeName": "Armour_Kinetic", "recipeLevel": 5},
                "WorkInProgress_modifications": {
                    "OutfittingFieldType_KineticResistance": {
                        "value": 1.4, "LessIsGood": False, "displayValue": "40.00%"}},
                "specialModifications": []},
            "MainEngines": {"module": {
                "name": "Int_Engine_Size7_Class5", "on": True, "priority": 0,
                "health": 1000000}},  # not engineered: no 'engineer' block
        },
    },
    "ships": {"7": {"id": 7, "name": "federation_corvette", "shipName": "Bishop",
                    "starsystem": {"name": "Shinrarta Dezhra"},
                    "station": {"name": "Jameson Memorial"}}},
}

def _fake_plugin_side():
    for _ in range(100):
        time.sleep(0.1)
        load.CONNECTOR.poll_request()  # idempotent; sets _pending_nonce on new request
        if load.CONNECTOR._pending_nonce:
            load.CONNECTOR.record_capi(SAMPLE_CAPI, False)
            load.CONNECTOR._flush()  # writer thread is stopped; flush explicitly
            return

t = threading.Thread(target=_fake_plugin_side, daemon=True)
t.start()
capi_res = srv.request_capi_refresh(wait_seconds=12)
t.join(timeout=15)
check("capi refresh refreshed", capi_res["status"] == "refreshed", str(capi_res.get("status")))
capi = capi_res.get("capi", {})
capi_ship = capi.get("current_ship") or {}
check("capi current ship captured", capi_ship.get("type") == "federation_corvette",
      str(capi_ship.get("type")))
check("capi engineering captured",
      capi_ship.get("engineered_module_count") == 5,
      str(capi_ship.get("engineered_module_count")))
capi_mods = {m["slot"]: m for m in capi_ship.get("modules", [])}
# CAPI engineering is normalised to the same summary shape as the journal path.
capi_fsd = capi_mods.get("FrameShiftDrive", {})
fsd_eng = capi_fsd.get("engineering") or {}
check("capi FSD blueprint matches journal codename", fsd_eng.get("blueprint") == "FSD_LongRange",
      str(fsd_eng.get("blueprint")))
check("capi FSD grade", fsd_eng.get("grade") == 5, str(fsd_eng.get("grade")))
# No journal FSD in the loadout -> engineer falls back to CAPI for both fields.
check("capi FSD engineer name resolved", fsd_eng.get("engineer") == "Mel Brandon",
      str(fsd_eng.get("engineer")))
check("capi FSD engineer_last_roll", fsd_eng.get("engineer_last_roll") == "Mel Brandon",
      str(fsd_eng.get("engineer_last_roll")))
# Engineer provenance: journal slot matches by item+blueprint, so engineer comes
# from the journal (Lei Cheung) while engineer_last_roll keeps CAPI's (Mel Brandon).
sh_eng = (capi_mods.get("Slot01_Size7", {}).get("engineering")) or {}
check("capi engineer from journal provenance", sh_eng.get("engineer") == "Lei Cheung",
      str(sh_eng.get("engineer")))
check("capi engineer_last_roll from CAPI", sh_eng.get("engineer_last_roll") == "Mel Brandon",
      str(sh_eng.get("engineer_last_roll")))
check("capi quality skips resistances (no median skew)", sh_eng.get("quality") == 0.5,
      str(sh_eng.get("quality")))
# Blueprint differs from journal's PowerPlant (Boosted vs Armoured) -> no merge,
# engineer stays CAPI's.
pp_engineer = (capi_mods.get("PowerPlant", {}).get("engineering") or {}).get("engineer")
check("capi no merge when blueprint differs", pp_engineer == "Etienne Dorn", str(pp_engineer))
# Unknown blueprint -> quality null and NOT flagged estimated (issue: was contradictory).
dss_eng = (capi_mods.get("Slot09_Size1", {}).get("engineering")) or {}
check("capi unknown-blueprint quality is null", dss_eng.get("quality") is None,
      str(dss_eng.get("quality")))
check("capi null quality not flagged estimated", dss_eng.get("quality_estimated") is False,
      str(dss_eng.get("quality_estimated")))
# Known blueprint, but its only varying stat is a resistance (skipped) -> same
# null + not-estimated result (no contradictory flag).
arm_eng = (capi_mods.get("Armour", {}).get("engineering")) or {}
check("capi resistance-only blueprint quality null + not estimated",
      arm_eng.get("quality") is None and arm_eng.get("quality_estimated") is False,
      f'{arm_eng.get("quality")}/{arm_eng.get("quality_estimated")}')
# Experimental display names normalised to the journal's Title Case.
check("capi experimental casing normalised (sentence -> title)",
      load._capi_special_effect({"special_phasing_sequence": "special_phasing_sequence"})
      == "Phasing Sequence",
      load._capi_special_effect({"special_phasing_sequence": "special_phasing_sequence"}))
check("capi experimental preserves acronyms",
      load._capi_special_effect({"special_fsd_interrupt": "special_fsd_interrupt"})
      == "FSD Interrupt",
      load._capi_special_effect({"special_fsd_interrupt": "special_fsd_interrupt"}))
check("capi experimental empty list -> None",
      load._capi_special_effect([]) is None, str(load._capi_special_effect([])))
check("capi FSD experimental friendly name", fsd_eng.get("experimental_effect") == "Mass Manager",
      str(fsd_eng.get("experimental_effect")))
check("capi quality estimated from roll (FSD G5 maxed)", fsd_eng.get("quality") == 1.0,
      str(fsd_eng.get("quality")))
check("capi quality flagged as estimated", fsd_eng.get("quality_estimated") is True,
      str(fsd_eng.get("quality_estimated")))
check("capi item lower-cased to match journal",
      capi_fsd.get("item") == "int_hyperdrive_size5_class5", str(capi_fsd.get("item")))
check("capi health normalised to 0..1", capi_fsd.get("health") == 1.0, str(capi_fsd.get("health")))
check("capi modifiers carry stripped labels + multipliers",
      any(m["label"] == "FSDOptimalMass" and m["multiplier"] == 1.55
          for m in fsd_eng.get("modifiers", [])), str(fsd_eng.get("modifiers")))
# Engineered module with no experimental effect ([] specialModifications) -> None.
pp_eng = (capi_mods.get("PowerPlant", {}).get("engineering")) or {}
check("capi engineered module w/o experimental -> None",
      pp_eng.get("blueprint") == "PowerPlant_Boosted" and pp_eng.get("experimental_effect") is None,
      str(pp_eng.get("experimental_effect")))
check("capi partial quality estimate", pp_eng.get("quality") == 0.5, str(pp_eng.get("quality")))
# Un-engineered module -> engineering is None.
check("capi un-engineered module -> engineering None",
      capi_mods.get("MainEngines", {}).get("engineering") is None,
      str(capi_mods.get("MainEngines", {}).get("engineering")))
check("capi fleet captured", len(capi.get("fleet") or []) == 1, str(len(capi.get("fleet") or [])))
check("capi surfaced in get_status", (srv.get_status().get("capi") or {}).get("status") == "received")

# --- Golden fixture: a real CAPI /profile dump (CMDR LEFT47, Loial / Krait_Light) ---
# Exercises low grades (G1/G2/G3), a freshly-rolled-to-a-lower-grade module
# (LargeHardpoint1 G2 vs the otherwise-identical LargeHardpoint2 G3), the
# engineer-disagreement pair (both Weapon_LightWeight, Mel Brandon vs The
# Dweller), resistance-skew (Slot02 shield), the null-quality estimator case
# (Slot09 Sensor_Expanded), and several specialModifications: [] modules.
with open(os.path.join(ROOT, "tests", "fixtures", "loial_capi_ship.json")) as fh:
    loial_ship = json.load(fh)
loial = load._capi_current_ship(loial_ship)  # no journal -> engineer == CAPI value
gm = {m["slot"]: m for m in loial["modules"]}

check("golden: ship type", loial["type"] == "Krait_Light", str(loial["type"]))
check("golden: engineered_module_count == 9", loial["engineered_module_count"] == 9,
      str(loial["engineered_module_count"]))

# FSD spot-check (the brief's parity anchor): Long Range G5, Mass Manager, q≈0.4.
g_fsd = gm["FrameShiftDrive"]
check("golden: FSD item lower-cased",
      g_fsd["item"] == "int_hyperdrive_overcharge_size5_class5", g_fsd["item"])
check("golden: FSD engineering", g_fsd["engineering"] == {
    "blueprint": "FSD_LongRange", "grade": 5, "quality": 0.4, "quality_estimated": True,
    "engineer": "Felicity Farseer", "engineer_last_roll": "Felicity Farseer",
    "experimental_effect": "Mass Manager",
    "modifiers": g_fsd["engineering"]["modifiers"]},  # modifiers checked separately
    str(g_fsd["engineering"]))
check("golden: FSD modifier multiplier form",
      {"label": "FSDOptimalMass", "multiplier": 1.49, "less_is_good": 0, "display": "49.00%"}
      in g_fsd["engineering"]["modifiers"], str(g_fsd["engineering"]["modifiers"]))

# Same blueprint, different engineer + grade (LargeHardpoint1 was re-rolled to G2).
check("golden: pulse LH2 grade/engineer",
      (gm["LargeHardpoint2"]["engineering"]["grade"], gm["LargeHardpoint2"]["engineering"]["engineer"])
      == (3, "The Dweller"), str(gm["LargeHardpoint2"]["engineering"]))
check("golden: pulse LH1 grade/engineer (fresher, lower grade)",
      (gm["LargeHardpoint1"]["engineering"]["grade"], gm["LargeHardpoint1"]["engineering"]["engineer"])
      == (2, "Mel Brandon"), str(gm["LargeHardpoint1"]["engineering"]))
check("golden: pulse experimental Stripped Down",
      gm["LargeHardpoint1"]["engineering"]["experimental_effect"] == "Stripped Down",
      str(gm["LargeHardpoint1"]["engineering"]["experimental_effect"]))

# Resistance-skew guard on real data: shield quality from ShieldGenStrength only.
check("golden: shield quality ignores resistances",
      gm["Slot02_Size5"]["engineering"]["quality"] == 1.0,
      str(gm["Slot02_Size5"]["engineering"]["quality"]))
check("golden: shield experimental Fast Charge",
      gm["Slot02_Size5"]["engineering"]["experimental_effect"] == "Fast Charge",
      str(gm["Slot02_Size5"]["engineering"]["experimental_effect"]))

# Estimator fall-throughs and low grades.
check("golden: DSS (Sensor_Expanded) quality null + not estimated",
      gm["Slot09_Size1"]["engineering"]["quality"] is None
      and gm["Slot09_Size1"]["engineering"]["quality_estimated"] is False,
      str(gm["Slot09_Size1"]["engineering"]))
check("golden: G1 power plant quality", gm["PowerPlant"]["engineering"]["quality"] == 1.0,
      str(gm["PowerPlant"]["engineering"]["quality"]))
check("golden: Radar partial quality", gm["Radar"]["engineering"]["quality"] == 0.333,
      str(gm["Radar"]["engineering"]["quality"]))
check("golden: experimental codenames resolved + null",
      gm["MainEngines"]["engineering"]["experimental_effect"] == "Drag Drives"
      and gm["PowerDistributor"]["engineering"]["experimental_effect"] == "Super Conduits"
      and gm["PowerPlant"]["engineering"]["experimental_effect"] is None,
      f'{gm["MainEngines"]["engineering"]["experimental_effect"]} / '
      f'{gm["PowerDistributor"]["engineering"]["experimental_effect"]}')
check("golden: un-engineered module -> engineering None",
      gm["Armour"]["engineering"] is None, str(gm["Armour"]["engineering"]))

# Engineer provenance merge against the real fixture: a journal Loadout naming a
# different originating engineer for the same fitted mod (item + blueprint match)
# wins for `engineer`; CAPI keeps `engineer_last_roll`.
loial_merged = load._capi_current_ship(loial_ship, {"Modules": [
    {"Slot": "LargeHardpoint1", "Item": "hpt_pulselaser_gimbal_large",
     "Engineering": {"BlueprintName": "Weapon_LightWeight", "Engineer": "Liz Ryder"}}]})
m_lh1 = {m["slot"]: m for m in loial_merged["modules"]}["LargeHardpoint1"]["engineering"]
check("golden+merge: engineer from journal, last_roll from CAPI",
      m_lh1["engineer"] == "Liz Ryder" and m_lh1["engineer_last_roll"] == "Mel Brandon",
      f'{m_lh1["engineer"]} / {m_lh1["engineer_last_roll"]}')

# --- Generic invariants across every real CAPI fixture ----------------------
# Each fixture is a real Save-Raw-Data ship. For every slot we assert the parse
# is internally consistent with the raw CAPI (so the parser, not the transcription,
# is what's under test): engineered iff a raw `engineer` block exists, blueprint/
# grade/engineer copied through, experimental codenames resolved (no `special_`
# leak), quality null-or-0..1 with an honest estimated flag, and item lower-cased.
def _capi_fixture(name):
    with open(os.path.join(ROOT, "tests", "fixtures", f"{name}_capi_ship.json")) as fh:
        return load._capi_current_ship(json.load(fh)), json.load(open(
            os.path.join(ROOT, "tests", "fixtures", f"{name}_capi_ship.json")))

EXPECTED_ENGINEERED = {"loial": 9, "aviendha": 17, "lan": 13}
for fixname, expected in EXPECTED_ENGINEERED.items():
    parsed, raw = _capi_fixture(fixname)
    pm = {m["slot"]: m for m in parsed["modules"]}
    issues, raw_eng = [], 0
    for slot, entry in raw["modules"].items():
        out, e = pm.get(slot, {}), pm.get(slot, {}).get("engineering")
        if out.get("item") != entry["module"]["name"].lower():
            issues.append(f"{slot}:item")
        eng_raw = entry.get("engineer")
        if isinstance(eng_raw, dict):
            raw_eng += 1
            if not e:
                issues.append(f"{slot}:null"); continue
            if e["blueprint"] != eng_raw.get("recipeName"): issues.append(f"{slot}:bp")
            if e["grade"] != eng_raw.get("recipeLevel"): issues.append(f"{slot}:grade")
            if e["engineer"] != eng_raw.get("engineerName"): issues.append(f"{slot}:eng")
            if e["engineer_last_roll"] != eng_raw.get("engineerName"): issues.append(f"{slot}:lastroll")
            if e["quality_estimated"] is not (e["quality"] is not None): issues.append(f"{slot}:flag")
            if e["quality"] is not None and not 0.0 <= e["quality"] <= 1.0: issues.append(f"{slot}:q={e['quality']}")
            if any(set(x) != {"label", "multiplier", "less_is_good", "display"} for x in e["modifiers"]):
                issues.append(f"{slot}:modkeys")
            spec = entry.get("specialModifications")
            resolved = e["experimental_effect"]
            if isinstance(spec, dict) and spec:
                if not resolved or resolved.startswith("special_"): issues.append(f"{slot}:exp={resolved}")
            elif resolved is not None:
                issues.append(f"{slot}:exp!None")
        elif e is not None:
            issues.append(f"{slot}:notNone")
    check(f"fixture {fixname}: {expected} engineered",
          parsed["engineered_module_count"] == expected and raw_eng == expected,
          f'{parsed["engineered_module_count"]} (raw {raw_eng})')
    check(f"fixture {fixname}: per-module invariants", not issues, "; ".join(issues[:8]))

# Targeted spot-checks for behaviours these two ships add over Loial.
av = {m["slot"]: m["engineering"] for m in _capi_fixture("aviendha")[0]["modules"] if m["engineering"]}
check("aviendha: armour hull-boost quality, resistances skipped", av["Armour"]["quality"] == 0.75,
      str(av["Armour"]["quality"]))
check("aviendha: overcharged G5 partial quality (0.6)", av["MediumHardpoint1"]["quality"] == 0.6,
      str(av["MediumHardpoint1"]["quality"]))
check("aviendha: Weapon_Efficient -> Phasing Sequence",
      av["LargeHardpoint1"]["experimental_effect"] == "Phasing Sequence",
      av["LargeHardpoint1"]["experimental_effect"])

ln = {m["slot"]: m["engineering"] for m in _capi_fixture("lan")[0]["modules"] if m["engineering"]}
check("lan: special_thermalshock -> Thermal Shock (casing)",
      ln["LargeHardpoint2"]["experimental_effect"] == "Thermal Shock",
      ln["LargeHardpoint2"]["experimental_effect"])
check("lan: special_powerplant_highcharge -> Monstered",
      ln["PowerPlant"]["experimental_effect"] == "Monstered", ln["PowerPlant"]["experimental_effect"])
check("lan: special_armour_chunky -> Deep Plating",
      ln["Armour"]["experimental_effect"] == "Deep Plating", ln["Armour"]["experimental_effect"])
check("lan: anomalous armour multiplier -> quality null (not clamped)",
      ln["Armour"]["quality"] is None and ln["Armour"]["quality_estimated"] is False,
      f'{ln["Armour"]["quality"]}/{ln["Armour"]["quality_estimated"]}')
check("lan: hull-reinforcement addition-only -> quality null + Layered Plating",
      ln["Slot02_Size4"]["quality"] is None
      and ln["Slot02_Size4"]["experimental_effect"] == "Layered Plating",
      f'{ln["Slot02_Size4"]["quality"]}/{ln["Slot02_Size4"]["experimental_effect"]}')

# Cooldown path: simulate EDMC having just queried (querytime ~ now) so the
# plugin reports cooldown instead of firing, and the MCP recommends a retry.
_cfg_store["querytime"] = int(time.time())  # last CAPI query was just now
def _fake_plugin_cooldown():
    for _ in range(100):
        time.sleep(0.1)
        load.CONNECTOR.poll_request()
        cap = load.CONNECTOR.snapshot.get("capi") or {}
        if cap.get("status") == "cooldown":
            load.CONNECTOR._flush()  # writer thread stopped; flush explicitly
            return

t2 = threading.Thread(target=_fake_plugin_cooldown, daemon=True)
t2.start()
cd = srv.request_capi_refresh(wait_seconds=12)
t2.join(timeout=15)
check("capi cooldown status", cd["status"] == "cooldown", str(cd.get("status")))
check("capi cooldown recommends retry",
      isinstance(cd.get("retry_after_seconds"), int) and 0 < cd["retry_after_seconds"] <= 61,
      str(cd.get("retry_after_seconds")))
check("capi cooldown did not fire", load.CONNECTOR._pending_nonce is None)
_cfg_store["querytime"] = 0  # reset so later/other runs aren't affected

# --- Fleet loadout cache -----------------------------------------------------
# A second ship gets a Loadout event; it should be cached and queryable even
# though it isn't the current ship.
SIDEWINDER_LOADOUT = {
    "timestamp": "2026-06-01T12:20:00Z", "event": "Loadout",
    "Ship": "sidewinder", "ShipID": 3, "ShipName": "Stiletto", "ShipIdent": "JT-99",
    "Modules": [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size2_class3", "On": True,
         "Priority": 0, "Health": 1.0,
         "Engineering": {"Engineer": "Felicity Farseer", "BlueprintName": "PowerPlant_Boosted",
                         "Level": 2, "Quality": 0.5}},
    ],
}
load.journal_entry("CMDR Jim", False, "Shinrarta Dezhra", "Jameson Memorial",
                   SIDEWINDER_LOADOUT, STATE)  # STATE still says current ship is the Corvette
load.CONNECTOR._flush()
sw = srv.get_ship_loadout("Stiletto")
check("ship loadout cache match by name", sw["matches"] == 1, str(sw.get("matches")))
check("ship loadout caches non-current ship",
      sw["ships"][0]["ship"]["type"] == "sidewinder", str(sw["ships"][0]["ship"].get("type")))
check("ship loadout keeps engineering", sw["ships"][0]["engineered_module_count"] == 1)
corv = srv.get_ship_loadout("corvette")
check("ship loadout match by type substring", corv["matches"] == 1
      and corv["ships"][0]["ship"]["type"] == "federation_corvette", str(corv.get("matches")))
miss = srv.get_ship_loadout("nonexistent_ship")
check("ship loadout reports cached ships on miss",
      miss["matches"] == 0 and len(miss["cached_ships"]) >= 2, str(miss.get("matches")))
fl = srv.get_fleet()
sw_fleet = next((s for s in fl["ships"] if str(s["ship_id"]) == "3"), None)
check("fleet flags detailed loadout", sw_fleet and sw_fleet["has_detailed_loadout"] is True,
      str(sw_fleet))

# Cache survives a restart: a fresh connector reads the snapshot back on start().
load.CONNECTOR._flush()
restarted = load._Connector()
restarted.start()
restored = restarted.snapshot.get("ship_loadouts") or {}
check("loadout cache restored on restart", set(restored.keys()) >= {"3", "7"}, str(sorted(restored)))
restarted.stop()

# --- Material trade calculator -----------------------------------------------
# Inventory (from STATE2): Raw has iron(G1), zinc(G2), tin(G3), antimony(G4),
# cadmium(G3). Encoded has scandatabanks(G1), etc.
load.CONNECTOR.snapshot["materials"] = {
    "raw": dict(STATE2["Raw"]), "manufactured": {}, "encoded": dict(STATE2["Encoded"])}
load.CONNECTOR._flush()

# Trade DOWN within the same raw group: antimony (G4, group 7) -> boron (G3, grp 7)
# is 1 source : 3 target (cheapest possible).
boron = srv.plan_material_trades("boron", quantity=3)
anti = next((o for o in boron["trade_options"] if o["source_symbol"] == "antimony"), None)
check("trade down same-subcat rate", anti and anti["source_per_target"] == round(1/3, 3),
      str(anti))
check("trade down need_to_trade rounds up", anti and anti["need_to_trade"] == 1, str(anti))

# Trade UP one grade, same raw group: boron is grp 7; antimony G4 from a G3... use
# tin(G3, grp? ) — instead test up within a group we know: zinc G2 grp4 -> cadmium
# G3 grp3 is cross-subcat + up 1 grade => 6 * 6 = 36 : 1.
cad = srv.plan_material_trades("cadmium", quantity=1)
zinc = next((o for o in cad["trade_options"] if o["source_symbol"] == "zinc"), None)
check("trade up cross-subcat rate is 36:1", zinc and zinc["source_per_target"] == 36.0, str(zinc))

# Type-locked: an Encoded target offers no Raw sources.
sdb = srv.plan_material_trades("scandatabanks", quantity=1)
check("trades are type-locked",
      all(srv._MAT_REF.get(o["source_symbol"], {}).get("type") == "Encoded"
          for o in sdb["trade_options"]), str([o["source_symbol"] for o in sdb["trade_options"]]))

# --- Click-to-update wiring --------------------------------------------------
# The installer records the repo path in install_info.json next to the plugin;
# the plugin reads it so the status label can launch the right update script.
fake_repo = tempfile.mkdtemp()
for _name in ("update.sh", "update.bat"):
    open(os.path.join(fake_repo, _name), "w").close()
fake_plugin_dir = tempfile.mkdtemp()
with open(os.path.join(fake_plugin_dir, "install_info.json"), "w") as fh:
    json.dump({"repo": fake_repo}, fh)
check("repo path read from install_info", load._read_repo_path(fake_plugin_dir) == fake_repo)
load._repo_path = load._read_repo_path(fake_plugin_dir)
up = load._updater_path()
check("updater path resolves to a script", up is not None
      and os.path.basename(up) in ("update.sh", "update.bat"), str(up))
load._repo_path = None
check("no updater path without repo info", load._updater_path() is None)
check("missing install_info yields no repo path",
      load._read_repo_path(tempfile.mkdtemp()) is None)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
