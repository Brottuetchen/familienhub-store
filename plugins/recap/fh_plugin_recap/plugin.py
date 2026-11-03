"""Monthly recap ("Rückblick") per person, in the style of a music-year story.

What it does:

- Every month (scheduled job) it builds, for each person who connected at least one source in *More -> My
  connections*, a story page about the previous month: Jellyfin viewing (Playback Reporting), ListenBrainz music,
  Audiobookshelf, podcasts (Nextcloud GPodder Sync) and Immich photos, with a soundtrack from Navidrome.
- The story is a page of the plugin (``pages``): the app opens it through a signed, short-lived link. Posters,
  photos, covers and audio clips are downloaded at build time, so the story loads nothing from other servers.
- ``POST /api/plugins/recap/build`` builds a month on demand, ``GET /api/plugins/recap/months`` lists the months,
  ``GET /api/plugins/recap/markdown/<month>`` returns an Obsidian note.

State is kept in files below ``<DATA_DIR>/plugin-data/recap/<user id>/`` (see ``store``), not in the database.
Each person only ever reaches their own folder. Needs a hub with personal plugin settings and plugin pages.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.core.logging_config import get_logger
from app.core.security import get_current_user
from app.plugins import Page, Plugin, Setting, get_setting

try:  # the personal-settings helpers came with plugin pages in the same hub release
    from app.plugins.api import get_personal_setting, notify
except ImportError:  # pragma: no cover - only on hubs without personal settings
    get_personal_setting = notify = None

from . import build, collectors, store, views
from .story import MONTHS, de_num

# httpx logs every request URL at INFO level; Navidrome's token and salt are part of the URL, so keep that quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = get_logger("plugin.recap")

PLUGIN_ID = "recap"
PAGE_ID = "recaps"
NEEDS_HUB = "a Familienhub with personal plugin settings and plugin pages"
COMPATIBLE = get_personal_setting is not None and hasattr(Plugin, "page") and hasattr(Plugin, "personal_settings")
TEST_TIMEOUT = 6.0

GLOBAL_KEYS = ("RECAP_JELLYFIN_URL", "RECAP_JELLYFIN_TOKEN", "RECAP_ABS_URL", "RECAP_IMMICH_URL",
               "RECAP_NAVIDROME_URL", "RECAP_NEXTCLOUD_URL")
PERSONAL_KEYS = ("RECAP_JELLYFIN_USER", "RECAP_LISTENBRAINZ_USER", "RECAP_LISTENBRAINZ_TOKEN", "RECAP_ABS_TOKEN",
                 "RECAP_IMMICH_KEY", "RECAP_NAVIDROME_USER", "RECAP_NAVIDROME_PASSWORD", "RECAP_NEXTCLOUD_USER",
                 "RECAP_NEXTCLOUD_APP_PASSWORD")

router = APIRouter(prefix=f"/api/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])


# --- settings --------------------------------------------------------------------------------------------------

def _url(value: Any, strip_api: bool = False) -> str:
    text = str(value or "").strip().rstrip("/")
    if strip_api and text.lower().endswith("/api"):
        text = text[:-4]
    return text if text.lower().startswith(("http://", "https://")) else ""


def _personal(user_id: int, key: str) -> str:
    if get_personal_setting is None:
        return ""
    return str(get_personal_setting(user_id, key, plugin_id=PLUGIN_ID) or "").strip()


def _global_urls() -> Dict[str, str]:
    return {"jellyfin_url": _url(get_setting("RECAP_JELLYFIN_URL")),
            "jellyfin_token": str(get_setting("RECAP_JELLYFIN_TOKEN") or "").strip(),
            "abs_url": _url(get_setting("RECAP_ABS_URL")),
            "immich_url": _url(get_setting("RECAP_IMMICH_URL"), strip_api=True),
            "navidrome_url": _url(get_setting("RECAP_NAVIDROME_URL")),
            "nextcloud_url": _url(get_setting("RECAP_NEXTCLOUD_URL"))}


def load_config(user_id: int, overrides: Optional[Dict[str, str]] = None) -> collectors.Config:
    """The server settings plus this person's own accounts. ``overrides`` (not yet saved personal values from the
    settings form) win over the stored ones."""
    def mine(key: str) -> str:
        if overrides is not None and key in overrides and str(overrides[key] or "").strip():
            return str(overrides[key]).strip()
        return _personal(user_id, key)

    return collectors.Config(
        **_global_urls(),
        jellyfin_user=mine("RECAP_JELLYFIN_USER"),
        listenbrainz_user=mine("RECAP_LISTENBRAINZ_USER"), listenbrainz_token=mine("RECAP_LISTENBRAINZ_TOKEN"),
        abs_token=mine("RECAP_ABS_TOKEN"), immich_key=mine("RECAP_IMMICH_KEY"),
        navidrome_user=mine("RECAP_NAVIDROME_USER"), navidrome_password=mine("RECAP_NAVIDROME_PASSWORD"),
        nextcloud_user=mine("RECAP_NEXTCLOUD_USER"), nextcloud_password=mine("RECAP_NEXTCLOUD_APP_PASSWORD"),
    )


def _tz():
    from app.config import settings

    return collectors.local_tz(getattr(settings, "timezone", "Europe/Berlin"))


def last_month() -> str:
    return build.prev_month(dt.datetime.now(_tz()).strftime("%Y-%m"))


def route_for(month: str) -> str:
    return f"/plugin-page/{PLUGIN_ID}/{PAGE_ID}?path={month}/"


# --- notification ----------------------------------------------------------------------------------------------

async def announce(user_id: int, month: str, data: Dict[str, Any]) -> None:
    """Tells the person their recap is ready, once per person and month (the snapshot remembers it)."""
    if notify is None or data.get("notified"):
        return
    year, number = map(int, month.split("-"))
    totals, jf = data.get("totals") or {}, data.get("jellyfin") or {}
    top = jf["series"][0]["name"] if jf.get("series") else None
    body = f"{de_num(totals.get('media_hours', 0))} Stunden Medien" + (f", ganz vorne: {top}" if top else "") + "."
    result = await notify(PLUGIN_ID, user_ids=[user_id], title=f"Dein {MONTHS[number - 1]}-Rückblick ist da",
                          body=body, data={"route": route_for(month)})
    if result.get("sent") or result.get("reason") in ("no_recipients", "invalid"):
        fresh = store.read_snapshot(user_id, month) or data
        fresh["notified"] = True
        store.write_snapshot(user_id, month, fresh)


# --- endpoints -------------------------------------------------------------------------------------------------

def _valid_month(month: str) -> str:
    if not store.MONTH_RE.match(month or ""):
        raise HTTPException(422, "Invalid month, expected YYYY-MM")
    return month


@router.post("/build")
async def build_endpoint(month: Optional[str] = Query(default=None), force: bool = Query(default=False),
                         user=Depends(get_current_user)):
    """Builds the logged-in person's recap (previous month, or ``?month=YYYY-MM``) in the background."""
    target = _valid_month(month) if month else last_month()
    if target > last_month():
        raise HTTPException(422, "Only finished months can be built")
    if store.read_snapshot(user.id, target) is not None and not force:
        return {"status": "exists", "month": target, "route": route_for(target)}
    cfg = load_config(user.id)
    if not cfg.sources():
        return {"status": "no_sources", "month": target}
    status = build.start(user.id, target, cfg, _tz(), on_done=announce, log=logger.warning)
    return {"status": status, "month": target, "route": route_for(target)}


