"""The list of devices that are unlocked right now, kept in a JSON file so a hub restart still re-locks them.

File: ``<DATA_DIR>/plugin-data/screentime/state.json`` (``DATA_DIR`` is the hub's persistent data volume).
Written atomically (temp file + rename), mode 0600. It holds device names, expiry times and child ids only,
never a password or an address.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

VERSION = 1


def data_dir() -> Path:
    try:
        from app.config import settings

        return Path(settings.data_dir)
    except Exception:  # noqa: BLE001 - very old hub / tests without the hub config
        return Path(os.environ.get("DATA_DIR", "/app/data"))


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path or data_dir() / "plugin-data" / "screentime" / "state.json"

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Entries by device name: ``until`` (epoch seconds), ``user_id``, ``lang``, ``warned``, ``failures``.
        A missing or damaged file means no entries."""
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        devices = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(devices, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for name, entry in devices.items():
            if isinstance(name, str) and isinstance(entry, dict) and isinstance(entry.get("until"), (int, float)) \
                    and not isinstance(entry.get("until"), bool):
                out[name] = {
                    "until": float(entry["until"]),
                    "user_id": entry["user_id"] if isinstance(entry.get("user_id"), int) else None,
                    "lang": entry["lang"] if entry.get("lang") in ("de", "en") else "en",
                    "warned": entry.get("warned") is True,
                    "failures": entry["failures"] if isinstance(entry.get("failures"), int) else 0,
                }
        return out

    def save(self, entries: Dict[str, Dict[str, Any]]) -> None:
        """Raises ``OSError`` if the file cannot be written; callers treat that as a failure."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": VERSION, "devices": entries}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
