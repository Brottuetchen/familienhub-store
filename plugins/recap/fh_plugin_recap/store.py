"""Where the recaps live on disk, and the strict path rules for serving them.

Layout below the hub's data directory (no database tables)::

    <DATA_DIR>/plugin-data/recap/<user id>/
        2026-10.json            snapshot of the month (also marks the month as built)
        2026-10.attempts.json   failed build attempts (removed after a success)
        2026-10/index.html      the story
        2026-10/img/*.jpg|png|webp|mp3   posters, photos, covers, soundtrack clips
        games/2026-10-31.json   daily state of the game statistics (cumulative play time per device and game)

Everything a person can ask for through a page link is mapped to one of these files by *building* the path from
validated parts, never by joining what the browser sent.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DAY_RE = re.compile(r"^20\d\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
MONTH_RE = re.compile(r"^(20\d\d)-(0[1-9]|1[0-2])$")
ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(jpg|png|webp|mp3)$")
CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "mp3": "audio/mpeg"}
MAX_ASSET_BYTES = 12 * 1024 * 1024


def base_dir() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "plugin-data" / "recap"


def user_dir(user_id: int) -> Path:
    return base_dir() / str(int(user_id))


def month_dir(user_id: int, month: str) -> Path:
    if not MONTH_RE.match(month):
        raise ValueError("bad month")
    return user_dir(user_id) / month


def snapshot_path(user_id: int, month: str) -> Path:
    if not MONTH_RE.match(month):
        raise ValueError("bad month")
    return user_dir(user_id) / f"{month}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_snapshot(user_id: int, month: str) -> Optional[Dict[str, Any]]:
    return read_json(snapshot_path(user_id, month))


def write_snapshot(user_id: int, month: str, data: Dict[str, Any]) -> None:
    _atomic_write(snapshot_path(user_id, month), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def has_story(user_id: int, month: str) -> bool:
    return (month_dir(user_id, month) / "index.html").is_file()


def list_months(user_id: int) -> List[str]:
    """Months with a finished story, newest first."""
    found = []
    try:
        names = os.listdir(user_dir(user_id))
    except OSError:
        return []
    for name in names:
        if name.endswith(".json") and MONTH_RE.match(name[:-5]) and has_story(user_id, name[:-5]):
            found.append(name[:-5])
    return sorted(found, reverse=True)


def history(user_id: int, before: str) -> List[Dict[str, Any]]:
    """The person's earlier snapshots (oldest first), the memory of the design engine."""
    out = []
    try:
        names = sorted(os.listdir(user_dir(user_id)))
    except OSError:
        return []
    for name in names:
        if name.endswith(".json") and MONTH_RE.match(name[:-5]) and name[:-5] < before:
            snap = read_json(user_dir(user_id) / name)
            if snap and snap.get("month") == name[:-5] and not snap.get("empty"):
                out.append(snap)
    return out


# --- daily game snapshots (ES-DE only keeps cumulative numbers, a month is the difference of two days) -----------

def games_path(user_id: int, day: str) -> Path:
    if not DAY_RE.match(day):
        raise ValueError("bad day")
    return user_dir(user_id) / "games" / f"{day}.json"


def game_days(user_id: int) -> List[str]:
    """The days that have a game snapshot, oldest first."""
    try:
        names = os.listdir(user_dir(user_id) / "games")
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and DAY_RE.match(n[:-5]))


def read_games(user_id: int, day: str) -> Optional[Dict[str, Any]]:
    return read_json(games_path(user_id, day))


def write_games(user_id: int, day: str, data: Dict[str, Any]) -> None:
    _atomic_write(games_path(user_id, day), json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# --- failed attempts (so a broken month is not retried every hour) ---------------------------------------------

def attempts(user_id: int, month: str) -> Dict[str, Any]:
    return read_json(user_dir(user_id) / f"{month}.attempts.json") or {"count": 0, "last": 0}


def record_attempt(user_id: int, month: str, now: float) -> None:
    prev = attempts(user_id, month)
    _atomic_write(user_dir(user_id) / f"{month}.attempts.json",
                  json.dumps({"count": int(prev.get("count", 0)) + 1, "last": now}).encode())


def clear_attempts(user_id: int, month: str) -> None:
    try:
        (user_dir(user_id) / f"{month}.attempts.json").unlink()
    except OSError:
        pass


# --- serving ---------------------------------------------------------------------------------------------------

def resolve(user_id: int, path: str) -> Optional[Tuple[str, Path]]:
    """Maps a page path to ``(kind, file)`` below the person's own folder, or ``None``.

    Accepted: ``2026-10``, ``2026-10/``, ``2026-10/index.html`` (the story) and ``2026-10/img/<name>.<ext>``
    (an asset). ``kind`` is ``"story"`` or the file extension of the asset. The result is always a regular file
    (no symlink) whose real path lies inside the person's folder.
    """
    parts = path.split("/")
    if parts and parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    if not parts or not MONTH_RE.match(parts[0]):
        return None
    month = parts[0]
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "index.html"):
        kind, target = "story", month_dir(user_id, month) / "index.html"
    elif len(parts) == 3 and parts[1] == "img" and ASSET_RE.match(parts[2]):
        kind, target = parts[2].rsplit(".", 1)[1], month_dir(user_id, month) / "img" / parts[2]
    else:
        return None
    try:
        if target.is_symlink() or not target.is_file():
            return None
        root = user_dir(user_id).resolve()
        if root not in target.resolve().parents:
            return None
    except OSError:
        return None
    return kind, target
