"""
EliteDangerousMCP — EDMarketConnector plugin.

Captures real-time ship loadout (with engineering modifications) and engineering
materials inventory from the game journal and writes them to a local JSON
snapshot file. A companion MCP server reads that file so an MCP client (Claude
Desktop, Ollama, …) can answer
questions about your loadouts and materials to help plan engineering runs.

All data stays on your machine. Nothing is sent anywhere by this plugin.
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from typing import Any, Optional

import myNotebook as nb  # type: ignore  # provided by EDMarketConnector
from config import config  # type: ignore  # provided by EDMarketConnector

try:
    from theme import theme  # type: ignore  # provided by EDMarketConnector
except Exception:  # pragma: no cover - older EDMC / running outside the app
    theme = None  # type: ignore

try:
    from EDMCLogging import get_main_logger  # type: ignore
    logger = get_main_logger()
except Exception:  # pragma: no cover - fallback for very old EDMC
    import logging
    logger = logging.getLogger("EliteDangerousMCP")

# --- Compatibility shims for pre-5.0.0 EDMC config API -----------------------
if not hasattr(config, "get_str"):
    config.get_str = config.get  # type: ignore[attr-defined]
if not hasattr(config, "get_bool"):
    config.get_bool = lambda key, default=False: bool(config.getint(key))  # type: ignore
if not hasattr(config, "get_int"):
    config.get_int = lambda key, default=0: config.getint(key)  # type: ignore

PLUGIN_NAME = "Elite Dangerous MCP"
VERSION = "0.8.2"
GITHUB_REPO = "Left47/EDMC-MCP"
CONFIG_PATH_KEY = "edclaude_state_path"
CONFIG_ENABLED_KEY = "edclaude_enabled"
WRITE_DEBOUNCE_SECONDS = 1.5
# How often (ms, main thread) we check for a queued CAPI-refresh request.
CAPI_POLL_MS = 2000
# Sibling file (next to the snapshot) the MCP server writes to request a refresh.
CAPI_REQUEST_FILE = "capi_request.json"

# Set by the background update check; surfaced on the main-window label.
_update_available: Optional[str] = None


def _check_for_update() -> None:
    """Best-effort: compare VERSION against the latest GitHub release tag."""
    global _update_available
    try:
        import requests  # bundled with EDMC
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", timeout=10)
        latest = resp.json().get("tag_name", "").lstrip("v")
        if latest and _version_tuple(latest) > _version_tuple(VERSION):
            _update_available = latest
            logger.info(f"EliteDangerousMCP: update available: v{latest} (have v{VERSION})")
            # NB: do not touch tkinter here — this runs on a worker thread.
            # plugin_app schedules a label refresh on the main loop instead.
    except Exception as exc:  # never disrupt the app over an update check
        logger.debug(f"EliteDangerousMCP update check skipped: {exc}")


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)

# Materials supplied via the journal `state` dict are organised into these keys.
MATERIAL_BUCKETS = {
    "raw": "Raw",
    "manufactured": "Manufactured",
    "encoded": "Encoded",
}


def default_state_path() -> str:
    """Default snapshot location, shared with the MCP server's default."""
    return os.path.join(os.path.expanduser("~"), ".elite-dangerous-claude", "state.json")


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_path(state_path: str) -> str:
    """Path of the refresh-request file the MCP server writes next to the snapshot."""
    return os.path.join(os.path.dirname(state_path) or ".", CAPI_REQUEST_FILE)


