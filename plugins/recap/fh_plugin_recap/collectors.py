"""Collectors: one coroutine per source. Each returns a plain dict for the snapshot and raises on failure; the
builder catches that per source, so one broken service never stops a recap.

All HTTP goes through ``httpx`` with timeouts. Nothing here logs a URL, a token or a response body.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import datetime as dt
import hashlib
import html
import json
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse

import httpx

LISTENBRAINZ = "https://api.listenbrainz.org"
COVERART = "https://coverartarchive.org"
USER_AGENT = "familienhub-recap/1.0"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FEED_BYTES = 3 * 1024 * 1024
CLIP_SECONDS = 11
CLIP_COUNT = 22  # one song per slide; a month with every special slide has about 20
COVER_CONCURRENCY = 4  # cover lookups running at the same time
COVER_MAX_JOBS = 80  # lookups per build; further items get the generated placeholder
COVER_SECONDS = 180  # all lookups of one build together
COVER_ITEM_SECONDS = 40  # one lookup with all its fallback steps
NAVI_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
LASTFM_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"  # the grey star image Last.fm serves for "no picture"
COVER_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, LookupError)
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_IMAGES = "https://cdn.cloudflare.steamstatic.com/steam/apps"
RA_API = "https://retroachievements.org/API"
RA_MEDIA = "https://media.retroachievements.org"
RA_RAREST = 6  # achievements with a badge picture in the story
RA_TOP_GAMES = 5
RA_MAX_AWARDS = 4
RA_AWARDS = {("Mastery/Completion", 1): "mastered", ("Mastery/Completion", 0): "completed",
             ("Game Beaten", 1): "beaten", ("Game Beaten", 0): "beaten_soft"}
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
GAME_PREFIX = "game:./"
GAME_SYSTEMS = {
    "gba": "Game Boy Advance", "gbc": "Game Boy Color", "gb": "Game Boy", "nes": "NES", "snes": "SNES",
    "n64": "Nintendo 64", "gc": "GameCube", "wii": "Wii", "wiiu": "Wii U", "switch": "Nintendo Switch",
    "nds": "Nintendo DS", "3ds": "Nintendo 3DS", "psx": "PlayStation", "ps2": "PlayStation 2", "ps3": "PlayStation 3",
    "psp": "PSP", "psvita": "PS Vita", "xbox": "Xbox", "xbox360": "Xbox 360", "steam": "Steam", "windows": "PC",
    "dreamcast": "Dreamcast", "megadrive": "Mega Drive",
}


@dataclass
class Config:
    """Everything one build needs, read once (so the background task never touches the hub's settings)."""

    jellyfin_url: str = ""
    jellyfin_token: str = ""
    jellyfin_user: str = ""
    listenbrainz_user: str = ""
    listenbrainz_token: str = ""
    abs_url: str = ""
    abs_token: str = ""
    immich_url: str = ""
    immich_key: str = ""
    navidrome_url: str = ""
    navidrome_user: str = ""
    navidrome_password: str = ""
    nextcloud_url: str = ""
    nextcloud_user: str = ""
    nextcloud_password: str = ""
    games_dir: str = "/app/external/games"
    romm_url: str = ""
    romm_token: str = ""
    games_devices: Tuple[str, ...] = ()
    ra_username: str = ""
    ra_api_key: str = ""

    def sources(self) -> List[str]:
        """The statistics sources this person has connected (the soundtrack alone is not one)."""
        found = []
        if self.jellyfin_url and self.jellyfin_token and self.jellyfin_user:
            found.append("jellyfin")
        if self.listenbrainz_user:
            found.append("music")
        if self.abs_url and self.abs_token:
            found.append("audiobooks")
        if self.nextcloud_url and self.nextcloud_user and self.nextcloud_password:
            found.append("podcasts")
        if self.immich_url and self.immich_key:
            found.append("photos")
        if self.games_devices:
            found.append("games")
        if self.ra_username and self.ra_api_key:
            found.append("retroachievements")
        return found

    @property
    def soundtrack(self) -> bool:
        return bool(self.navidrome_url and self.navidrome_user and self.navidrome_password)


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=6.0), follow_redirects=True,
                             headers={"User-Agent": USER_AGENT})


