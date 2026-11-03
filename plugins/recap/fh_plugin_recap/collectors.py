"""Collectors: one coroutine per source. Each returns a plain dict for the snapshot and raises on failure; the
builder catches that per source, so one broken service never stops a recap.

All HTTP goes through ``httpx`` with timeouts. Nothing here logs a URL, a token or a response body.
"""

from __future__ import annotations

import base64
import collections
import datetime as dt
import hashlib
import html
import re
import secrets
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx

LISTENBRAINZ = "https://api.listenbrainz.org"
COVERART = "https://coverartarchive.org"
USER_AGENT = "familienhub-recap/1.0"
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_FEED_BYTES = 3 * 1024 * 1024
CLIP_SECONDS = 11
CLIP_COUNT = 10
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


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
    Only real images are kept (checked by content, not by what the server claims)."""
    try:
        response = await client.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    content = response.content
    ext = sniff_image(content)
    if ext is None or len(content) < 500 or len(content) > MAX_IMAGE_BYTES:
        return None
    filename = f"{name}.{ext}"
    (directory / filename).write_bytes(content)
    return f"img/{filename}"


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


async def collect_jellyfin(client, cfg: Config, start, end, img: Path) -> Dict[str, Any]:
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

    async def poster(item_id, name):
        if not item_id:
            try:
                found = (await get_json(client, f"{base}/Items", headers=headers, params={
                    "searchTerm": name, "IncludeItemTypes": "Series,Movie", "Recursive": "true", "Limit": 1}))["Items"]
                item_id = found[0]["Id"] if found else None
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
                item_id = None
        if not item_id or not ID_RE.match(str(item_id)):
            return None
        return await save_image(client, f"{base}/Items/{item_id}/Images/Primary?maxHeight=600&quality=85", img,
                                f"jf-{slug(name)}", headers)

    top_series = sorted(series.items(), key=lambda kv: -kv[1]["secs"])
    series_out = []
    for i, (n, v) in enumerate(top_series[:5]):
        series_out.append({"name": n, "hours": round(v["secs"] / 3600, 1), "episodes": v["episodes"],
                           "seasons": sorted(v["seasons"]), "poster": await poster(v["id"], n) if i < 3 else None})
    movies.sort(key=lambda x: -x["secs"])
    movies_out = [{"name": x["name"], "year": x["year"], "hours": round(x["secs"] / 3600, 1),
                   "poster": await poster(x["id"], x["name"])} for x in movies[:8]]

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

async def collect_music(client, cfg: Config, start, end, img: Path, tz) -> Dict[str, Any]:
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
    covers: Dict[tuple, str] = {}
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
            if rel and key not in covers:
                covers[key] = rel
        days.add(dt.datetime.fromtimestamp(x["listened_at"], tz).date())

    async def cover(key, name):
        rel = covers.get(key)
        if not rel or not ID_RE.match(str(rel)):
            return None
        return await save_image(client, f"{COVERART}/release/{rel}/front-250", img, f"mu-{slug(name)}")

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
    top_artists = []
    for i, (a, n) in enumerate(artists.most_common(5)):
        top_artists.append({"name": a, "plays": n, "cover": await cover(("a", a), a) if i == 0 else None})
    top_tracks = [{"name": t, "artist": a, "plays": n, "cover": await cover(("t", t, a), f"{t}-{a}")}
                  for (t, a), n in tracks.most_common(5)]
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

async def collect_audiobookshelf(client, cfg: Config, start, end, img: Path, tz) -> Dict[str, Any]:
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
        cov = None
        if len(out) < 3 and ID_RE.match(str(item_id)):
            cov = await save_image(client, f"{base}/api/items/{item_id}/cover?width=400", img, f"ab-{slug(b['title'])}", headers)
        out.append({"title": b["title"], "author": b["author"], "hours": round(b["secs"] / 3600, 1),
                    "finished": item_id in finished, "cover": cov})
    total = sum(b["secs"] for b in books.values())
    return {"hours": round(total / 3600, 1), "minutes": round(total / 60), "books": out, "finished": len(finished),
            "days": sorted(days), "titles_count": len([b for b in books.values() if b["secs"] >= 60])}


# --- Podcasts (AntennaPod through Nextcloud GPodder Sync) -------------------------------------------------------

def _safe_feed_url(url: Any) -> bool:
    parsed = urlparse(str(url or ""))
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


async def collect_podcasts(client, cfg: Config, start, end, img: Path) -> Dict[str, Any]:
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
        cov = await save_image(client, image, img, f"pc-{slug(title)}") if image and _safe_feed_url(image) and not out else None
        out.append({"title": title, "hours": round(p["secs"] / 3600, 1), "episodes": len(p["episodes"]), "cover": cov})
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