def _read_request(state_path: str) -> Optional[dict[str, Any]]:
    try:
        with open(_request_path(state_path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# Fallback if the EDMC-provided constant can't be imported (matches EDMC's value).
_DEFAULT_CAPI_COOLDOWN = 60


def _capi_cooldown_remaining() -> float:
    """Seconds until EDMC will allow another live CAPI query — based on the
    timestamp of the last query (which EDMC persists to config as 'querytime',
    bumped on every CAPI request including its own automatic ones) and Frontier's
    global cooldown. Returns 0.0 when a query is allowed right now. Runs on the
    same machine as EDMC, so the clocks match."""
    try:
        from companion import capi_query_cooldown as cooldown  # type: ignore
    except Exception:
        cooldown = _DEFAULT_CAPI_COOLDOWN
    try:
        last = config.get_int("querytime", default=0)
    except Exception:
        last = 0
    if not last:
        return 0.0
    remaining = (last + cooldown) - time.time()
    return remaining if remaining > 0 else 0.0


def _iso_in(seconds: float) -> str:
    """ISO-8601 UTC timestamp `seconds` from now."""
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Frontier CAPI parsing ---------------------------------------------------
# CAPI ("Companion API") data arrives via the cmdr_data hook and carries the
# authoritative live loadout/fleet straight from Frontier — including the live
# engineering the game doesn't always re-emit to the journal. Its shape differs
# from the journal's, so we normalise it here to the same engineering summary the
# MCP server builds from journal Loadout events (_engineering_summary in
# ed_claude_mcp.py), so the CAPI and journal paths are interchangeable.
#
# CAPI engineering lives on the *slot* entry (a sibling of "module"), not inside
# the module:
#     "PowerPlant": {
#         "module": {"name": "Int_Powerplant_Size6_Class5", "health": 1000000, ...},
#         "engineer": {"engineerName": "Etienne Dorn",
#                      "recipeName": "PowerPlant_Boosted", "recipeLevel": 5},
#         "WorkInProgress_modifications": {
#             "OutfittingFieldType_PowerCapacity": {"value": 1.4, "LessIsGood": false, ...}, ...},
#         "specialModifications": {"special_..": "special_.."}  # or [] when none
#     }
# CAPI omits two things the journal has. The per-stat *absolute* before/after
# values can't be recovered (CAPI only gives multipliers, 1.4 = +40%), so we
# surface the multiplier. Blueprint *quality* is also absent — but quality scales
# the roll linearly between each stat's grade minimum and maximum, so we
# *estimate* it from how far the modified stats sit into their grade's range
# (_capi_quality, using ranges from EDCD/coriolis-data).

# Experimental ("special") effect codename -> friendly name, matching the
# journal's ExperimentalEffect_Localised. Sourced from EDCD/coriolis-data
# (modifications/specials.json).
_SPECIAL_EFFECTS = {
    "special_armour_chunky": "Deep Plating",
    "special_armour_explosive": "Layered Plating",
    "special_armour_kinetic": "Angled Plating",
    "special_armour_thermic": "Reflective Plating",
    "special_auto_loader": "Auto loader",
    "special_blinding_shell": "Dazzle shell",
    "special_choke_canister": "Choke canister",
    "special_concordant_sequence": "Concordant sequence",
    "special_corrosive_shell": "Corrosive shell",
    "special_deep_cut_payload": "Penetrator Payload",
    "special_dispersal_field": "Dispersal field",
    "special_distortion_field": "Inertial impact",
    "special_drag_munitions": "Drag munitions",
    "special_emissive_munitions": "Emissive munitions",
    "special_engine_cooled": "Thermal Spread",
    "special_engine_haulage": "Drive Distributors",
    "special_engine_lightweight": "Stripped Down",
    "special_engine_overloaded": "Drag Drives",
    "special_engine_toughened": "Double Braced",
    "special_feedback_cascade": "Feedback cascade (Legacy)",
    "special_feedback_cascade_cooled": "Feedback Cascade",
    "special_force_shell": "Force shell",
    "special_fsd_cooled": "Thermal Spread",
    "special_fsd_fuelcapacity": "Deep Charge",
    "special_fsd_heavy": "Mass Manager",
    "special_fsd_interrupt": "FSD interrupt",
    "special_fsd_lightweight": "Stripped Down",
    "special_fsd_toughened": "Double Braced",
    "special_high_yield_shell": "High yield shell",
    "special_hullreinforcement_chunky": "Deep Plating",
    "special_hullreinforcement_explosive": "Layered Plating",
    "special_hullreinforcement_kinetic": "Angled Plating",
    "special_hullreinforcement_thermic": "Reflective Plating",
    "special_incendiary_rounds": "Incendiary rounds",
    "special_ion_disruptor": "Ion disruptor",
    "special_lock_breaker": "Target lock breaker",
    "special_mass_lock": "Mass Lock Munition",
    "special_mass_lock_munition": "Mass lock munition",
    "special_overload_munitions": "Overload munitions",
    "special_penetrator_munitions": "Penetrator Munitions",
    "special_penetrator_payload": "Penetrator payload",
    "special_phasing_sequence": "Phasing sequence",
    "special_plasma_slug": "Plasma slug (Legacy)",
    "special_plasma_slug_cooled": "Plasma Slug",
    "special_plasma_slug_pa": "Plasma Slug",
    "special_powerdistributor_capacity": "Cluster Capacitors",
    "special_powerdistributor_efficient": "Flow Control",
    "special_powerdistributor_fast": "Super Conduits",
    "special_powerdistributor_lightweight": "Stripped Down",
    "special_powerdistributor_toughened": "Double Braced",
    "special_powerplant_cooled": "Thermal Spread",
    "special_powerplant_highcharge": "Monstered",
    "special_powerplant_lightweight": "Stripped Down",
    "special_powerplant_toughened": "Double Braced",
    "special_radiant_canister": "Radiant Canister",
    "special_regeneration_sequence": "Regeneration sequence",
    "special_reverberating_cascade": "Reverberating cascade",
    "special_scramble_spectrum": "Scramble spectrum",
    "special_screening_shell": "Screening shell",
    "special_shield_efficient": "Lo-draw",
    "special_shield_health": "Hi-Cap",
    "special_shield_kinetic": "Force Block",
    "special_shield_lightweight": "Stripped Down",
    "special_shield_regenerative": "Fast Charge",
    "special_shield_resistive": "Multi-weave",
    "special_shield_thermic": "Thermo Block",
    "special_shield_toughened": "Double Braced",
    "special_shieldbooster_chunky": "Super Capacitors",
    "special_shieldbooster_efficient": "Flow Control",
    "special_shieldbooster_explosive": "Blast Block",
    "special_shieldbooster_kinetic": "Force Block",
    "special_shieldbooster_thermic": "Thermo Block",
    "special_shieldbooster_toughened": "Double Braced",
    "special_shieldcell_efficient": "Flow Control",
    "special_shieldcell_gradual": "Recycling Cell",
    "special_shieldcell_lightweight": "Stripped Down",
    "special_shieldcell_oversized": "Boss Cells",
    "special_shieldcell_toughened": "Double Braced",
    "special_shiftlock_canister": "Shift-lock canister",
    "special_smart_rounds": "Smart rounds",
    "special_super_penetrator": "Super penetrator (Legacy)",
    "special_super_penetrator_cooled": "Super Penetrator",
    "special_thermal_cascade": "Thermal cascade",
    "special_thermal_conduit": "Thermal conduit",
    "special_thermal_vent": "Thermal vent",
    "special_thermalshock": "Thermal shock",
    "special_weapon_damage": "Oversized",
    "special_weapon_efficient": "Flow Control",
    "special_weapon_lightweight": "Stripped Down",
    "special_weapon_rateoffire": "Multi-servos",
    "special_weapon_toughened": "Double Braced",
}

_OFT_PREFIX = "OutfittingFieldType_"

# Per-blueprint, per-grade [min, max] *fractional* ranges for each stat that
# varies with quality, keyed by the CAPI stat name (OutfittingFieldType_ prefix
# stripped). Used by _capi_quality to estimate quality from the live multipliers.
# Generated from EDCD/coriolis-data (modifications/blueprints.json joined with
# modifierActions.json, keeping only stats whose range actually varies). Refresh
# by re-running that join when the game adds blueprints.
_BP_QUALITY_RANGES = {
    "AFM_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3.0)}},
    "Armour_Advanced": {"1":{"ExplosiveResistance":(0,0.03),"KineticResistance":(0,0.03),"Mass":(0,-0.15),"ThermicResistance":(0,0.03)}, "2":{"ExplosiveResistance":(0.03,0.06),"KineticResistance":(0.03,0.06),"Mass":(-0.15,-0.25),"ThermicResistance":(0.03,0.06)}, "3":{"ExplosiveResistance":(0.06,0.09),"KineticResistance":(0.06,0.09),"Mass":(-0.25,-0.35),"ThermicResistance":(0.06,0.09)}, "4":{"ExplosiveResistance":(0.09,0.12),"KineticResistance":(0.09,0.12),"Mass":(-0.35,-0.45),"ThermicResistance":(0.09,0.12)}, "5":{"ExplosiveResistance":(0.12,0.15),"KineticResistance":(0.12,0.15),"Mass":(-0.45,-0.55),"ThermicResistance":(0.12,0.15)}},
    "Armour_Explosive": {"1":{"ExplosiveResistance":(0,0.12)}, "2":{"ExplosiveResistance":(0.12,0.19)}, "3":{"ExplosiveResistance":(0.19,0.26)}, "4":{"ExplosiveResistance":(0.26,0.33)}, "5":{"ExplosiveResistance":(0.33,0.4)}},
    "Armour_HeavyDuty": {"1":{"DefenceModifierHealthMultiplier":(0,0.12),"ExplosiveResistance":(0,0.01),"KineticResistance":(0,0.01),"ThermicResistance":(0,0.01)}, "2":{"DefenceModifierHealthMultiplier":(0.12,0.17),"ExplosiveResistance":(0.01,0.02),"KineticResistance":(0.01,0.02),"ThermicResistance":(0.01,0.02)}, "3":{"DefenceModifierHealthMultiplier":(0.17,0.22),"ExplosiveResistance":(0.02,0.03),"KineticResistance":(0.02,0.03),"ThermicResistance":(0.02,0.03)}, "4":{"DefenceModifierHealthMultiplier":(0.22,0.27),"ExplosiveResistance":(0.03,0.04),"KineticResistance":(0.03,0.04),"ThermicResistance":(0.03,0.04)}, "5":{"DefenceModifierHealthMultiplier":(0.27,0.32),"ExplosiveResistance":(0.04,0.05),"KineticResistance":(0.04,0.05),"ThermicResistance":(0.04,0.05)}},
    "Armour_Kinetic": {"1":{"KineticResistance":(0,0.12)}, "2":{"KineticResistance":(0.12,0.19)}, "3":{"KineticResistance":(0.19,0.26)}, "4":{"KineticResistance":(0.26,0.33)}, "5":{"KineticResistance":(0.33,0.4)}},
    "Armour_Thermic": {"1":{"ThermicResistance":(0,0.12)}, "2":{"ThermicResistance":(0.12,0.19)}, "3":{"ThermicResistance":(0.19,0.26)}, "4":{"ThermicResistance":(0.26,0.33)}, "5":{"ThermicResistance":(0.33,0.4)}},
    "CollectionLimpet_LightWeight": {"1":{"Mass":(0,-0.45)}, "2":{"Mass":(-0.45,-0.55)}, "3":{"Mass":(-0.55,-0.65)}, "4":{"Mass":(-0.65,-0.75)}, "5":{"Mass":(-0.75,-0.85)}},
    "CollectionLimpet_Reinforced": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "CollectionLimpet_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "Engine_Dirty": {"1":{"EngineOptPerformance":(0,0.12),"ShieldGenStrength":(0,0.12)}, "2":{"EngineOptPerformance":(0.12,0.19),"ShieldGenStrength":(0.12,0.19)}, "3":{"EngineOptPerformance":(0.19,0.26),"ShieldGenStrength":(0.19,0.26)}, "4":{"EngineOptPerformance":(0.26,0.33),"ShieldGenStrength":(0.26,0.33)}, "5":{"EngineOptPerformance":(0.33,0.4),"ShieldGenStrength":(0.33,0.4)}},
    "Engine_Reinforced": {"1":{"Integrity":(0,0.3)}, "2":{"EngineHeatRate":(-0.1,-0.2),"FSDHeatRate":(-0.1,-0.2),"Integrity":(0.3,0.5),"ShieldBankHeat":(-0.1,-0.2),"ThermalLoad":(-0.1,-0.2)}, "3":{"EngineHeatRate":(-0.2,-0.3),"FSDHeatRate":(-0.2,-0.3),"Integrity":(0.5,0.7),"ShieldBankHeat":(-0.2,-0.3),"ThermalLoad":(-0.2,-0.3)}, "4":{"EngineHeatRate":(-0.3,-0.4),"FSDHeatRate":(-0.3,-0.4),"Integrity":(0.7,0.9),"ShieldBankHeat":(-0.3,-0.4),"ThermalLoad":(-0.3,-0.4)}, "5":{"EngineHeatRate":(-0.4,-0.5),"FSDHeatRate":(-0.4,-0.5),"Integrity":(0.9,1.1),"ShieldBankHeat":(-0.4,-0.5),"ThermalLoad":(-0.4,-0.5)}},
    "Engine_Tuned": {"1":{"EngineHeatRate":(0,-0.2),"EngineOptPerformance":(0,0.08),"FSDHeatRate":(0,-0.2),"ShieldBankHeat":(0,-0.2),"ShieldGenStrength":(0,0.08),"ThermalLoad":(0,-0.2)}, "2":{"EngineHeatRate":(-0.2,-0.3),"EngineOptPerformance":(0.08,0.13),"FSDHeatRate":(-0.2,-0.3),"ShieldBankHeat":(-0.2,-0.3),"ShieldGenStrength":(0.08,0.13),"ThermalLoad":(-0.2,-0.3)}, "3":{"EngineHeatRate":(-0.3,-0.4),"EngineOptPerformance":(0.13,0.18),"FSDHeatRate":(-0.3,-0.4),"ShieldBankHeat":(-0.3,-0.4),"ShieldGenStrength":(0.13,0.18),"ThermalLoad":(-0.3,-0.4)}, "4":{"EngineHeatRate":(-0.4,-0.5),"EngineOptPerformance":(0.18,0.23),"FSDHeatRate":(-0.4,-0.5),"ShieldBankHeat":(-0.4,-0.5),"ShieldGenStrength":(0.18,0.23),"ThermalLoad":(-0.4,-0.5)}, "5":{"EngineHeatRate":(-0.5,-0.6),"EngineOptPerformance":(0.23,0.28),"FSDHeatRate":(-0.5,-0.6),"ShieldBankHeat":(-0.5,-0.6),"ShieldGenStrength":(0.23,0.28),"ThermalLoad":(-0.5,-0.6)}},
    "FSD_FastBoot": {"1":{"BootTime":(0,-0.2),"EngineOptimalMass":(0,0.03),"FSDOptimalMass":(0,0.03),"ShieldGenOptimalMass":(0,0.03)}, "2":{"BootTime":(-0.2,-0.35),"EngineOptimalMass":(0.03,0.06),"FSDOptimalMass":(0.03,0.06),"ShieldGenOptimalMass":(0.03,0.06)}, "3":{"BootTime":(-0.35,-0.5),"EngineOptimalMass":(0.06,0.09),"FSDOptimalMass":(0.06,0.09),"ShieldGenOptimalMass":(0.06,0.09)}, "4":{"BootTime":(-0.5,-0.65),"EngineOptimalMass":(0.09,0.12),"FSDOptimalMass":(0.09,0.12),"ShieldGenOptimalMass":(0.09,0.12)}, "5":{"BootTime":(-0.65,-0.8),"EngineOptimalMass":(0.12,0.15),"FSDOptimalMass":(0.12,0.15),"ShieldGenOptimalMass":(0.12,0.15)}},
    "FSD_LongRange": {"1":{"EngineOptimalMass":(0,0.15),"FSDOptimalMass":(0,0.15),"ShieldGenOptimalMass":(0,0.15)}, "2":{"EngineOptimalMass":(0.15,0.25),"FSDOptimalMass":(0.15,0.25),"ShieldGenOptimalMass":(0.15,0.25)}, "3":{"EngineOptimalMass":(0.25,0.35),"FSDOptimalMass":(0.25,0.35),"ShieldGenOptimalMass":(0.25,0.35)}, "4":{"EngineOptimalMass":(0.35,0.45),"FSDOptimalMass":(0.35,0.45),"ShieldGenOptimalMass":(0.35,0.45)}, "5":{"EngineOptimalMass":(0.45,0.55),"FSDOptimalMass":(0.45,0.55),"ShieldGenOptimalMass":(0.45,0.55)}},
    "FSD_Shielded": {"1":{"EngineHeatRate":(0,-0.1),"EngineOptimalMass":(0,0.03),"FSDHeatRate":(0,-0.1),"FSDOptimalMass":(0,0.03),"Integrity":(0,0.25),"ShieldBankHeat":(0,-0.1),"ShieldGenOptimalMass":(0,0.03),"ThermalLoad":(0,-0.1)}, "2":{"EngineHeatRate":(-0.1,-0.15),"EngineOptimalMass":(0.03,0.06),"FSDHeatRate":(-0.1,-0.15),"FSDOptimalMass":(0.03,0.06),"Integrity":(0.25,0.5),"ShieldBankHeat":(-0.1,-0.15),"ShieldGenOptimalMass":(0.03,0.06),"ThermalLoad":(-0.1,-0.15)}, "3":{"EngineHeatRate":(-0.15,-0.2),"EngineOptimalMass":(0.06,0.09),"FSDHeatRate":(-0.15,-0.2),"FSDOptimalMass":(0.06,0.09),"Integrity":(0.5,0.75),"ShieldBankHeat":(-0.15,-0.2),"ShieldGenOptimalMass":(0.06,0.09),"ThermalLoad":(-0.15,-0.2)}, "4":{"EngineHeatRate":(-0.2,-0.25),"EngineOptimalMass":(0.09,0.12),"FSDHeatRate":(-0.2,-0.25),"FSDOptimalMass":(0.09,0.12),"Integrity":(0.75,1),"ShieldBankHeat":(-0.2,-0.25),"ShieldGenOptimalMass":(0.09,0.12),"ThermalLoad":(-0.2,-0.25)}, "5":{"EngineHeatRate":(-0.25,-0.3),"EngineOptimalMass":(0.12,0.15),"FSDHeatRate":(-0.25,-0.3),"FSDOptimalMass":(0.12,0.15),"Integrity":(1,1.25),"ShieldBankHeat":(-0.25,-0.3),"ShieldGenOptimalMass":(0.12,0.15),"ThermalLoad":(-0.25,-0.3)}},
    "FSDinterdictor_Expanded": {"1":{"FSDInterdictorFacingLimit":(0,0.4),"FSDInterdictorRange":(-0.1,0.1)}, "2":{"FSDInterdictorFacingLimit":(0.4,0.6)}, "3":{"FSDInterdictorFacingLimit":(0.6,0.8)}, "4":{"FSDInterdictorFacingLimit":(0.8,1)}, "5":{"FSDInterdictorFacingLimit":(1,1.2)}},
    "FSDinterdictor_LongRange": {"1":{"FSDInterdictorRange":(0,0.2)}, "2":{"FSDInterdictorRange":(0.2,0.3)}, "3":{"FSDInterdictorRange":(0.3,0.4)}, "4":{"FSDInterdictorRange":(0.4,0.5)}, "5":{"FSDInterdictorRange":(0.5,0.6)}},
    "FuelScoop_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3.0)}},
    "FuelTransferLimpet_LightWeight": {"1":{"Mass":(0,-0.45)}, "2":{"Mass":(-0.45,-0.55)}, "3":{"Mass":(-0.55,-0.65)}, "4":{"Mass":(-0.65,-0.75)}, "5":{"Mass":(-0.75,-0.85)}},
    "FuelTransferLimpet_Reinforced": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "FuelTransferLimpet_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "HatchBreakerLimpet_LightWeight": {"1":{"Mass":(0,-0.45)}, "2":{"Mass":(-0.45,-0.55)}, "3":{"Mass":(-0.55,-0.65)}, "4":{"Mass":(-0.65,-0.75)}, "5":{"Mass":(-0.75,-0.85)}},
    "HatchBreakerLimpet_Reinforced": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "HatchBreakerLimpet_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "HullReinforcement_Advanced": {"1":{"DefenceModifierHealthMultiplier":(0,0.08),"Mass":(0,-0.08)}, "2":{"DefenceModifierHealthMultiplier":(0.08,0.12),"Mass":(-0.08,-0.12)}, "3":{"DefenceModifierHealthMultiplier":(0.12,0.16),"Mass":(-0.12,-0.16)}, "4":{"DefenceModifierHealthMultiplier":(0.16,0.2),"Mass":(-0.16,-0.2)}, "5":{"DefenceModifierHealthMultiplier":(0.2,0.24),"Mass":(-0.2,-0.24)}},
    "HullReinforcement_Explosive": {"1":{"DefenceModifierHealthAddition":(0,0.03),"ExplosiveResistance":(0,0.12)}, "2":{"DefenceModifierHealthAddition":(0.03,0.06),"ExplosiveResistance":(0.12,0.19)}, "3":{"DefenceModifierHealthAddition":(0.06,0.09),"ExplosiveResistance":(0.19,0.26)}, "4":{"DefenceModifierHealthAddition":(0.09,0.12),"ExplosiveResistance":(0.26,0.33)}, "5":{"DefenceModifierHealthAddition":(0.12,0.15),"ExplosiveResistance":(0.33,0.4)}},
    "HullReinforcement_HeavyDuty": {"1":{"DefenceModifierHealthAddition":(0,0.24),"ExplosiveResistance":(0,0.03),"KineticResistance":(0,0.03),"ThermicResistance":(0,0.03)}, "2":{"DefenceModifierHealthAddition":(0.24,0.36),"ExplosiveResistance":(0.03,0.06),"KineticResistance":(0.03,0.06),"ThermicResistance":(0.03,0.06)}, "3":{"DefenceModifierHealthAddition":(0.36,0.48),"ExplosiveResistance":(0.06,0.09),"KineticResistance":(0.06,0.09),"ThermicResistance":(0.06,0.09)}, "4":{"DefenceModifierHealthAddition":(0.48,0.6),"ExplosiveResistance":(0.09,0.12),"KineticResistance":(0.09,0.12),"ThermicResistance":(0.09,0.12)}, "5":{"DefenceModifierHealthAddition":(0.6,0.72),"ExplosiveResistance":(0.12,0.15),"KineticResistance":(0.12,0.15),"ThermicResistance":(0.12,0.15)}},
    "HullReinforcement_Kinetic": {"1":{"DefenceModifierHealthAddition":(0,0.03),"KineticResistance":(0,0.12)}, "2":{"DefenceModifierHealthAddition":(0.03,0.06),"KineticResistance":(0.12,0.19)}, "3":{"DefenceModifierHealthAddition":(0.06,0.09),"KineticResistance":(0.19,0.26)}, "4":{"DefenceModifierHealthAddition":(0.09,0.12),"KineticResistance":(0.26,0.33)}, "5":{"DefenceModifierHealthAddition":(0.12,0.15),"KineticResistance":(0.33,0.4)}},
    "HullReinforcement_Thermic": {"1":{"DefenceModifierHealthAddition":(0,0.03),"ThermicResistance":(0,0.12)}, "2":{"DefenceModifierHealthAddition":(0.03,0.06),"ThermicResistance":(0.12,0.19)}, "3":{"DefenceModifierHealthAddition":(0.06,0.09),"ThermicResistance":(0.19,0.26)}, "4":{"DefenceModifierHealthAddition":(0.09,0.12),"ThermicResistance":(0.26,0.33)}, "5":{"DefenceModifierHealthAddition":(0.12,0.15),"ThermicResistance":(0.33,0.4)}},
    "LifeSupport_LightWeight": {"1":{"Mass":(0,-0.45)}, "2":{"Mass":(-0.45,-0.55)}, "3":{"Mass":(-0.55,-0.65)}, "4":{"Mass":(-0.65,-0.75)}, "5":{"Mass":(-0.75,-0.85)}},
    "LifeSupport_Reinforced": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "LifeSupport_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3.0)}},
    "MC_Overcharged": {"1":{"Damage":(0,0.3)}, "2":{"Damage":(0.3,0.4)}, "3":{"Damage":(0.4,0.5)}, "4":{"Damage":(0.5,0.6)}, "5":{"Damage":(0.6,0.7)}},
    "Misc_ChaffCapacity": {"1":{"AmmoMaximum":(0,0.5)}},
    "Misc_LightWeight": {"1":{"Mass":(0,-0.45)}, "2":{"Mass":(-0.45,-0.55)}, "3":{"Mass":(-0.55,-0.65)}, "4":{"Mass":(-0.65,-0.75)}, "5":{"Mass":(-0.75,-0.85)}},
    "Misc_PointDefenseCapacity": {"1":{"AmmoMaximum":(0,0.5)}},
    "Misc_Reinforced": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "Misc_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "PowerDistributor_HighCapacity": {"1":{"EnginesCapacity":(0,0.1),"Integrity":(0,0.1),"SystemsCapacity":(0,0.1),"WeaponsCapacity":(0,0.1)}, "2":{"EnginesCapacity":(0.1,0.18),"Integrity":(0.1,0.18),"SystemsCapacity":(0.08,0.1),"WeaponsCapacity":(0.1,0.18)}, "3":{"EnginesCapacity":(0.18,0.26),"Integrity":(0.15,0.2),"SystemsCapacity":(0.18,0.26),"WeaponsCapacity":(0.18,0.26)}, "4":{"EnginesCapacity":(0.26,0.34),"Integrity":(0.2,0.25),"SystemsCapacity":(0.26,0.34),"WeaponsCapacity":(0.26,0.34)}, "5":{"EnginesCapacity":(0.34,0.42),"Integrity":(0.25,0.3),"SystemsCapacity":(0.34,0.42),"WeaponsCapacity":(0.34,0.42)}},
    "PowerDistributor_HighFrequency": {"1":{"EnginesRecharge":(0,0.09),"SystemsRecharge":(0,0.09),"WeaponsRecharge":(0,0.09)}, "2":{"EnginesRecharge":(0.09,0.18),"SystemsRecharge":(0.09,0.18),"WeaponsRecharge":(0.09,0.18)}, "3":{"EnginesRecharge":(0.18,0.27),"SystemsRecharge":(0.18,0.27),"WeaponsRecharge":(0.18,0.27)}, "4":{"EnginesRecharge":(0.27,0.36),"SystemsRecharge":(0.27,0.36),"WeaponsRecharge":(0.27,0.36)}, "5":{"EnginesRecharge":(0.36,0.45),"SystemsRecharge":(0.36,0.45),"WeaponsRecharge":(0.36,0.45)}},
    "PowerDistributor_PriorityEngines": {"1":{"EnginesCapacity":(0,0.2),"EnginesRecharge":(0,0.16)}, "2":{"EnginesCapacity":(0.2,0.3),"EnginesRecharge":(0.16,0.23)}, "3":{"EnginesCapacity":(0.3,0.4),"EnginesRecharge":(0.23,0.3)}, "4":{"EnginesCapacity":(0.4,0.5),"EnginesRecharge":(0.3,0.37)}, "5":{"EnginesCapacity":(0.5,0.6),"EnginesRecharge":(0.37,0.44)}},
    "PowerDistributor_PrioritySystems": {"1":{"SystemsCapacity":(0,0.2),"SystemsRecharge":(0,0.16)}, "2":{"SystemsCapacity":(0.2,0.3),"SystemsRecharge":(0.16,0.23)}, "3":{"SystemsCapacity":(0.3,0.4),"SystemsRecharge":(0.23,0.3)}, "4":{"SystemsCapacity":(0.4,0.5),"SystemsRecharge":(0.3,0.37)}, "5":{"SystemsCapacity":(0.5,0.6),"SystemsRecharge":(0.37,0.44)}},
    "PowerDistributor_PriorityWeapons": {"1":{"WeaponsCapacity":(0,0.2),"WeaponsRecharge":(0,0.16)}, "2":{"WeaponsCapacity":(0.2,0.3),"WeaponsRecharge":(0.16,0.23)}, "3":{"WeaponsCapacity":(0.3,0.4),"WeaponsRecharge":(0.23,0.3)}, "4":{"WeaponsCapacity":(0.4,0.5),"WeaponsRecharge":(0.3,0.37)}, "5":{"WeaponsCapacity":(0.5,0.6),"WeaponsRecharge":(0.37,0.44)}},
    "PowerDistributor_Shielded": {"1":{"Integrity":(0,0.4),"PowerDraw":(0,-0.1)}, "2":{"Integrity":(0.4,0.8),"PowerDraw":(-0.1,-0.15)}, "3":{"Integrity":(0.8,1.2),"PowerDraw":(-0.15,-0.2)}, "4":{"Integrity":(1.2,1.6),"PowerDraw":(-0.2,-0.25)}, "5":{"Integrity":(1.6,2),"PowerDraw":(-0.25,-0.3)}},
    "PowerPlant_Armoured": {"1":{"HeatEfficiency":(0,-0.04),"Integrity":(0,0.4),"PowerCapacity":(0,0.04)}, "2":{"HeatEfficiency":(-0.04,-0.06),"Integrity":(0.3,0.6),"PowerCapacity":(0,0.06)}, "3":{"HeatEfficiency":(-0.06,-0.08),"Integrity":(0.6,0.8),"PowerCapacity":(0.06,0.08)}, "4":{"HeatEfficiency":(-0.08,-0.1),"Integrity":(0.5,1),"PowerCapacity":(0.08,0.1)}, "5":{"HeatEfficiency":(-0.1,-0.12),"Integrity":(1,1.2),"PowerCapacity":(0.1,0.12)}},
    "PowerPlant_Boosted": {"1":{"PowerCapacity":(0,0.12)}, "2":{"PowerCapacity":(0.12,0.19)}, "3":{"PowerCapacity":(0.19,0.26)}, "4":{"PowerCapacity":(0.26,0.33)}, "5":{"PowerCapacity":(0.33,0.4)}},
    "PowerPlant_Stealth": {"1":{"HeatEfficiency":(0,-0.25)}, "2":{"HeatEfficiency":(-0.25,-0.35)}, "3":{"HeatEfficiency":(-0.35,-0.45)}, "4":{"HeatEfficiency":(-0.45,-0.55)}, "5":{"HeatEfficiency":(-0.55,-0.65)}},
    "ProspectingLimpet_LightWeight": {"1":{"Mass":(0,-0.45)}, "2":{"Mass":(-0.45,-0.55)}, "3":{"Mass":(-0.55,-0.65)}, "4":{"Mass":(-0.65,-0.75)}, "5":{"Mass":(-0.75,-0.85)}},
    "ProspectingLimpet_Reinforced": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "ProspectingLimpet_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3)}},
    "Refineries_Shielded": {"1":{"Integrity":(0,0.6)}, "2":{"Integrity":(0.6,1.2)}, "3":{"Integrity":(1.2,1.8)}, "4":{"Integrity":(1.8,2.4)}, "5":{"Integrity":(2.4,3.0)}},
    "Scanner_LongRange": {"1":{"MaximumRange":(0,0.24),"Range":(0,0.24),"ScannerRange":(0,0.24)}, "2":{"MaximumRange":(0.24,0.48),"Range":(0.24,0.48),"ScannerRange":(0.24,0.48)}, "3":{"MaximumRange":(0.48,0.72),"Range":(0.48,0.72),"ScannerRange":(0.48,0.72)}, "4":{"MaximumRange":(0.72,0.96),"Range":(0.72,0.96),"ScannerRange":(0.72,0.96)}, "5":{"MaximumRange":(0.96,1.2),"Range":(0.96,1.2),"ScannerRange":(0.96,1.2)}},
    "Scanner_WideAngle": {"1":{"MaxAngle":(0,0.4),"SensorTargetScanAngle":(0,0.4)}, "2":{"MaxAngle":(0.4,0.8),"SensorTargetScanAngle":(0.4,0.8)}, "3":{"MaxAngle":(0.8,1.2),"SensorTargetScanAngle":(0.8,1.2)}, "4":{"MaxAngle":(1.2,1.6),"SensorTargetScanAngle":(1.2,1.6)}, "5":{"MaxAngle":(1.6,2),"SensorTargetScanAngle":(1.6,2)}},
    "Sensor_FastScan": {"1":{"ScannerTimeToScan":(0,-0.2)}, "2":{"ScannerTimeToScan":(-0.2,-0.35)}, "3":{"ScannerTimeToScan":(-0.35,-0.5)}, "4":{"ScannerTimeToScan":(-0.5,-0.65)}},
    "Sensor_LightWeight": {"1":{"Mass":(0,-0.2)}, "2":{"Mass":(-0.2,-0.35)}, "3":{"Mass":(-0.35,-0.5)}, "4":{"Mass":(-0.5,-0.65)}, "5":{"Mass":(-0.65,-0.8)}},
    "Sensor_LongRange": {"1":{"MaximumRange":(0,0.15),"Range":(0,0.15),"ScannerRange":(0,0.15)}, "2":{"MaximumRange":(0.15,0.3),"Range":(0.15,0.3),"ScannerRange":(0.15,0.3)}, "3":{"MaximumRange":(0.3,0.45),"Range":(0.3,0.45),"ScannerRange":(0.3,0.45)}, "4":{"MaximumRange":(0.45,0.6),"Range":(0.45,0.6),"ScannerRange":(0.45,0.6)}, "5":{"MaximumRange":(0.6,0.75),"Range":(0.6,0.75),"ScannerRange":(0.6,0.75)}},
    "Sensor_WideAngle": {"1":{"MaxAngle":(0,0.4),"SensorTargetScanAngle":(0,0.4)}, "2":{"MaxAngle":(0.4,0.8),"SensorTargetScanAngle":(0.4,0.8)}, "3":{"MaxAngle":(0.8,1.2),"SensorTargetScanAngle":(0.8,1.2)}, "4":{"MaxAngle":(1.2,1.6),"SensorTargetScanAngle":(1.2,1.6)}, "5":{"MaxAngle":(1.6,2),"SensorTargetScanAngle":(1.6,2)}},
    "ShieldBooster_Explosive": {"1":{"ExplosiveResistance":(0,0.07)}, "2":{"ExplosiveResistance":(0.07,0.12)}, "3":{"ExplosiveResistance":(0.12,0.17)}, "4":{"ExplosiveResistance":(0.17,0.22)}, "5":{"ExplosiveResistance":(0.22,0.27)}},
    "ShieldBooster_HeavyDuty": {"1":{"DefenceModifierShieldMultiplier":(0.03,0.1),"Integrity":(0,0.03)}, "2":{"DefenceModifierShieldMultiplier":(0.1,0.17),"Integrity":(0.03,0.06)}, "3":{"DefenceModifierShieldMultiplier":(0.17,0.24),"Integrity":(0.06,0.09)}, "4":{"DefenceModifierShieldMultiplier":(0.24,0.31),"Integrity":(0.09,0.12)}, "5":{"DefenceModifierShieldMultiplier":(0.31,0.38),"Integrity":(0.12,0.15)}},
    "ShieldBooster_Kinetic": {"1":{"KineticResistance":(0,0.07)}, "2":{"KineticResistance":(0.07,0.12)}, "3":{"KineticResistance":(0.12,0.17)}, "4":{"KineticResistance":(0.17,0.22)}, "5":{"KineticResistance":(0.22,0.27)}},
    "ShieldBooster_Thermic": {"1":{"ThermicResistance":(0,0.07)}, "2":{"ThermicResistance":(0.07,0.12)}, "3":{"ThermicResistance":(0.12,0.17)}, "4":{"ThermicResistance":(0.17,0.22)}, "5":{"ThermicResistance":(0.22,0.27)}},
    "ShieldCellBank_Rapid": {"1":{"ShieldBankReinforcement":(0,0.05),"ShieldBankSpinUp":(0,-0.1)}, "2":{"ShieldBankReinforcement":(0.05,0.1),"ShieldBankSpinUp":(-0.1,-0.2)}, "3":{"ShieldBankReinforcement":(0.1,0.15),"ShieldBankSpinUp":(-0.2,-0.3)}, "4":{"ShieldBankReinforcement":(0.15,0.2),"ShieldBankSpinUp":(-0.3,-0.4)}},
    "ShieldCellBank_Specialised": {"1":{"BootTime":(0,-0.08),"EngineHeatRate":(0,-0.06),"FSDHeatRate":(0,-0.06),"ShieldBankHeat":(0,-0.06),"ShieldBankReinforcement":(0,0.04),"ThermalLoad":(0,-0.06)}, "2":{"BootTime":(-0.08,-0.16),"EngineHeatRate":(-0.06,-0.12),"FSDHeatRate":(-0.06,-0.12),"ShieldBankHeat":(-0.06,-0.12),"ShieldBankReinforcement":(0.04,0.06),"ThermalLoad":(-0.06,-0.12)}, "3":{"BootTime":(-0.16,-0.24),"EngineHeatRate":(-0.12,-0.18),"FSDHeatRate":(-0.12,-0.18),"ShieldBankHeat":(-0.12,-0.18),"ShieldBankReinforcement":(0.06,0.08),"ThermalLoad":(-0.12,-0.18)}, "4":{"BootTime":(-0.24,-0.32),"EngineHeatRate":(-0.18,-0.24),"FSDHeatRate":(-0.18,-0.24),"ShieldBankHeat":(-0.18,-0.24),"ShieldBankReinforcement":(0.08,0.1),"ThermalLoad":(-0.18,-0.24)}},
    "ShieldGenerator_Kinetic": {"1":{"Integrity":(0,0.2),"KineticResistance":(0,0.1)}, "2":{"Integrity":(0.2,0.25),"KineticResistance":(0.1,0.2)}, "3":{"Integrity":(0.25,0.3),"KineticResistance":(0.2,0.3)}, "4":{"Integrity":(0.3,0.35),"KineticResistance":(0.3,0.4)}, "5":{"Integrity":(0.35,0.4),"KineticResistance":(0.4,0.5)}},
    "ShieldGenerator_Optimised": {"1":{"EngineOptPerformance":(0,0.03),"Mass":(0,-0.18),"PowerDraw":(0,-0.2),"ShieldGenStrength":(0,0.03)}, "2":{"EngineOptPerformance":(0.03,0.06),"Mass":(-0.18,-0.26),"PowerDraw":(-0.2,-0.25),"ShieldGenStrength":(0.03,0.06)}, "3":{"EngineOptPerformance":(0.06,0.09),"Mass":(-0.26,-0.34),"PowerDraw":(-0.25,-0.3),"ShieldGenStrength":(0.06,0.09)}, "4":{"EngineOptPerformance":(0.09,0.12),"Mass":(-0.34,-0.42),"PowerDraw":(-0.3,-0.35),"ShieldGenStrength":(0.09,0.12)}, "5":{"EngineOptPerformance":(0.12,0.15),"Mass":(-0.42,-0.5),"PowerDraw":(-0.35,-0.4),"ShieldGenStrength":(0.12,0.15)}},
    "ShieldGenerator_Reinforced": {"1":{"EngineOptPerformance":(0,0.14),"ExplosiveResistance":(0,0.045),"KineticResistance":(0,0.045),"ShieldGenStrength":(0,0.14),"ThermicResistance":(0,0.045)}, "2":{"EngineOptPerformance":(0.14,0.2),"ExplosiveResistance":(0.045,0.075),"KineticResistance":(0.045,0.075),"ShieldGenStrength":(0.14,0.2),"ThermicResistance":(0.045,0.075)}, "3":{"EngineOptPerformance":(0.2,0.26),"ExplosiveResistance":(0.075,0.105),"KineticResistance":(0.075,0.105),"ShieldGenStrength":(0.2,0.26),"ThermicResistance":(0.075,0.105)}, "4":{"EngineOptPerformance":(0.26,0.32),"ExplosiveResistance":(0.105,0.135),"KineticResistance":(0.105,0.135),"ShieldGenStrength":(0.26,0.32),"ThermicResistance":(0.105,0.135)}, "5":{"EngineOptPerformance":(0.32,0.38),"ExplosiveResistance":(0.135,0.165),"KineticResistance":(0.135,0.165),"ShieldGenStrength":(0.32,0.38),"ThermicResistance":(0.135,0.165)}},
    "ShieldGenerator_Thermic": {"1":{"Integrity":(0,0.2),"ThermicResistance":(0,0.1)}, "2":{"Integrity":(0.2,0.25),"ThermicResistance":(0.1,0.2)}, "3":{"Integrity":(0.25,0.3),"ThermicResistance":(0.2,0.3)}, "4":{"Integrity":(0.3,0.35),"ThermicResistance":(0.3,0.4)}, "5":{"Integrity":(0.35,0.4),"ThermicResistance":(0.4,0.5)}},
    "Weapon_DoubleShot": {"1":{"weapon_burst_rof":(0,6)}, "2":{"weapon_burst_rof":(6,8)}, "3":{"weapon_burst_rof":(8,10)}, "4":{"weapon_burst_rof":(10,12)}, "5":{"weapon_burst_rof":(12,14)}},
    "Weapon_Efficient": {"1":{"Damage":(0,0.08),"EngineHeatRate":(0,-0.38),"FSDHeatRate":(0,-0.38),"ShieldBankHeat":(0,-0.38),"ThermalLoad":(0,-0.38)}, "2":{"Damage":(0.08,0.12),"DistributorDraw":(0,-0.15),"EnergyPerRegen":(0,-0.15),"EngineHeatRate":(-0.38,-0.43),"FSDHeatRate":(-0.38,-0.43),"PowerDraw":(0,-0.12),"ShieldBankHeat":(-0.38,-0.43),"ThermalLoad":(-0.38,-0.43)}, "3":{"Damage":(0.12,0.16),"DistributorDraw":(-0.15,-0.25),"EnergyPerRegen":(-0.15,-0.25),"EngineHeatRate":(-0.43,-0.48),"FSDHeatRate":(-0.43,-0.48),"PowerDraw":(-0.12,-0.24),"ShieldBankHeat":(-0.43,-0.48),"ThermalLoad":(-0.43,-0.48)}, "4":{"Damage":(0.16,0.2),"DistributorDraw":(-0.25,-0.35),"EnergyPerRegen":(-0.25,-0.35),"EngineHeatRate":(-0.48,-0.52),"FSDHeatRate":(-0.48,-0.52),"PowerDraw":(-0.24,-0.36),"ShieldBankHeat":(-0.48,-0.52),"ThermalLoad":(-0.48,-0.52)}, "5":{"Damage":(0.2,0.24),"DistributorDraw":(-0.35,-0.45),"EnergyPerRegen":(-0.35,-0.45),"EngineHeatRate":(-0.52,-0.6),"FSDHeatRate":(-0.52,-0.6),"PowerDraw":(-0.36,-0.48),"ShieldBankHeat":(-0.52,-0.6),"ThermalLoad":(-0.52,-0.6)}},
    "Weapon_Focused": {"1":{"ArmourPenetration":(0,0.4),"MaximumRange":(0,0.36),"Range":(0,0.36),"ScannerRange":(0,0.36),"ShotSpeed":(0,0.36)}, "2":{"ArmourPenetration":(0.4,0.6),"MaximumRange":(0.36,0.52),"Range":(0.36,0.52),"ScannerRange":(0.36,0.52),"ShotSpeed":(0.36,0.52)}, "3":{"ArmourPenetration":(0.6,0.8),"MaximumRange":(0.52,0.68),"Range":(0.52,0.68),"ScannerRange":(0.52,0.68),"ShotSpeed":(0.52,0.68)}, "4":{"ArmourPenetration":(0.8,1),"MaximumRange":(0.68,0.84),"Range":(0.68,0.84),"ScannerRange":(0.68,0.84),"ShotSpeed":(0.68,0.84)}, "5":{"ArmourPenetration":(1,1.2),"MaximumRange":(0.84,1),"Range":(0.84,1),"ScannerRange":(0.84,1),"ShotSpeed":(0.84,1)}},
    "Weapon_HighCapacity": {"1":{"AmmoClipSize":(0,0.36),"AmmoMaximum":(0,0.36),"RateOfFire":(0,-0.02),"weapon_clip_size_override":(0,0.36)}, "2":{"AmmoClipSize":(0.36,0.52),"AmmoMaximum":(0.36,0.52),"RateOfFire":(-0.02,-0.04),"weapon_clip_size_override":(0.36,0.52)}, "3":{"AmmoClipSize":(0.52,0.68),"AmmoMaximum":(0.52,0.68),"RateOfFire":(-0.04,-0.06),"weapon_clip_size_override":(0.52,0.68)}, "4":{"AmmoClipSize":(0.68,0.84),"AmmoMaximum":(0.68,0.84),"RateOfFire":(-0.06,-0.08),"weapon_clip_size_override":(0.68,0.84)}, "5":{"AmmoClipSize":(0.84,1),"AmmoMaximum":(0.84,1),"RateOfFire":(-0.08,-0.1),"weapon_clip_size_override":(0.84,1)}},
    "Weapon_LightWeight": {"1":{"Mass":(0,-0.3)}, "2":{"DistributorDraw":(0,-0.2),"EnergyPerRegen":(0,-0.2),"Mass":(-0.3,-0.45),"PowerDraw":(0,-0.1)}, "3":{"DistributorDraw":(-0.2,-0.25),"EnergyPerRegen":(-0.2,-0.25),"Mass":(-0.45,-0.6),"PowerDraw":(-0.1,-0.2)}, "4":{"DistributorDraw":(-0.25,-0.3),"EnergyPerRegen":(-0.25,-0.3),"Mass":(-0.6,-0.75),"PowerDraw":(-0.2,-0.3)}, "5":{"DistributorDraw":(-0.3,-0.35),"EnergyPerRegen":(-0.3,-0.35),"Mass":(-0.75,-0.9),"PowerDraw":(-0.3,-0.4)}},
    "Weapon_LongRange": {"1":{"MaximumRange":(0,0.2),"Range":(0,0.2),"ScannerRange":(0,0.2),"ShotSpeed":(0,0.2)}, "2":{"DamageFalloffRange":(0.2,0.4),"MaximumRange":(0.2,0.4),"Range":(0.2,0.4),"ScannerRange":(0.2,0.4),"ShotSpeed":(0.2,0.4)}, "3":{"DamageFalloffRange":(0.4,0.6),"MaximumRange":(0.4,0.6),"Range":(0.4,0.6),"ScannerRange":(0.4,0.6),"ShotSpeed":(0.4,0.6)}, "4":{"DamageFalloffRange":(0.6,0.8),"MaximumRange":(0.6,0.8),"Range":(0.6,0.8),"ScannerRange":(0.6,0.8),"ShotSpeed":(0.6,0.8)}, "5":{"DamageFalloffRange":(0.8,1),"MaximumRange":(0.8,1),"Range":(0.8,1),"ScannerRange":(0.8,1),"ShotSpeed":(0.8,1)}},
    "Weapon_Overcharged": {"1":{"Damage":(0,0.3)}, "2":{"Damage":(0.3,0.4)}, "3":{"Damage":(0.4,0.5)}, "4":{"Damage":(0.5,0.6)}, "5":{"Damage":(0.6,0.7)}},
    "Weapon_RapidFire": {"1":{"RateOfFire":(0,-0.08),"ReloadTime":(0,-0.25)}, "2":{"DistributorDraw":(0,-0.05),"EnergyPerRegen":(0,-0.05),"RateOfFire":(-0.08,-0.17),"ReloadTime":(-0.25,-0.35)}, "3":{"DistributorDraw":(-0.05,-0.15),"EnergyPerRegen":(-0.05,-0.15),"RateOfFire":(-0.17,-0.26),"ReloadTime":(-0.35,-0.45)}, "4":{"DistributorDraw":(-0.15,-0.25),"EnergyPerRegen":(-0.15,-0.25),"RateOfFire":(-0.26,-0.35),"ReloadTime":(-0.45,-0.55)}, "5":{"DistributorDraw":(-0.25,-0.35),"EnergyPerRegen":(-0.25,-0.35),"RateOfFire":(-0.35,-0.44),"ReloadTime":(-0.55,-0.65)}},
    "Weapon_ShortRange": {"1":{"Damage":(0.15,0.27)}, "2":{"Damage":(0.27,0.39)}, "3":{"Damage":(0.39,0.51)}, "4":{"Damage":(0.51,0.63)}, "5":{"Damage":(0.63,0.75)}},
    "Weapon_Sturdy": {"1":{"ArmourPenetration":(0,0.2),"EngineHeatRate":(0,-0.1),"FSDHeatRate":(0,-0.1),"Integrity":(0,1),"ShieldBankHeat":(0,-0.1),"ThermalLoad":(0,-0.1)}, "2":{"ArmourPenetration":(0.2,0.3),"EngineHeatRate":(-0.1,-0.15),"FSDHeatRate":(-0.1,-0.15),"ShieldBankHeat":(-0.1,-0.15),"ThermalLoad":(-0.1,-0.15)}, "3":{"ArmourPenetration":(0.3,0.4),"EngineHeatRate":(-0.15,-0.2),"FSDHeatRate":(-0.15,-0.2),"ShieldBankHeat":(-0.15,-0.2),"ThermalLoad":(-0.15,-0.2)}, "4":{"ArmourPenetration":(0.4,0.5),"EngineHeatRate":(-0.2,-0.25),"FSDHeatRate":(-0.2,-0.25),"ShieldBankHeat":(-0.2,-0.25),"ThermalLoad":(-0.2,-0.25)}, "5":{"ArmourPenetration":(0.5,0.6),"EngineHeatRate":(-0.25,-0.3),"FSDHeatRate":(-0.25,-0.3),"ShieldBankHeat":(-0.25,-0.3),"ThermalLoad":(-0.25,-0.3)}},
}


def _title_effect(name: str) -> str:
    """Capitalise the first letter of each space-separated word, leaving the rest
    untouched, to match the journal's Title Case. coriolis is sometimes sentence
    case (e.g. 'Phasing sequence' -> 'Phasing Sequence'); doing it this way (not
    str.title()) preserves acronyms ('FSD interrupt' -> 'FSD Interrupt') and forms
    like 'Hi-Cap'."""
    return " ".join((w[:1].upper() + w[1:]) if w else w for w in name.split(" "))


def _capi_special_effect(special: Any) -> Optional[str]:
    """CAPI 'specialModifications' is {codename: codename} when an experimental
    effect is applied, or an empty list/None when not. Return the friendly name
    the journal uses (Title Case), falling back to the raw codename if it's
    unrecognised."""
    if isinstance(special, dict) and special:
        codename = next(iter(special))
        name = _SPECIAL_EFFECTS.get(codename)
        return _title_effect(name) if name else codename
    return None


def _capi_modifiers(wip: Any) -> list[dict[str, Any]]:
    """CAPI 'WorkInProgress_modifications' -> a normalised list. CAPI reports
    each stat as a *multiplier* (e.g. 1.4 = +40%, 0.85 = -15%) plus the game's
    own display string — not the journal's absolute before/after values — so we
    surface the multiplier under its own key to avoid conflating the two."""
    out: list[dict[str, Any]] = []
    if not isinstance(wip, dict):
        return out
    for field, info in wip.items():
        if not isinstance(info, dict):
            continue
        label = field[len(_OFT_PREFIX):] if field.startswith(_OFT_PREFIX) else field
        out.append({
            "label": label,
            "multiplier": info.get("value"),
            "less_is_good": 1 if info.get("LessIsGood") else 0,
            "display": info.get("displayValue"),
        })
    return out


# Stats whose CAPI multiplier isn't a clean (1 + fraction) scaling, so they
# don't fit the linear quality model: resistances combine non-linearly, and the
# *_Addition stats are absolute amounts. Excluded from the quality estimate — at
# low grades a resistance can land inside the sane window and skew the median.
_QUALITY_SKIP = {"KineticResistance", "ThermicResistance", "ExplosiveResistance",
                 "DefenceModifierHealthAddition"}


def _capi_quality(blueprint: Optional[str], grade: Any, wip: Any) -> Optional[float]:
    """Estimate blueprint quality (0..1) from the live stat multipliers. CAPI
    doesn't report quality, but a roll scales each stat linearly between its
    grade minimum and maximum, so quality = (value - min) / (max - min) for any
    varying stat. We compute that for every clean stat we can match, drop
    differently-scaled outliers, and take the median. Returns None when nothing
    usable matches (unknown blueprint, or one whose only varying stats are
    resistances). The result is an estimate — it can differ from the journal's
    exact quality by a little."""
    ranges = _BP_QUALITY_RANGES.get(blueprint or "", {}).get(str(grade))
    if not ranges or not isinstance(wip, dict):
        return None
    qs = []
    for field, info in wip.items():
        if not isinstance(info, dict):
            continue
        label = field[len(_OFT_PREFIX):] if field.startswith(_OFT_PREFIX) else field
        if label in _QUALITY_SKIP:
            continue
        rng = ranges.get(label)
        mult = info.get("value")
        if not rng or not isinstance(mult, (int, float)):
            continue
        lo, hi = rng
        if hi == lo:
            continue
        q = ((mult - 1.0) - lo) / (hi - lo)
        if -0.1 <= q <= 1.1:   # within range for correctly-scaled stats; drops resistances etc.
            qs.append(q)
    if not qs:
        return None
    qs.sort()
    n = len(qs)
    median = qs[n // 2] if n % 2 else (qs[n // 2 - 1] + qs[n // 2]) / 2.0
    return round(min(1.0, max(0.0, median)), 3)


def _capi_engineering(slot: dict[str, Any],
                      journal_engineer: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Normalise a CAPI slot's engineering into the same summary shape the
    journal path emits (_engineering_summary), returning None for an un-engineered
    module (no 'engineer' block).

    Field sources reflect which path is authoritative for each: CAPI is the
    fresher source for the roll-volatile fields (grade, quality, modifiers), so
    those come from CAPI. For engineer, CAPI reports the *last* engineer to roll
    the module, which drifts when it's re-rolled at a remote workshop — so we
    prefer the originating engineer from the journal (passed in by the caller
    when the slot still holds the same mod) and expose CAPI's under
    engineer_last_roll.

    'quality' is an *estimate* (CAPI omits it; see _capi_quality);
    'quality_estimated' is True only for the quality field, and only when a value
    was actually derived."""
    eng = slot.get("engineer")
    if not isinstance(eng, dict):
        return None
    capi_engineer = eng.get("engineerName")
    quality = _capi_quality(eng.get("recipeName"), eng.get("recipeLevel"),
                            slot.get("WorkInProgress_modifications"))
    return {
        "blueprint": eng.get("recipeName"),   # raw codename, exactly as the journal stores it
        "grade": eng.get("recipeLevel"),
        "quality": quality,
        "quality_estimated": quality is not None,
        "engineer": journal_engineer or capi_engineer,
        "engineer_last_roll": capi_engineer,
        "experimental_effect": _capi_special_effect(slot.get("specialModifications")),
        "modifiers": _capi_modifiers(slot.get("WorkInProgress_modifications")),
    }


def _capi_current_ship(ship: dict[str, Any],
                       journal_loadout: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    # Map slot -> (item, blueprint, engineer) from the cached journal Loadout, so
    # _capi_engineering can prefer the journal's originating engineer over CAPI's
    # last-roll one — but only when the slot still holds the same engineered mod
    # (same item and blueprint), otherwise the journal entry is stale/unrelated.
    j_eng: dict[str, tuple] = {}
    if isinstance(journal_loadout, dict):
        for jm in journal_loadout.get("Modules", []):
            je = jm.get("Engineering") or {}
            name = je.get("Engineer")
            if name:
                j_eng[jm.get("Slot")] = (jm.get("Item"), je.get("BlueprintName"), name)

    modules = []
    for slot, entry in (ship.get("modules") or {}).items():
        mod = entry.get("module") if isinstance(entry, dict) else None
        if not isinstance(mod, dict):
            continue
        health = mod.get("health")
        name = mod.get("name")
        # Lower-case to match the journal path (the game emits lower-case item
        # names there; CAPI uses PascalCase).
        item = name.lower() if isinstance(name, str) else name
        capi_eng = entry.get("engineer") if isinstance(entry.get("engineer"), dict) else {}
        j = j_eng.get(slot)
        journal_engineer = (
            j[2] if (j and j[0] == item and j[1] == capi_eng.get("recipeName")) else None)
        modules.append({
            "slot": slot,
            "item": item,
            "on": mod.get("on"),
            "priority": mod.get("priority"),
            # CAPI reports health on a 0..1000000 scale; the journal uses 0..1.
            "health": health / 1_000_000 if isinstance(health, (int, float)) else health,
            "value": mod.get("value"),
            "engineering": _capi_engineering(entry, journal_engineer),
        })
    value = ship.get("value")
    return {
        "ship_id": ship.get("id"),
        "type": ship.get("name"),
        "name": ship.get("shipName"),
        "ident": ship.get("shipID"),
        "value": value.get("total") if isinstance(value, dict) else value,
        "module_count": len(modules),
        "engineered_module_count": sum(1 for m in modules if m["engineering"]),
        "modules": modules,
    }


def _capi_fleet(ships: Any) -> list[dict[str, Any]]:
    if isinstance(ships, dict):
        items = list(ships.values())
    elif isinstance(ships, list):
        items = ships
    else:
        return []
    out = []
    for s in items:
        if not isinstance(s, dict):
            continue
        value = s.get("value")
        out.append({
            "ship_id": s.get("id"),
            "type": s.get("name"),
            "name": s.get("shipName"),
            "system": (s.get("starsystem") or {}).get("name"),
            "station": (s.get("station") or {}).get("name"),
            "value": value.get("total") if isinstance(value, dict) else value,
        })
    return out


def _normalize_engineers(raw: dict[str, Any]) -> dict[str, Any]:
    """EDMC stores state['Engineers'] as name -> (Rank, RankProgress) once an
    engineer is unlocked, or a status string ('Known'/'Invited'/...) otherwise.
    Normalise both into a uniform dict for the snapshot."""
    out: dict[str, Any] = {}
    for name, val in raw.items():
        if isinstance(val, (tuple, list)):
            out[name] = {
                "status": "Unlocked",
                "rank": val[0] if len(val) > 0 else None,
                "rank_progress": val[1] if len(val) > 1 else 0,
            }
        else:
            out[name] = {"status": val}
    return out


class _Connector:
    """Holds in-memory state and a debounced background writer thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot: dict[str, Any] = {"schema": 1}
        # Full Loadout journal events keyed by ShipID, so stored ships keep
        # their last-known engineering even when not currently boarded.
        self.loadouts: dict[int, dict[str, Any]] = {}
        self.path: str = default_state_path()
        self.enabled: bool = True
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # CAPI-refresh request tracking (touched only on the main thread).
        self._last_request_nonce: Optional[str] = None
        self._pending_nonce: Optional[str] = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self.path = config.get_str(CONFIG_PATH_KEY) or default_state_path()
        # Enabled by default; persisted as int (1/0) by prefs_changed.
        self.enabled = bool(config.get_bool(CONFIG_ENABLED_KEY, default=True))
        # Seed the last-seen request nonce so a stale request file left over from
        # a previous session doesn't fire a spurious refresh on startup.
        existing = _read_request(self.path)
        if existing:
            self._last_request_nonce = existing.get("nonce")
        # Restore the per-ship loadout cache from the previous session's snapshot
        # so stored ships' engineering survives an EDMC restart.
        self._load_cached_loadouts()
        self._thread = threading.Thread(target=self._writer_loop, name="EliteDangerousMCPWriter", daemon=True)
        self._thread.start()

    def _load_cached_loadouts(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        cached = prev.get("ship_loadouts") or {}
        for sid, lo in cached.items():
            try:
                self.loadouts[int(sid)] = lo
            except (ValueError, TypeError):
                continue
        # Seed the live snapshot so the cache is present before the first event.
        if cached:
            with self.lock:
                self.snapshot["ship_loadouts"] = dict(cached)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # Final synchronous flush so the snapshot reflects the last events.
        self._flush()

    def mark_dirty(self) -> None:
        self._wake.set()

    # -- background writer ----------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            # Wait for a change, then debounce a burst of events (e.g. a
            # crafting session emitting many MaterialCollected entries).
            self._wake.wait()
            if self._stop.is_set():
                break
            self._wake.clear()
            self._stop.wait(WRITE_DEBOUNCE_SECONDS)
            self._flush()

    def _flush(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            data = json.dumps(self.snapshot, indent=1, default=str)
            path = self.path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, path)  # atomic on the same filesystem
        except OSError as exc:
            logger.error(f"EliteDangerousMCP: failed to write {path}: {exc}")

    # -- snapshot building ----------------------------------------------------
    def update(self, cmdr: str, system: Optional[str], station: Optional[str],
               entry: dict[str, Any], state: dict[str, Any]) -> None:
        event = entry.get("event")

        # Capture full Loadout events (these carry per-module Engineering data
        # that the summarised state['Modules'] may not fully preserve).
        if event == "Loadout":
            ship_id = entry.get("ShipID")
            if ship_id is not None:
                self.loadouts[ship_id] = entry

        with self.lock:
            snap = self.snapshot
            snap["schema"] = 1
            snap["updated"] = entry.get("timestamp")
            snap["last_event"] = event
            snap["cmdr"] = cmdr
            snap["game"] = {
                "version": state.get("GameVersion"),
                "build": state.get("GameBuild"),
                "language": state.get("GameLanguage"),
                "horizons": state.get("Horizons"),
                "odyssey": state.get("Odyssey"),
            }
            snap["location"] = {
                "system": system or state.get("SystemName"),
                "station": station or state.get("StationName"),
                "station_type": state.get("StationType"),
                "body": state.get("Body"),
                "docked": state.get("IsDocked"),
                "on_foot": state.get("OnFoot"),
            }
            snap["credits"] = state.get("Credits")

            current_id = state.get("ShipID")
            snap["current_ship"] = {
                "ship_id": current_id,
                "type": state.get("ShipType"),
                "name": state.get("ShipName"),
                "ident": state.get("ShipIdent"),
                "hull_value": state.get("HullValue"),
                "modules_value": state.get("ModulesValue"),
                "rebuy": state.get("Rebuy"),
                "unladen_mass": state.get("UnladenMass"),
                "cargo_capacity": state.get("CargoCapacity"),
                "max_jump_range": state.get("MaxJumpRange"),
                "fuel_capacity": state.get("FuelCapacity"),
                "loadout": self.loadouts.get(current_id) if current_id is not None else None,
            }

            # Fleet inventory: merge StoredShips with any ship we've seen a
            # Loadout for. StoredShips only appears when docked at a shipyard.
            ships: dict[str, Any] = snap.get("ships", {})
            if event == "StoredShips":
                ships = {}
                here = {"system": entry.get("StarSystem"), "station": entry.get("StationName")}
                for s in entry.get("ShipsHere", []):
                    ships[str(s.get("ShipID"))] = {
                        "type": s.get("ShipType"), "name": s.get("Name"),
                        "value": s.get("Value"), "location": here, "in_transit": False,
                    }
                for s in entry.get("ShipsRemote", []):
                    ships[str(s.get("ShipID"))] = {
                        "type": s.get("ShipType"), "name": s.get("Name"),
                        "value": s.get("Value"),
                        "location": {"system": s.get("StarSystem"), "station": s.get("StationName")},
                        "in_transit": s.get("InTransit", False),
                    }
            if current_id is not None:
                ships[str(current_id)] = {
                    "type": state.get("ShipType"), "name": state.get("ShipName"),
                    "ident": state.get("ShipIdent"), "current": True,
                    "location": {"system": snap["location"]["system"],
                                 "station": snap["location"]["station"]},
                }
            snap["ships"] = ships

            # Per-ship loadout cache: the last-known full loadout (modules +
            # engineering) of every ship we've boarded, so the client can inspect any
            # ship in the fleet, not just the current one. Survives restarts via
            # _load_cached_loadouts().
            snap["ship_loadouts"] = {str(sid): lo for sid, lo in self.loadouts.items()}

            # Materials are kept current in state on every event.
            snap["materials"] = {
                key: dict(state.get(src) or {}) for key, src in MATERIAL_BUCKETS.items()
            }
            snap["material_totals"] = {
                key: sum((state.get(src) or {}).values()) for key, src in MATERIAL_BUCKETS.items()
            }

            snap["cargo"] = dict(state.get("Cargo") or {})
            snap["engineers"] = _normalize_engineers(state.get("Engineers") or {})

        self.mark_dirty()

    # -- live CAPI refresh ----------------------------------------------------
    def poll_request(self) -> None:
        """Main-thread: pick up a refresh request from the MCP server and ask
        EDMC to fire a live CAPI query. Cheap enough to run on a short timer."""
        if not self.enabled:
            return
        req = _read_request(self.path)
        if not req:
            return
        nonce = req.get("nonce")
        if not nonce or nonce == self._last_request_nonce:
            return
        self._last_request_nonce = nonce

        # Honour Frontier's global cooldown: firing during it is a no-op, so
        # report it (with when to retry) instead of leaving the caller to wait.
        remaining = _capi_cooldown_remaining()
        if remaining > 0:
            self._pending_nonce = None
            with self.lock:
                capi = dict(self.snapshot.get("capi") or {})
                capi.update({
                    "status": "cooldown",
                    "request_nonce": nonce,
                    "requested_at": _utcnow_iso(),
                    # Mark this request handled so the MCP server returns at once.
                    "response_nonce": nonce,
                    "cooldown_remaining_seconds": round(remaining, 1),
                    "cooldown_until": _iso_in(remaining),
                })
                self.snapshot["capi"] = capi
            self.mark_dirty()
            logger.info(f"EliteDangerousMCP: CAPI refresh requested but on cooldown "
                        f"({remaining:.0f}s remaining)")
            return

        self._pending_nonce = nonce
        with self.lock:
            capi = dict(self.snapshot.get("capi") or {})
            capi.update({
                "status": "requested",
                "request_nonce": nonce,
                "requested_at": _utcnow_iso(),
            })
            self.snapshot["capi"] = capi
        self.mark_dirty()
        _fire_capi_update()

    def record_capi(self, data: dict[str, Any], is_beta: bool) -> None:
        """Capture a Frontier CAPI response (delivered via the cmdr_data hook).
        Records the live ship loadout and fleet, tagged with the request nonce
        so the MCP server can tell its refresh request was fulfilled."""
        commander = data.get("commander") or {}
        ship = data.get("ship") or {}
        with self.lock:
            prev = self.snapshot.get("capi") or {}
            self.snapshot["capi"] = {
                "status": "received",
                "responded_at": _utcnow_iso(),
                "request_nonce": prev.get("request_nonce"),
                "requested_at": prev.get("requested_at"),
                # None when this CAPI update wasn't triggered by a client request
                # (e.g. EDMC's automatic pull on docking) — still worth capturing.
                "response_nonce": self._pending_nonce,
                "is_beta": bool(is_beta),
                "commander": {
                    "name": commander.get("name"),
                    "credits": commander.get("credits"),
                    "docked": commander.get("docked"),
                },
                "location": {
                    "system": (data.get("lastSystem") or {}).get("name"),
                    "station": (data.get("lastStarport") or {}).get("name"),
                },
                # Pass the cached journal Loadout for this ship so engineer
                # provenance can be merged in (see _capi_current_ship).
                "current_ship": _capi_current_ship(
                    ship, self.loadouts.get(ship.get("id"))) if ship else None,
                "fleet": _capi_fleet(data.get("ships")),
            }
        self._pending_nonce = None
        self.mark_dirty()


CONNECTOR = _Connector()

# UI variables (main thread only)
_enabled_var: Optional[tk.IntVar] = None
_path_var: Optional[tk.StringVar] = None
_status_label: Optional[tk.Label] = None

# Where the connector repo (with the update scripts) was installed from. Recorded
# by the installer in install_info.json next to this plugin, so the "click to
# update" action knows which update.bat / update.sh to run.
_repo_path: Optional[str] = None


def _read_repo_path(plugin_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(plugin_dir, "install_info.json"), encoding="utf-8") as fh:
            repo = json.load(fh).get("repo")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return repo if repo and os.path.isdir(repo) else None


def _updater_path() -> Optional[str]:
    """Path of the platform update script, if we know the repo and it exists."""
    if not _repo_path:
        return None
    name = "update.bat" if sys.platform.startswith("win") else "update.sh"
    path = os.path.join(_repo_path, name)
    return path if os.path.isfile(path) else None


def _launch_updater() -> bool:
    """Run the update script (in a visible console where possible) and return
    whether it was launched. Best-effort and never raises."""
    updater = _updater_path()
    if not updater:
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(updater)  # type: ignore[attr-defined]  # opens its own console
            return True
        # Linux: prefer a visible terminal so the user can watch progress.
        hold = f'"{updater}"; echo; read -n1 -rsp "Update finished - press any key to close..."'
        for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"],
                     ["konsole", "-e"], ["xterm", "-e"]):
            if shutil.which(term[0]):
                subprocess.Popen(term + ["bash", "-lc", hold], cwd=_repo_path)
                return True
        subprocess.Popen(["bash", updater], cwd=_repo_path)  # headless fallback
        return True
    except Exception as exc:  # pragma: no cover - platform/launch quirks
        logger.error(f"EliteDangerousMCP: failed to launch updater: {exc}")
        return False


# === EDMC plugin entry points ===============================================

def plugin_start3(plugin_dir: str) -> str:
    global _repo_path
    _repo_path = _read_repo_path(plugin_dir)
    CONNECTOR.start()
    logger.info(f"EliteDangerousMCP v{VERSION} started; snapshot path: {CONNECTOR.path}")
    threading.Thread(target=_check_for_update, name="EliteDangerousMCPUpdateCheck", daemon=True).start()
    return PLUGIN_NAME


def plugin_stop() -> None:
    CONNECTOR.stop()
    logger.info("EliteDangerousMCP stopped")


def _refresh_status_label() -> None:
    """Make the main-window label reflect the real enabled state (main thread).

    Colours are left to EDMC's theme (see _theme_label) so the text stays legible
    on the default, dark, and transparent themes — hardcoding e.g. blue made it
    unreadable on the dark theme's black background."""
    if _status_label is None:
        return
    if CONNECTOR.enabled:
        _status_label["text"] = "Elite Dangerous MCP: Running"
    else:
        _status_label["text"] = "Elite Dangerous MCP: Off (enable in Settings)"
    if _update_available:
        # Clickable when we know where the update script lives (recorded by the
        # installer); otherwise just announce it.
        if _updater_path():
            _status_label["text"] += f"  (update v{_update_available} — click to update)"
            _status_label["cursor"] = "hand2"
        else:
            _status_label["text"] += f"  (update v{_update_available} available)"
            _status_label["cursor"] = ""


def _on_status_click(event: object = None) -> None:
    """Run the updater when the user clicks the label and an update is available."""
    if not _update_available or _status_label is None:
        return
    if _launch_updater():
        _status_label["text"] = (
            f"Elite Dangerous MCP: Updating to v{_update_available}… "
            f"restart EDMC & your MCP client when it finishes")
        _status_label["cursor"] = ""
    else:
        _status_label["text"] = (
            f"Elite Dangerous MCP: Update v{_update_available} ready — "
            f"run update.bat in your EDMC-MCP folder")


def _fire_capi_update() -> None:
    """Generate the virtual event EDMC binds to its "Update" button, firing a
    live CAPI query. EDMC enforces its own global cooldown, so calling this while
    on cooldown is a harmless no-op (no fresh data simply won't arrive)."""
    if _status_label is None:
        return
    try:
        _status_label.event_generate("<<Invoke>>", when="tail")
        logger.info("EliteDangerousMCP: requested a live CAPI update (<<Invoke>>)")
    except tk.TclError as exc:
        logger.error(f"EliteDangerousMCP: could not fire CAPI update: {exc}")


def _poll_capi_request() -> None:
    """Main-thread timer: service any queued CAPI-refresh request, then reschedule."""
    try:
        CONNECTOR.poll_request()
    except Exception as exc:  # never let the timer die
        logger.error(f"EliteDangerousMCP CAPI poll error: {exc}", exc_info=True)
    finally:
        if _status_label is not None:
            _status_label.after(CAPI_POLL_MS, _poll_capi_request)


def _theme_label(widget: tk.Label) -> None:
    """Hand the label to EDMC's theme so it's coloured like the rest of the main
    window and re-coloured when the user switches theme. Best-effort: leaving the
    foreground unset lets the theme own it (orange on dark, system on default)."""
    if theme is None:
        return
    try:
        if hasattr(theme, "register"):
            theme.register(widget)
        if hasattr(theme, "update"):
            theme.update(widget)
    except Exception as exc:  # pragma: no cover - never break the UI over theming
        logger.debug(f"EliteDangerousMCP: theme registration skipped: {exc}")


def plugin_app(parent: tk.Frame) -> tk.Label:
    global _status_label
    _status_label = tk.Label(parent)
    _status_label.bind("<Button-1>", _on_status_click)
    _theme_label(_status_label)
    _refresh_status_label()
    # Pick up the background update-check result on the main thread (tkinter-safe).
    _status_label.after(12000, _refresh_status_label)
    # Start the timer that lets the client (via the MCP server) request CAPI refreshes.
    _status_label.after(CAPI_POLL_MS, _poll_capi_request)
    return _status_label


def plugin_prefs(parent: nb.Notebook, cmdr: str, is_beta: bool) -> tk.Frame:
    global _enabled_var, _path_var
    _enabled_var = tk.IntVar(value=1 if CONNECTOR.enabled else 0)
    _path_var = tk.StringVar(value=CONNECTOR.path)

    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)
    version_text = f"Elite Dangerous MCP — v{VERSION}"
    if _update_available:
        version_text += f"  (update v{_update_available} available)"
    nb.Label(frame, text=version_text).grid(
        row=0, column=0, columnspan=3, sticky=tk.W, padx=8, pady=(8, 0))
    nb.Label(frame, text="Writes ship loadouts & engineering materials to a local").grid(
        row=1, column=0, columnspan=3, sticky=tk.W, padx=8, pady=(8, 0))
    nb.Label(frame, text="JSON file for the Elite Dangerous MCP server to read.").grid(
        row=2, column=0, columnspan=3, sticky=tk.W, padx=8)
    nb.Checkbutton(frame, text="Enabled", variable=_enabled_var).grid(
        row=3, column=0, sticky=tk.W, padx=8, pady=8)
    nb.Label(frame, text="Snapshot file:").grid(row=4, column=0, sticky=tk.W, padx=8)
    nb.EntryMenu(frame, textvariable=_path_var, width=50).grid(
        row=4, column=1, columnspan=2, sticky=tk.EW, padx=8, pady=4)
    return frame


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    if _enabled_var is not None:
        CONNECTOR.enabled = bool(_enabled_var.get())
        config.set(CONFIG_ENABLED_KEY, 1 if _enabled_var.get() else 0)
    if _path_var is not None:
        new_path = _path_var.get().strip() or default_state_path()
        CONNECTOR.path = new_path
        config.set(CONFIG_PATH_KEY, new_path)
    _refresh_status_label()
    CONNECTOR.mark_dirty()


def journal_entry(cmdr: str, is_beta: bool, system: Optional[str], station: Optional[str],
                  entry: dict[str, Any], state: dict[str, Any]) -> Optional[str]:
    try:
        CONNECTOR.update(cmdr, system, station, entry, state)
    except Exception as exc:  # never let a plugin error disrupt EDMC
        logger.error(f"EliteDangerousMCP journal_entry error: {exc}", exc_info=True)
    return None


def cmdr_data(data: dict[str, Any], is_beta: bool) -> None:
    """EDMC hook: fresh Frontier CAPI data (Live galaxy). Fired both by EDMC's
    own pulls and by the refreshes we request via _fire_capi_update()."""
    try:
        CONNECTOR.record_capi(data, is_beta)
    except Exception as exc:  # never let a plugin error disrupt EDMC
        logger.error(f"EliteDangerousMCP cmdr_data error: {exc}", exc_info=True)


def cmdr_data_legacy(data: dict[str, Any], is_beta: bool) -> None:
    """EDMC hook: fresh Frontier CAPI data for the Legacy galaxy."""
    cmdr_data(data, is_beta)