def describe_error(exc: BaseException) -> str:
    """A short, secret-free description of a failure for the snapshot (never ``str(exc)``: it may hold a URL)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTPStatusError {exc.response.status_code}"
    return type(exc).__name__


async def get_json(client: httpx.AsyncClient, url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None,
                   body: Any = None) -> Any:
    if body is not None:
        response = await client.post(url, headers=headers, params=params, json=body)
    else:
        response = await client.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "x"


def sniff_image(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def save_image(client: httpx.AsyncClient, url: str, directory: Path, name: str,
                     headers: Optional[dict] = None) -> Optional[str]:
    """Downloads an image into ``directory`` and returns its relative path (``img/<name>.<ext>``), or None.
    Only http(s) addresses, at most ``MAX_IMAGE_BYTES``, a content type of ``image/*`` (when the server names one)
    and real image data (checked by content, not by what the server claims) are accepted."""
    if not _safe_feed_url(url):
        return None
    content = b""
    try:
        async with client.stream("GET", url, headers=headers, timeout=30.0) as response:
            response.raise_for_status()
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind and not kind.startswith("image/"):
                return None
            async for chunk in response.aiter_bytes():
                content += chunk
                if len(content) > MAX_IMAGE_BYTES:
                    return None
    except (httpx.HTTPError, ValueError):
        return None
    ext = sniff_image(content)
    if ext is None or len(content) < 500:
        return None
    filename = f"{name}.{ext}"
    (directory / filename).write_bytes(content)
    return f"img/{filename}"


class Covers:
    """Runs the cover lookups of one build: a few at a time, each with its own time limit, all together within one
    time budget and at most ``max_jobs`` of them. A lookup that fails, times out or is over the cap just gives None
    (the story then shows a generated placeholder), so covers can never fail or stall a build."""

    def __init__(self, concurrency: int = COVER_CONCURRENCY, max_jobs: int = COVER_MAX_JOBS,
                 seconds: float = COVER_SECONDS, item_seconds: float = COVER_ITEM_SECONDS) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.left = max_jobs
        self.seconds = seconds
        self.item_seconds = item_seconds
        self.deadline: Optional[float] = None

    async def one(self, make: Callable[[], Awaitable[Optional[str]]]) -> Optional[str]:
        loop = asyncio.get_running_loop()
        if self.left <= 0:
            return None
        self.left -= 1
        if self.deadline is None:
            self.deadline = loop.time() + self.seconds
        async with self.sem:
            remaining = self.deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(make(), min(self.item_seconds, remaining))
            except Exception:  # noqa: BLE001 - timeouts and anything else: no cover, never a failed build
                return None

    async def fill(self, jobs: List[Tuple[dict, str, Callable[[], Awaitable[Optional[str]]]]]) -> None:
        """``jobs``: (item, field, lookup). Sets ``item[field]`` to the stored image path or None."""
        results = await asyncio.gather(*(self.one(make) for _, _, make in jobs))
        for (item, field, _), path in zip(jobs, results):
            item[field] = path


# --- Navidrome (Subsonic API), used for soundtrack and as a cover source ----------------------------------------

def navi_url(cfg: "Config", endpoint: str, **extra) -> str:
    salt = secrets.token_hex(6)
    auth = {"u": cfg.navidrome_user, "t": hashlib.md5((cfg.navidrome_password + salt).encode()).hexdigest(),  # noqa: S324 - Subsonic's token scheme
            "s": salt, "v": "1.16.1", "c": "familienhub-recap", "f": "json"}
    return f"{cfg.navidrome_url}/rest/{endpoint}?" + urlencode({**auth, **extra})


async def navi_json(client, cfg: "Config", endpoint: str, **extra) -> dict:
    res = (await get_json(client, navi_url(cfg, endpoint, **extra)))["subsonic-response"]
    if res.get("status", "ok") != "ok":
        raise LookupError("Navidrome error")
    return res


async def navi_cover(client, cfg: "Config", cover_id: Any, directory: Path, name: str) -> Optional[str]:
    if not cover_id or not NAVI_ID_RE.match(str(cover_id)):
        return None
    return await save_image(client, navi_url(cfg, "getCoverArt.view", id=cover_id, size=500), directory, name)


async def navi_song_cover(client, cfg: "Config", directory: Path, title: str, artist: str, name: str) -> Optional[str]:
    """Cover of the Navidrome song that matches title and artist."""
    wanted_title, wanted_artist = title.casefold(), artist.casefold()
    for query in (title, f"{title} {artist}"):
        res = await navi_json(client, cfg, "search3", query=query, songCount=20, artistCount=0, albumCount=0)
        songs = res.get("searchResult3", {}).get("song", [])
        same = [x for x in songs if str(x.get("artist") or "").casefold() == wanted_artist]
        hit = (next((x for x in same if str(x.get("title") or "").casefold() == wanted_title), None)
               or next((x for x in same if wanted_title in str(x.get("title") or "").casefold()), None))
        if hit:
            return await navi_cover(client, cfg, hit.get("coverArt") or hit.get("albumId"), directory, name)
    return None


async def navi_artist_cover(client, cfg: "Config", directory: Path, artist: str, name: str) -> Optional[str]:
    """An artist picture from Navidrome: its artist info image, else the cover of the artist's most played album or song."""
    wanted = artist.casefold()
    res = await navi_json(client, cfg, "search3", query=artist, artistCount=5, songCount=0, albumCount=0)
    match = next((x for x in res.get("searchResult3", {}).get("artist", []) if str(x.get("name") or "").casefold() == wanted), None)
    if match and NAVI_ID_RE.match(str(match.get("id"))):
        try:
            info = (await navi_json(client, cfg, "getArtistInfo2.view", id=match["id"], count=0)).get("artistInfo2", {})
            for key in ("largeImageUrl", "mediumImageUrl"):
                url = info.get(key)
                if url and LASTFM_PLACEHOLDER not in str(url):
                    path = await save_image(client, url, directory, name)
                    if path:
                        return path
        except COVER_ERRORS:
            pass
        try:
            albums = (await navi_json(client, cfg, "getArtist.view", id=match["id"])).get("artist", {}).get("album", [])
            for album in sorted(albums, key=lambda x: -(x.get("playCount") or 0))[:2]:
                path = await navi_cover(client, cfg, album.get("coverArt") or album.get("id"), directory, name)
                if path:
                    return path
        except COVER_ERRORS:
            pass
    res = await navi_json(client, cfg, "search3", query=artist, songCount=30, artistCount=0, albumCount=0)
    songs = [x for x in res.get("searchResult3", {}).get("song", []) if str(x.get("artist") or "").casefold() == wanted]
    for song in sorted(songs, key=lambda x: -(x.get("playCount") or 0))[:2]:
        path = await navi_cover(client, cfg, song.get("coverArt") or song.get("albumId"), directory, name)
        if path:
            return path
    return None


def local_tz(name: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone name: fall back to the hub's default
        return ZoneInfo("Europe/Berlin")


# --- Jellyfin (Playback Reporting) -----------------------------------------------------------------------------

def _hex_id(value: Any) -> Optional[str]:
    text = str(value or "").replace("-", "").lower()
    return text if re.fullmatch(r"[0-9a-f]{32}", text) else None


async def jellyfin_user_id(client: httpx.AsyncClient, cfg: Config) -> Optional[str]:
    """The Jellyfin user id of the configured user name (mapping by name, case-insensitive)."""
    headers = {"Authorization": f'MediaBrowser Token="{cfg.jellyfin_token}"'}
    users = await get_json(client, f"{cfg.jellyfin_url}/Users", headers=headers)
    wanted = cfg.jellyfin_user.strip().lower()
    for user in users:
        if str(user.get("Name", "")).strip().lower() == wanted:
            return _hex_id(user.get("Id"))
    return None


async def jellyfin_poster(client, cfg: Config, img: Path, kind: str, item_id: Optional[str], name: str,
                          year: Optional[int]) -> Optional[str]:
    """Primary image of the item; if it is gone (Maintainerr deletes what was watched) search the library by name,
    and as the last resort ask Jellyfin's remote search (TMDb and others) for a picture and download that."""
    base = cfg.jellyfin_url
    headers = {"Authorization": f'MediaBrowser Token="{cfg.jellyfin_token}"'}
    file_name = f"jf-{kind.lower()}-{slug(name)}"
    tried = set()
    if item_id and ID_RE.match(str(item_id)):
        tried.add(item_id)
        path = await save_image(client, f"{base}/Items/{item_id}/Images/Primary?maxHeight=600&quality=85", img, file_name, headers)
        if path:
            return path
    try:
        params = {"searchTerm": name, "IncludeItemTypes": kind, "Recursive": "true", "Limit": 5}
        found = (await get_json(client, f"{base}/Items", headers=headers, params={**params, **({"Years": year} if year else {})}))["Items"]
        if not found and year:
            found = (await get_json(client, f"{base}/Items", headers=headers, params=params))["Items"]
    except COVER_ERRORS:
        found = []
    found = [x for x in found if ID_RE.match(str(x.get("Id"))) and x["Id"] not in tried
             and ("ImageTags" not in x or (x["ImageTags"] or {}).get("Primary"))]
    found.sort(key=lambda x: str(x.get("Name") or "").casefold() != name.casefold())
    for item in found[:2]:
        path = await save_image(client, f"{base}/Items/{item['Id']}/Images/Primary?maxHeight=600&quality=85", img, file_name, headers)
        if path:
            return path
    try:
        info = {"Name": name, **({"Year": year} if year else {})}
        results = await get_json(client, f"{base}/Items/RemoteSearch/{kind}", headers=headers,
                                 body={"SearchInfo": info, "IncludeDisabledProviders": False})
    except COVER_ERRORS:
        return None
    urls = [re.sub(r"(image\.tmdb\.org/t/p/)original/", r"\1w500/", str(r["ImageUrl"])) for r in results[:10]
            if isinstance(r, dict) and _safe_feed_url(r.get("ImageUrl")) and "/missing/" not in str(r["ImageUrl"])]
    urls.sort(key=lambda u: "image.tmdb.org" not in u)  # TMDb first, the order stays otherwise
    for url in urls[:3]:
        path = await save_image(client, url, img, file_name)  # a third-party image host: never send the Jellyfin key
        if path:
            return path
    return None


async def collect_jellyfin(client, cfg: Config, start, end, img: Path, covers: Optional["Covers"] = None) -> Dict[str, Any]:
    covers = covers or Covers()
    base = cfg.jellyfin_url
    headers = {"Authorization": f'MediaBrowser Token="{cfg.jellyfin_token}"'}
    user_id = await jellyfin_user_id(client, cfg)
    if not user_id:
        raise LookupError("Jellyfin user not found")
    # Only this person's plays: the id is validated as 32 hex digits, so it can go into the query as is.
    query = ("SELECT ItemId, ItemType, ItemName, PlayDuration, DateCreated FROM PlaybackActivity "
             f"WHERE DateCreated >= '{start:%Y-%m-%d}' AND DateCreated < '{end:%Y-%m-%d}' "
             f"AND lower(replace(UserId, '-', '')) = '{user_id}' AND ItemType IN ('Episode','Movie')")
    rows = (await get_json(client, f"{base}/user_usage_stats/submit_custom_query", headers=headers,
                           body={"CustomQueryString": query, "ReplaceUserId": False}))["results"]

    per_item = collections.defaultdict(lambda: {"secs": 0, "type": "", "name": ""})
    per_day: collections.Counter = collections.Counter()
    latest = None
    for item_id, typ, name, secs, created in rows:
        secs = int(secs)
        when = dt.datetime.fromisoformat(str(created)[:19])  # Playback Reporting stores local time
        item = per_item[item_id]
        item["secs"] += secs
        item["type"], item["name"] = typ, name
        per_day[when.date()] += secs
        if secs > 600 and 2 <= when.hour < 6 and (latest is None or when.hour > latest[0].hour):
            latest = (when, name)

    # Match series: through the Jellyfin items (as this user), else from the name ("Series - s01e02 - Title")
    meta: Dict[str, Any] = {}
    ids = [i for i in per_item if ID_RE.match(str(i))]
    for i in range(0, len(ids), 80):
        try:
            res = await get_json(client, f"{base}/Users/{user_id}/Items", headers=headers,
                                 params={"Ids": ",".join(ids[i:i + 80]), "Fields": "ProductionYear"})
            meta.update({x["Id"]: x for x in res["Items"]})
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass

    series = collections.defaultdict(lambda: {"secs": 0, "episodes": 0, "id": None, "seasons": set()})
    movies = []
    for item_id, item in per_item.items():
        m = meta.get(item_id, {})
        if item["type"] == "Episode":
            name = m.get("SeriesName") or item["name"].split(" - s")[0]
            se = series[name]
            se["secs"] += item["secs"]
            se["id"] = se["id"] or m.get("SeriesId")
            if item["secs"] >= 300:
                se["episodes"] += 1
            season = m.get("ParentIndexNumber") or (re.search(r" - s(\d+)e", item["name"]) or [None, None])[1]
            if season:
                se["seasons"].add(int(season))
        elif item["secs"] >= 600:
            movies.append({"name": m.get("Name") or item["name"], "id": item_id if m else None,
                           "year": m.get("ProductionYear"), "secs": item["secs"]})

    finished = []
    for name, v in sorted(series.items(), key=lambda kv: -kv[1]["secs"])[:8]:
        if not v["id"] or not ID_RE.match(str(v["id"])):
            continue
        try:
            data = await get_json(client, f"{base}/Users/{user_id}/Items/{v['id']}", headers=headers)
            user_data = data.get("UserData") or {}
            if user_data.get("Played") or user_data.get("UnplayedItemCount") == 0:
                finished.append(name)
        except (httpx.HTTPError, ValueError, AttributeError):
            pass

    top_series = sorted(series.items(), key=lambda kv: -kv[1]["secs"])
    series_out = [{"name": n, "hours": round(v["secs"] / 3600, 1), "episodes": v["episodes"],
                   "seasons": sorted(v["seasons"]), "poster": None} for n, v in top_series[:5]]
    movies.sort(key=lambda x: -x["secs"])
    movies_out = [{"name": x["name"], "year": x["year"], "hours": round(x["secs"] / 3600, 1), "poster": None} for x in movies[:8]]
    jobs = [(out, "poster", (lambda n=n, v=v: jellyfin_poster(client, cfg, img, "Series", v["id"], n, None))) for out, (n, v) in zip(series_out, top_series)]
    jobs += [(out, "poster", (lambda x=x: jellyfin_poster(client, cfg, img, "Movie", x["id"], x["name"], x["year"]))) for out, x in zip(movies_out, movies)]
    await covers.fill(jobs)

    best_day = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
    best_day_titles = []
    if best_day:
        names: collections.Counter = collections.Counter()
        for item_id, typ, name, secs, created in rows:
            if str(created)[:10] == best_day[0].isoformat():
                names[name.split(" - s")[0]] += int(secs)
        best_day_titles = [n for n, _ in names.most_common(2)]
    ep_secs = sum(v["secs"] for v in series.values())
    mv_secs = sum(x["secs"] for x in movies)
    return {
        "series_hours": round(ep_secs / 3600, 1),
        "movie_hours": round(mv_secs / 3600, 1),
        "episodes": sum(v["episodes"] for v in series.values()),
        "movies_count": len(movies),
        "series": series_out,
        "series_count": len(series),
        "movies": movies_out,
        "best_day": {"date": best_day[0].isoformat(), "hours": round(best_day[1] / 3600, 1),
                     "titles": best_day_titles} if best_day else None,
        "night_owl": {"time": latest[0].strftime("%H:%M"), "date": latest[0].date().isoformat(),
                      "title": latest[1]} if latest else None,
        "active_days": len(per_day),
        "days": {d.isoformat(): round(v / 3600, 2) for d, v in sorted(per_day.items())},
        "finished_series": finished,
    }


# --- ListenBrainz ----------------------------------------------------------------------------------------------

async def collect_music(client, cfg: Config, start, end, img: Path, tz, covers: Optional[Covers] = None) -> Dict[str, Any]:
    covers = covers or Covers()
    user = quote(cfg.listenbrainz_user.strip(), safe="")
    headers = {"Authorization": "Token " + cfg.listenbrainz_token} if cfg.listenbrainz_token else {}
    lo, hi = int(start.timestamp()), int(end.timestamp())
    listens: List[dict] = []
    ts = hi
    for _ in range(40):  # at most 40,000 listens a month
        payload = (await get_json(client, f"{LISTENBRAINZ}/1/user/{user}/listens", headers=headers,
                                  params={"max_ts": ts, "count": 1000}))["payload"]["listens"]
        batch = [x for x in payload if x["listened_at"] >= lo]
        listens += batch
        if len(batch) < 1000 or len(batch) < len(payload):
            break
        ts = batch[-1]["listened_at"]

    artists: collections.Counter = collections.Counter()
    tracks: collections.Counter = collections.Counter()
    releases: collections.Counter = collections.Counter()
    release_of: Dict[tuple, str] = {}
    ms_total, days = 0, set()
    for x in listens:
        md = x["track_metadata"]
        mp = md.get("mbid_mapping") or {}
        ai = md.get("additional_info") or {}
        ms_total += ai.get("duration_ms") or int((ai.get("duration") or 0) * 1000) or 180000
        artist = md.get("artist_name") or "?"
        artists[artist] += 1
        tracks[(md.get("track_name") or "?", artist)] += 1
        releases[(md.get("release_name") or "?", artist)] += 1
        rel = mp.get("caa_release_mbid") or mp.get("release_mbid") or ai.get("release_mbid")
        for key in (("t", md.get("track_name"), artist), ("a", artist), ("r", md.get("release_name"), artist)):
            if rel and key not in release_of:
                release_of[key] = rel
        days.add(dt.datetime.fromtimestamp(x["listened_at"], tz).date())

    async def caa(key, file_name):
        rel = release_of.get(key)
        if not rel or not ID_RE.match(str(rel)):
            return None
        return await save_image(client, f"{COVERART}/release/{rel}/front-250", img, file_name)

    async def track_cover(title, artist):
        """Cover Art Archive, else the cover of the matching song in Navidrome."""
        file_name = f"mu-{slug(f'{title}-{artist}')}"
        path = await caa(("t", title, artist), file_name)
        if path or not cfg.soundtrack:
            return path
        try:
            return await navi_song_cover(client, cfg, img, title, artist, file_name)
        except COVER_ERRORS:
            return None

    async def artist_cover(artist):
        """Navidrome (artist picture, then its most played album or song), last the Cover Art Archive release of the month."""
        file_name = f"ar-{slug(artist)}"
        if cfg.soundtrack:
            try:
                path = await navi_artist_cover(client, cfg, img, artist, file_name)
            except COVER_ERRORS:
                path = None
            if path:
                return path
        return await caa(("a", artist), file_name)

    new_artists = []
    try:
        stats = (await get_json(client, f"{LISTENBRAINZ}/1/stats/user/{user}/artists", headers=headers,
                                params={"range": "all_time", "count": 1000}))["payload"]["artists"]
        alltime = {x["artist_name"]: x["listen_count"] for x in stats}
        for a, n in artists.most_common(40):
            if n >= 3 and alltime.get(a, 0) <= n + 1:
                new_artists.append({"name": a, "plays": n})
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        pass
    top_artists = [{"name": a, "plays": n, "cover": None} for a, n in artists.most_common(5)]
    top_tracks = [{"name": t, "artist": a, "plays": n, "cover": None} for (t, a), n in tracks.most_common(5)]
    await covers.fill([(x, "cover", (lambda x=x: track_cover(x["name"], x["artist"]))) for x in top_tracks]
                      + [(x, "cover", (lambda x=x: artist_cover(x["name"]))) for x in top_artists])
    return {
        "new_artists": new_artists[:5],
        "days": sorted(d.isoformat() for d in days),
        "plays": len(listens),
        "minutes": round(ms_total / 60000),
        "hours": round(ms_total / 3600000, 1),
        "artists_count": len(artists),
        "tracks_count": len(tracks),
        "active_days": len(days),
        "top_artists": top_artists,
        "top_tracks": top_tracks,
        "top_releases": [{"name": r, "artist": a, "plays": n} for (r, a), n in releases.most_common(3)],
    }


# --- Audiobookshelf --------------------------------------------------------------------------------------------

async def collect_audiobookshelf(client, cfg: Config, start, end, img: Path, tz, covers: Optional[Covers] = None) -> Dict[str, Any]:
    covers = covers or Covers()
    headers = {"Authorization": "Bearer " + cfg.abs_token}
    base = cfg.abs_url
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    sessions: List[dict] = []
    for page in range(100):
        res = await get_json(client, f"{base}/api/me/listening-sessions", headers=headers,
                             params={"itemsPerPage": 200, "page": page})
        sessions += res["sessions"]
        if (page + 1) * 200 >= res["total"] or not res["sessions"]:
            break
    books = collections.defaultdict(lambda: {"secs": 0, "title": "", "author": ""})
    days = set()
    for x in sessions:
        if x.get("mediaType") != "book" or not lo <= x["startedAt"] < hi:
            continue
        b = books[x["libraryItemId"]]
        b["secs"] += x["timeListening"] or 0
        days.add(dt.datetime.fromtimestamp(x["startedAt"] / 1000, tz).date().isoformat())
        title = x.get("displayTitle") or ""
        b["title"] = title.replace(".", " ").strip() if re.search(r"\w\.\w", title) else title
        b["author"] = x.get("displayAuthor") or ""
    me = await get_json(client, f"{base}/api/me", headers=headers)
    finished = [p["libraryItemId"] for p in me.get("mediaProgress", [])
                if p.get("isFinished") and p.get("finishedAt") and lo <= p["finishedAt"] < hi]
    top = sorted(books.items(), key=lambda kv: -kv[1]["secs"])
    out = []
    for item_id, b in top[:5]:
        if b["secs"] < 60:
            continue
        out.append({"title": b["title"], "author": b["author"], "hours": round(b["secs"] / 3600, 1),
                    "finished": item_id in finished, "cover": None, "_id": item_id})

    async def book_cover(item_id, title):
        if not ID_RE.match(str(item_id)):
            return None
        return await save_image(client, f"{base}/api/items/{item_id}/cover?width=400", img, f"ab-{slug(title)}", headers)

    await covers.fill([(x, "cover", (lambda x=x: book_cover(x["_id"], x["title"]))) for x in out])
    for x in out:
        x.pop("_id")
    total = sum(b["secs"] for b in books.values())
    return {"hours": round(total / 3600, 1), "minutes": round(total / 60), "books": out, "finished": len(finished),
            "days": sorted(days), "titles_count": len([b for b in books.values() if b["secs"] >= 60])}


# --- Podcasts (AntennaPod through Nextcloud GPodder Sync) -------------------------------------------------------

def _safe_feed_url(url: Any) -> bool:
    parsed = urlparse(str(url or ""))
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


async def collect_podcasts(client, cfg: Config, start, end, img: Path, covers: Optional[Covers] = None) -> Dict[str, Any]:
    covers = covers or Covers()
    auth = base64.b64encode(f"{cfg.nextcloud_user}:{cfg.nextcloud_password}".encode()).decode()
    res = await get_json(client, f"{cfg.nextcloud_url}/index.php/apps/gpoddersync/episode_action",
                         headers={"Authorization": "Basic " + auth, "OCS-APIRequest": "true"},
                         params={"since": int(start.timestamp())})
    pods = collections.defaultdict(lambda: {"secs": 0, "episodes": set()})
    for a in res.get("actions", []):
        if (a.get("action") or "").upper() != "PLAY":
            continue
        when = dt.datetime.fromisoformat(a["timestamp"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if not start <= when < end:
            continue
        played = max(0, (a.get("position") or 0) - (a.get("started") or 0))
        p = pods[a["podcast"]]
        p["secs"] += played
        p["episodes"].add(a.get("episode"))
    out = []
    for feed, p in sorted(pods.items(), key=lambda kv: -kv[1]["secs"])[:5]:
        title, image = feed if not _safe_feed_url(feed) else urlparse(feed).hostname, None
        if _safe_feed_url(feed):
            try:
                feed_response = await client.get(feed, timeout=20.0)
                feed_response.raise_for_status()
                raw = feed_response.content
                if len(raw) <= MAX_FEED_BYTES and b"<!ENTITY" not in raw and b"<!DOCTYPE" not in raw.upper():
                    channel = ET.fromstring(raw).find("channel")
                    title = channel.findtext("title") or title
                    node = channel.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
                    image = node.get("href") if node is not None else channel.findtext("image/url")
                else:
                    # Long-running podcasts have feeds of several MB: read title and image from the channel head only
                    head_title, head_image = _channel_head(raw)
                    title, image = head_title or title, head_image
            except (httpx.HTTPError, ET.ParseError, AttributeError, ValueError):
                pass
        out.append({"title": title, "hours": round(p["secs"] / 3600, 1), "episodes": len(p["episodes"]), "cover": None,
                    "_image": image})
    await covers.fill([(x, "cover", (lambda x=x: save_image(client, x["_image"], img, f"pc-{slug(x['title'])}")))
                       for x in out if x["_image"] and _safe_feed_url(x["_image"])])
    for x in out:
        x.pop("_image")
    total = sum(p["secs"] for p in pods.values())
    return {"connected": True, "hours": round(total / 3600, 1), "minutes": round(total / 60),
            "episodes": sum(len(p["episodes"]) for p in pods.values()), "podcasts": out}


def _channel_head(raw: bytes):
    """Title and cover from the part of a feed before the first <item>, without parsing the XML."""
    head = raw[:262144].split(b"<item", 1)[0].decode("utf-8", "replace")
    title = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", head, re.S)
    image = (re.search(r"<itunes:image[^>]*href=[\"']([^\"']+)", head)
             or re.search(r"<image>.*?<url>\s*([^<\s]+)\s*</url>", head, re.S))
    text = html.unescape(title.group(1)).strip()[:200] if title else None
    return text or None, image.group(1) if image else None


# --- Immich ----------------------------------------------------------------------------------------------------

async def collect_immich(client, cfg: Config, start, end, img: Path) -> Dict[str, Any]:
    headers = {"x-api-key": cfg.immich_key, "Accept": "application/json"}
    base = cfg.immich_url + "/api"
    items: List[dict] = []
    page: Optional[int] = 1
    while page and page <= 60:
        res = await get_json(client, base + "/search/metadata", headers=headers, body={
            "takenAfter": start.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "takenBefore": end.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "size": 1000, "page": page, "withExif": True})
        items += res["assets"]["items"]
        nxt = res["assets"].get("nextPage")
        page = int(nxt) if nxt else None
    photos = [a for a in items if a["type"] == "IMAGE"]
    by_day = collections.defaultdict(list)
    for a in photos:
        by_day[a["localDateTime"][:10]].append(a)
    places: collections.Counter = collections.Counter((a.get("exifInfo") or {}).get("city") for a in items)
    places.pop(None, None)
    geo = collections.defaultdict(list)
    for a in items:
        ex = a.get("exifInfo") or {}
        if ex.get("city") and ex.get("latitude") is not None and ex.get("longitude") is not None:
            geo[ex["city"]].append((ex["latitude"], ex["longitude"], ex.get("country")))
    countries: collections.Counter = collections.Counter((a.get("exifInfo") or {}).get("country") for a in items)
    countries.pop(None, None)

    # Highlights: favourites first, else one camera picture from each of the busiest days
    picks = [a for a in photos if a.get("isFavorite")][:6]
    for day, assets in sorted(by_day.items(), key=lambda kv: -len(kv[1])):
        if len(picks) >= 6:
            break
        cams = [a for a in assets if (a.get("exifInfo") or {}).get("make")] or assets
        cand = cams[len(cams) // 2]
        if cand not in picks:
            picks.append(cand)
    picks.sort(key=lambda a: a["localDateTime"])
    highlights = []
    for a in picks:
        if not ID_RE.match(str(a["id"])):
            continue
        p = await save_image(client, f'{base}/assets/{a["id"]}/thumbnail?size=preview', img, f'ph-{str(a["id"])[:8]}', headers)
        if p:
            highlights.append({"img": p, "date": a["localDateTime"][:10], "city": (a.get("exifInfo") or {}).get("city")})
    best = max(by_day.items(), key=lambda kv: len(kv[1])) if by_day else None
    return {
        "photos": len(photos), "videos": len(items) - len(photos), "total": len(items),
        "best_day": {"date": best[0], "count": len(best[1])} if best else None,
        "places": [{"name": n, "count": c} for n, c in places.most_common(5)],
        "place_geo": [{"name": n, "count": len(v), "lat": sum(x[0] for x in v) / len(v),
                       "lon": sum(x[1] for x in v) / len(v), "country": v[0][2]} for n, v in geo.items()],
        "countries": [n for n, _ in countries.most_common()],
        "days_with_photos": len({a["localDateTime"][:10] for a in items}),
        "highlights": highlights,
    }


# --- Games (ES-DE play statistics) -----------------------------------------------------------------------------
# ES-DE keeps one gamelist per system with the *cumulative* play time and launch count of every game. One file per
# device (``<games_dir>/<device>.json``) is mounted read-only into the hub. The numbers of a month are therefore a
# difference between two states: see ``games_delta``.

def parse_devices(value: Any) -> Tuple[str, ...]:
    """``"pc, deck"`` -> ``("pc", "deck")``: only plain file stems, no duplicates."""
    out: List[str] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if DEVICE_RE.match(part) and part not in out:
            out.append(part)
    return tuple(out)


def system_label(system: str) -> str:
    return GAME_SYSTEMS.get(system.lower(), system.upper())


def _strip_ext(name: str) -> str:
    """Drops a file extension (letters and digits, at most 7: ``.iso``, ``.xci``, ``.desktop``), not ``Vol.2``."""
    return re.sub(r"\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,7}$", "", name)


def clean_game_name(rel: str) -> str:
    """Display name of a game from its path inside the system folder: the folder name for folder games, else the
    file name; without extension and without tags like ``(Europe)``, ``(En,Fr,De)``, ``(Rev 2)`` or ``[!]``."""
    parts = [x for x in str(rel).split("/") if x and x != "."]
    if not parts:
        return ""
    name = parts[-2] if len(parts) > 1 else parts[0]
    if len(parts) == 1:
        name = _strip_ext(name)
    name = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -")
    match = re.match(r"^(.+?), (The|A|An|Der|Die|Das) - (.+)$", name) or re.match(r"^(.+?), (The|A|An|Der|Die|Das)$", name)
    if match:  # "Simpsons, The - Hit & Run" -> "The Simpsons - Hit & Run"
        name = f"{match.group(2)} {match.group(1)}" + (f" - {match.group(3)}" if match.lastindex == 3 else "")
    return name or parts[-1]


EDITION_RE = re.compile(r"\s+(?:remastered|enhanced|definitive|complete|goty|game of the year|edition|deluxe)\b.*$",
                        re.IGNORECASE)


def steam_terms(term: str) -> List[str]:
    """Search terms from exact to loose: the store search finds nothing for e.g. "Wild Hunt — Remastered", so also
    try the part before a dash and the name without an edition suffix."""
    out: List[str] = []
    head = re.split(r"\s+[—–-]\s+", term)[0]
    for candidate in (term, head, EDITION_RE.sub("", head)):
        candidate = candidate.strip(" —–-")
        if candidate and candidate not in out:
            out.append(candidate)
    return out


async def steam_cover(client: httpx.AsyncClient, img: Path, term: str, name: str) -> Optional[str]:
    """Box art from the public Steam store (no key): the first search hit's portrait, else its header picture."""
    app_id = ""
    for candidate in steam_terms(term):
        res = await get_json(client, STEAM_SEARCH, params={"term": candidate, "l": "german", "cc": "DE"})
        items = res.get("items") or []
        app_id = str((items[0] if items else {}).get("id") or "")
        if app_id.isdigit():
            break
    if not app_id.isdigit():
        return None
    for file in ("library_600x900.jpg", "header.jpg"):
        path = await save_image(client, f"{STEAM_IMAGES}/{app_id}/{file}", img, name)
        if path:
            return path
    return None


class Romm:
    """The games library in RomM (API 5.x, bearer token): ES-DE system -> platform by folder name (``fs_slug``), then
    the ROM by file name. Platforms and look-ups are cached for one build. Every failure (not configured, not
    reachable, login rejected, odd answers) just gives None, so the story falls back to Steam or a placeholder."""

    def __init__(self, client: httpx.AsyncClient, cfg: Config) -> None:
        self.client, self.base = client, cfg.romm_url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + cfg.romm_token}
        self.enabled = bool(self.base and cfg.romm_token)
        self._platforms: Optional[Dict[str, int]] = None
        self._found: Dict[Tuple[str, str], Optional[dict]] = {}

    async def platforms(self) -> Dict[str, int]:
        if self._platforms is None:
            self._platforms = {}
            if self.enabled:
                try:
                    items = await get_json(self.client, f"{self.base}/api/platforms", headers=self.headers)
                    self._platforms = {str(x["fs_slug"]).lower(): int(x["id"]) for x in items
                                       if isinstance(x, dict) and x.get("fs_slug") and str(x.get("id", "")).isdigit()}
                except COVER_ERRORS:
                    pass
        return self._platforms

    async def find(self, system: str, rel: str) -> Optional[dict]:
        """The RomM entry of ``<system>/<rel>``: ``fs_name`` is the file name, for folder games the folder name."""
        key = (system, rel)
        if key in self._found:
            return self._found[key]
        self._found[key] = None
        platform = (await self.platforms()).get(system.lower())
        parts = [x for x in str(rel).split("/") if x and x != "."]
        if platform is None or not parts:
            return None
        fs_name = parts[-2] if len(parts) > 1 else parts[0]
        stem = _strip_ext(fs_name)
        try:
            page = await get_json(self.client, f"{self.base}/api/roms", headers=self.headers,
                                  params={"platform_ids": platform, "search_term": stem, "limit": 50})
            items = [x for x in (page.get("items") or []) if isinstance(x, dict)]
        except COVER_ERRORS:
            return None
        loose = {stem.casefold(), clean_game_name(rel).casefold()}
        hit = (next((x for x in items if x.get("fs_name") == fs_name), None)
               or next((x for x in items if str(x.get("fs_name_no_ext") or "").casefold() in loose
                        or str(x.get("fs_name_no_tags") or "").casefold() in loose), None))
        self._found[key] = hit
        return hit

    async def cover(self, hit: dict, img: Path, name: str) -> Optional[str]:
        """The big cover, else the small one (both served by RomM, with the token), else the metadata picture's own
        address (a third-party host: no token)."""
        for field in ("path_cover_large", "path_cover_small"):
            path = hit.get(field)
            if isinstance(path, str) and path.startswith("/") and not path.startswith("//"):
                found = await save_image(self.client, self.base + path, img, name, self.headers)
                if found:
                    return found
        if _safe_feed_url(hit.get("url_cover")):
            return await save_image(self.client, hit["url_cover"], img, name)
        return None


def read_games(games_dir: str, devices) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{device: {"<system>/<relative path>": {"playtime", "playcount", "lastplayed"}}}`` of every readable device
    file. Anything that is not a ``game:./`` entry, and missing or odd fields, is skipped without a fuss."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for device in devices:
        if not DEVICE_RE.match(device):
            continue
        try:
            raw = json.loads((Path(games_dir) / f"{device}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        games: Dict[str, Dict[str, Any]] = {}
        for system, entries in raw.items():
            if not isinstance(entries, dict) or not isinstance(system, str):
                continue
            for key, entry in entries.items():
                if not (isinstance(key, str) and key.startswith(GAME_PREFIX) and isinstance(entry, dict)):
                    continue
                last = (entry.get("flags") or {}).get("lastplayed") if isinstance(entry.get("flags"), dict) else None
                last = last[0] if isinstance(last, list) and last else last
                games[f"{system}/{key[len(GAME_PREFIX):]}"] = {
                    "playtime": _whole(entry.get("playtime")), "playcount": _whole(entry.get("playcount")),
                    "lastplayed": last if isinstance(last, str) else None}
        out[device] = games
    return out


def _whole(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _played_between(stamp: Any, start, end) -> bool:
    """Is the ES-DE time stamp (``20261025T114002``, local time) in [start, end)?"""
    try:
        when = dt.datetime.strptime(str(stamp), "%Y%m%dT%H%M%S")
    except ValueError:
        return False
    return start.replace(tzinfo=None) <= when < end.replace(tzinfo=None)


def games_delta(base: Optional[Dict[str, Any]], final: Dict[str, Any], start, end) -> Dict[str, Dict[str, Any]]:
    """Per game what was played in [start, end): the end state minus the baseline, summed over all devices.

    ``base`` and ``final`` are ``{device: {game key: record}}`` (``base`` None = no snapshot at all). Devices are
    compared one by one, so a device that appears later does not count its whole history as one month. A game is in
    when its playtime grew; for a device without a baseline (nothing to subtract) it is in when it was last played
    within the month. That first month is best effort: the cumulative numbers also hold earlier play, and the end
    state may already hold play from the next month (the first snapshot is taken after the fact)."""
    out: Dict[str, Dict[str, Any]] = {}
    for device, games in final.items():
        before = (base or {}).get(device)
        for key, now in games.items():
            old = (before or {}).get(key)
            seconds = now["playtime"] - (old["playtime"] if old else 0)
            launches = max(0, now["playcount"] - (old["playcount"] if old else 0))
            if before is not None:
                if seconds <= 0:
                    continue
            elif not _played_between(now.get("lastplayed"), start, end) or seconds <= 0:
                continue
            item = out.setdefault(key, {"seconds": 0, "sessions": 0, "new": True})
            item["seconds"] += seconds
            item["sessions"] += launches
            item["new"] = item["new"] and old is None
    return out


async def collect_games(client, cfg: Config, start, end, img: Path, base: Optional[Dict[str, Any]],
                        final: Optional[Dict[str, Any]] = None, covers: Optional[Covers] = None) -> Dict[str, Any]:
    """The games of the month. ``final`` is the state at the end of the month (the first snapshot of the next month),
    else the current device files."""
    covers = covers or Covers()
    if final is None:
        final = read_games(cfg.games_dir, cfg.games_devices)
    if not final:
        raise LookupError("No game statistics readable")
    played = games_delta(base, final, start, end)
    rows = []
    for key, v in played.items():
        system, rel = key.split("/", 1)
        rows.append({"name": clean_game_name(rel), "system": system_label(system), "minutes": round(v["seconds"] / 60),
                     "sessions": v["sessions"], "cover": None, "new": v["new"], "_key": key})
    rows = [x for x in rows if x["minutes"] > 0 and x["name"]]
    rows.sort(key=lambda x: (-x["minutes"], x["name"].casefold()))
    top = rows[:8]
    romm = Romm(client, cfg)

    async def game_cover(row):
        """RomM (title and cover) for everything but Steam/PC games, then the Steam store, else None (placeholder)."""
        system, rel = row["_key"].split("/", 1)
        hit = None
        if system not in ("steam", "windows"):
            hit = await romm.find(system, rel)
            if hit and str(hit.get("name") or "").strip():
                row["name"] = str(hit["name"]).strip()
        file_name = f"gm-{slug(row['name'])}"
        if hit:
            path = await romm.cover(hit, img, file_name)
            if path:
                return path
        try:
            return await steam_cover(client, img, row["name"], file_name)
        except COVER_ERRORS:
            return None

    await covers.fill([(x, "cover", (lambda x=x: game_cover(x))) for x in top])
    for x in rows:
        x.pop("_key")
    systems: collections.Counter = collections.Counter()
    for x in rows:
        systems[x["system"]] += x["minutes"]
    return {"hours": round(sum(x["minutes"] for x in rows) / 60, 1), "minutes": sum(x["minutes"] for x in rows),
            "sessions": sum(x["sessions"] for x in rows), "games_count": len(rows), "systems_count": len(systems),
            "systems": [{"name": n, "minutes": m} for n, m in systems.most_common()],
            "top_system": systems.most_common(1)[0][0] if systems else None, "games": top}


# --- RetroAchievements (Web API, the person's own user name and API key) ----------------------------------------
# The key travels as a query parameter (that is how the API works), so it only ever goes through ``params`` and no
# error text is kept: ``describe_error`` records the class of a failure, never a URL.

def _ra_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _ra_media(path: Any) -> Optional[str]:
    """Address of a picture on the RetroAchievements media host from the API's path (``/Badge/123.png``)."""
    text = str(path or "").strip()
    if re.fullmatch(r"/[A-Za-z0-9_./-]{1,120}", text) and ".." not in text and not text.startswith("//"):
        return RA_MEDIA + text
    return None


def _ra_time(value: Any) -> Optional[dt.datetime]:
    """The API's timestamps (``2026-10-05 12:34:56`` or ISO with an offset) are UTC; None if unreadable."""
    try:
        when = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


async def ra_json(client, cfg: Config, endpoint: str, **extra) -> Any:
    return await get_json(client, f"{RA_API}/{endpoint}", params={"u": cfg.ra_username, "y": cfg.ra_api_key, **extra})


async def ra_awards(client, cfg: Config, start, end) -> List[dict]:
    """Games mastered, completed or beaten in [start, end). Soft: any failure gives no awards, the achievements stay."""
    try:
        raw = await ra_json(client, cfg, "API_GetUserAwards.php")
        items = raw.get("VisibleUserAwards") if isinstance(raw, dict) else raw
        out, seen = [], set()
        for a in items if isinstance(items, list) else []:
            if not isinstance(a, dict):
                continue
            kind = RA_AWARDS.get((str(a.get("AwardType") or ""), 1 if _ra_int(a.get("AwardDataExtra")) else 0))
            when = _ra_time(a.get("AwardedAt"))
            key = (kind, a.get("AwardData"))
            if kind is None or key in seen or when is None or not start <= when < end or not str(a.get("Title") or "").strip():
                continue
            seen.add(key)
            out.append({"title": str(a["Title"]).strip(), "console": str(a.get("ConsoleName") or "").strip(), "kind": kind,
                        "hardcore": kind in ("mastered", "beaten"), "icon": None,
                        "_icon": _ra_media(a.get("ImageIcon")), "_id": _ra_int(a.get("AwardData"))})
        return out
    except COVER_ERRORS:
        return []


async def collect_retroachievements(client, cfg: Config, start, end, img: Path,
                                    covers: Optional[Covers] = None) -> Dict[str, Any]:
    """The achievements earned in [start, end) (both in the hub's timezone). An achievement earned in both modes
    counts once, as hardcore. The badges of the rarest ones and the icons of the top games are stored as files."""
    covers = covers or Covers()
    lo, hi = int(start.timestamp()), int(end.timestamp())
    raw = await ra_json(client, cfg, "API_GetAchievementsEarnedBetween.php", f=lo, t=hi)
    if not isinstance(raw, list):
        raise LookupError("Unexpected RetroAchievements answer")
    earned: Dict[int, dict] = {}
    for a in raw:
        if not isinstance(a, dict) or not _ra_int(a.get("AchievementID")) or not str(a.get("Title") or "").strip():
            continue
        when = _ra_time(a.get("Date"))
        if when is not None and not lo <= when.timestamp() < hi:
            continue
        key = _ra_int(a["AchievementID"])
        if key not in earned or (_ra_int(a.get("HardcoreMode")) and not _ra_int(earned[key].get("HardcoreMode"))):
            earned[key] = a
    games: Dict[int, dict] = {}
    rows = []
    for key, a in earned.items():
        points, true_points = _ra_int(a.get("Points")), _ra_int(a.get("TrueRatio"))
        badge = a.get("BadgeURL") or (f"/Badge/{a['BadgeName']}.png" if re.fullmatch(r"\w{1,20}", str(a.get("BadgeName") or "")) else None)
        game_id = _ra_int(a.get("GameID"))
        rows.append({"id": key, "title": str(a.get("Title") or "").strip(), "description": str(a.get("Description") or "").strip(),
                     "points": points, "true_points": true_points, "hardcore": bool(_ra_int(a.get("HardcoreMode"))),
                     "game": str(a.get("GameTitle") or "").strip(), "badge": None, "_badge": _ra_media(badge)})
        g = games.setdefault(game_id, {"name": str(a.get("GameTitle") or "").strip(), "console": str(a.get("ConsoleName") or "").strip(),
                                       "count": 0, "points": 0, "icon": None, "_icon": _ra_media(a.get("GameIcon")), "_id": game_id})
        g["count"] += 1
        g["points"] += points
    top_games = sorted((g for g in games.values() if g["name"]), key=lambda g: (-g["count"], -g["points"], g["name"].casefold()))[:RA_TOP_GAMES]
    # The rarest: the most "true" points per point (the weighting makes an achievement worth more the fewer have it)
    rarest = sorted((x for x in rows if x["points"] > 0), key=lambda x: (-x["true_points"] / x["points"], -x["points"], x["title"].casefold()))[:RA_RAREST]
    awards = (await ra_awards(client, cfg, start, end))[:RA_MAX_AWARDS] if rows else []

    async def pic(url, name):
        return await save_image(client, url, img, name) if url else None

    jobs = [(x, "badge", (lambda x=x: pic(x["_badge"], f"ra-b{x['id']}"))) for x in rarest] \
        + [(g, "icon", (lambda g=g: pic(g["_icon"], f"ra-g{g['_id']}"))) for g in top_games] \
        + [(a, "icon", (lambda a=a: pic(a["_icon"], f"ra-a{a['_id']}"))) for a in awards]
    await covers.fill(jobs)
    for x in rarest:
        x.pop("_badge")
    for g in top_games:
        g.pop("_icon"), g.pop("_id")
    for a in awards:
        a.pop("_icon"), a.pop("_id")
    hardcore = sum(1 for x in rows if x["hardcore"])
    return {"total": len(rows), "points": sum(x["points"] for x in rows), "true_points": sum(x["true_points"] for x in rows),
            "hardcore": hardcore, "softcore": len(rows) - hardcore, "games_count": len({x["game"] for x in rows}),
            "games": top_games, "rarest": [{k: v for k, v in x.items() if k != "id"} for x in rarest], "awards": awards}


# --- Soundtrack from Navidrome (Subsonic API) ------------------------------------------------------------------

async def collect_soundtrack(client, cfg: Config, music: Dict[str, Any], out: Path) -> List[Dict[str, Any]]:
    """Short MP3 clips (about one slide long) from Navidrome: the top tracks first, then more of the top artists'."""
    if not cfg.soundtrack:
        return []
    salt = secrets.token_hex(6)
    auth = {"u": cfg.navidrome_user, "t": hashlib.md5((cfg.navidrome_password + salt).encode()).hexdigest(),  # noqa: S324 - Subsonic's token scheme
            "s": salt, "v": "1.16.1", "c": "familienhub-recap", "f": "json"}

    def url(endpoint: str, **extra) -> str:
        return f"{cfg.navidrome_url}/rest/{endpoint}?" + urlencode({**auth, **extra})

    async def search(query: str, n: int) -> List[dict]:
        res = await get_json(client, url("search3", query=query, songCount=n, artistCount=0, albumCount=0))
        return res["subsonic-response"].get("searchResult3", {}).get("song", [])

    picked: List[dict] = []
    seen = set()
    for t in music.get("top_tracks", []):
        hit = next((x for x in await search(t["name"], 10) if x.get("artist", "").lower() == t["artist"].lower()), None)
        if hit and hit["id"] not in seen:
            picked.append(hit)
            seen.add(hit["id"])
    for a in music.get("top_artists", []):
        songs = [x for x in await search(a["name"], 30)
                 if x.get("artist", "").lower() == a["name"].lower() and x["id"] not in seen]
        songs.sort(key=lambda x: -(x.get("playCount") or 0))
        for x in songs[:3]:
            picked.append(x)
            seen.add(x["id"])
    # Too little listened this month (or not in Navidrome): fill up with the all-time favourites from ListenBrainz
    if len(picked) < CLIP_COUNT and cfg.listenbrainz_user:
        try:
            headers = {"Authorization": "Token " + cfg.listenbrainz_token} if cfg.listenbrainz_token else {}
            recs = (await get_json(client, f"{LISTENBRAINZ}/1/stats/user/{quote(cfg.listenbrainz_user, safe='')}/recordings",
                                   headers=headers, params={"range": "all_time", "count": 60}))["payload"]["recordings"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            recs = []
        for r in recs:
            if len(picked) >= CLIP_COUNT:
                break
            hit = next((x for x in await search(r["track_name"], 10)
                        if x.get("artist", "").lower() == (r.get("artist_name") or "").lower()), None)
            if hit and hit["id"] not in seen:
                picked.append(hit)
                seen.add(hit["id"])
    clips = []
    for i, x in enumerate(picked[:CLIP_COUNT]):
        duration = x.get("duration") or 180
        offset = int(duration * 0.35) if duration > 60 else 0  # roughly the chorus, not the intro
        limit = CLIP_SECONDS * 16000  # 128 kbit/s is about 16 kB per second
        audio = b""
        try:
            async with client.stream("GET", url("stream.view", id=x["id"], format="mp3", maxBitRate=128,
                                                timeOffset=offset), timeout=60.0) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    audio += chunk
                    if len(audio) >= limit:
                        break
        except httpx.HTTPError:
            continue
        audio = audio[:limit]
        if len(audio) < 20000:
            continue
        name = f"clip-{i:02d}.mp3"
        (out / name).write_bytes(audio)
        clips.append({"src": "img/" + name, "title": x.get("title"), "artist": x.get("artist")})
    return clips
