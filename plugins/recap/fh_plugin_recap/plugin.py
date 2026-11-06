"""Monthly recap ("Rückblick") per person, in the style of a music-year story.

What it does:

- Every month (scheduled job) it builds, for each person who connected at least one source in *More -> My
  connections*, a story page about the previous month: Jellyfin viewing (Playback Reporting), ListenBrainz music,
  Audiobookshelf, podcasts (Nextcloud GPodder Sync), Immich photos and games (ES-DE play statistics, Steam library), with a soundtrack from Navidrome.
- On 1 January it also builds the annual recap ("Jahresrückblick") of the year that just ended, from the same sources over the
  whole year; ``POST .../build?year=YYYY`` builds a finished year on demand. It lives next to the months (``2026/``, ``2026.json``).
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
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.core.logging_config import get_logger
from app.core.security import get_current_admin_user, get_current_user
from app.plugins import Page, Plugin, Setting, get_setting

try:  # the personal-settings helpers came with plugin pages in the same hub release
    from app.plugins.api import get_personal_setting, notify
except ImportError:  # pragma: no cover - only on hubs without personal settings
    get_personal_setting = notify = None

from . import build, collectors, gallery, store, views
from .story import MONTHS, de_num

# httpx logs every request URL at INFO level; Navidrome's token and salt are part of the URL, so keep that quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = get_logger("plugin.recap")
REDESIGN_PATH = re.compile(r"^(\d{4}-(?:0[1-9]|1[0-2])|20\d\d)/(neu|status)$")  # a month or a year
# Design preview as a page: "preview/" is the gallery, "preview/g~palette~shape~type~offset~variants" and "preview/s~..." (the story) keep the choices in the path
PREVIEW_PATH = re.compile(r"^preview(?:/(?:([gs])~([a-z]+)~([a-z]+)~([a-z]+)~(\d)~([a-z0-9.]*)))?/?$")

PLUGIN_ID = "recap"
PAGE_ID = "recaps"
NEEDS_HUB = "a Familienhub with personal plugin settings and plugin pages"
COMPATIBLE = get_personal_setting is not None and hasattr(Plugin, "page") and hasattr(Plugin, "personal_settings")
TEST_TIMEOUT = 6.0

GLOBAL_KEYS = ("RECAP_JELLYFIN_URL", "RECAP_JELLYFIN_TOKEN", "RECAP_ABS_URL", "RECAP_IMMICH_URL",
               "RECAP_NAVIDROME_URL", "RECAP_NEXTCLOUD_URL", "RECAP_GAMES_DIR", "RECAP_ROMM_URL",
               "RECAP_ROMM_TOKEN")
PERSONAL_KEYS = ("RECAP_JELLYFIN_USER", "RECAP_LISTENBRAINZ_USER", "RECAP_LISTENBRAINZ_TOKEN", "RECAP_ABS_TOKEN",
                 "RECAP_IMMICH_KEY", "RECAP_NAVIDROME_USER", "RECAP_NAVIDROME_PASSWORD", "RECAP_NEXTCLOUD_USER",
                 "RECAP_NEXTCLOUD_APP_PASSWORD", "RECAP_GAMES_DEVICES", "RECAP_RA_USERNAME", "RECAP_RA_API_KEY",
                 "RECAP_STEAM_ID", "RECAP_STEAM_API_KEY")

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
            "nextcloud_url": _url(get_setting("RECAP_NEXTCLOUD_URL")),
            "games_dir": str(get_setting("RECAP_GAMES_DIR") or "").strip() or "/app/external/games",
            "romm_url": _url(get_setting("RECAP_ROMM_URL")),
            "romm_token": str(get_setting("RECAP_ROMM_TOKEN") or "").strip()}


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
        games_devices=collectors.parse_devices(mine("RECAP_GAMES_DEVICES")),
        ra_username=mine("RECAP_RA_USERNAME"), ra_api_key=mine("RECAP_RA_API_KEY"),
        steam_id=mine("RECAP_STEAM_ID"), steam_api_key=mine("RECAP_STEAM_API_KEY"),
    )


def is_admin(user: Any) -> bool:
    """Admins as the hub defines them (``get_current_admin_user``): the ``is_admin`` flag or the admin role."""
    return bool(getattr(user, "is_admin", False) or getattr(user, "role", None) == "admin")


def _tz():
    from app.config import settings

    return collectors.local_tz(getattr(settings, "timezone", "Europe/Berlin"))


def last_month() -> str:
    return build.prev_month(dt.datetime.now(_tz()).strftime("%Y-%m"))


def this_month() -> str:
    return dt.datetime.now(_tz()).strftime("%Y-%m")


def last_year() -> Optional[str]:
    """The year that just ended while we are still in January (the window for the automatic annual build), else None."""
    now = dt.datetime.now(_tz())
    return str(now.year - 1) if now.month == 1 else None


def current_year() -> int:
    """Years before this one are finished."""
    return dt.datetime.now(_tz()).year


def route_for(month: str) -> str:
    return f"/plugin-page/{PLUGIN_ID}/{PAGE_ID}?path={month}/"


# --- notification ----------------------------------------------------------------------------------------------

async def announce(user_id: int, month: str, data: Dict[str, Any]) -> None:
    """Tells the person their recap is ready, once per person and month (the snapshot remembers it)."""
    if notify is None or data.get("notified"):
        return
    totals, jf = data.get("totals") or {}, data.get("jellyfin") or {}
    top = jf["series"][0]["name"] if jf.get("series") else None
    body = f"{de_num(totals.get('media_hours', 0))} Stunden Medien" + (f", ganz vorne: {top}" if top else "") + "."
    title = f"Dein Jahresrückblick {month} ist da" if store.is_year(month) else f"Dein {MONTHS[int(month[5:]) - 1]}-Rückblick ist da"
    result = await notify(PLUGIN_ID, user_ids=[user_id], title=title,
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


def _valid_year(year: str) -> str:
    if not store.YEAR_RE.match(str(year or "")):
        raise HTTPException(422, "Invalid year, expected YYYY")
    return str(year)


@router.post("/build")
async def build_endpoint(month: Optional[str] = Query(default=None), force: bool = Query(default=False),
                         redesign: bool = Query(default=False), year: Optional[str] = Query(default=None),
                         preview: bool = Query(default=False), user=Depends(get_current_user)):
    """Builds the logged-in person's recap (previous month, or ``?month=YYYY-MM``) in the background, or with
    ``?year=YYYY`` the annual recap of a finished year. ``redesign`` rebuilds an existing one with a fresh design
    instead of keeping the stored one. ``preview=true`` builds the *running* month up to today as an interim state
    (``YYYY-MM-preview``): kept apart from the real month, never announced, not part of the design memory or any
    comparison, rebuilt on every call."""
    if year is not None and month is not None:
        raise HTTPException(422, "Give either a month or a year")
    if preview:
        if year is not None:
            raise HTTPException(422, "A preview is only for the running month")
        if (_valid_month(month) if month else this_month()) != this_month():
            raise HTTPException(422, "A preview is only for the running month")
        target = store.preview_period(this_month())
        cfg = load_config(user.id)
        if not cfg.sources():
            return {"status": "no_sources", "month": target}
        status = build.start(user.id, target, cfg, _tz(), redesign=redesign, on_done=announce, log=logger.warning)
        return {"status": status, "month": target, "route": route_for(target), "preview": True}
    if year is not None:
        target = _valid_year(year)
        if int(target) >= current_year():
            raise HTTPException(422, "Only finished years can be built")
    else:
        target = _valid_month(month) if month else last_month()
    if year is None and target > last_month():
        raise HTTPException(422, "Only finished months can be built")
    if store.read_snapshot(user.id, target) is not None and not (force or redesign):
        return {"status": "exists", "month": target, "route": route_for(target)}
    cfg = load_config(user.id)
    if not cfg.sources():
        return {"status": "no_sources", "month": target}
    status = build.start(user.id, target, cfg, _tz(), redesign=redesign, on_done=announce, log=logger.warning)
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
    years = []
    for year in store.list_years(user.id):
        data = store.read_snapshot(user.id, year) or {}
        years.append({"year": year, "label": views.year_label(year), "route": route_for(year),
                      "media_hours": (data.get("totals") or {}).get("media_hours", 0),
                      "photos": (data.get("photos") or {}).get("total", 0),
                      "failed_sources": sorted((data.get("errors") or {}).keys())})
    previews = []
    for period in store.list_previews(user.id):  # the running month's interim state, marked as such
        data = store.read_snapshot(user.id, period) or {}
        previews.append({"month": store.preview_month(period), "period": period, "preview": True, "label": views.month_label(period),
                         "route": route_for(period), "until": data.get("until"), "generated": data.get("generated"),
                         "media_hours": (data.get("totals") or {}).get("media_hours", 0),
                         "photos": (data.get("photos") or {}).get("total", 0),
                         "failed_sources": sorted((data.get("errors") or {}).keys())})
    building = [m for (uid, m) in list(build._running) if uid == user.id and build.is_running(uid, m)]
    return {"items": items, "years": years, "previews": previews, "building": building}


@router.get("/markdown/{month}", response_class=PlainTextResponse)
async def markdown_endpoint(month: str, user=Depends(get_current_user)):
    """The Obsidian note of a month, or of a year (``/markdown/2026``), for an external agent with an access key."""
    year = store.is_year(month)
    data = store.read_snapshot(user.id, _valid_year(month) if year else _valid_month(month))
    if not data or data.get("empty") or not store.has_story(user.id, month):
        raise HTTPException(404, "No recap for this period")
    text = views.render_obsidian_year(data, route_for(month)) if year else views.render_obsidian(data, route_for(month))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


# --- design preview ----------------------------------------------------------------------------------------------

def _api_link(kind, palette, shape, type_, offset, sel) -> str:
    query = urlencode({"palette": palette, "shape": shape, "type": type_, "offset": offset, "v": gallery.encode_sel(sel)})
    return f"/api/plugins/{PLUGIN_ID}/preview{'/story' if kind == 's' else ''}?{query}"


def _page_link(kind, palette, shape, type_, offset, sel) -> str:
    return f"{kind}~{palette}~{shape}~{type_}~{offset}~{gallery.encode_sel(sel)}"  # relative to preview/


def preview_html(kind, link, palette=None, shape=None, type_=None, offset=None, v="") -> str:
    palette, shape, type_, offset, sel = gallery.params(palette, shape, type_, offset, v)
    if kind == "s":
        return gallery.render_story(palette, shape, type_, offset, sel, link("g", palette, shape, type_, offset, sel))
    return gallery.render_gallery(palette, shape, type_, offset, sel, link)


@router.get("/preview", response_class=HTMLResponse)
async def preview_endpoint(palette: str = "", shape: str = "", type: str = "", offset: str = "", v: str = "",
                           user=Depends(get_current_admin_user)):
    """Every slide design (admins only) with made-up data, side by side."""
    return HTMLResponse(preview_html("g", _api_link, palette, shape, type, offset, v))


@router.get("/preview/story", response_class=HTMLResponse)
async def preview_story_endpoint(palette: str = "", shape: str = "", type: str = "", offset: str = "", v: str = "",
                                 user=Depends(get_current_admin_user)):
    """The full swipeable sample (admins only) story for a choice of palette, shape, type and variants."""
    return HTMLResponse(preview_html("s", _api_link, palette, shape, type, offset, v))


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
            id=PLUGIN_ID, name="Recap", version="1.8.0",
            description="A monthly recap per person: series, music, audiobooks, podcasts, photos, games and achievements as a story.",
            capabilities=frozenset(),
            settings=(
                Setting("RECAP_JELLYFIN_URL", "Jellyfin address", required=False, type="url"),
                Setting("RECAP_JELLYFIN_TOKEN", "Jellyfin server token", secret=True, required=False, type="password"),
                Setting("RECAP_ABS_URL", "Audiobookshelf address", required=False, type="url"),
                Setting("RECAP_IMMICH_URL", "Immich address", required=False, type="url"),
                Setting("RECAP_NAVIDROME_URL", "Navidrome address", required=False, type="url"),
                Setting("RECAP_NEXTCLOUD_URL", "Nextcloud address", required=False, type="url"),
                Setting("RECAP_GAMES_DIR", "Folder with the ES-DE game statistics", required=False,
                        default="/app/external/games"),
                Setting("RECAP_ROMM_URL", "RomM address", required=False, type="url"),
                Setting("RECAP_ROMM_TOKEN", "RomM client token", secret=True, required=False, type="password"),
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
                Setting("RECAP_GAMES_DEVICES", "Game devices (ES-DE)", required=False),
                Setting("RECAP_RA_USERNAME", "RetroAchievements user name", required=False),
                Setting("RECAP_RA_API_KEY", "RetroAchievements API key", secret=True, required=False, type="password"),
                Setting("RECAP_STEAM_ID", "Steam ID (SteamID64)", required=False),
                Setting("RECAP_STEAM_API_KEY", "Steam Web API key", secret=True, required=False, type="password"),
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
        for uid in ids:  # the daily game snapshot (cheap, no network); before anything that may return early
            try:
                build.snapshot_games(uid, load_config(uid), _tz())
            except Exception:  # noqa: BLE001 - never disturbs the builds below
                pass
        for uid in ids:  # the daily Steam snapshot (one request per person, only until it worked)
            await build.snapshot_steam(uid, load_config(uid), _tz())
        year = last_year()  # the annual recap is built in January only, for the year that just ended
        for uid in ids:  # a notification that did not go out yet (rate limit, restart in between)
            for period in (month, year):
                data = store.read_snapshot(uid, period) if period else None
                if data and not data.get("notified") and not data.get("empty") and store.has_story(uid, period):
                    await announce(uid, period, data)
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
                return  # one person per run; the next run takes the next one
        if year:
            self._schedule_year(ids, year, month)

    def _schedule_year(self, ids, year: str, december: str) -> None:
        """Starts at most one annual build per run, once per person (the snapshot marks it as done, failed attempts are
        paced like the months'). The year waits for December's monthly recap so its curve can use the snapshot, unless
        that one has given up."""
        if build.busy():
            return
        for uid in ids:
            if store.read_snapshot(uid, year) is not None:
                continue
            if store.read_snapshot(uid, december) is None and store.attempts(uid, december)["count"] < build.MAX_ATTEMPTS:
                cfg = load_config(uid)
                if cfg.sources():
                    continue
            attempts = store.attempts(uid, year)
            if attempts["count"] >= build.MAX_ATTEMPTS or time.time() - attempts["last"] < build.RETRY_AFTER:
                continue
            cfg = load_config(uid)
            if not cfg.sources():
                continue
            if build.start(uid, year, cfg, _tz(), on_done=announce, log=logger.warning) == "started":
                return

    # -- pages --------------------------------------------------------------------------------------------------

    async def page(self, user, page_id: str, path: str):
        if page_id != PAGE_ID:
            return None
        if path in ("", "index.html"):
            return "text/html; charset=utf-8", views.render_index(user.id, admin=is_admin(user)).encode("utf-8")
        look = PREVIEW_PATH.match(path)
        if look:
            if not is_admin(user):  # the design preview is for admins only
                return None
            kind, *choice = look.groups()
            text = preview_html(kind or "g", _page_link, *choice)
            if "/" not in path.rstrip("/") and not path.endswith("/"):  # opened as "preview": make the relative links resolve
                text = text.replace("<head>", '<head><base href="preview/">', 1)
            return "text/html; charset=utf-8", text.encode("utf-8")
        action = REDESIGN_PATH.match(path)
        if action:
            return self._redesign(user.id, *action.groups())
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

    def _redesign(self, user_id: int, month: str, step: str):  # ``month`` may also be a year
        """``<month>/neu`` rebuilds a month the person already has with a fresh design (a GET, but only reachable
        through the person's own short-lived signed page link); ``<month>/status`` tells the waiting page when it is done."""
        if store.read_snapshot(user_id, month) is None:
            return None
        if step == "status":
            body = json.dumps({"running": build.is_running(user_id, month)})
            return "application/json", body.encode("utf-8")
        status = "running" if build.is_running(user_id, month) else "no_sources"
        cfg = load_config(user_id)
        if cfg.sources() and status != "running":
            status = build.start(user_id, month, cfg, _tz(), redesign=True, on_done=announce, log=logger.warning)
        return "text/html; charset=utf-8", views.render_redesign(month, status).encode("utf-8")

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
            if cfg.games_devices:
                async def games():
                    readable = collectors.read_games(cfg.games_dir, cfg.games_devices)
                    return "connected" if readable else "no device file readable (check the device names)"
                lines.append(await _check(client, "Games", games))
                if cfg.romm_url and cfg.romm_token:
                    async def romm():
                        (await client.get(f"{cfg.romm_url}/api/platforms", headers={"Authorization": "Bearer " + cfg.romm_token})
                         ).raise_for_status()
                        return "connected"
                    lines.append(await _check(client, "RomM", romm))
            if cfg.ra_username or cfg.ra_api_key:
                if not (cfg.ra_username and cfg.ra_api_key):
                    lines.append("RetroAchievements: incomplete (needs user name and API key)")
                else:
                    async def retroachievements():
                        await collectors.ra_json(client, cfg, "API_GetUserProfile.php")
                        return "connected"
                    lines.append(await _check(client, "RetroAchievements", retroachievements))
            if cfg.steam_id or cfg.steam_api_key:
                if not (cfg.steam_id and cfg.steam_api_key):
                    lines.append("Steam: incomplete (needs SteamID64 and API key)")
                else:
                    async def steam():
                        try:
                            await collectors.steam_owned(client, cfg)
                        except LookupError:
                            return "no games visible (set the profile's game details to public)"
                        return "connected"
                    lines.append(await _check(client, "Steam", steam))
        if not lines:
            return {"ok": True, "level": "info", "message": "Nothing to test yet. Fill in at least one account."}
        failed = [ln for ln in lines if not ln.endswith("connected")]
        return {"ok": not failed, "level": "ok" if not failed else "warn", "message": "; ".join(lines)[:300]}

