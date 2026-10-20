"""WebUntis timetable and substitutions for Familienhub (capability ``timetable``).

- ``timetable`` / ``timetable_people``: the lessons of a child (cancelled, substituted, rooms, teachers, notes) and
  the students the account can see, for the parents' mapping dropdown.
- ``scheduled``: every 15 minutes (06:00-22:00 hub time) new cancellations and substitutions are pushed to the
  child's parents and to the child (``app.plugins.api.notify``).
- ``test_settings``: a real login with the typed values.

Needs hub 1.0.0-beta.3 (timetable hook, scheduled jobs, notify API). On an older hub the plugin reports that and
stays inactive instead of crashing.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.logging_config import get_logger
from app.plugins import Plugin, Setting, get_setting

from .client import WebUntisClient, WebUntisError
from .messages import State, changes, compose, lesson_key
from .parsing import normalize_server

try:  # the hooks of hub 1.0.0-beta.3
    from app.plugins.api import notify
except Exception:  # noqa: BLE001 - older hub: no plugin API
    notify = None  # type: ignore[assignment]

logger = get_logger("plugin.webuntis")

COMPATIBLE = bool(notify is not None and hasattr(Plugin, "timetable") and hasattr(Plugin, "scheduled"))
NEEDS_HUB = "needs Familienhub 1.0.0-beta.3 or newer"

CACHE_TTL = 600.0          # a fresh answer per child and range
STALE_TTL = 6 * 3600.0     # shown when WebUntis fails
TIMEGRID_TTL = 6 * 3600.0
HUB_BUDGET = 2.6           # the hub gives timetable() 3 s; the fetch itself goes on in the background
BACKOFF = 600.0            # after a rejected login nothing is tried for 10 minutes (do not lock the account)
JOB_BUDGET = 100.0         # the hub stops a job after 120 s
DAY_START, DAY_END = 6, 22

MESSAGES = {
    "server": "WebUntis is not reachable (server not answering).",
    "not_found": "Server or school name not found. Check both against your WebUntis login address.",
    "credentials": "WebUntis rejected the username or password.",
    "no_right": "This account may not read timetables.",
    "session": "The WebUntis session expired.",
    "malformed": "WebUntis answered with something unexpected. Check server and school name.",
    "rpc": "WebUntis reported an error.",
}


@dataclass(frozen=True)
class Config:
    server: str
    school: str
    username: str
    password: str
    notify: bool
    notify_days: int

    @property
    def account(self) -> Tuple[str, str, str]:
        return (self.server, self.school, self.username)  # no password: it is not part of a cache key


def _flag(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _days(value: Any, default: int = 1) -> int:
    try:
        return max(0, min(7, int(float(str(value).strip()))))
    except (TypeError, ValueError):
        return default


def read_config(values: Optional[Dict[str, Any]] = None) -> Optional[Config]:
    """Settings (typed values or ``get_setting``) -> Config; None when something required is missing or malformed."""
    def get(key: str) -> Any:
        return values.get(key) if values is not None else get_setting(key)

    server = normalize_server(get("WEBUNTIS_SERVER"))
    school, user, password = (str(get(k) or "").strip() for k in ("WEBUNTIS_SCHOOL", "WEBUNTIS_USERNAME", "WEBUNTIS_PASSWORD"))
    if not (server and school and user and password):
        return None
    return Config(server, school, user, password, _flag(get("WEBUNTIS_NOTIFY")), _days(get("WEBUNTIS_NOTIFY_DAYS")))


def _state_path() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "plugins" / "webuntis" / "state.json"


def _now() -> datetime:
    from app.core.timeutil import local_tz

    return datetime.now(local_tz())


def _ok(outcome: Any) -> bool:
    """Nothing more to do for this change: it was sent, or there was nobody to send it to."""
    return isinstance(outcome, dict) and (bool(outcome.get("sent")) or outcome.get("reason") == "no_recipients")


class WebUntisPlugin(Plugin):
    def __init__(self):
        super().__init__(
            id="webuntis", name="WebUntis", version="1.0.0",
            description="Fills the children's timetable from WebUntis and pushes cancellations and substitutions.",
            capabilities=frozenset({"timetable"}),
            settings=(Setting("WEBUNTIS_SERVER", "WebUntis server"),
                      Setting("WEBUNTIS_SCHOOL", "School (login name)"),
                      Setting("WEBUNTIS_USERNAME", "Username"),
                      Setting("WEBUNTIS_PASSWORD", "Password", secret=True),
                      Setting("WEBUNTIS_NOTIFY", "Push on cancellations and substitutions", required=False),
                      Setting("WEBUNTIS_NOTIFY_DAYS", "Days ahead to report", required=False)),
        )
        self.schedule_minutes = 15
        self._cache: Dict[Tuple[Any, ...], Tuple[float, List[Dict[str, Any]]]] = {}
        self._inflight: Dict[Tuple[Any, ...], "asyncio.Task"] = {}
        self._grid: Dict[Tuple[str, str, str], Tuple[float, Dict[str, int]]] = {}
        self._blocked: Dict[Tuple[str, str, str], float] = {}

    # --- compatibility and status ---------------------------------------------------------------------------

    def is_configured(self) -> bool:
        return COMPATIBLE and super().is_configured()

    def missing_settings(self) -> List[str]:
        return [NEEDS_HUB] if not COMPATIBLE else super().missing_settings()

    async def check(self) -> Optional[Dict[str, Any]]:
        if not COMPATIBLE:
            return {"status": "error", "message": f"WebUntis {NEEDS_HUB}"}
        if read_config() is None and super().is_configured():
            return {"status": "error", "message": "Server address or settings look wrong"}
        return None

    def health_url(self) -> Optional[str]:
        return None  # the address is a user-chosen server; the connection test is the check

    # --- timetable hook -------------------------------------------------------------------------------------

    def _is_blocked(self, cfg: Config) -> bool:
        return self._blocked.get(cfg.account, 0.0) > time.monotonic()

    async def _fetch(self, cfg: Config, person_id: str, first: date, last: date) -> List[Dict[str, Any]]:
        """One session: login, time grid (cached), lessons, logout. Raises WebUntisError only."""
        async with WebUntisClient(cfg.server, cfg.school, cfg.username, cfg.password) as client:
            try:
                periods = await self._periods(client, cfg)
                return await client.lessons(person_id, first, last, periods)
            except WebUntisError as exc:
                if exc.kind == "credentials":
                    self._blocked[cfg.account] = time.monotonic() + BACKOFF
                raise

    async def _periods(self, client: WebUntisClient, cfg: Config) -> Dict[str, int]:
        hit = self._grid.get(cfg.account)
        if hit and time.monotonic() - hit[0] < TIMEGRID_TTL:
            return hit[1]
        grid = await client.timegrid()
        if grid:
            self._grid[cfg.account] = (time.monotonic(), grid)
        return grid

    async def _fetch_cached(self, cfg: Config, person_id: str, first: date, last: date) -> List[Dict[str, Any]]:
        key = (cfg.account, person_id, first, last)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        stale = hit[1] if hit and time.monotonic() - hit[0] < STALE_TTL else []
        if self._is_blocked(cfg):
            return stale
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(cfg, person_id, first, last))
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._finished(k, t))
        try:
            # shield: if the hub stops waiting after 3 s, the request goes on and fills the cache for the next ask
            lessons = await asyncio.wait_for(asyncio.shield(task), HUB_BUDGET)
        except asyncio.TimeoutError:
            return stale
        except WebUntisError as exc:
            logger.warning("WebUntis timetable not available: %s", exc.kind)
            return stale
        except Exception as exc:  # noqa: BLE001 - a plugin must never break the app
            logger.warning("WebUntis timetable failed: %s", type(exc).__name__)
            return stale
        return lessons

    def _finished(self, key: Tuple[Any, ...], task: "asyncio.Task") -> None:
        self._inflight.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            if not isinstance(exc, WebUntisError):
                logger.warning("WebUntis timetable failed: %s", type(exc).__name__)
            return
        if len(self._cache) > 200:
            self._cache.clear()
        self._cache[key] = (time.monotonic(), task.result())

    async def timetable(self, child, start, end, db) -> List[Dict[str, Any]]:
        if not self.is_configured():
            return []
        cfg = read_config()
        person = str(getattr(child, "person_id", "") or "")
        if cfg is None or not person:
            return []
        try:
            lessons = await self._fetch_cached(cfg, person, start, end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebUntis timetable failed: %s", type(exc).__name__)
            return []
        return [dict(x) for x in lessons]

    async def timetable_people(self) -> List[Dict[str, Any]]:
        cfg = read_config() if self.is_configured() else None
        if cfg is None or self._is_blocked(cfg):
            return []
        try:
            async with WebUntisClient(cfg.server, cfg.school, cfg.username, cfg.password) as client:
                return await client.students()
        except WebUntisError as exc:
            if exc.kind == "credentials":
                self._blocked[cfg.account] = time.monotonic() + BACKOFF
            logger.warning("WebUntis people not available: %s", exc.kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebUntis people failed: %s", type(exc).__name__)
        return []

    # --- connection test ------------------------------------------------------------------------------------

    async def test_settings(self, values: Dict[str, str]) -> Optional[Dict[str, Any]]:
        def result(level: str, message: str) -> Dict[str, Any]:
            return {"ok": level in ("ok", "info", "warn"), "level": level, "message": message}

        if not COMPATIBLE:
            return result("error", f"WebUntis {NEEDS_HUB}.")
        if normalize_server(values.get("WEBUNTIS_SERVER")) is None:
            return result("error", "The server does not look like a host name. Use only the part before /WebUntis, "
                                   "e.g. xyz.webuntis.com.")
        cfg = read_config(values)
        if cfg is None:
            return result("error", "School name, username and password are required.")
        try:
            async with WebUntisClient(cfg.server, cfg.school, cfg.username, cfg.password) as client:
                await client.login()
                people = await client.students()
                kind = client.person_type
        except WebUntisError as exc:
            return result("error", MESSAGES.get(exc.kind, MESSAGES["rpc"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebUntis test failed: %s", type(exc).__name__)
            return result("error", MESSAGES["server"])
        message = f"Connected to {cfg.school} as {cfg.username}; students visible: {len(people)}"
        if not people:
            hint = " This looks like a teacher account." if kind == 2 else ""
            return result("warn", message + ". No students found: use a parent or student account." + hint)
        return result("ok", message)

    # --- scheduled job: push new cancellations and substitutions --------------------------------------------

    async def scheduled(self, db) -> None:
        if not self.is_configured():
            return
        cfg = read_config()
        now = _now()
        if cfg is None or not cfg.notify or not DAY_START <= now.hour < DAY_END or self._is_blocked(cfg):
            return
        from app.models.timetable import TimetableSource
        from app.models.user import User

        rows = db.query(TimetableSource).filter(TimetableSource.plugin_id == self.id).all()
        kids = []
        for row in rows:
            user = db.get(User, row.child_id)
            if user is not None and user.is_active:
                kids.append((user, str(row.person_id)))
        if not kids:
            return
        today = now.date()
        last = today + timedelta(days=cfg.notify_days)
        fetched: Dict[int, List[Dict[str, Any]]] = {}
        try:
            async with WebUntisClient(cfg.server, cfg.school, cfg.username, cfg.password) as client:
                periods = await self._periods(client, cfg)
                deadline = time.monotonic() + JOB_BUDGET
                for user, person in kids:
                    if time.monotonic() > deadline:
                        break
                    try:
                        fetched[user.id] = await client.lessons(person, today, last, periods)
                    except WebUntisError as exc:
                        if exc.kind == "credentials":
                            self._blocked[cfg.account] = time.monotonic() + BACKOFF
                            break
                        logger.warning("WebUntis job: a child could not be read (%s)", exc.kind)
        except WebUntisError as exc:
            if exc.kind == "credentials":
                self._blocked[cfg.account] = time.monotonic() + BACKOFF
            logger.warning("WebUntis job not possible: %s", exc.kind)
            return
        if not fetched:
            return
        state = State(_state_path())
        state.forget_others([u.id for u, _ in kids])
        parents = db.query(User).filter(User.is_active == True, User.role.in_(("partner", "admin"))).all()  # noqa: E712
        for user, person in kids:
            if user.id in fetched:
                await self._report(state, user, person, fetched[user.id], parents, today)
        if not state.save():
            logger.warning("WebUntis job: state file not writable (changes may be reported again)")

    async def _report(self, state: State, child, person: str, lessons: List[Dict[str, Any]], parents, today: date) -> None:
        current = changes(lessons)
        seen = state.child(child.id, person)
        keys_now = {day: sorted({lesson_key(x) for x in items}) for day, items in current.items()}
        if seen is None:  # first look at this child: remember, do not flood
            state.set_child(child.id, person, keys_now, today)
            return
        new: Dict[date, List[Dict[str, Any]]] = {}
        for day, items in current.items():
            fresh = [x for x in items if lesson_key(x) not in set(seen.get(day, []))]
            if fresh:
                new[date.fromisoformat(day)] = fresh
        if new and not await self._send(child, parents, new, today):
            return  # not delivered (limit, error): keep the old state and try again next time
        state.set_child(child.id, person, keys_now, today)

    async def _send(self, child, parents, new: Dict[date, List[Dict[str, Any]]], today: date) -> bool:
        groups: Dict[str, List[int]] = {}
        for person in parents:
            groups.setdefault("de" if (person.language or "de") == "de" else "en", []).append(person.id)
        child_lang = "de" if (getattr(child, "language", None) or "de") == "de" else "en"
        name = child.effective_name
        delivered = False
        sent_any = False
        for lang, ids in groups.items():
            ids = [i for i in ids if i != child.id]
            text = compose(name, new, today, lang)
            if text and ids:
                outcome = await notify(self.id, user_ids=ids, title=text[0], body=text[1], data={"route": "/today"})
                sent_any = True
                delivered = delivered or _ok(outcome)
        text = compose(name, new, today, child_lang)
        if text:
            outcome = await notify(self.id, user_ids=[child.id], title=text[0], body=text[1], data={"route": "/today"})
            sent_any = True
            delivered = delivered or _ok(outcome)
        return delivered or not sent_any
