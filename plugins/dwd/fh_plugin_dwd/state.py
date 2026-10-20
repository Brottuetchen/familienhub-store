"""Remembers which warnings were already announced, in a small JSON file, so that nothing is announced twice
across runs and restarts. Pure logic (``plan``) plus load/save; no hub imports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dwd import Alert, _dt

FILE_NAME = "plugin_dwd_state.json"
KEEP_WITHOUT_END = timedelta(days=7)


def state_path(data_dir: str) -> Path:
    return Path(data_dir) / FILE_NAME


def load(path: Path) -> Dict[str, Dict[str, Any]]:
    """The saved entries ``{alert id: {rank, notified, event_code, onset, expires}}``; anything unreadable = empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {str(k): v for k, v in entries.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def save(path: Path, entries: Dict[str, Dict[str, Any]]) -> bool:
    """Atomic write (temp file + rename). False if the folder is not writable."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dwd_state_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "alerts": entries}, fh)
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


@dataclass
class Pending:
    alert: Alert
    update: bool  # an earlier warning of this event was announced at a lower level


def _overlaps(a_on: Optional[datetime], a_ex: Optional[datetime], entry: Dict[str, Any]) -> bool:
    b_on, b_ex = _dt(entry.get("onset")), _dt(entry.get("expires"))
    if a_on is None or b_on is None:
        return False
    far = datetime.max.replace(tzinfo=a_on.tzinfo)
    return a_on < (b_ex or far) and b_on < (a_ex or far)


def prune(entries: Dict[str, Dict[str, Any]], now: datetime) -> Dict[str, Dict[str, Any]]:
    out = {}
    for k, v in entries.items():
        end = _dt(v.get("expires"))
        if end is None:
            seen = _dt(v.get("onset"))
            keep = seen is not None and now - seen < KEEP_WITHOUT_END
        else:
            keep = end > now
        if keep:
            out[k] = v
    return out


def plan(alerts: List[Alert], entries: Dict[str, Dict[str, Any]], min_rank: int, now: datetime):
    """Decide what to announce. Returns ``(pending, new_entries)``.

    - An alert id that was announced at this level or higher is not announced again.
    - DWD re-issues a warning that changes under a *new* id. A new id whose event code is the same as a tracked
      warning and whose time window overlaps it is the same warning: it counts as announced at the level of that
      one, and is announced as an *update* only if its level is higher.
    - Alerts below ``min_rank`` are remembered (so a later upgrade is recognised) but never announced.
    - Expired entries are dropped.
    """
    entries = prune(entries, now)
    pending: List[Pending] = []
    for a in alerts:
        entry = entries.get(a.id)
        if entry is None:
            inherited = 0
            for other in entries.values():
                if (a.event_code is not None and other.get("event_code") == a.event_code
                        and _overlaps(a.onset, a.expires, other)):
                    inherited = max(inherited, int(other.get("notified", 0) or 0))
            entry = {"rank": a.rank, "notified": inherited, "event_code": a.event_code,
                     "onset": a.onset.isoformat(), "expires": a.expires.isoformat() if a.expires else None}
            entries[a.id] = entry
        else:
            entry["rank"] = a.rank
            entry["expires"] = a.expires.isoformat() if a.expires else None
        notified = int(entry.get("notified", 0) or 0)
        if a.rank >= min_rank and a.rank > notified:
            pending.append(Pending(a, update=notified > 0))
    return pending, entries


def mark_notified(entries: Dict[str, Dict[str, Any]], done: List[Pending]) -> None:
    for p in done:
        if p.alert.id in entries:
            entries[p.alert.id]["notified"] = p.alert.rank
