"""DWD severe-weather warnings and pollen forecast for Familienhub.

Hooks: ``today_cards`` (cards ``warnings`` and ``pollen``), ``scheduled`` (every 15 minutes: push for new warnings),
``test_settings`` (checks the typed values against the real services). One endpoint, ``/api/plugins/dwd/status``.

Data: Deutscher Wetterdienst (DWD); warnings delivered by Bright Sky.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Request

from app.core.i18n import request_language
from app.core.logging_config import get_logger
from app.core.security import get_current_user
from app.plugins import Plugin, Setting, get_setting

from . import dwd, state

logger = get_logger("plugin.dwd")
router = APIRouter(prefix="/api/plugins/dwd", tags=["dwd"])

MIN_HUB_VERSION = "1.0.0-beta.3"
NOTIFY_ROLES = ("admin", "partner", "child")  # guests do not get warnings
MAX_SINGLE_NOTIFICATIONS = 3  # more new warnings in one run are sent as one summary
DAYS = {"de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"), "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}

TEXT = {
    "not_covered": {"de": "Der Ort liegt außerhalb des DWD-Gebiets", "en": "This location is outside the DWD area"},
    "unavailable": {"de": "Der Deutsche Wetterdienst ist gerade nicht erreichbar",
                    "en": "The German Weather Service is not reachable right now"},
    "no_location": {"de": "Kein Ort eingestellt", "en": "No location set"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lang(user: Any = None, request: Any = None) -> str:
    if request is not None:
        return request_language(request)
    return "en" if str(getattr(user, "language", None) or "de").lower().startswith("en") else "de"


# --- settings ---

def hub_problem() -> Optional[str]:
    """Why this hub cannot run the plugin, or None. Hubs older than 1.0.0-beta.3 lack the plugin hooks."""
    try:
        import importlib

        importlib.import_module("app.plugins.api")
        from app.plugins.base import Plugin as Base
    except Exception:  # noqa: BLE001 - an old hub simply has no such module
        return f"This plugin needs Familienhub {MIN_HUB_VERSION} or newer (plugin hooks are missing)."
    if not all(hasattr(Base, name) for name in ("today_cards", "scheduled", "test_settings")):
        return f"This plugin needs Familienhub {MIN_HUB_VERSION} or newer (plugin hooks are missing)."
    return None


def _setting(values: Optional[Dict[str, Any]], key: str) -> str:
    raw = values.get(key) if values is not None else get_setting(key)
    return "" if raw is None else str(raw).strip()


def parse_float(text: str, low: float, high: float) -> Optional[float]:
    try:
        v = float(text.replace(",", "."))
    except ValueError:
        return None
    return v if low <= v <= high else None


def _weather_coords() -> Optional[Tuple[float, float]]:
    """The weather plugin's location, but only when it was really set: the hub's built-in default location
    must never be taken for the family's home."""
    try:
        from app.config import Settings, settings
        from app.services.config_store import was_saved

        fields = Settings.model_fields
        explicit = all(was_saved(k) for k in ("WEATHER_LATITUDE", "WEATHER_LONGITUDE")) or (
            settings.weather_latitude != fields["weather_latitude"].default
            or settings.weather_longitude != fields["weather_longitude"].default)
        if not explicit:
            return None
        lat = parse_float(str(get_setting("WEATHER_LATITUDE") or ""), -90, 90)
        lon = parse_float(str(get_setting("WEATHER_LONGITUDE") or ""), -180, 180)
        return (lat, lon) if lat is not None and lon is not None else None
    except Exception:  # noqa: BLE001
        return None


