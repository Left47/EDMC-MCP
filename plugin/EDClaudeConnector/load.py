"""
EDClaudeConnector — EDMarketConnector plugin.

Captures real-time ship loadout (with engineering modifications) and engineering
materials inventory from the game journal and writes them to a local JSON
snapshot file. A companion MCP server reads that file so Claude can answer
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
if not hasattr(config, "get_int"):
    config.get_int = lambda key, default=0: config.getint(key)  # type: ignore

PLUGIN_NAME = "ED Claude Connector"
VERSION = "0.6.1"
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
            logger.info(f"EDClaudeConnector: update available: v{latest} (have v{VERSION})")
            # NB: do not touch tkinter here — this runs on a worker thread.
            # plugin_app schedules a label refresh on the main loop instead.
    except Exception as exc:  # never disrupt the app over an update check
        logger.debug(f"EDClaudeConnector update check skipped: {exc}")


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
# authoritative live loadout/fleet straight from Frontier — including details
# the game doesn't always write to the journal. Its shape differs from the
# journal's, so we surface the useful raw fields rather than risk mis-mapping.

def _capi_engineering(mod: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(mod, dict):
        return None
    engineer = mod.get("engineer")
    mods = mod.get("modifications") or mod.get("modifiers")
    special = mod.get("specialModifications")
    blueprint = mod.get("recipeName") or mod.get("blueprintName")
    if not any((engineer, mods, special, blueprint)):
        return None
    out: dict[str, Any] = {}
    if blueprint:
        out["blueprint"] = blueprint
    if special:
        out["experimental"] = special
    if engineer is not None:
        out["engineer"] = engineer
    if mods is not None:
        out["modifications"] = mods
    return out


def _capi_current_ship(ship: dict[str, Any]) -> dict[str, Any]:
    modules = []
    for slot, entry in (ship.get("modules") or {}).items():
        mod = entry.get("module") if isinstance(entry, dict) else None
        if not isinstance(mod, dict):
            continue
        modules.append({
            "slot": slot,
            "item": mod.get("name"),
            "on": mod.get("on"),
            "priority": mod.get("priority"),
            "health": mod.get("health"),
            "value": mod.get("value"),
            "engineering": _capi_engineering(mod),
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
        self._thread = threading.Thread(target=self._writer_loop, name="EDClaudeWriter", daemon=True)
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

            # Per-ship loadout cache: the last-known full loadout (modules +
            # engineering) of every ship we've boarded, so Claude can inspect any
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
            logger.info(f"EDClaudeConnector: CAPI refresh requested but on cooldown "
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
                # None when this CAPI update wasn't triggered by a Claude request
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
                "current_ship": _capi_current_ship(ship) if ship else None,
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
        logger.error(f"EDClaudeConnector: failed to launch updater: {exc}")
        return False


# === EDMC plugin entry points ===============================================

def plugin_start3(plugin_dir: str) -> str:
    global _repo_path
    _repo_path = _read_repo_path(plugin_dir)
    CONNECTOR.start()
    logger.info(f"EDClaudeConnector v{VERSION} started; snapshot path: {CONNECTOR.path}")
    threading.Thread(target=_check_for_update, name="EDClaudeUpdateCheck", daemon=True).start()
    return PLUGIN_NAME


def plugin_stop() -> None:
    CONNECTOR.stop()
    logger.info("EDClaudeConnector stopped")


def _refresh_status_label() -> None:
    """Make the main-window label reflect the real enabled state (main thread)."""
    if _status_label is None:
        return
    if CONNECTOR.enabled:
        _status_label["text"] = "ED Claude Connector: Running"
        _status_label["foreground"] = "green"
    else:
        _status_label["text"] = "ED Claude Connector: Off (enable in Settings)"
        _status_label["foreground"] = "grey"
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
            f"ED Claude Connector: Updating to v{_update_available}… "
            f"restart EDMC & Claude Desktop when it finishes")
        _status_label["foreground"] = "blue"
        _status_label["cursor"] = ""
    else:
        _status_label["text"] = (
            f"ED Claude Connector: Update v{_update_available} ready — "
            f"run update.bat in your EDMC-MCP folder")


def _fire_capi_update() -> None:
    """Generate the virtual event EDMC binds to its "Update" button, firing a
    live CAPI query. EDMC enforces its own global cooldown, so calling this while
    on cooldown is a harmless no-op (no fresh data simply won't arrive)."""
    if _status_label is None:
        return
    try:
        _status_label.event_generate("<<Invoke>>", when="tail")
        logger.info("EDClaudeConnector: requested a live CAPI update (<<Invoke>>)")
    except tk.TclError as exc:
        logger.error(f"EDClaudeConnector: could not fire CAPI update: {exc}")


def _poll_capi_request() -> None:
    """Main-thread timer: service any queued CAPI-refresh request, then reschedule."""
    try:
        CONNECTOR.poll_request()
    except Exception as exc:  # never let the timer die
        logger.error(f"EDClaudeConnector CAPI poll error: {exc}", exc_info=True)
    finally:
        if _status_label is not None:
            _status_label.after(CAPI_POLL_MS, _poll_capi_request)


def plugin_app(parent: tk.Frame) -> tk.Label:
    global _status_label
    _status_label = tk.Label(parent)
    _status_label.bind("<Button-1>", _on_status_click)
    _refresh_status_label()
    # Pick up the background update-check result on the main thread (tkinter-safe).
    _status_label.after(12000, _refresh_status_label)
    # Start the timer that lets Claude (via the MCP server) request CAPI refreshes.
    _status_label.after(CAPI_POLL_MS, _poll_capi_request)
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
        logger.error(f"EDClaudeConnector journal_entry error: {exc}", exc_info=True)
    return None


def cmdr_data(data: dict[str, Any], is_beta: bool) -> None:
    """EDMC hook: fresh Frontier CAPI data (Live galaxy). Fired both by EDMC's
    own pulls and by the refreshes we request via _fire_capi_update()."""
    try:
        CONNECTOR.record_capi(data, is_beta)
    except Exception as exc:  # never let a plugin error disrupt EDMC
        logger.error(f"EDClaudeConnector cmdr_data error: {exc}", exc_info=True)


def cmdr_data_legacy(data: dict[str, Any], is_beta: bool) -> None:
    """EDMC hook: fresh Frontier CAPI data for the Legacy galaxy."""
    cmdr_data(data, is_beta)
