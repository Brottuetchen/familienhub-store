"""Texts of the push notifications (German and English) and the diff of the "already reported" state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CHANGED = ("cancelled", "substitution", "changed")
WEEKDAYS = {"de": ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}


def lesson_key(lesson: Dict[str, Any]) -> str:
    """Identity of a reported change. A new note, room or teacher on the same lesson is a new change."""
    raw = "|".join(str(lesson.get(k) or "") for k in ("start", "end", "status", "title", "location", "teacher", "note"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324 - a fingerprint, not security


def changes(lessons: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Cancelled / substituted / changed lessons by ISO day."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for lesson in lessons:
        if lesson.get("status") in CHANGED:
            out.setdefault(lesson["date"], []).append(lesson)
    return out


def ordinal(n: int, lang: str) -> str:
    if lang == "de":
        return f"{n}."
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def when_word(day: date, today: date, lang: str) -> str:
    if day == today:
        return "heute" if lang == "de" else "today"
    if day == today + timedelta(days=1):
        return "morgen" if lang == "de" else "tomorrow"
    name = WEEKDAYS["de" if lang == "de" else "en"][day.weekday()]
    return f"{name} {day.day}.{day.month}." if lang == "de" else f"{name} {day.day}/{day.month}"


def _span(lessons: List[Dict[str, Any]], lang: str) -> str:
    """``3rd–4th period`` / ``3.–4. Stunde``; without period numbers the start time (``08:00``)."""
    periods = [x["period"] for x in lessons if x.get("period")]
    if len(periods) == len(lessons) and periods:
        a, b = min(periods), max(periods)
        label = ordinal(a, lang) if a == b else f"{ordinal(a, lang)}–{ordinal(b, lang)}"
        return f"{label} Stunde" if lang == "de" else f"{label} period"
    return lessons[0]["start"] + ("" if len(lessons) == 1 else f"–{lessons[-1]['end']}")


def _room(loc: str, lang: str) -> str:
    if re.fullmatch(r"[\w.-]{1,6}", loc):
        return f"Raum {loc}" if lang == "de" else f"Room {loc}"
    return loc


def _runs(cancelled: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Consecutive periods of the same subject belong together."""
    runs: List[List[Dict[str, Any]]] = []
    for lesson in sorted(cancelled, key=lambda x: (x["start"], x["end"])):
        last = runs[-1][-1] if runs else None
        if last and last["title"] == lesson["title"] and last.get("period") and lesson.get("period") == last["period"] + 1:
            runs[-1].append(lesson)
        else:
            runs.append([lesson])
    return runs


def phrases(day: date, lessons: List[Dict[str, Any]], today: date, lang: str) -> List[str]:
    """One short sentence per cancelled run / substituted lesson of a day."""
    when = when_word(day, today, lang)
    out: List[str] = []
    for run in _runs([x for x in lessons if x["status"] == "cancelled"]):
        span = _span(run, lang)
        out.append(f"{span} {run[0]['title']} ausgefallen {when}" if lang == "de"
                   else f"{span} {run[0]['title']} cancelled {when}")
    for lesson in sorted((x for x in lessons if x["status"] != "cancelled"), key=lambda x: x["start"]):
        span = _span([lesson], lang)
        kind = ("Vertretung" if lesson["status"] == "substitution" else "Änderung") if lang == "de" else (
            "substitution" if lesson["status"] == "substitution" else "change")
        details = [d for d in (_room(lesson["location"], lang) if lesson.get("location") else "", lesson.get("teacher") or "",
                               lesson.get("note") or "") if d]
        text = f"{span} {kind} ({lesson['title']})"
        text += (": " + ", ".join(details)) if details else ""
        out.append(f"{text} {when}")
    return out


def compose(child_name: str, by_day: Dict[date, List[Dict[str, Any]]], today: date, lang: str) -> Optional[Tuple[str, str]]:
    """``(title, body)`` for one child, or None when there is nothing to say. One change: the title says it all."""
    lang = "de" if lang == "de" else "en"
    sentences: List[str] = []
    for day in sorted(by_day):
        sentences += phrases(day, by_day[day], today, lang)
    if not sentences:
        return None
    if len(sentences) == 1:
        return f"{child_name}: {sentences[0]}", ""
    noun = f"{len(sentences)} Änderungen" if lang == "de" else f"{len(sentences)} changes"
    return f"{child_name}: {noun}", "; ".join(sentences)


# --- the small state file --------------------------------------------------------------------------------------


class State:
    """``{"children": {"<hub id>": {"person": "<id>", "days": {"2026-10-20": ["<key>", ...]}}}}`` in one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {"version": 1, "children": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("children"), dict):
                self.data = loaded
        except (OSError, ValueError):
            pass  # no file yet, or unreadable: start over (the next run only records a baseline)

    def child(self, child_id: int, person: str) -> Optional[Dict[str, List[str]]]:
        entry = self.data["children"].get(str(child_id))
        if not isinstance(entry, dict) or entry.get("person") != person or not isinstance(entry.get("days"), dict):
            return None
        return entry["days"]

    def set_child(self, child_id: int, person: str, days: Dict[str, List[str]], today: date) -> None:
        keep = {d: v for d, v in days.items() if d >= today.isoformat()}  # prune days that are over
        self.data["children"][str(child_id)] = {"person": person, "days": keep}

    def forget_others(self, child_ids: List[int]) -> None:
        wanted = {str(i) for i in child_ids}
        self.data["children"] = {k: v for k, v in self.data["children"].items() if k in wanted}

    def save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False
