"""Pure logic of the departures plugin: parsing transport.rest answers, filters, time window and texts.

Nothing here does network access, so it is easy to test. The plugin class (plugin.py) adds fetching and caching.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

DEFAULT_BASE_URL = "https://v6.db.transport.rest"
DEFAULT_MAX = 4
MAX_ROWS = 6  # a Today card shows at most 6 lines
MAX_WALK = 90
PLATFORM = re.compile(r"^\d{1,3}[A-Za-z]?$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
STOP_ID = re.compile(r"^[A-Za-z0-9:_.\-]*\d[A-Za-z0-9:_.\-]*$")  # HAFAS ids contain a digit and no spaces


@dataclass(frozen=True)
class Departure:
    line: str
    product: str
    direction: str
    planned: datetime  # always timezone-aware
    when: Optional[datetime]  # real-time estimate; None if cancelled or not known
    delay_min: Optional[int]
    cancelled: bool
    platform: Optional[str]
    stop_name: Optional[str] = None

    @property
    def effective(self) -> datetime:
        return self.when or self.planned


@dataclass(frozen=True)
class Config:
    base_url: str
    stop: str
    lines: Tuple[str, ...]
    directions: Tuple[str, ...]
    max_rows: int
    walk: int
    show_from: Optional[time]
    show_until: Optional[time]
    weekdays_only: bool


# --- settings ---

def split_list(value: Any) -> Tuple[str, ...]:
    return tuple(p.strip() for p in str(value or "").replace(";", ",").split(",") if p.strip())


def parse_hhmm(value: Any) -> Optional[time]:
    m = HHMM.match(str(value or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(high, int(number)))


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def normalize_base_url(value: Any) -> Optional[str]:
    """The base URL without trailing slash, the default for an empty value, None if it is not an http(s) address."""
    text = str(value or "").strip() or DEFAULT_BASE_URL
    if not re.match(r"^https?://[^\s/?#@]+(:\d+)?(/[^\s?#]*)?$", text):
        return None
    return text.rstrip("/")


def read_config(get) -> Config:
    """Settings as the plugin uses them at run time: lenient, wrong values fall back to defaults."""
    return Config(
        base_url=normalize_base_url(get("DEPARTURES_BASE_URL")) or DEFAULT_BASE_URL,
        stop=str(get("DEPARTURES_STOP") or "").strip(),
        lines=split_list(get("DEPARTURES_LINES")),
        directions=split_list(get("DEPARTURES_DIRECTIONS")),
        max_rows=_int(get("DEPARTURES_MAX"), DEFAULT_MAX, 1, MAX_ROWS),
        walk=_int(get("DEPARTURES_WALK_MINUTES"), 0, 0, MAX_WALK),
        show_from=parse_hhmm(get("DEPARTURES_SHOW_FROM")),
        show_until=parse_hhmm(get("DEPARTURES_SHOW_UNTIL")),
        weekdays_only=truthy(get("DEPARTURES_WEEKDAYS_ONLY")),
    )


def validate_values(values: Dict[str, Any]) -> List[str]:
    """Strict check of typed (unsaved) settings; returns problems, empty if fine."""
    problems: List[str] = []
    if not str(values.get("DEPARTURES_STOP") or "").strip():
        problems.append("Enter a stop ID (or a stop name to look up).")
    if str(values.get("DEPARTURES_BASE_URL") or "").strip() and normalize_base_url(values.get("DEPARTURES_BASE_URL")) is None:
        problems.append("The transport.rest address must start with http:// or https://.")
    for key, low, high, label in (("DEPARTURES_MAX", 1, MAX_ROWS, "Number of departures"),
                                  ("DEPARTURES_WALK_MINUTES", 0, MAX_WALK, "Walking time")):
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue
        try:
            number = float(raw.replace(",", "."))
        except ValueError:
            problems.append(f"{label} must be a number between {low} and {high}.")
            continue
        if not math.isfinite(number) or number != int(number) or not low <= number <= high:
            problems.append(f"{label} must be a whole number between {low} and {high}.")
    for key, label in (("DEPARTURES_SHOW_FROM", "Show from"), ("DEPARTURES_SHOW_UNTIL", "Show until")):
        raw = str(values.get(key) or "").strip()
        if raw and parse_hhmm(raw) is None:
            problems.append(f"{label} must look like 06:30 (HH:MM, 24 hours).")
    return problems


def looks_like_id(stop: str) -> bool:
    return bool(STOP_ID.match(stop.strip()))


# --- time window ---

def in_window(now: datetime, cfg: Config) -> bool:
    """Is the card shown at ``now`` (local, aware)? One-sided windows run to/from midnight; from > until wraps midnight."""
    if cfg.weekdays_only and now.weekday() >= 5:
        return False
    start, end = cfg.show_from, cfg.show_until
    if start is None and end is None:
        return True
    clock = now.time().replace(second=0, microsecond=0)
    start = start or time(0, 0)
    if end is None:
        return clock >= start
    if start <= end:
        return start <= clock <= end
    return clock >= start or clock <= end  # overnight


# --- parsing ---

def parse_time(value: Any, tz: tzinfo) -> Optional[datetime]:
    """ISO time of the API as an aware datetime in ``tz``. A time without offset is taken as ``tz`` wall time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _text(value: Any, limit: int = 80) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, (str, int, float)) else ""


