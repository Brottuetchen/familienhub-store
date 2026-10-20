"""Screen time as a reward: unlock a child's device for N minutes, then block it again.

Hooks used (docs/plugins.md, "Extension points"):

- ``reward_actions`` / ``run_reward_action``: the reward action "Unlock internet" (device + minutes),
- ``schedule_minutes`` / ``scheduled``: re-lock expired devices, retry failed locks, warn the child shortly before
  the end, tell the parents when locking keeps failing,
- ``today_cards``: parents see which devices are unlocked and for how long,
- ``test_settings``: a real connection test with the unsaved values,
- ``app.plugins.api.notify``: the notifications.

Safety rule of the whole plugin: **when in doubt, block**. State is written before a device is opened, a failed
unlock leaves a record that makes the scheduler lock the device again, and a lock that cannot be done is retried
and reported to the parents; it is never silent.
"""

from __future__ import annotations

import asyncio
import math
import time
import weakref
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.logging_config import get_logger
from app.plugins import Plugin, Setting, get_setting

from . import config as cfgmod
from .backends import BackendError, make_backend, run
from .state import StateStore

try:  # hubs older than 1.0.0-beta.3 have no plugin notifications
    from app.plugins.api import notify
except Exception:  # noqa: BLE001
    notify = None  # type: ignore[assignment]

logger = get_logger("plugin.screentime")

PLUGIN_ID = "screentime"
WARN_SECONDS = 5 * 60
MAX_TOTAL_MINUTES = 8 * 60  # however often a reward is redeemed, a device is never open longer than this at once
OLD_HUB = ("This hub is too old for the screen time plugin: it needs Familienhub 1.0.0-beta.3 or newer "
           "(reward actions and scheduled jobs).")


def hub_supports_hooks() -> bool:
    """False on hubs that predate the hooks this plugin needs; the plugin then stays inactive."""
    return notify is not None and all(
        hasattr(Plugin, name) for name in ("reward_actions", "run_reward_action", "scheduled", "test_settings"))


def _result(ok: bool, message: str) -> Dict[str, Any]:
    return {"ok": ok, "message": message}


def _test_result(level: str, message: str) -> Dict[str, Any]:
    return {"ok": level in ("ok", "info", "warn"), "level": level, "message": message}


