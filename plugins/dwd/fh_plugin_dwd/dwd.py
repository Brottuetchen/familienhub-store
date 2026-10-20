"""Data side of the DWD plugin: fetching, parsing, filtering. No hub imports, so it is easy to test.

Sources:
- Warnings: Bright Sky (https://brightsky.dev), a free JSON API in front of the official DWD alerts.
- Pollen: the DWD open data file ``s31fg.json`` (Pollenflug-Gefahrenindex).

Data: Deutscher Wetterdienst (DWD), via Bright Sky for the warnings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

BRIGHTSKY_ALERTS = "https://api.brightsky.dev/alerts"
POLLEN_URL = "https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json"
TIMEOUT = 6.0  # seconds, below the hub's 8 s budget for plugin calls
MAX_BYTES = 2_000_000
ALERTS_TTL = 300.0
POLLEN_TTL = 1800.0
STALE_MAX = 3600.0  # an answer from the cache may be this old when the service is down

# --- severities ---
SEVERITIES = ("minor", "moderate", "severe", "extreme")
SEVERITY_RANK = {name: i + 1 for i, name in enumerate(SEVERITIES)}
LEVEL_NAME = {  # DWD's own names for the four levels
    "de": {1: "Wetterwarnung", 2: "Markante Wetterwarnung", 3: "Unwetterwarnung", 4: "Extreme Unwetterwarnung"},
    "en": {1: "Weather warning", 2: "Significant weather warning", 3: "Severe weather warning",
           4: "Extreme severe weather warning"},
}
LEVEL_BADGE = {"de": {1: "Wetterwarnung", 2: "Markant", 3: "Unwetter", 4: "Extrem"},
               "en": {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}}

# --- pollen ---
POLLEN_TYPES = ("Hasel", "Erle", "Esche", "Birke", "Graeser", "Roggen", "Beifuss", "Ambrosia")
POLLEN_LABEL = {
    "de": {"Hasel": "Hasel", "Erle": "Erle", "Esche": "Esche", "Birke": "Birke", "Graeser": "Gräser",
           "Roggen": "Roggen", "Beifuss": "Beifuß", "Ambrosia": "Ambrosia"},
    "en": {"Hasel": "Hazel", "Erle": "Alder", "Esche": "Ash", "Birke": "Birch", "Graeser": "Grasses",
           "Roggen": "Rye", "Beifuss": "Mugwort", "Ambrosia": "Ragweed"},
}
LEVELS = ("none", "low", "medium", "high")
LEVEL_WORD = {"de": {"none": "keine", "low": "gering", "medium": "mittel", "high": "hoch"},
              "en": {"none": "none", "low": "low", "medium": "medium", "high": "high"}}
# DWD scale: 0 none, 0-1 none to low, 1 low, 1-2 low to medium, 2 medium, 2-3 medium to high, 3 high.
# In-between values round up, so a forecast "1-2" shows as medium and "0-1" as low.
DWD_VALUE_LEVEL = {"0": "none", "0-1": "low", "1": "low", "1-2": "medium", "2": "medium", "2-3": "high", "3": "high"}


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def pollen_level(value: Any) -> str:
    """DWD index value (``"0"`` … ``"3"``, with in-between ``"0-1"``, ``"1-2"``, ``"2-3"``) → none/low/medium/high.
    Unknown values (``"-1"`` for no data, nonsense) count as none."""
    return DWD_VALUE_LEVEL.get(str(value).strip().replace(" ", "").replace("–", "-"), "none")


def pollen_type_key(text: str) -> Optional[str]:
    """``Birke``, ``birch``, ``Gräser``, ``graeser``, ``Beifuß`` … → the DWD key, or None."""
    t = text.strip().casefold().replace("ä", "ae").replace("ß", "ss")
    for key in POLLEN_TYPES:
        names = {key.casefold(), POLLEN_LABEL["de"][key].casefold().replace("ä", "ae").replace("ß", "ss"),
                 POLLEN_LABEL["en"][key].casefold()}
        if t in names:
            return key
    return None


def parse_pollen_types(text: str) -> Tuple[List[str], List[str]]:
    """Comma list → (DWD keys in DWD order, unknown names). Empty text = all types."""
    names = [p for p in (text or "").replace(";", ",").split(",") if p.strip()]
    keys, unknown = set(), []
    for name in names:
        key = pollen_type_key(name)
        (keys.add(key) if key else unknown.append(name.strip()))
    return ([k for k in POLLEN_TYPES if k in keys] if names else list(POLLEN_TYPES)), unknown


# --- alerts ---

@dataclass(frozen=True)
class Alert:
    id: str
    rank: int
    event_code: Optional[int]
    event: Dict[str, str]
    headline: Dict[str, str]
    description: Dict[str, str]
    onset: datetime
    expires: Optional[datetime]


def _dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _text(raw: Dict[str, Any], base: str) -> Dict[str, str]:
    out = {}
    for lang in ("de", "en"):
        v = raw.get(f"{base}_{lang}")
        if isinstance(v, str) and v.strip():
            out[lang] = " ".join(v.split())
    return out


def parse_alerts(payload: Any, now: datetime) -> List[Alert]:
    """Bright Sky ``/alerts`` answer → active or upcoming alerts, most severe first. Expired, cancelled and
    malformed entries are dropped; a missing severity counts as minor."""
    raw_list = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(raw_list, list):
        raise ValueError("unexpected alerts shape")
    out: List[Alert] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        status = raw.get("status")
        if isinstance(status, str) and status.lower() not in ("actual", ""):
            continue
        if str(raw.get("response_type") or "").lower() == "allclear":
            continue
        ident = raw.get("alert_id") or raw.get("id")
        if ident in (None, ""):
            continue
        onset = _dt(raw.get("onset")) or _dt(raw.get("effective"))
        expires = _dt(raw.get("expires"))
        if onset is None or (expires is not None and expires <= now):
            continue
        sev = str(raw.get("severity") or "").lower()
        code = raw.get("event_code")
        out.append(Alert(
            id=str(ident), rank=SEVERITY_RANK.get(sev, 1), event_code=code if isinstance(code, int) else None,
            event=_text(raw, "event"), headline=_text(raw, "headline"), description=_text(raw, "description"),
            onset=onset, expires=expires))
    out.sort(key=lambda a: (-a.rank, a.onset, a.id))
    return out


def pick(texts: Dict[str, str], lang: str) -> str:
    return texts.get(lang) or texts.get("de") or texts.get("en") or ""


# --- pollen ---

@dataclass
class PollenForecast:
    region: str
    last_update: str
    today: Dict[str, str] = field(default_factory=dict)  # DWD type key -> level, the highest of the matching parts
    tomorrow: Dict[str, str] = field(default_factory=dict)


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


def _region_parts(name: str) -> List[str]:
    return [_norm(p) for p in str(name).replace(",", " und ").split(" und ") if p.strip()]


def match_regions(content: List[Dict[str, Any]], wanted: str) -> List[Dict[str, Any]]:
    """Entries of the pollen file that match ``wanted``: a partregion id or region id (digits), a partregion name
    (``Rhein-Main``) or a region / federal state name (``Hessen``, ``Berlin``). Several entries can match."""
    w = _norm(wanted)
    if not w:
        return []
    out = []
    for e in content:
        if not isinstance(e, dict):
            continue
        if w.isdigit():
            hit = w in (str(e.get("partregion_id")), str(e.get("region_id"))) and w != "-1"
        else:
            hit = (w == _norm(e.get("partregion_name", "")) or w == _norm(e.get("region_name", ""))
                   or w in _region_parts(e.get("region_name", "")))
        if hit:
            out.append(e)
    return out


def region_choices(content: List[Dict[str, Any]]) -> List[str]:
    out = []
    for e in content:
        if isinstance(e, dict):
            pid, pn = e.get("partregion_id"), e.get("partregion_name")
            out.append(f"{pid}: {pn}" if pid not in (None, -1) and pn else f"{e.get('region_id')}: {e.get('region_name')}")
    return out


def _display_region(entries: List[Dict[str, Any]], explicit: bool) -> str:
    names = {e.get("partregion_name") or e.get("region_name") or "" for e in entries}
    if len(entries) == 1:
        return next(iter(names))
    return str(entries[0].get("region_name") or "")


def forecast_for(payload: Any, entries_wanted: str) -> Optional[PollenForecast]:
    """Merge the matching regions of the pollen file; several parts of one region take the highest level."""
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, list):
        raise ValueError("unexpected pollen shape")
    entries = match_regions(content, entries_wanted)
    if not entries:
        return None
    result = PollenForecast(region=_display_region(entries, True), last_update=str(payload.get("last_update") or ""))
    for e in entries:
        pollen = e.get("Pollen")
        if not isinstance(pollen, dict):
            continue
        for key in POLLEN_TYPES:
            day = pollen.get(key)
            if not isinstance(day, dict):
                continue
            for slot, target in (("today", result.today), ("tomorrow", result.tomorrow)):
                lvl = pollen_level(day.get(slot))
                if level_rank(lvl) >= level_rank(target.get(key, "none")):
                    target[key] = lvl
    return result


# --- fetching with a small in-memory cache ---

_cache: Dict[Any, Tuple[float, Any]] = {}


def clear_cache() -> None:
    _cache.clear()


def _cached(key: Any, ttl: float) -> Optional[Any]:
    hit = _cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < ttl else None


def _stale(key: Any) -> Optional[Any]:
    hit = _cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < STALE_MAX else None


async def _get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        r = await client.get(url, params=params, headers={"User-Agent": "familienhub-plugin-dwd"})
    if r.status_code == 404:
        return {"__status": 404, "detail": r.text[:200]}
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise ValueError("answer too large")
    return r.json()


@dataclass
class AlertsResult:
    status: str  # ok | unavailable | not_covered
    alerts: List[Alert] = field(default_factory=list)
    location: Dict[str, Any] = field(default_factory=dict)
    stale: bool = False


async def fetch_alerts(lat: float, lon: float, tz: str, now: datetime, *, force: bool = False) -> AlertsResult:
    key = ("alerts", round(lat, 3), round(lon, 3))
    if not force:
        hit = _cached(key, ALERTS_TTL)
        if hit is not None:
            return AlertsResult("ok", parse_alerts(hit, now), hit.get("location") or {})
    try:
        data = await _get_json(BRIGHTSKY_ALERTS, {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "tz": tz})
        if isinstance(data, dict) and data.get("__status") == 404:
            # Bright Sky answers 404 "Requested position is not covered by the DWD" for places outside Germany
            return AlertsResult("not_covered" if "covered" in str(data.get("detail")) else "unavailable")
        alerts = parse_alerts(data, now)
    except (httpx.HTTPError, ValueError, TypeError):
        old = _stale(key)
        if old is not None:
            try:
                return AlertsResult("ok", parse_alerts(old, now), old.get("location") or {}, stale=True)
            except ValueError:
                pass
        return AlertsResult("unavailable")
    _cache[key] = (time.monotonic(), data)
    loc = data.get("location")
    return AlertsResult("ok", alerts, loc if isinstance(loc, dict) else {})


async def fetch_pollen_file(*, force: bool = False) -> Optional[Dict[str, Any]]:
    """The raw pollen file, or None when DWD is not reachable or answers nonsense."""
    key = ("pollen",)
    if not force:
        hit = _cached(key, POLLEN_TTL)
        if hit is not None:
            return hit
    try:
        data = await _get_json(POLLEN_URL)
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ValueError("unexpected pollen shape")
    except (httpx.HTTPError, ValueError, TypeError):
        return _stale(key)
    _cache[key] = (time.monotonic(), data)
    return data