def parse_departures(payload: Any, tz: tzinfo) -> Optional[List[Departure]]:
    """Departures of a ``/stops/{id}/departures`` answer, sorted by time. None if the answer has the wrong shape.
    v6 answers ``{"departures": [...]}``; a bare list (older versions) is accepted too. Broken rows are skipped."""
    rows = payload.get("departures") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    out: List[Departure] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        planned = parse_time(row.get("plannedWhen") or row.get("when"), tz)
        if planned is None:
            continue
        cancelled = row.get("cancelled") is True
        when = None if cancelled else parse_time(row.get("when"), tz)
        delay = row.get("delay")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool):
            delay_min: Optional[int] = int(round(delay / 60))
        elif when is not None:
            delay_min = int(round((when - planned).total_seconds() / 60))
        else:
            delay_min = None
        line = row.get("line") if isinstance(row.get("line"), dict) else {}
        name = _text(line.get("name")) or _text(row.get("name")) or "?"
        stop = row.get("stop") if isinstance(row.get("stop"), dict) else {}
        out.append(Departure(
            line=name,
            product=_text(line.get("product") or line.get("mode"), 30).lower(),
            direction=_text(row.get("direction")),
            planned=planned, when=when, delay_min=delay_min, cancelled=cancelled,
            platform=_text(row.get("platform") or row.get("plannedPlatform"), 12) or None,
            stop_name=_text(stop.get("name"), 60) or None,
        ))
    out.sort(key=lambda d: d.effective)
    return out


# --- filters ---

def _squash(text: str) -> str:
    return "".join(text.split()).lower()


def line_matches(name: str, wanted: Iterable[str]) -> bool:
    """``Bus 12`` matches ``Bus 12``, ``bus12`` and ``12`` (the part after the first word)."""
    wanted = [w for w in wanted if w]
    if not wanted:
        return True
    full = _squash(name)
    parts = name.split(None, 1)
    rest = _squash(parts[1]) if len(parts) == 2 else ""
    return any(_squash(w) == full or (rest and _squash(w) == rest) for w in wanted)


def direction_matches(direction: str, wanted: Iterable[str]) -> bool:
    wanted = [w.lower() for w in wanted if w]
    return not wanted or any(w in direction.lower() for w in wanted)


def select(deps: List[Departure], cfg: Config, now: datetime, *, limit: Optional[int] = None) -> List[Departure]:
    """Filters by line and direction, hides what cannot be reached (walking time), keeps the next ``max_rows``."""
    earliest = now + timedelta(minutes=cfg.walk)
    out = [d for d in deps
           if line_matches(d.line, cfg.lines) and direction_matches(d.direction, cfg.directions)
           # a departure is gone once it left, and not reachable if it leaves before you arrive; a cancelled one is
           # judged by its planned time
           and (d.planned if d.cancelled else d.effective) >= earliest.replace(second=0, microsecond=0)]
    return out[: (limit or cfg.max_rows)]


# --- texts ---

TEXTS = {
    "en": {"cancelled": "cancelled", "now": "now", "in": "in {m} min", "leave_now": "leave now",
           "leave_in": "leave in {m} min", "title": "Departures", "badge_now": "now", "badge": "{m} min"},
    "de": {"cancelled": "fällt aus", "now": "jetzt", "in": "in {m} Min.", "leave_now": "jetzt losgehen",
           "leave_in": "Losgehen in {m} Min.", "title": "Abfahrten", "badge_now": "jetzt", "badge": "{m} Min."},
}


def language_of(user: Any) -> str:
    lang = str(getattr(user, "language", "") or "").lower()[:2]
    return lang if lang in TEXTS else "de"


def clock(moment: datetime) -> str:
    return f"{moment.hour}:{moment.minute:02d}"


def minutes_until(moment: datetime, now: datetime) -> int:
    """Whole minutes from ``now`` (minute precision) to ``moment``, never negative."""
    return max(0, int((moment - now.replace(second=0, microsecond=0)).total_seconds() // 60))


def delay_suffix(dep: Departure) -> str:
    if dep.cancelled or not dep.delay_min:
        return ""
    return f" ({dep.delay_min:+d})"


def card_line(dep: Departure, cfg: Config, now: datetime, lang: str) -> Dict[str, Optional[str]]:
    t = TEXTS[lang]
    direction = f" → {dep.direction}" if dep.direction else ""
    text = f"{dep.line}{direction} · {clock(dep.effective)}{delay_suffix(dep)}"
    if dep.cancelled:
        secondary = t["cancelled"]
    else:
        m = minutes_until(dep.effective, now) - cfg.walk
        if cfg.walk:
            secondary = t["leave_now"] if m <= 0 else t["leave_in"].format(m=m)
        else:
            secondary = t["now"] if m <= 0 else t["in"].format(m=m)
    if dep.platform and PLATFORM.match(dep.platform):  # real instances also send things like "Pos. 15" or "1 (U5)"
        plat = ("Gleis " if lang == "de" else "platform ") + dep.platform
        secondary = f"{secondary} · {plat}"
    return {"text": text, "secondary": secondary}


def build_card(deps: List[Departure], cfg: Config, now: datetime, lang: str, title: str) -> Optional[Dict[str, Any]]:
    """The Today card, or None if there is nothing to show."""
    if not deps:
        return None
    t = TEXTS[lang]
    card: Dict[str, Any] = {
        "id": "departures",
        "title": title or t["title"],
        "icon": "directions_bus",
        "lines": [card_line(d, cfg, now, lang) for d in deps],
    }
    nxt = next((d for d in deps if not d.cancelled), None)
    if nxt is not None:
        m = minutes_until(nxt.effective, now)
        card["badge"] = t["badge_now"] if m <= 0 else t["badge"].format(m=m)
    return card


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone name or no tz database
        return ZoneInfo("UTC")
