"""
EDClaudeConnector — EDMarketConnector plugin.

Captures real-time ship loadout (with engineering modifications) and engineering
materials inventory from the game journal and writes them to a local JSON
snapshot file. A companion MCP server reads that file so Claude can answer
questions about your loadouts and materials to help plan engineering runs.

All data stays on your machine. Nothing is sent anywhere by this plugin.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from typing import Any, Optional

import myNotebook as nb  # type: ignore  # provided by EDMarketConnector
from config import config  # type: ignore  # provided by EDMarketConnector

try:
    from EDMCLogging import get_main_logger  # type: ignore
    logger = get_main_logger()
except Exception:  # pragma: no cover - fallback for very old EDMC
    import logging
    logger = logging.getLogger("EDClaudeConnector")

# --- Compatibility shims for pre-5.0.0 EDMC config API -----------------------
if not hasattr(config, "get_str"):
    config.get_str = config.get  # type: ignore[attr-defined]
if not hasattr(config, "get_bool"):
    config.get_bool = lambda key, default=False: bool(config.getint(key))  # type: ignore

PLUGIN_NAME = "ED Claude Connector"
CONFIG_PATH_KEY = "edclaude_state_path"
CONFIG_ENABLED_KEY = "edclaude_enabled"
WRITE_DEBOUNCE_SECONDS = 1.5

# Materials supplied via the journal `state` dict are organised into these keys.
MATERIAL_BUCKETS = {
    "raw": "Raw",
    "manufactured": "Manufactured",
    "encoded": "Encoded",
}


def default_state_path() -> str:
    """Default snapshot location, shared with the MCP server's default."""
    return os.path.join(os.path.expanduser("~"), ".elite-dangerous-claude", "state.json")


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

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self.path = config.get_str(CONFIG_PATH_KEY) or default_state_path()
        self.enabled = config.get_bool(CONFIG_ENABLED_KEY) if config.get_str(CONFIG_ENABLED_KEY) is not None else True
        self._thread = threading.Thread(target=self._writer_loop, name="EDClaudeWriter", daemon=True)
        self._thread.start()

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
            logger.error(f"EDClaudeConnector: failed to write {path}: {exc}")

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

            # Materials are kept current in state on every event.
            snap["materials"] = {
                key: dict(state.get(src) or {}) for key, src in MATERIAL_BUCKETS.items()
            }
            snap["material_totals"] = {
                key: sum((state.get(src) or {}).values()) for key, src in MATERIAL_BUCKETS.items()
            }

            snap["cargo"] = dict(state.get("Cargo") or {})

        self.mark_dirty()


CONNECTOR = _Connector()

# UI variables (main thread only)
_enabled_var: Optional[tk.IntVar] = None
_path_var: Optional[tk.StringVar] = None
_status_label: Optional[tk.Label] = None


# === EDMC plugin entry points ===============================================

def plugin_start3(plugin_dir: str) -> str:
    CONNECTOR.start()
    logger.info(f"EDClaudeConnector started; snapshot path: {CONNECTOR.path}")
    return PLUGIN_NAME


def plugin_stop() -> None:
    CONNECTOR.stop()
    logger.info("EDClaudeConnector stopped")


def plugin_app(parent: tk.Frame) -> tk.Label:
    global _status_label
    _status_label = tk.Label(parent, text="Claude: ready", foreground="green")
    return _status_label


def plugin_prefs(parent: nb.Notebook, cmdr: str, is_beta: bool) -> tk.Frame:
    global _enabled_var, _path_var
    _enabled_var = tk.IntVar(value=1 if CONNECTOR.enabled else 0)
    _path_var = tk.StringVar(value=CONNECTOR.path)

    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)
    nb.Label(frame, text="Writes ship loadouts & engineering materials to a local").grid(
        row=0, column=0, columnspan=3, sticky=tk.W, padx=8, pady=(8, 0))
    nb.Label(frame, text="JSON file for the ED Claude MCP server to read.").grid(
        row=1, column=0, columnspan=3, sticky=tk.W, padx=8)
    nb.Checkbutton(frame, text="Enabled", variable=_enabled_var).grid(
        row=2, column=0, sticky=tk.W, padx=8, pady=8)
    nb.Label(frame, text="Snapshot file:").grid(row=3, column=0, sticky=tk.W, padx=8)
    nb.EntryMenu(frame, textvariable=_path_var, width=50).grid(
        row=3, column=1, columnspan=2, sticky=tk.EW, padx=8, pady=4)
    return frame


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    if _enabled_var is not None:
        CONNECTOR.enabled = bool(_enabled_var.get())
        config.set(CONFIG_ENABLED_KEY, _enabled_var.get())
    if _path_var is not None:
        new_path = _path_var.get().strip() or default_state_path()
        CONNECTOR.path = new_path
        config.set(CONFIG_PATH_KEY, new_path)
    if _status_label is not None:
        _status_label["text"] = "Claude: ready" if CONNECTOR.enabled else "Claude: off"
        _status_label["foreground"] = "green" if CONNECTOR.enabled else "grey"
    CONNECTOR.mark_dirty()


def journal_entry(cmdr: str, is_beta: bool, system: Optional[str], station: Optional[str],
                  entry: dict[str, Any], state: dict[str, Any]) -> Optional[str]:
    try:
        CONNECTOR.update(cmdr, system, station, entry, state)
    except Exception as exc:  # never let a plugin error disrupt EDMC
        logger.error(f"EDClaudeConnector journal_entry error: {exc}", exc_info=True)
    return None
