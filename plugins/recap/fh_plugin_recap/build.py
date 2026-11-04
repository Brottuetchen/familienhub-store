"""Building a month for one person, and the background jobs that run builds.

A full build downloads posters, photos and about ten audio clips, which is more than the hub's 120 second limit
for a scheduled job allows. So the scheduled job only *starts* a build as a background task and returns at once.
At most one build runs at a time, and a month is never built twice in parallel.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple


from . import collectors, store, story

BUILD_TIMEOUT = 15 * 60  # seconds for one whole build
MAX_ATTEMPTS = 3
RETRY_AFTER = 6 * 3600  # seconds between failed attempts


class BuildFailed(Exception):
    """Nothing could be collected because a source failed, so there is nothing to show (tried again later)."""


def prev_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def month_bounds(month: str, tz) -> Tuple[dt.datetime, dt.datetime]:
    y, m = map(int, month.split("-"))
    return dt.datetime(y, m, 1, tzinfo=tz), dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)


def compute_totals(data: Dict[str, Any]) -> Dict[str, float]:
    jf, mu, ab, pc = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts"))
    t = {"series_hours": jf.get("series_hours", 0), "movie_hours": jf.get("movie_hours", 0),
         "music_hours": mu.get("hours", 0), "audiobook_hours": ab.get("hours", 0), "podcast_hours": pc.get("hours", 0),
         "games_hours": (data.get("games") or {}).get("hours", 0)}
    t["media_hours"] = round(sum(t.values()), 1)
    return t


def snapshot_games(user_id: int, cfg: collectors.Config, tz) -> bool:
    """Writes today's game snapshot (the device files as they are now) unless there is one already. At most one per
    local day and person, no network, and silent: a missing mount or a full disk must never disturb the scheduler."""
    if not cfg.games_devices:
        return False
    try:
        day = dt.datetime.now(tz).date().isoformat()
        if store.games_path(user_id, day).exists():
            return False
        state = collectors.read_games(cfg.games_dir, cfg.games_devices)
        if not state:
            return False
        store.write_games(user_id, day, state)
        return True
    except (OSError, ValueError):
        return False


def game_states(user_id: int, start, end):
    """``(baseline, end state)`` for [start, end): the latest snapshot on or before the first day, and the earliest one
    on or after the day the next month begins (None = use the current device files)."""
    days = store.game_days(user_id)
    first, after = start.date().isoformat(), end.date().isoformat()
    before = [d for d in days if d <= first]
    later = [d for d in days if d >= after]
    base = store.read_games(user_id, before[-1]) if before else None
    final = store.read_games(user_id, later[0]) if later else None
    return base, final


def steam_states(user_id: int, start, end):
    """``(baseline, end state, time of the end state)`` of the Steam library for [start, end), chosen like
    ``game_states``; without a later snapshot the end state is None (ask Steam now). The time is the start of the
    snapshot's day, the earliest it can have been taken."""
    days = store.steam_days(user_id)
    first, after = start.date().isoformat(), end.date().isoformat()
    before = [d for d in days if d <= first]
    later = [d for d in days if d >= after]
    base = store.read_steam(user_id, before[-1]) if before else None
    final = store.read_steam(user_id, later[0]) if later else None
    if final is None:
        return base, None, None
    return base, final, dt.datetime.fromisoformat(later[0]).replace(tzinfo=start.tzinfo)


async def snapshot_steam(user_id: int, cfg: collectors.Config, tz) -> bool:
    """Writes today's Steam snapshot unless there is one already: one API request per local day and person while it
    works, a failed request is tried again on the next run. Silent like ``snapshot_games`` (the error would only say
    what the snapshot cannot hold anyway)."""
    if not (cfg.steam_id and cfg.steam_api_key):
        return False
    try:
        day = dt.datetime.now(tz).date().isoformat()
        if store.steam_path(user_id, day).exists():
            return False
        async with collectors.new_client() as client:
            state = await collectors.steam_owned(client, cfg)
        store.write_steam(user_id, day, state)
        return True
    except Exception:  # noqa: BLE001 - never disturbs the scheduler or the build
        return False