def resolve_location(values: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """(lat, lon) and a problem text. Own settings first, then the weather plugin's location."""
    lat_t, lon_t = _setting(values, "DWD_LATITUDE"), _setting(values, "DWD_LONGITUDE")
    if lat_t or lon_t:
        lat, lon = parse_float(lat_t, -90, 90), parse_float(lon_t, -180, 180)
        if not lat_t or not lon_t:
            return None, "Enter both latitude and longitude, or leave both empty."
        if lat is None or lon is None:
            return None, "Latitude (-90 to 90) and longitude (-180 to 180) must be numbers."
        return (lat, lon), None
    coords = _weather_coords()
    if coords:
        return coords, None
    return None, ("No location: enter latitude and longitude, or set the location of the weather plugin "
                  "(the hub's default location is not used).")


def check_severity(text: str) -> Tuple[str, Optional[str]]:
    t = (text or "moderate").strip().lower()
    if t in dwd.SEVERITIES:
        return t, None
    return "moderate", f"Push level must be one of {', '.join(dwd.SEVERITIES)}."


def check_pollen_min(text: str) -> Tuple[str, Optional[str]]:
    t = (text or "medium").strip().lower()
    if t in ("low", "medium", "high"):
        return t, None
    return "medium", "Pollen level must be one of low, medium, high."


def _truthy(text: str, default: bool = True) -> bool:
    return default if text == "" else text.lower() in ("1", "true", "yes", "on")


def _timezone_name() -> str:
    name = str(get_setting("TIMEZONE") or "Europe/Berlin")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return name
    except Exception:  # noqa: BLE001
        return "Europe/Berlin"


def _local(d: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    return d.astimezone(ZoneInfo(_timezone_name()))


def window_text(alert: dwd.Alert, now: datetime, lang: str) -> str:
    """``Tue 14:00–20:00``, ``Tue 22:00 – Wed 06:00``, ``until Tue 20:00`` (already started)."""
    days = DAYS[lang]
    end = _local(alert.expires) if alert.expires else None

    def at(d: datetime) -> str:
        return f"{days[d.weekday()]} {d:%H:%M}"

    if alert.onset <= now:
        if end is None:
            return "bis auf Weiteres" if lang == "de" else "until further notice"
        return f"bis {at(end)}" if lang == "de" else f"until {at(end)}"
    start = _local(alert.onset)
    if end is None:
        return f"ab {at(start)}" if lang == "de" else f"from {at(start)}"
    if start.date() == end.date():
        return f"{at(start)}–{end:%H:%M}"
    return f"{at(start)} – {at(end)}"


class Context:
    """Settings of one call, parsed once."""

    def __init__(self, values: Optional[Dict[str, Any]] = None):
        self.location, self.location_problem = resolve_location(values)
        self.min_severity, _ = check_severity(_setting(values, "DWD_MIN_SEVERITY"))
        self.min_rank = dwd.SEVERITY_RANK[self.min_severity]
        self.notify = _truthy(_setting(values, "DWD_NOTIFY"))
        self.pollen_region = _setting(values, "DWD_POLLEN_REGION")
        self.pollen_types, _ = dwd.parse_pollen_types(_setting(values, "DWD_POLLEN_TYPES"))
        self.pollen_min, _ = check_pollen_min(_setting(values, "DWD_POLLEN_MIN"))


# --- the two data bundles used by cards, endpoint and job ---

async def load_warnings(ctx: Context, *, force: bool = False) -> dwd.AlertsResult:
    if ctx.location is None:
        return dwd.AlertsResult("unavailable")
    return await dwd.fetch_alerts(ctx.location[0], ctx.location[1], _timezone_name(), _now(), force=force)


async def load_pollen(ctx: Context, warnings: Optional[dwd.AlertsResult] = None) -> Optional[dwd.PollenForecast]:
    wanted = ctx.pollen_region
    if not wanted:
        if warnings is None:
            warnings = await load_warnings(ctx)
        wanted = str(warnings.location.get("state") or "")
    if not wanted:
        return None
    payload = await dwd.fetch_pollen_file()
    if payload is None:
        return None
    try:
        return dwd.forecast_for(payload, wanted)
    except ValueError:
        return None


def warning_card(alerts: List[dwd.Alert], lang: str, now: datetime) -> Optional[Dict[str, Any]]:
    if not alerts:
        return None
    top = max(a.rank for a in alerts)
    lines = []
    for a in alerts[:6]:
        headline = dwd.pick(a.headline, lang) or dwd.pick(a.event, lang)
        lines.append({"text": headline, "secondary": window_text(a, now, lang)})
    if len(alerts) > 6:
        extra = len(alerts) - 5
        lines[5] = {"text": f"+{extra} weitere Warnungen" if lang == "de" else f"+{extra} more warnings"}
    plural = len(alerts) > 1
    title = ("Wetterwarnungen" if plural else "Wetterwarnung") if lang == "de" else (
        "Weather warnings" if plural else "Weather warning")
    return {"id": "warnings", "title": title, "icon": "warning", "badge": dwd.LEVEL_BADGE[lang][top], "lines": lines}


def pollen_card(fc: Optional[dwd.PollenForecast], types: List[str], minimum: str, lang: str) -> Optional[Dict[str, Any]]:
    if fc is None:
        return None
    floor = dwd.level_rank(minimum)
    rows = [(k, fc.today.get(k, "none"), fc.tomorrow.get(k, "none")) for k in types]
    rows = [r for r in rows if dwd.level_rank(r[1]) >= floor and dwd.level_rank(r[1]) > 0]
    if not rows:
        return None
    rows.sort(key=lambda r: -dwd.level_rank(r[1]))  # stable: DWD order within a level
    word = dwd.LEVEL_WORD[lang]
    tomorrow_label = "morgen" if lang == "de" else "tomorrow"
    lines = [{"text": f"{dwd.POLLEN_LABEL[lang][k]}: {word[today]}",
              "secondary": f"{tomorrow_label}: {word[tom]}"} for k, today, tom in rows[:6]]
    top = max(dwd.level_rank(r[1]) for r in rows)
    title = "Pollenflug" if lang == "de" else "Pollen"
    if fc.region:
        title = f"{title} · {fc.region}"
    badge = word[dwd.LEVELS[top]].capitalize()
    return {"id": "pollen", "title": title, "icon": "eco", "badge": badge, "lines": lines}


# --- notifications ---

def notification_text(p: state.Pending, lang: str, now: datetime) -> Tuple[str, str]:
    a = p.alert
    level = dwd.LEVEL_NAME[lang][a.rank]
    event = dwd.pick(a.event, lang)
    title = f"{level}: {event}" if event else level
    if p.update:
        title = ("Aktualisiert: " if lang == "de" else "Update: ") + title
    parts = [window_text(a, now, lang)]
    desc = dwd.pick(a.description, lang) or dwd.pick(a.headline, lang)
    if desc:
        parts.append(desc)
    return title[:100], ". ".join(parts)[:500]


def summary_text(pending: List[state.Pending], lang: str) -> Tuple[str, str]:
    n = len(pending)
    top = max(p.alert.rank for p in pending)
    title = f"{n} neue Wetterwarnungen" if lang == "de" else f"{n} new weather warnings"
    events = ", ".join(dict.fromkeys(dwd.pick(p.alert.event, lang) for p in pending if dwd.pick(p.alert.event, lang)))
    head = dwd.LEVEL_NAME[lang][top]
    body = f"{'Höchste Stufe' if lang == 'de' else 'Highest level'}: {head}. {events}"
    return title, body


def recipients_by_language(db) -> Dict[str, List[int]]:
    from app.models.user import User

    out: Dict[str, List[int]] = {}
    for u in db.query(User).filter(User.is_active == True).order_by(User.id).all():  # noqa: E712
        if (u.role or "").lower() in NOTIFY_ROLES:
            out.setdefault(_lang(u), []).append(u.id)
    return out


class DwdPlugin(Plugin):
    schedule_minutes = 15

    def __init__(self) -> None:
        super().__init__(
            id="dwd",
            name="DWD weather warnings & pollen",
            version="1.0.0",
            description="Official severe-weather warnings and the pollen forecast from the German Weather Service",
            capabilities=frozenset(),
            settings=(
                Setting("DWD_LATITUDE", "Latitude", required=False),
                Setting("DWD_LONGITUDE", "Longitude", required=False),
                Setting("DWD_MIN_SEVERITY", "Push from severity", required=False),
                Setting("DWD_NOTIFY", "Push for new warnings", required=False),
                Setting("DWD_POLLEN_REGION", "Pollen region", required=False),
                Setting("DWD_POLLEN_TYPES", "Pollen types", required=False),
                Setting("DWD_POLLEN_MIN", "Show pollen from", required=False),
            ),
        )

    # -- guard and status --

    def not_ready_reason(self) -> Optional[str]:
        return hub_problem() or Context().location_problem

    def is_configured(self) -> bool:
        try:
            return self.not_ready_reason() is None
        except Exception:  # noqa: BLE001 - never crash the plugin list
            return False

    def missing_settings(self) -> List[str]:
        try:
            if hub_problem() is None and Context().location is None:
                return ["DWD_LATITUDE", "DWD_LONGITUDE"]
        except Exception:  # noqa: BLE001
            pass
        return []

    def health_url(self) -> Optional[str]:
        return None

    async def check(self) -> Optional[Dict[str, Any]]:
        if not self.active:
            return {"status": "off", "message": self.not_ready_reason() or "Off"}
        ctx = Context()
        res = await load_warnings(ctx)
        if res.status == "ok":
            n = len(res.alerts)
            return {"status": "ok", "message": f"{n} active warning{'s' if n != 1 else ''}"}
        if res.status == "not_covered":
            return {"status": "error", "message": TEXT["not_covered"]["en"]}
        return {"status": "error", "message": TEXT["unavailable"]["en"]}

    def routers(self):
        return [router]

    # -- Today --

    async def today_cards(self, user, db):
        try:
            return await self._cards(user)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DWD cards failed: %s", type(exc).__name__)
            return []

    async def _cards(self, user) -> List[Dict[str, Any]]:
        if hub_problem():
            return []
        ctx, lang = Context(), _lang(user)
        if ctx.location is None:
            return []
        warnings = await load_warnings(ctx)
        cards = []
        card = warning_card(warnings.alerts, lang, _now()) if warnings.status == "ok" else None
        if card:
            cards.append(card)
        pollen = pollen_card(await load_pollen(ctx, warnings), ctx.pollen_types, ctx.pollen_min, lang)
        if pollen:
            cards.append(pollen)
        return cards

    # -- scheduled job --

    async def scheduled(self, db) -> None:
        if hub_problem():
            return
        ctx = Context()
        if ctx.location is None or not ctx.notify:
            return
        res = await load_warnings(ctx, force=True)
        if res.status != "ok":
            return
        path = state.state_path(_data_dir())
        now = _now()
        pending, entries = state.plan(res.alerts, state.load(path), ctx.min_rank, now)
        done: List[state.Pending] = []
        if pending:
            done = await self._send(db, pending, now)
        state.mark_notified(entries, done)
        if not state.save(path, entries):
            logger.warning("DWD: could not save the state file; warnings may be announced again after a restart")

    async def _send(self, db, pending: List[state.Pending], now: datetime) -> List[state.Pending]:
        from app.plugins.api import notify

        groups = recipients_by_language(db)
        if not groups:
            return list(pending)  # nobody to tell: do not keep retrying
        summary = len(pending) > MAX_SINGLE_NOTIFICATIONS
        sent_any = False
        for lang, ids in groups.items():
            messages = [summary_text(pending, lang)] if summary else [notification_text(p, lang, now) for p in pending]
            for title, body in messages:
                result = await notify("dwd", user_ids=ids, title=title, body=body, data={"route": "/today"})
                sent_any = sent_any or bool(result.get("sent"))
                if result.get("reason") == "rate_limited":
                    break
        return list(pending) if sent_any else []

    # -- connection test --

    async def test_settings(self, values):
        values = {k: ("" if v is None else str(v)) for k, v in (values or {}).items()}
        problem = hub_problem()
        if problem:
            return {"ok": False, "level": "error", "message": problem}
        errors = []
        location, loc_problem = resolve_location(values)
        if loc_problem:
            errors.append(loc_problem)
        sev, sev_err = check_severity(_setting(values, "DWD_MIN_SEVERITY"))
        pmin, pmin_err = check_pollen_min(_setting(values, "DWD_POLLEN_MIN"))
        _, unknown = dwd.parse_pollen_types(_setting(values, "DWD_POLLEN_TYPES"))
        errors += [e for e in (sev_err, pmin_err) if e]
        if unknown:
            known = ", ".join(dwd.POLLEN_LABEL["en"][k] for k in dwd.POLLEN_TYPES)
            errors.append(f"Unknown pollen type: {', '.join(unknown)}. Known: {known}.")
        if errors:
            return {"ok": False, "level": "error", "message": " ".join(errors)}

        lat, lon = location
        res = await dwd.fetch_alerts(lat, lon, _timezone_name(), _now(), force=True)
        if res.status == "not_covered":
            return {"ok": False, "level": "error", "message": "The DWD does not cover this location. Use a place in Germany."}
        if res.status != "ok":
            return {"ok": False, "level": "error", "message": "Bright Sky (DWD warnings) is not reachable right now."}
        place = res.location.get("name") or "your location"
        state_name = str(res.location.get("state") or "")
        warn_text = (f"{len(res.alerts)} warning(s) active now" if res.alerts else "no warnings active now")
        message = f"Warnings OK for {place}: {warn_text}."
        level = "ok"

        payload = await dwd.fetch_pollen_file(force=True)
        if payload is None:
            return {"ok": True, "level": "warn", "message": message + " The pollen forecast could not be loaded."}
        region = _setting(values, "DWD_POLLEN_REGION")
        wanted = region or state_name
        try:
            fc = dwd.forecast_for(payload, wanted) if wanted else None
        except ValueError:
            fc = None
        if fc is None:
            choices = "; ".join(dwd.region_choices(payload.get("content", [])))
            if region:
                return {"ok": False, "level": "error",
                        "message": f"{message} Pollen region '{region}' not found. Use one of: {choices}."}
            return {"ok": True, "level": "warn",
                    "message": f"{message} No pollen region found for this location; enter DWD_POLLEN_REGION. Options: {choices}."}
        return {"ok": True, "level": level, "message": f"{message} Pollen region: {fc.region}."}


def _data_dir() -> str:
    try:
        from app.config import settings

        return str(settings.data_dir)
    except Exception:  # noqa: BLE001
        import os

        return os.environ.get("DATA_DIR", "/app/data")


@router.get("/status")
async def status(request: Request, _user=Depends(get_current_user)) -> dict:
    """Current warnings and pollen for the family's location: ``{status, error, items, pollen}``."""
    lang = _lang(request=request)
    try:
        if hub_problem():
            return {"status": "not_configured", "error": hub_problem(), "items": [], "pollen": None}
        ctx = Context()
        if ctx.location is None:
            return {"status": "not_configured", "error": TEXT["no_location"][lang], "items": [], "pollen": None}
        res = await load_warnings(ctx)
        if res.status != "ok":
            return {"status": "unavailable", "error": TEXT[res.status][lang], "items": [], "pollen": None}
        now = _now()
        items = [{"id": a.id, "severity": dwd.SEVERITIES[a.rank - 1], "event": dwd.pick(a.event, lang),
                  "headline": dwd.pick(a.headline, lang), "window": window_text(a, now, lang),
                  "onset": a.onset.isoformat(), "expires": a.expires.isoformat() if a.expires else None}
                 for a in res.alerts]
        fc = await load_pollen(ctx, res)
        pollen = None if fc is None else {
            "region": fc.region, "updated": fc.last_update,
            "today": {k: v for k, v in fc.today.items() if v != "none"},
            "tomorrow": {k: v for k, v in fc.tomorrow.items() if v != "none"}}
        return {"status": "ok", "error": None, "items": items, "pollen": pollen}
    except Exception as exc:  # noqa: BLE001 - never an error page
        logger.warning("DWD status failed: %s", type(exc).__name__)
        return {"status": "unavailable", "error": TEXT["unavailable"][lang], "items": [], "pollen": None}