@router.get("/months")
async def months_endpoint(user=Depends(get_current_user)):
    items = []
    for month in store.list_months(user.id):
        data = store.read_snapshot(user.id, month) or {}
        items.append({"month": month, "label": views.month_label(month), "route": route_for(month),
                      "media_hours": (data.get("totals") or {}).get("media_hours", 0),
                      "photos": (data.get("photos") or {}).get("total", 0),
                      "failed_sources": sorted((data.get("errors") or {}).keys())})
    building = [m for (uid, m) in list(build._running) if uid == user.id and build.is_running(uid, m)]
    return {"items": items, "building": building}


@router.get("/markdown/{month}", response_class=PlainTextResponse)
async def markdown_endpoint(month: str, user=Depends(get_current_user)):
    """The Obsidian note of a month (for an external agent with an access key)."""
    data = store.read_snapshot(user.id, _valid_month(month))
    if not data or data.get("empty") or not store.has_story(user.id, month):
        raise HTTPException(404, "No recap for this month")
    return PlainTextResponse(views.render_obsidian(data, route_for(month)), media_type="text/markdown; charset=utf-8")


# --- connection test -------------------------------------------------------------------------------------------

async def _check(client: httpx.AsyncClient, name: str, call) -> str:
    """One short, address-free line per source."""
    try:
        return f"{name}: {await call()}"
    except httpx.TimeoutException:
        return f"{name}: no answer in time"
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        return f"{name}: " + ("login rejected" if code in (401, 403) else f"error {code}")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return f"{name}: not reachable"


