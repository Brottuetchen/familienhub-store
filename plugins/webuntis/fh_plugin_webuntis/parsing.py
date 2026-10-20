"""Pure parsing helpers: WebUntis answers -> the hub's lesson dicts. No network, no hub imports."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:\d{1,5})?$")


def normalize_server(raw: Any) -> Optional[str]:
    """``https://xyz.webuntis.com/WebUntis/?school=x`` or ``XYZ.webuntis.com`` -> ``xyz.webuntis.com``.

    Returns None when the value does not look like a host name (spaces, user info, no dot ...)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    if not text or "@" in text or not HOST_RE.match(text) or "." not in text.split(":")[0] or ".." in text:
        return None
    if ":" in text and not 0 < int(text.rsplit(":", 1)[1]) < 65536:
        return None
    return text


def untis_time(value: Any) -> Optional[str]:
    """745 -> ``07:45``, 1305 -> ``13:05``."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    hours, minutes = divmod(number, 100)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return f"{hours:02d}:{minutes:02d}"


def untis_date(value: Any) -> Optional[date]:
    """20261020 -> date(2026, 10, 20)."""
    text = str(value)
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def untis_day(day: date) -> int:
    return day.year * 10000 + day.month * 100 + day.day


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _label(entries: Any, names: Optional[Dict[Any, str]] = None) -> str:
    """Long name, else short name, of the first element (``su``, ``ro``, ``te``); ``names`` resolves bare ids."""
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get("longname")) or _text(entry.get("name"))
        if not label and names:
            label = names.get(entry.get("id"), "")
        if label:
            return label
    return ""


def _changed(entries: Any) -> bool:
    """A room / teacher / subject entry with ``orgid`` was replaced by another one."""
    return isinstance(entries, list) and any(isinstance(e, dict) and e.get("orgid") not in (None, 0, "") for e in entries)


def needs_names(raw: Iterable[Any]) -> Dict[str, set]:
    """Which element kinds carry bare ids without any name (then ``getSubjects`` etc. are worth a call)."""
    missing: Dict[str, set] = {"su": set(), "ro": set(), "te": set()}
    for lesson in raw:
        if not isinstance(lesson, dict):
            continue
        for kind in missing:
            for entry in lesson.get(kind) or []:
                if isinstance(entry, dict) and not (_text(entry.get("longname")) or _text(entry.get("name"))):
                    missing[kind].add(entry.get("id"))
    return {k: v for k, v in missing.items() if v}


def parse_lessons(raw: Any, first: date, last: date, *, periods: Optional[Dict[str, int]] = None,
                  names: Optional[Dict[str, Dict[Any, str]]] = None) -> List[Dict[str, Any]]:
    """``getTimetable`` result -> hub lessons. Unusable rows are dropped.

    ``periods`` maps ``"HH:MM"`` (start) to the number of the lesson from the school's time grid.
    ``code``: ``cancelled`` -> status ``cancelled``; ``irregular`` -> ``substitution`` (something was replaced or there
    is a substitution text) or ``changed``. The note is the substitution text, else the info text; the lesson text
    (``lstext``) is only a note when the lesson is cancelled or irregular (otherwise it is a permanent remark).
    """
    names = names or {}
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for lesson in raw:
        if not isinstance(lesson, dict):
            continue
        day, start, end = untis_date(lesson.get("date")), untis_time(lesson.get("startTime")), untis_time(lesson.get("endTime"))
        if day is None or start is None or end is None or end < start or not (first <= day <= last):
            continue
        code = _text(lesson.get("code")).lower()
        subst, info, lstext = _text(lesson.get("substText")), _text(lesson.get("info")), _text(lesson.get("lstext"))
        if code == "cancelled":
            status = "cancelled"
        elif code == "irregular":
            moved = any(_changed(lesson.get(k)) for k in ("su", "ro", "te"))
            status = "substitution" if (moved or subst) else "changed"
        else:
            status = "normal"
        note = subst or info or (lstext if status != "normal" else "")
        title = _label(lesson.get("su"), names.get("su")) or lstext[:80] or "?"
        row: Dict[str, Any] = {"date": day.isoformat(), "start": start, "end": end, "title": title, "kind": "school",
                               "status": status}
        room, teacher = _label(lesson.get("ro"), names.get("ro")), _label(lesson.get("te"), names.get("te"))
        if room:
            row["location"] = room
        if teacher:
            row["teacher"] = teacher
        if note:
            row["note"] = note
        if periods and start in periods:
            row["period"] = periods[start]
        out.append(row)
    out.sort(key=lambda r: (r["date"], r["start"], r["end"], r["title"]))
    return out


def parse_timegrid(raw: Any) -> Dict[str, int]:
    """``getTimegridUnits`` -> ``{"08:00": 1, "08:55": 2, ...}`` (the unit names must be numbers)."""
    grid: Dict[str, int] = {}
    if not isinstance(raw, list):
        return grid
    for day in raw:
        units = day.get("timeUnits") if isinstance(day, dict) else None
        for unit in units if isinstance(units, list) else []:
            if not isinstance(unit, dict):
                continue
            start, name = untis_time(unit.get("startTime")), str(unit.get("name", "")).strip()
            if start and name.isdigit() and 0 < int(name) < 30:
                grid.setdefault(start, int(name))
    return grid


# --- the web client's "weekly data" answer (fallback for accounts that may not use getTimetable) ---------------

_STATE_STATUS = {"CANCEL": "cancelled", "CANCELLED": "cancelled", "SUBSTITUTION": "substitution",
                 "ROOMSUBSTITUTION": "substitution", "SHIFT": "changed", "ADDITIONAL": "changed"}


def parse_weekly(raw: Any, element_id: Any, first: date, last: date, *,
                 periods: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """``/WebUntis/api/public/timetable/weekly/data`` -> hub lessons. Raises ValueError for another shape."""
    try:
        data = raw["data"]["result"]["data"]
        rows = data["elementPeriods"][str(element_id)]
        known = {(e.get("type"), e.get("id")): e for e in data.get("elements", []) if isinstance(e, dict)}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("unexpected weekly data") from exc
    kinds = {3: "su", 4: "ro", 2: "te"}
    out: List[Dict[str, Any]] = []
    for period in rows if isinstance(rows, list) else []:
        if not isinstance(period, dict):
            continue
        day, start, end = untis_date(period.get("date")), untis_time(period.get("startTime")), untis_time(period.get("endTime"))
        if day is None or start is None or end is None or end < start or not (first <= day <= last):
            continue
        status = _STATE_STATUS.get(str(period.get("cellState", "")).upper(), "normal")
        labels: Dict[str, str] = {}
        for el in period.get("elements") or []:
            kind = kinds.get(el.get("type")) if isinstance(el, dict) else None
            full = known.get((el.get("type"), el.get("id"))) if kind else None
            if kind and full and kind not in labels:
                labels[kind] = _text(full.get("longName")) or _text(full.get("name")) or _text(full.get("displayname"))
        note = _text(period.get("substText")) or _text(period.get("periodText")) or (
            _text(period.get("lessonText")) if status != "normal" else "")
        row: Dict[str, Any] = {"date": day.isoformat(), "start": start, "end": end, "title": labels.get("su") or "?",
                               "kind": "school", "status": status}
        if labels.get("ro"):
            row["location"] = labels["ro"]
        if labels.get("te"):
            row["teacher"] = labels["te"]
        if note:
            row["note"] = note
        if periods and start in periods:
            row["period"] = periods[start]
        out.append(row)
    out.sort(key=lambda r: (r["date"], r["start"], r["end"], r["title"]))
    return out
