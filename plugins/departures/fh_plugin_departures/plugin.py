"""Departures at your stop: the next buses and trains on the Today screen, from a transport.rest (HAFAS) instance.

Extension points used (docs/plugins.md): ``today_cards`` (the card) and ``test_settings`` (connection test), plus
``check`` for the admin overview. The plugin has no routes and no capability.

Be a good guest on a public free API: answers are cached for 45 s (failures for 30 s), concurrent requests share one
call, nothing is polled in the background, and the timeout is 8 s.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from app.core.logging_config import get_logger
from app.plugins import Plugin, Setting, get_setting

from . import logic
from .logic import Departure

logger = get_logger("plugin.departures")

VERSION = "1.0.0"
MIN_HUB = "1.0.0-beta.3"
USER_AGENT = f"familienhub-plugin-departures/{VERSION} (+https://github.com/Brottuetchen/familienhub-store)"
HTTP_TIMEOUT = 8.0  # seconds, hard limit for one request
CARD_WAIT = 3.5  # the hub gives today_cards 4 s; a slower call keeps running in the background and fills the cache
CACHE_OK = 45.0
CACHE_FAIL = 30.0
NAME_CACHE = 6 * 3600.0
FETCH_RESULTS = 30

INCOMPATIBLE = (f"This plugin needs Familienhub {MIN_HUB} or newer (Today cards and connection test). "
                "Update the hub to use it.")


def _now() -> datetime:
    return datetime.now().astimezone()


def hub_problem() -> Optional[str]:
    """None if this hub has the plugin hooks (1.0.0-beta.3 and newer), else an explanation. Feature detection, not a
    version compare: the hooks are what the plugin needs. Never raises."""
    try:
        if (importlib.util.find_spec("app.plugins.hooks") is None or not hasattr(Plugin, "today_cards")
                or not hasattr(Plugin, "test_settings")):
            return INCOMPATIBLE
    except Exception:  # noqa: BLE001 - odd import state: treat as incompatible rather than crash
        return INCOMPATIBLE
    return None


def hub_zone():
    name = "Europe/Berlin"
    try:
        from app.config import settings

        name = str(getattr(settings, "timezone", "") or name)
    except Exception:  # noqa: BLE001
        pass
    return logic.zone(name)


@dataclass
class Fetched:
    status: str  # ok | unavailable | rate_limited | not_found
    deps: List[Departure]


# (base, stop) -> (monotonic time stored, result); (base, name) -> (stored, id, name)
_cache: Dict[Tuple[str, str], Tuple[float, Fetched]] = {}
_names: Dict[Tuple[str, str], Tuple[float, Optional[Tuple[str, str]]]] = {}
_stop_titles: Dict[Tuple[str, str], str] = {}
_inflight: Dict[Tuple[int, str, str], "asyncio.Task[Fetched]"] = {}


def clear_caches() -> None:
    _cache.clear()
    _names.clear()
    _stop_titles.clear()
    _inflight.clear()


async def _request(client: httpx.AsyncClient, base: str, path: str, params: Dict[str, Any]) -> httpx.Response:
    return await client.get(f"{base}{path}", params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def _client(timeout: float = HTTP_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False)


def unknown_stop(r: httpx.Response) -> bool:
    """The instance does not know this stop id: 400/404, or (seen on v6.vbb.transport.rest) a 500 saying the id is invalid."""
    if r.status_code in (400, 404):
        return True
    if r.status_code == 500:
        try:
            body = r.json()
        except ValueError:
            return False
        return isinstance(body, dict) and "id must be" in str(body.get("message") or body.get("msg") or "")
    return False


async def _fetch_departures(base: str, stop_id: str, walk: int, tz) -> Fetched:
    params = {"duration": min(240, 90 + walk), "results": FETCH_RESULTS, "remarks": "false", "linesOfStops": "false"}
    try:
        async with _client() as client:
            r = await _request(client, base, f"/stops/{quote(stop_id, safe='')}/departures", params)
        if r.status_code == 429:
            logger.warning("Departures: the transport.rest instance says 'too many requests'")
            return Fetched("rate_limited", [])
        if unknown_stop(r):
            return Fetched("not_found", [])
        r.raise_for_status()
        deps = logic.parse_departures(r.json(), tz)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Departures: instance not usable (%s)", exc.__class__.__name__)  # no addresses in the log
        return Fetched("unavailable", [])
    if deps is None:
        logger.warning("Departures: unexpected answer shape")
        return Fetched("unavailable", [])
    for d in deps:
        if d.stop_name:
            _stop_titles[(base, stop_id)] = d.stop_name
            break
    return Fetched("ok", deps)


async def get_departures(base: str, stop_id: str, walk: int, tz, wait: Optional[float] = None) -> Fetched:
    """Cached, shared fetch. A call that takes longer than ``wait`` returns ``unavailable`` for now but keeps running
    (shielded) so the next screen load finds the answer in the cache."""
    key = (base, stop_id)
    hit = _cache.get(key)
    if hit and _time.monotonic() - hit[0] < (CACHE_OK if hit[1].status == "ok" else CACHE_FAIL):
        return hit[1]
    loop = asyncio.get_running_loop()
    task_key = (id(loop), base, stop_id)
    task = _inflight.get(task_key)
    if task is None or task.done():
        task = loop.create_task(_fetch_departures(base, stop_id, walk, tz))
        _inflight[task_key] = task

        def _done(t: "asyncio.Task[Fetched]") -> None:
            _inflight.pop(task_key, None)
            if not t.cancelled() and t.exception() is None:
                _cache[key] = (_time.monotonic(), t.result())

        task.add_done_callback(_done)
    try:
        return await asyncio.wait_for(asyncio.shield(task), CARD_WAIT if wait is None else wait)
    except asyncio.TimeoutError:
        return Fetched("unavailable", [])


async def search_stops(base: str, query: str, timeout: float = HTTP_TIMEOUT) -> Optional[List[Dict[str, str]]]:
    """Stops matching a name, ``[{id, name}]``; None if the instance cannot be used."""
    params = {"query": query, "results": 5, "stops": "true", "addresses": "false", "poi": "false", "linesOfStops": "false"}
    try:
        async with _client(timeout) as client:
            r = await _request(client, base, "/locations", params)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    found = []
    for item in data:
        if isinstance(item, dict) and item.get("type") in ("stop", "station") and item.get("id") and item.get("name"):
            found.append({"id": str(item["id"]), "name": " ".join(str(item["name"]).split())[:60]})
    return found


async def resolve_stop(base: str, stop: str) -> Optional[Tuple[str, str]]:
    """``(id, name)`` for the setting: an id is used as it is, a name is looked up once (cached 6 h)."""
    if logic.looks_like_id(stop):
        return stop.strip(), _stop_titles.get((base, stop.strip()), "")
    key = (base, stop.strip().lower())
    hit = _names.get(key)
    if hit and _time.monotonic() - hit[0] < (NAME_CACHE if hit[1] else CACHE_FAIL):
        return hit[1]
    found = await search_stops(base, stop.strip(), timeout=CARD_WAIT)
    result = (found[0]["id"], found[0]["name"]) if found else None
    _names[key] = (_time.monotonic(), result)
    return result


class DeparturesPlugin(Plugin):
    def __init__(self) -> None:
        problem = hub_problem()
        super().__init__(
            id="departures",  # same as "id" in familienhub-plugin.json
            name="Departures at your stop",
            version=VERSION,
            description="Next buses and trains from your stop on Today (transport.rest)"
                        + (f" - {problem}" if problem else ""),
            capabilities=frozenset(),
            settings=(
                Setting("DEPARTURES_STOP", "Stop (ID)"),
                Setting("DEPARTURES_BASE_URL", "transport.rest address", required=False),
                Setting("DEPARTURES_LINES", "Only these lines", required=False),
                Setting("DEPARTURES_DIRECTIONS", "Only these directions", required=False),
                Setting("DEPARTURES_MAX", "Number of departures", required=False),
                Setting("DEPARTURES_WALK_MINUTES", "Walking time to the stop (minutes)", required=False),
                Setting("DEPARTURES_SHOW_FROM", "Show from (HH:MM)", required=False),
                Setting("DEPARTURES_SHOW_UNTIL", "Show until (HH:MM)", required=False),
                Setting("DEPARTURES_WEEKDAYS_ONLY", "Monday to Friday only", required=False),
            ),
        )

    def is_configured(self) -> bool:
        """False on a hub without the plugin hooks (older than 1.0.0-beta.3): the plugin then stays inactive."""
        if hub_problem():
            return False
        return super().is_configured()

    def health_url(self) -> Optional[str]:
        return None  # the default would probe the base URL; check() below uses the cached stop request instead

    async def check(self) -> Optional[Dict[str, Any]]:
        if hub_problem():
            return {"status": "error", "message": INCOMPATIBLE}
        cfg = logic.read_config(get_setting)
        resolved = await resolve_stop(cfg.base_url, cfg.stop)
        if resolved is None:
            return {"status": "error", "message": "Stop not found"}
        result = await get_departures(cfg.base_url, resolved[0], cfg.walk, hub_zone(), wait=HTTP_TIMEOUT)
        if result.status == "ok":
            return {"status": "ok", "message": "Reachable"}
        return {"status": "error", "message": {"rate_limited": "Too many requests", "not_found": "Stop not found"}
                .get(result.status, "Not reachable")}

    async def today_cards(self, user, db) -> List[Dict[str, Any]]:
        if hub_problem():
            return []
        cfg = logic.read_config(get_setting)
        if not cfg.stop:
            return []
        tz = hub_zone()
        now = _now().astimezone(tz)
        if not logic.in_window(now, cfg):
            return []  # outside the time window: no request at all
        resolved = await resolve_stop(cfg.base_url, cfg.stop)
        if resolved is None:
            return []
        stop_id = resolved[0]
        result = await get_departures(cfg.base_url, stop_id, cfg.walk, tz)
        if result.status != "ok":
            return []
        deps = logic.select(result.deps, cfg, now)
        title = _stop_titles.get((cfg.base_url, stop_id)) or resolved[1]
        card = logic.build_card(deps, cfg, now, logic.language_of(user), title)
        return [card] if card else []

    async def test_settings(self, values: Dict[str, str]) -> Dict[str, Any]:
        """Connection test with the typed values: resolves the stop and shows its name and the next departure."""
        def result(ok: bool, level: str, message: str) -> Dict[str, Any]:
            return {"ok": ok, "level": level, "message": message}

        problem = hub_problem()
        if problem:
            return result(False, "error", problem)
        values = {k: ("" if v is None else str(v)) for k, v in (values or {}).items()}
        problems = logic.validate_values(values)
        if problems:
            return result(False, "error", " ".join(problems))
        cfg = logic.read_config(values.get)
        tz = hub_zone()
        now = _now().astimezone(tz)

        note = ""
        stop_id, name = cfg.stop, ""
        if not logic.looks_like_id(cfg.stop):
            found = await search_stops(cfg.base_url, cfg.stop)
            if found is None:
                return result(False, "error", "The transport.rest instance is not reachable or answers with something unexpected. "
                                               "Check the address.")
            if not found:
                return result(False, "error", f"No stop found for \"{cfg.stop}\". Try another spelling, or search for it "
                                              "(see the README) and enter the ID.")
            stop_id, name = found[0]["id"], found[0]["name"]
            others = "; ".join(f"{f['name']} = {f['id']}" for f in found[1:3])
            note = (f" \"{cfg.stop}\" was resolved to {name}. Enter the ID {stop_id} as the stop to make it exact"
                    + (f" (other matches: {others})" if others else "") + ".")

        async with _client() as client:
            try:
                r = await _request(client, cfg.base_url, f"/stops/{quote(stop_id, safe='')}/departures",
                                   {"duration": min(240, 90 + cfg.walk), "results": FETCH_RESULTS, "remarks": "false",
                                    "linesOfStops": "false"})
            except httpx.HTTPError:
                return result(False, "error", "The transport.rest instance is not reachable. Check the address and the "
                                              "internet connection of your server.")
        if unknown_stop(r):
            return result(False, "error", f"The instance does not know a stop with the ID {stop_id}. Search for your stop "
                                          "(see the README) and enter its ID. IDs differ between instances.")
        if r.status_code == 429:
            return result(False, "warn", "The instance says too many requests. Wait a minute and test again.")
        if r.status_code != 200:
            return result(False, "error", f"The instance answers with error {r.status_code}.")
        try:
            deps = logic.parse_departures(r.json(), tz)
        except ValueError:
            deps = None
        if deps is None:
            return result(False, "error", "The address answers, but not like a transport.rest instance (unexpected answer).")

        name = name or next((d.stop_name for d in deps if d.stop_name), "") or stop_id
        if name != stop_id:
            _stop_titles[(cfg.base_url, stop_id)] = name
        if not deps:
            return result(True, "warn", f"Stop found: {name}, but no departures in the next 90 minutes (night or "
                                        f"disruption?).{note}")
        shown = logic.select(deps, cfg, now, limit=1)
        level = "warn" if note else "ok"
        if not shown:
            filters = bool(cfg.lines or cfg.directions or cfg.walk)
            hint = " Check the line, direction and walking-time settings." if filters else ""
            return result(True, "warn", f"Stop found: {name}. None of its next departures matches your settings.{hint}{note}")
        card = logic.card_line(shown[0], cfg, now, "en")
        window = ""
        if cfg.show_from or cfg.show_until or cfg.weekdays_only:
            window = (" The card is only shown "
                      + ("Monday to Friday" if cfg.weekdays_only else "")
                      + (" " if cfg.weekdays_only and (cfg.show_from or cfg.show_until) else "")
                      + (f"between {cfg.show_from.strftime('%H:%M') if cfg.show_from else '00:00'} and "
                         f"{cfg.show_until.strftime('%H:%M') if cfg.show_until else '24:00'}"
                         if cfg.show_from or cfg.show_until else "") + ".")
        return result(True, level, f"Stop found: {name}. Next: {card['text']} ({card['secondary']}).{window}{note}")