async def build_month(user_id: int, month: str, cfg: collectors.Config, tz, *, redesign: bool = False) -> Dict[str, Any]:
    """Collects, designs and writes one month. Raises ``BuildFailed`` when nothing could be collected and a source failed.
    The story and its files are assembled in a temporary folder and moved into place at the end, the snapshot
    is written last: a month counts as built only when its snapshot exists."""
    start, end = month_bounds(month, tz)
    root = store.user_dir(user_id)
    work = root / f".build-{month}"
    shutil.rmtree(work, ignore_errors=True)
    img = work / "img"
    img.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {"month": month, "generated": dt.datetime.now(tz).isoformat(timespec="seconds"),
                            "seed": str(user_id), "errors": {}}
    try:
        covers = collectors.Covers()  # one time budget and one cap for all cover lookups of this build
        async with collectors.new_client() as client:
            steps: Dict[str, Callable[[], Awaitable[Any]]] = {
                "jellyfin": lambda: collectors.collect_jellyfin(client, cfg, start, end, img, covers),
                "music": lambda: collectors.collect_music(client, cfg, start, end, img, tz, covers),
                "audiobooks": lambda: collectors.collect_audiobookshelf(client, cfg, start, end, img, tz, covers),
                "podcasts": lambda: collectors.collect_podcasts(client, cfg, start, end, img, covers),
                "photos": lambda: collectors.collect_immich(client, cfg, start, end, img),
                "games": lambda: collectors.collect_games(client, cfg, start, end, img, *game_states(user_id, start, end), covers,
                                                          steam_states(user_id, start, end), data["errors"]),
                "retroachievements": lambda: collectors.collect_retroachievements(client, cfg, start, end, img, covers),
            }
            wanted = cfg.sources()
            await snapshot_steam(user_id, cfg, tz)  # a build on demand also keeps today's state (no-op if taken)
            for key, step in steps.items():
                if key not in wanted:
                    data[key] = {}
                    continue
                try:
                    data[key] = await step()
                except Exception as exc:  # noqa: BLE001 - a broken source must not stop the recap
                    data[key] = {}
                    data["errors"][key] = collectors.describe_error(exc)
            data["totals"] = compute_totals(data)
            data["soundtrack"] = []
            try:
                data["soundtrack"] = await collectors.collect_soundtrack(client, cfg, data["music"], img)
            except Exception as exc:  # noqa: BLE001
                data["errors"]["soundtrack"] = collectors.describe_error(exc)

        got_data = bool(data["totals"]["media_hours"] or (data["photos"] or {}).get("total")
                        or (data["retroachievements"] or {}).get("total"))
        if not got_data and data["errors"]:
            raise BuildFailed("nothing collected and a source failed")

        old = store.read_snapshot(user_id, month) or {}
        history = store.history(user_id, month)
        prev = history[-1] if history and history[-1]["month"] == prev_month(month) else None
        if not got_data:  # connected and reachable, but nothing happened that month: no story
            data["empty"] = True
            data["notified"] = True
            store.write_snapshot(user_id, month, data)
            return data
        data["highlights"] = story.highlights(data, history)
        if old.get("design") and not redesign:
            data["design"] = old["design"]
        else:
            # A redesign rolls again with a new seed and also avoids everything the month's current design used
            data["design_round"] = int(old.get("design_round") or 0) + (1 if old.get("design") else 0)
            data["design"] = story.choose_design(data, history, current=old.get("design") if redesign else None)
        if "lead" not in data["design"]:
            data["design"]["lead"] = story.pick_lead(data, data["highlights"], history)
        data["notified"] = bool(old.get("notified"))
        data.setdefault("design_round", int(old.get("design_round") or 0))
        (work / "index.html").write_text(story.render(data, prev, history), encoding="utf-8")

        target = store.month_dir(user_id, month)
        trash = root / f".old-{month}"
        shutil.rmtree(trash, ignore_errors=True)
        if target.exists():
            target.rename(trash)
        work.rename(target)
        shutil.rmtree(trash, ignore_errors=True)
        store.write_snapshot(user_id, month, data)
        store.clear_attempts(user_id, month)
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- background jobs -------------------------------------------------------------------------------------------

_running: Dict[Tuple[int, str], "asyncio.Task[Any]"] = {}
_background: Set["asyncio.Task[Any]"] = set()


def reset() -> None:
    """Forget running jobs (tests)."""
    _running.clear()
    _background.clear()


def busy() -> bool:
    return any(not t.done() for t in _running.values())


def is_running(user_id: int, month: str) -> bool:
    task = _running.get((user_id, month))
    return task is not None and not task.done()


async def wait_all() -> None:
    """Waits for the running builds (tests)."""
    while _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


def start(user_id: int, month: str, cfg: collectors.Config, tz, *, redesign: bool = False,
          on_done: Optional[Callable[[int, str, Dict[str, Any]], Awaitable[None]]] = None, log=None) -> str:
    """Starts a build in the background: ``"started"``, ``"running"`` (this month is being built) or ``"busy"``
    (another build is running). Must be called from the event loop."""
    if is_running(user_id, month):
        return "running"
    if busy():
        return "busy"

    async def job() -> None:
        try:
            data = await asyncio.wait_for(build_month(user_id, month, cfg, tz, redesign=redesign), BUILD_TIMEOUT)
            if on_done is not None and not data.get("empty"):
                await on_done(user_id, month, data)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - never leaves the task unobserved
            if log is not None:
                log("Recap build failed: %s", type(exc).__name__)
            try:
                store.record_attempt(user_id, month, dt.datetime.now().timestamp())
            except OSError:
                pass
        finally:
            _running.pop((user_id, month), None)

    task = asyncio.get_running_loop().create_task(job())
    _running[(user_id, month)] = task
    _background.add(task)
    task.add_done_callback(_background.discard)
    return "started"


__all__ = ["BuildFailed", "build_month", "snapshot_games", "snapshot_steam", "game_states", "steam_states", "start", "busy", "is_running", "prev_month", "month_bounds"]