class ScreentimePlugin(Plugin):
    def __init__(self, state: Optional[StateStore] = None) -> None:
        super().__init__(
            id=PLUGIN_ID,
            name="Screen time as a reward",
            version="1.0.0",
            description="Unlock a child's internet access for N minutes when a parent approves a reward",
            capabilities=frozenset(),
            settings=(
                Setting("SCREENTIME_BACKEND", "System (fritzbox, adguard or pihole)"),
                Setting("SCREENTIME_URL", "Address"),
                Setting("SCREENTIME_USERNAME", "Username", required=False),
                Setting("SCREENTIME_PASSWORD", "Password", secret=True),
                Setting("SCREENTIME_DEVICES", "Devices"),
                Setting("SCREENTIME_DEFAULT_LOCKED", "Block again afterwards", required=False),
                Setting("SCREENTIME_VERIFY_TLS", "Verify HTTPS certificate", required=False),
            ),
        )
        self.schedule_minutes = 5
        self.state = state or StateStore()
        self._locks: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()
        self._timers: Dict[str, "asyncio.Task[None]"] = {}

    # ---- configuration --------------------------------------------------------------------------------------

    def _values(self) -> Dict[str, Any]:
        return {key: get_setting(key) for key in cfgmod.KEYS}

    def _config(self) -> Optional[cfgmod.Config]:
        if not hub_supports_hooks():
            return None
        return cfgmod.parse(self._values())[0]

    def is_configured(self) -> bool:
        try:
            return self._config() is not None
        except Exception:  # noqa: BLE001 - never crash the hub's plugin list
            return False

    async def check(self) -> Optional[Dict[str, Any]]:
        if not hub_supports_hooks():
            return {"status": "unavailable", "message": OLD_HUB}
        return None

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    # ---- reward action --------------------------------------------------------------------------------------

    def reward_actions(self) -> List[Dict[str, Any]]:
        cfg = self._config()
        if cfg is None or not cfg.devices:
            return []
        return [{
            "id": "unlock",
            "label": "Unlock internet",
            "params": [
                {"key": "device", "label": "Device", "type": "select", "required": True,
                 "options": [{"value": name, "label": name} for name in cfg.devices]},
                {"key": "minutes", "label": f"Minutes ({cfgmod.MIN_MINUTES}-{cfgmod.MAX_MINUTES})", "type": "number",
                 "required": True, "default": cfgmod.DEFAULT_MINUTES,
                 "help": "How long the device is online. Redeeming again while it is online adds the time."},
            ],
        }]

    async def run_reward_action(self, action_id, params, child, reward, db) -> Dict[str, Any]:
        cfg = self._config()
        if cfg is None:
            return _result(False, "Screen time plugin is not set up (or the hub is too old)")
        if action_id != "unlock":
            return _result(False, "Unknown action")
        name = str((params or {}).get("device", ""))
        if name not in cfg.devices:
            return _result(False, "This device is no longer in the plugin's device list")
        minutes = cfgmod.parse_minutes((params or {}).get("minutes", cfgmod.DEFAULT_MINUTES))
        if minutes is None:
            return _result(False, f"Minutes must be a whole number from {cfgmod.MIN_MINUTES} to {cfgmod.MAX_MINUTES}")
        backend = make_backend(cfg)
        async with self._lock():
            entries = self.state.load()
            now = time.time()
            previous = entries.get(name)
            active = previous is not None and previous["until"] > now
            start = previous["until"] if active else now
            until = min(start + minutes * 60, now + MAX_TOTAL_MINUTES * 60)
            user_id = getattr(child, "id", None)
            entry = {"until": until, "user_id": user_id if isinstance(user_id, int) else None,
                     "lang": "de" if getattr(child, "language", None) == "de" else "en",
                     "warned": False, "failures": 0}
            try:  # state first: if we cannot remember to lock again, we do not open
                self.state.save({**entries, name: entry})
            except OSError:
                logger.warning("Screen time: state file not writable, device not unlocked")
                return _result(False, "Could not save the timer (data folder not writable), nothing was unlocked")
            try:
                await run(backend.set_blocked(name, False))
            except BackendError as exc:
                # The call may have worked although the answer got lost: keep a record that expires at once, so the
                # scheduler blocks the device again to be safe. A still-running earlier unlock stays as it was.
                self._remember_after_failure(entries, name, previous, now)
                logger.warning("Screen time: unlock failed (%s)", cfg.backend)
                return _result(False, f"Could not unlock {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                self._remember_after_failure(entries, name, previous, now)
                logger.warning("Screen time: unlock failed: %s", type(exc).__name__)
                return _result(False, f"Could not unlock {name}")
        self._arm(name, until)
        local = datetime.fromtimestamp(until, self._tz()).strftime("%H:%M")
        extra = " (time added)" if active else ""
        if start + minutes * 60 > now + MAX_TOTAL_MINUTES * 60:
            extra += " (limited to 8 hours at a time)"
        return _result(True, f"{name} is online until {local}{extra}")

    def _remember_after_failure(self, entries, name, previous, now) -> None:
        try:
            if previous is not None and previous["until"] > now:
                self.state.save({**entries, name: previous})
            else:
                self.state.save({**entries, name: {**(previous or {"user_id": None, "lang": "en", "warned": True,
                                                                  "failures": 0}), "until": now}})
        except OSError:
            logger.warning("Screen time: state file not writable after a failed unlock")

    @staticmethod
    def _tz():
        try:
            from app.config import settings

            return ZoneInfo(settings.timezone)
        except Exception:  # noqa: BLE001
            return None  # local time of the server

    # ---- timers and the scheduled job ------------------------------------------------------------------------

    def _arm(self, name: str, until: float) -> None:
        """Lock at the exact minute while the hub keeps running (the 5-minute job is the safety net)."""
        try:
            loop = asyncio.get_running_loop()
            old = self._timers.get(name)
            if old is not None and not old.done():
                old.cancel()
            self._timers[name] = loop.create_task(self._timer(name, until))
        except RuntimeError:
            pass

    async def _timer(self, name: str, until: float) -> None:
        try:
            await asyncio.sleep(max(0.0, until - time.time()))
            await self._expire_due(only=name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Screen time: timer failed: %s", type(exc).__name__)

    async def scheduled(self, db) -> None:
        await self._expire_due()

    async def _expire_due(self, only: Optional[str] = None) -> None:
        cfg = self._config()
        if cfg is None:
            return
        backend = make_backend(cfg)
        async with self._lock():
            entries = self.state.load()
            now = time.time()
            for name, entry in list(entries.items()):
                if only is not None and name != only:
                    continue
                if name not in cfg.devices:
                    logger.warning("Screen time: a device was removed from the settings while unlocked; "
                                   "it cannot be locked by the plugin any more")
                    entries.pop(name)
                    self._save(entries)
                    await self._notify_parents(
                        "Device no longer managed / Gerät nicht mehr verwaltet",
                        f"{name} was unlocked when it was removed from the plugin settings. Check that it is blocked. "
                        f"/ {name} war freigeschaltet, als es aus den Einstellungen entfernt wurde. Bitte Sperre prüfen.")
                    continue
                if entry["until"] > now + 1.0:
                    timer = self._timers.get(name)
                    if timer is None or timer.done():
                        self._arm(name, entry["until"])
                    if entry["until"] - now <= WARN_SECONDS and not entry["warned"]:
                        entry["warned"] = True
                        self._save(entries)
                        await self._notify_child(entry, "About 5 minutes left", f"Internet time on {name} ends soon.",
                                                 "Noch etwa 5 Minuten", f"Die Internetzeit auf {name} endet bald.")
                    continue
                await self._expire(cfg, backend, entries, name, entry)

    def _save(self, entries) -> None:
        try:
            self.state.save(entries)
        except OSError:
            logger.warning("Screen time: state file not writable")

    async def _expire(self, cfg, backend, entries, name: str, entry: Dict[str, Any]) -> None:
        if cfg.default_locked:
            try:
                await run(backend.set_blocked(name, True))
            except Exception as exc:  # noqa: BLE001
                entry["failures"] += 1
                self._save(entries)
                reason = str(exc) if isinstance(exc, BackendError) else type(exc).__name__
                logger.warning("Screen time: could not block a device again (attempt %d): %s", entry["failures"], reason)
                if entry["failures"] == 2 or entry["failures"] % 12 == 0:
                    await self._notify_parents(
                        "Device could not be blocked / Gerät nicht gesperrt",
                        f"{name} is still online after its time ran out. The plugin keeps trying. Check the "
                        f"connection or block it by hand. / {name} ist nach Ablauf der Zeit noch online. "
                        "Das Plugin versucht es weiter. Bitte Verbindung prüfen oder manuell sperren.")
                return
        entries.pop(name, None)
        self._save(entries)
        task = self._timers.pop(name, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        if cfg.default_locked:
            await self._notify_child(entry, "Internet time is over", f"{name} is blocked again.",
                                     "Internetzeit ist vorbei", f"{name} ist wieder gesperrt.")
        else:
            await self._notify_child(entry, "Internet time is over", f"The time for {name} has run out.",
                                     "Internetzeit ist vorbei", f"Die Zeit für {name} ist abgelaufen.")

    # ---- notifications ----------------------------------------------------------------------------------------

    async def _notify_child(self, entry, title_en: str, body_en: str, title_de: str, body_de: str) -> None:
        if notify is None or not isinstance(entry.get("user_id"), int):
            return
        de = entry.get("lang") == "de"
        try:
            await notify(PLUGIN_ID, user_ids=[entry["user_id"]], title=title_de if de else title_en,
                         body=body_de if de else body_en)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Screen time: notification failed: %s", type(exc).__name__)

    async def _notify_parents(self, title: str, body: str) -> None:
        if notify is None:
            return
        try:
            await notify(PLUGIN_ID, roles=["partner", "admin"], title=title, body=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Screen time: notification failed: %s", type(exc).__name__)

    # ---- Today card -------------------------------------------------------------------------------------------

    async def today_cards(self, user, db) -> List[Dict[str, Any]]:
        role = str(getattr(user, "role", "") or "").lower()
        if role not in ("partner", "admin") and getattr(user, "is_admin", False) is not True:
            return []
        now = time.time()
        live = sorted(((n, e["until"]) for n, e in self.state.load().items() if e["until"] > now), key=lambda x: x[1])
        if not live:
            return []
        return [{
            "id": "unlocked", "title": "Internet unlocked", "icon": "wifi", "badge": str(len(live)),
            "lines": [{"text": n, "secondary": f"{max(1, math.ceil((u - now) / 60))} min left"} for n, u in live],
        }]

    # ---- connection test --------------------------------------------------------------------------------------

    async def test_settings(self, values: Dict[str, str]) -> Dict[str, Any]:
        if not hub_supports_hooks():
            return _test_result("error", OLD_HUB)
        cfg, errors = cfgmod.parse(values)
        if cfg is None:
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            return _test_result("error", errors[0] + more)
        backend = make_backend(cfg)

        async def probe() -> Dict[str, Any]:
            description = await backend.probe()
            states, blocked, open_ = [], 0, []
            for name in cfg.devices:
                try:
                    is_blocked = await backend.device_state(name)
                except BackendError as exc:
                    return _test_result("error", f"{description} is reachable, but device '{name}': {exc}")
                states.append(f"{name}: {'blocked' if is_blocked else 'online'}")
                if is_blocked:
                    blocked += 1
                else:
                    open_.append(name)
            text = f"Connected to {description}. " + "; ".join(states)
            if cfg.default_locked and open_:
                return _test_result("warn", text + ". Not blocked yet: nothing happens when the time ends. "
                                    "Block these devices once (see the README)")
            return _test_result("ok", text)

        try:
            return await run(probe(), 13)
        except BackendError as exc:
            return _test_result("error", str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Screen time: connection test failed: %s", type(exc).__name__)
            return _test_result("error", "The connection test failed")