# --- plugin ----------------------------------------------------------------------------------------------------

class RecapPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(
            id=PLUGIN_ID, name="Recap", version="1.0.1",
            description="A monthly recap per person: series, music, audiobooks, podcasts and photos as a story.",
            capabilities=frozenset(),
            settings=(
                Setting("RECAP_JELLYFIN_URL", "Jellyfin address", required=False, type="url"),
                Setting("RECAP_JELLYFIN_TOKEN", "Jellyfin server token", secret=True, required=False, type="password"),
                Setting("RECAP_ABS_URL", "Audiobookshelf address", required=False, type="url"),
                Setting("RECAP_IMMICH_URL", "Immich address", required=False, type="url"),
                Setting("RECAP_NAVIDROME_URL", "Navidrome address", required=False, type="url"),
                Setting("RECAP_NEXTCLOUD_URL", "Nextcloud address", required=False, type="url"),
            ),
            personal_settings=(
                Setting("RECAP_JELLYFIN_USER", "Jellyfin user name", required=False),
                Setting("RECAP_LISTENBRAINZ_USER", "ListenBrainz user name", required=False),
                Setting("RECAP_LISTENBRAINZ_TOKEN", "ListenBrainz token", secret=True, required=False, type="password"),
                Setting("RECAP_ABS_TOKEN", "Audiobookshelf API token", secret=True, required=False, type="password"),
                Setting("RECAP_IMMICH_KEY", "Immich API key", secret=True, required=False, type="password"),
                Setting("RECAP_NAVIDROME_USER", "Navidrome user name", required=False),
                Setting("RECAP_NAVIDROME_PASSWORD", "Navidrome password", secret=True, required=False, type="password"),
                Setting("RECAP_NEXTCLOUD_USER", "Nextcloud user name", required=False),
                Setting("RECAP_NEXTCLOUD_APP_PASSWORD", "Nextcloud app password", secret=True, required=False,
                        type="password"),
            ),
            pages=(Page(PAGE_ID, {"de": "Rückblicke", "en": "Recaps"}, "celebration"),),
        )
        self.schedule_minutes = 60

    def routers(self):
        return [router]

    def is_configured(self) -> bool:
        # Works per person: nothing is required from the admin, a person without accounts simply gets no recap.
        return COMPATIBLE

    def is_set_up(self) -> bool:
        return any(get_setting(k) not in (None, "") for k in GLOBAL_KEYS)

    # -- scheduled job: starts at most one background build per run ---------------------------------------------

    async def scheduled(self, db) -> None:
        from app.models.user import User

        month = last_month()
        ids = [u.id for u in db.query(User).filter(User.is_active == True).order_by(User.id).all()]  # noqa: E712
        for uid in ids:  # a notification that did not go out yet (rate limit, restart in between)
            data = store.read_snapshot(uid, month)
            if data and not data.get("notified") and not data.get("empty") and store.has_story(uid, month):
                await announce(uid, month, data)
        if build.busy():
            return
        for uid in ids:
            if store.read_snapshot(uid, month) is not None:
                continue
            attempts = store.attempts(uid, month)
            if attempts["count"] >= build.MAX_ATTEMPTS or time.time() - attempts["last"] < build.RETRY_AFTER:
                continue
            cfg = load_config(uid)
            if not cfg.sources():
                continue
            if build.start(uid, month, cfg, _tz(), on_done=announce, log=logger.warning) == "started":
                break  # one person per run; the next run takes the next one

    # -- pages --------------------------------------------------------------------------------------------------

    async def page(self, user, page_id: str, path: str):
        if page_id != PAGE_ID:
            return None
        if path in ("", "index.html"):
            return "text/html; charset=utf-8", views.render_index(user.id).encode("utf-8")
        hit = store.resolve(user.id, path)
        if hit is None:
            return None
        kind, file = hit
        if file.stat().st_size > store.MAX_ASSET_BYTES * 2:
            return None
        body = file.read_bytes()
        if kind == "story":
            text = body.decode("utf-8")
            clean = path.rstrip("/")
            if not path.endswith("/") and "/" not in clean:  # opened as "2026-10": make relative files resolve
                text = text.replace("<head>", f'<head><base href="{clean}/">', 1)
            return "text/html; charset=utf-8", text.encode("utf-8")
        return store.CONTENT_TYPES[kind], body

    # -- connection test ----------------------------------------------------------------------------------------

    async def test_personal_settings(self, user, values: Dict[str, str]) -> Dict[str, Any]:
        cfg = load_config(user.id, overrides=values)
        lines: List[str] = []
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": collectors.USER_AGENT}) as client:
            if cfg.jellyfin_user:
                if not (cfg.jellyfin_url and cfg.jellyfin_token):
                    lines.append("Jellyfin: not set up by the admin")
                else:
                    async def jellyfin():
                        return "connected" if await collectors.jellyfin_user_id(client, cfg) else "user name not found"
                    lines.append(await _check(client, "Jellyfin", jellyfin))
            if cfg.listenbrainz_user:
                async def listenbrainz():
                    user_part = quote(cfg.listenbrainz_user, safe="")
                    if cfg.listenbrainz_token:
                        r = await client.get(f"{collectors.LISTENBRAINZ}/1/validate-token",
                                             headers={"Authorization": "Token " + cfg.listenbrainz_token})
                        r.raise_for_status()
                        info = r.json()
                        if not info.get("valid"):
                            return "token rejected"
                        if str(info.get("user_name", "")).lower() != cfg.listenbrainz_user.lower():
                            return "token belongs to another user"
                        return "connected"
                    r = await client.get(f"{collectors.LISTENBRAINZ}/1/user/{user_part}/listen-count")
                    r.raise_for_status()
                    return "connected"
                lines.append(await _check(client, "ListenBrainz", listenbrainz))
            if cfg.abs_token:
                if not cfg.abs_url:
                    lines.append("Audiobookshelf: not set up by the admin")
                else:
                    async def abs_():
                        (await client.get(f"{cfg.abs_url}/api/me", headers={"Authorization": "Bearer " + cfg.abs_token})
                         ).raise_for_status()
                        return "connected"
                    lines.append(await _check(client, "Audiobookshelf", abs_))
            if cfg.immich_key:
                if not cfg.immich_url:
                    lines.append("Immich: not set up by the admin")
                else:
                    async def immich():
                        (await client.get(f"{cfg.immich_url}/api/users/me", headers={"x-api-key": cfg.immich_key})
                         ).raise_for_status()
                        return "connected"
                    lines.append(await _check(client, "Immich", immich))
            if cfg.navidrome_user or cfg.navidrome_password:
                if not (cfg.navidrome_url and cfg.navidrome_user and cfg.navidrome_password):
                    lines.append("Navidrome: incomplete (needs server address, user name and password)")
                else:
                    async def navidrome():
                        salt = secrets.token_hex(6)
                        query = urlencode({"u": cfg.navidrome_user, "s": salt, "v": "1.16.1", "c": "familienhub-recap", "f": "json",
                                           "t": hashlib.md5((cfg.navidrome_password + salt).encode()).hexdigest()})  # noqa: S324
                        r = await client.get(f"{cfg.navidrome_url}/rest/ping.view?{query}")
                        r.raise_for_status()
                        reply = r.json()["subsonic-response"]
                        return "connected" if reply.get("status") == "ok" else "login rejected"
                    lines.append(await _check(client, "Navidrome", navidrome))
            if cfg.nextcloud_user or cfg.nextcloud_password:
                if not (cfg.nextcloud_url and cfg.nextcloud_user and cfg.nextcloud_password):
                    lines.append("Nextcloud: incomplete (needs server address, user name and app password)")
                else:
                    async def nextcloud():
                        r = await client.get(f"{cfg.nextcloud_url}/index.php/apps/gpoddersync/episode_action",
                                             params={"since": int(time.time())}, auth=(cfg.nextcloud_user, cfg.nextcloud_password),
                                             headers={"OCS-APIRequest": "true"})
                        if r.status_code == 404:
                            return "GPodder Sync is not installed"
                        r.raise_for_status()
                        return "connected"
                    lines.append(await _check(client, "Nextcloud", nextcloud))
        if not lines:
            return {"ok": True, "level": "info", "message": "Nothing to test yet. Fill in at least one account."}
        failed = [ln for ln in lines if not ln.endswith("connected")]
        return {"ok": not failed, "level": "ok" if not failed else "warn", "message": "; ".join(lines)[:300]}

