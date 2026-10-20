"""Immich "On this day": photos from this day in earlier years on the Today screen.

What it does:

- ``today_cards``: up to N cards (default 3) "On this day, 3 years ago" with a thumbnail, from Immich's memories
  API (``GET /api/memories``), with fallbacks for older servers (``/api/assets/memory-lane``) and for album
  filters or archived photos (``POST /api/search/metadata``, one query per past year). Cached per day.
- ``/api/plugins/immich/thumbnail/<asset id>``: the plugin's own authenticated route that streams a preview through
  the hub (``proxy_image``) with the API key. It serves **only** assets the plugin itself picked for today.
- ``test_settings``: "Connected as <name>, Immich <version>".

The plugin only reads. Nothing is written to Immich or to the hub's database. Needs Familienhub 1.0.0-beta.3
or newer (the Today-card hook); on older hubs it reports itself as not set up instead of failing.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.logging_config import get_logger
from app.core.security import get_current_user
from app.plugins import Plugin, Setting, get_setting

try:  # the image proxy came with the same hub release as today_cards
    from app.plugins.api import proxy_image
except ImportError:  # pragma: no cover - only on hubs older than 1.0.0-beta.3
    proxy_image = None

logger = get_logger("plugin.immich")

PLUGIN_ID = "immich"
NEEDS_HUB = "Familienhub 1.0.0-beta.3 or newer"
# Hubs older than 1.0.0-beta.3 have no Today-card hook (and no image proxy)
COMPATIBLE = hasattr(Plugin, "today_cards") and proxy_image is not None

REQUEST_TIMEOUT = 3.0  # per request to Immich (the hub gives today_cards 4 s in total)
TEST_TIMEOUT = 6.0
BUDGET_SECONDS = 3.5  # whole lookup, so we always answer before the hub's 4 s limit
MAX_YEARS_BACK = 15
DEFAULT_MAX_PHOTOS, MAX_PHOTOS_LIMIT = 3, 5
CACHE_OK_SECONDS = 6 * 3600  # per day; a new day gets a new entry
CACHE_FAIL_SECONDS = 120  # do not hammer a server that is down
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ALBUM_ID_RE = ASSET_ID_RE

router = APIRouter(prefix=f"/api/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])

# {cache key: (expires monotonic, photos)} and the whitelist of asset ids that may be proxied
_cache: Dict[str, Any] = {}
_allowed: Dict[str, float] = {}


def reset_state() -> None:
    """Forget cache and whitelist (tests)."""
    _cache.clear()
    _allowed.clear()


# --- settings -------------------------------------------------------------------------------------------------

def _base_url(raw: Any = None) -> str:
    value = str(get_setting("IMMICH_URL") if raw is None else raw or "").strip().rstrip("/")
    if value.lower().endswith("/api"):
        value = value[:-4]
    return value


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _album_ids(raw: Any) -> List[str]:
    """Valid album ids from a comma separated list; anything else is dropped."""
    out: List[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part and ALBUM_ID_RE.match(part) and part.lower() not in out:
            out.append(part.lower())
    return out[:20]


def _max_photos(raw: Any) -> int:
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PHOTOS
    return max(1, min(MAX_PHOTOS_LIMIT, n))


class _Config:
    def __init__(self, base: str, key: str, albums: List[str], archived: bool, max_photos: int) -> None:
        self.base, self.key, self.albums, self.archived, self.max_photos = base, key, albums, archived, max_photos

    @classmethod
    def load(cls) -> "_Config":
        return cls(
            _base_url(), str(get_setting("IMMICH_API_KEY") or "").strip(),
            _album_ids(get_setting("IMMICH_ALBUM_IDS")), _truthy(get_setting("IMMICH_INCLUDE_ARCHIVED", "false")),
            _max_photos(get_setting("IMMICH_MAX_PHOTOS", str(DEFAULT_MAX_PHOTOS))),
        )

    @property
    def headers(self) -> Dict[str, str]:
        return {"x-api-key": self.key, "Accept": "application/json"}

    def fingerprint(self) -> str:
        # no secret in it: only what changes the selection
        return "|".join((self.base, ",".join(self.albums), str(self.archived), str(self.max_photos)))


# --- Immich responses -----------------------------------------------------------------------------------------

def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _asset_date(asset: Dict[str, Any]) -> Optional[date]:
    """The day the photo was taken, on the wall clock of where it was taken (``localDateTime``)."""
    for field in ("localDateTime", "fileCreatedAt"):
        moment = _parse_time(asset.get(field))
        if moment is not None:
            if field == "localDateTime":  # Immich stores local time with a nominal "Z"; the digits are local
                return moment.date()
            return moment.astimezone(timezone.utc).date()
    return None


def _photo(asset: Any, years_ago: Optional[int], today: date) -> Optional[Dict[str, Any]]:
    """A compact photo record, or None for videos, trashed items and anything without a usable id."""
    if not isinstance(asset, dict):
        return None
    asset_id = asset.get("id")
    if not isinstance(asset_id, str) or not ASSET_ID_RE.match(asset_id):
        return None
    if str(asset.get("type", "IMAGE")).upper() != "IMAGE" or asset.get("isTrashed") is True:
        return None
    taken = _asset_date(asset)
    if years_ago is None and taken is not None:
        years_ago = today.year - taken.year
    if years_ago is None or years_ago < 1:
        return None
    exif = asset.get("exifInfo") if isinstance(asset.get("exifInfo"), dict) else {}
    description = exif.get("description")
    return {
        "id": asset_id,
        "years_ago": years_ago,
        "date": taken.isoformat() if taken else None,
        "favorite": asset.get("isFavorite") is True,
        "archived": asset.get("isArchived") is True,
        "description": description.strip() if isinstance(description, str) and description.strip() else None,
    }


def _choose(photos: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Up to ``limit`` photos: favourites first, different years before a second photo of the same year."""
    unique: Dict[str, Dict[str, Any]] = {}
    for p in photos:
        unique.setdefault(p["id"], p)
    ranked = sorted(unique.values(), key=lambda p: (not p["favorite"], p["years_ago"], p["id"]))
    chosen: List[Dict[str, Any]] = []
    years = set()
    for p in ranked:
        if p["years_ago"] not in years:
            chosen.append(p)
            years.add(p["years_ago"])
    for p in ranked:
        if p not in chosen:
            chosen.append(p)
    return sorted(chosen[:limit], key=lambda p: (p["years_ago"], not p["favorite"], p["id"]))


# --- talking to Immich ----------------------------------------------------------------------------------------

async def _get_json(client: httpx.AsyncClient, cfg: _Config, path: str, params: Optional[dict] = None):
    """Parsed JSON, or None if Immich is unreachable or answers something else than 200 + JSON.
    ``LookupError`` marks "this endpoint does not exist here" (older/newer server): the caller tries the next way."""
    try:
        response = await client.get(f"{cfg.base}{path}", params=params, headers=cfg.headers)
    except httpx.HTTPError as exc:
        logger.warning("Immich request failed: %s", type(exc).__name__)  # never log addresses or keys
        return None
    if response.status_code in (400, 404, 405, 410, 422):
        raise LookupError(path)
    if response.status_code != 200:
        logger.warning("Immich answered HTTP %d", response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning("Immich sent an answer that is not JSON")
        return None


async def _from_memories(client, cfg: _Config, today: date) -> Optional[List[Dict[str, Any]]]:
    """Immich's own "On this day" memories. None = not available here, [] = none for today."""
    moment = datetime(today.year, today.month, today.day, 12, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        data = await _get_json(client, cfg, "/api/memories", {"for": moment})
    except LookupError:
        try:  # servers before the memories API
            old = await _get_json(client, cfg, "/api/assets/memory-lane", {"day": today.day, "month": today.month})
        except LookupError:
            return None
        return _from_memory_lane(old, today)
    if data is None:
        raise ConnectionError("memories")
    if not isinstance(data, list):
        return None
    photos: List[Dict[str, Any]] = []
    for memory in data:
        if not isinstance(memory, dict) or memory.get("type") not in (None, "on_this_day"):
            continue
        info = memory.get("data") if isinstance(memory.get("data"), dict) else {}
        year = info.get("year") if isinstance(info.get("year"), int) else None
        ago = today.year - year if year else None
        for asset in memory.get("assets") if isinstance(memory.get("assets"), list) else []:
            p = _photo(asset, ago, today)
            if p:
                photos.append(p)
    return photos


def _from_memory_lane(data: Any, today: date) -> Optional[List[Dict[str, Any]]]:
    if data is None:
        raise ConnectionError("memory-lane")
    if not isinstance(data, list):
        return None
    photos: List[Dict[str, Any]] = []
    for lane in data:
        if not isinstance(lane, dict):
            continue
        ago = lane.get("yearsAgo") if isinstance(lane.get("yearsAgo"), int) else None
        for asset in lane.get("assets") if isinstance(lane.get("assets"), list) else []:
            p = _photo(asset, ago, today)
            if p:
                photos.append(p)
    return photos


async def _search_year(client, cfg: _Config, today: date, years_ago: int) -> List[Dict[str, Any]]:
    year = today.year - years_ago
    try:
        day = date(year, today.month, today.day)  # 29 February: years without it are skipped
    except ValueError:
        return []
    # A day plus a margin on both sides: the server compares in UTC, we keep only photos whose local day matches
    after = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(days=1)
    before = after + timedelta(days=3)
    body: Dict[str, Any] = {
        "takenAfter": after.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "takenBefore": before.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "type": "IMAGE",
        "size": 50,
        "withExif": True,
    }
    if cfg.archived:
        body["withArchived"] = True
    if cfg.albums:
        body["albumIds"] = cfg.albums
    try:
        response = await client.post(f"{cfg.base}/api/search/metadata", json=body, headers=cfg.headers)
    except httpx.HTTPError as exc:
        logger.warning("Immich search failed: %s", type(exc).__name__)
        raise ConnectionError("search") from exc
    if response.status_code != 200:
        logger.warning("Immich search answered HTTP %d", response.status_code)
        raise ConnectionError("search")
    try:
        items = (response.json().get("assets") or {}).get("items")
    except (ValueError, AttributeError):
        raise ConnectionError("search") from None
    photos = []
    for asset in items if isinstance(items, list) else []:
        taken = _asset_date(asset) if isinstance(asset, dict) else None
        if taken is not None and (taken.month, taken.day) != (today.month, today.day):
            continue
        p = _photo(asset, years_ago, today)
        if p:
            photos.append(p)
    return photos


async def _from_search(client, cfg: _Config, today: date) -> List[Dict[str, Any]]:
    results = await asyncio.gather(
        *(_search_year(client, cfg, today, n) for n in range(1, MAX_YEARS_BACK + 1)), return_exceptions=True)
    photos: List[Dict[str, Any]] = []
    failed = 0
    for result in results:
        if isinstance(result, BaseException):
            failed += 1
        else:
            photos.extend(result)
    if failed == len(results):
        raise ConnectionError("search")
    return photos


async def _collect(cfg: _Config, today: date) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
        photos: Optional[List[Dict[str, Any]]] = None
        # memories know nothing of album filters or archived photos: those go straight to the search
        if not cfg.albums and not cfg.archived:
            photos = await _from_memories(client, cfg, today)
        if not photos:
            photos = await _from_search(client, cfg, today)
    if not cfg.archived:
        photos = [p for p in photos if not p["archived"]]
    return _choose(photos, cfg.max_photos)


async def _photos_for_today(cfg: _Config, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Today's photos, cached per day and configuration. Never raises; [] when Immich is down."""
    today = today or date.today()
    key = f"{today.isoformat()}|{cfg.fingerprint()}"
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        _remember(hit[1])
        return hit[1]
    try:
        photos = await asyncio.wait_for(_collect(cfg, today), BUDGET_SECONDS)
        ttl = CACHE_OK_SECONDS
    except Exception as exc:  # noqa: BLE001 - never into the Today screen
        logger.warning("Immich lookup failed: %s", type(exc).__name__)
        photos, ttl = [], CACHE_FAIL_SECONDS
    for stale in [k for k, v in _cache.items() if v[0] <= now]:
        del _cache[stale]
    _cache[key] = (now + ttl, photos)
    _remember(photos)
    return photos


def _remember(photos: List[Dict[str, Any]]) -> None:
    """Only assets chosen for today may be fetched through the thumbnail route."""
    now = time.monotonic()
    for stale in [k for k, t in _allowed.items() if t <= now]:
        del _allowed[stale]
    for p in photos:
        _allowed[p["id"]] = now + 2 * 86400


# --- route ----------------------------------------------------------------------------------------------------

@router.get("/thumbnail/{asset_id}")
async def thumbnail(asset_id: str, request: Request, _user=Depends(get_current_user)):
    """A preview of one of today's photos. Not an open proxy: unknown ids are 404, whatever Immich would say."""
    cfg = _Config.load()
    if not COMPATIBLE or not cfg.base or not cfg.key:
        raise HTTPException(404, "Not found")
    expires = _allowed.get(asset_id) if ASSET_ID_RE.match(asset_id) else None
    if expires is None or expires <= time.monotonic():
        raise HTTPException(404, "Not found")
    return await proxy_image(
        f"{cfg.base}/api/assets/{asset_id}/thumbnail?size=preview", {"x-api-key": cfg.key},
        request=request, timeout=8.0, max_age=86400,
    )


# --- texts ----------------------------------------------------------------------------------------------------

def _language(user: Any) -> str:
    lang = str(getattr(user, "language", "") or "")[:2].lower()
    return lang if lang in ("de", "en") else "de"


def _years_text(n: int, lang: str) -> str:
    if lang == "en":
        return "1 year ago" if n == 1 else f"{n} years ago"
    return "vor 1 Jahr" if n == 1 else f"vor {n} Jahren"


def _date_text(value: Optional[str], lang: str) -> Optional[str]:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return d.strftime("%d.%m.%Y") if lang == "de" else d.strftime("%Y-%m-%d")


def _link(cfg: _Config, asset_id: str) -> Optional[str]:
    """The asset in Immich's web UI, only when the configured address is https (the hub drops anything else)."""
    return f"{cfg.base}/photos/{asset_id}" if cfg.base.lower().startswith("https://") else None


def build_cards(cfg: _Config, photos: List[Dict[str, Any]], lang: str) -> List[Dict[str, Any]]:
    title = "On this day" if lang == "en" else "An diesem Tag"
    cards = []
    for index, p in enumerate(photos):
        lines = [{"text": p["description"] or _years_text(p["years_ago"], lang),
                  "secondary": _date_text(p["date"], lang)}]
        if p["description"]:
            lines.insert(0, {"text": _years_text(p["years_ago"], lang)})
        card: Dict[str, Any] = {
            "id": "on-this-day" if index == 0 else f"on-this-day-{index + 1}",
            "title": f"{title}, {_years_text(p['years_ago'], lang)}",
            "icon": "photo",
            "lines": lines,
            "image": f"/api/plugins/{PLUGIN_ID}/thumbnail/{p['id']}",
        }
        link = _link(cfg, p["id"])
        if link:
            card["link"] = link
        cards.append(card)
    return cards


# --- the plugin -----------------------------------------------------------------------------------------------

class ImmichPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(
            id=PLUGIN_ID,  # same as "id" in familienhub-plugin.json
            name="Immich: On this day",
            version="1.0.0",
            description="Photos from this day in earlier years, from Immich",
            capabilities=frozenset(),
            settings=(
                Setting("IMMICH_URL", "Address of Immich"),
                Setting("IMMICH_API_KEY", "API key", secret=True),
                Setting("IMMICH_ALBUM_IDS", "Only these albums", required=False),
                Setting("IMMICH_INCLUDE_ARCHIVED", "Include archived photos", required=False),
                Setting("IMMICH_MAX_PHOTOS", "Number of photos", required=False),
            ),
        )

    def routers(self):
        return [router]

    # compatibility guard: on a hub without the hooks the plugin stays "not set up" and explains why
    def is_configured(self) -> bool:
        return COMPATIBLE and super().is_configured()

    def missing_settings(self) -> List[str]:
        if not COMPATIBLE:
            return [f"Needs {NEEDS_HUB}"]
        return super().missing_settings()

    async def check(self) -> Optional[Dict[str, Any]]:
        if not COMPATIBLE:
            return {"status": "error", "message": f"Needs {NEEDS_HUB}"}
        return None

    async def today_cards(self, user, db):
        """Cards "On this day, 3 years ago", one per photo; none when there is nothing (or Immich is down)."""
        if not COMPATIBLE:
            return []
        cfg = _Config.load()
        if not cfg.base or not cfg.key:
            return []
        try:
            photos = await _photos_for_today(cfg)
            return build_cards(cfg, photos, _language(user))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Immich card failed: %s", type(exc).__name__)
            return []

    async def test_settings(self, values):
        """Ping, then ``/api/users/me`` (checks the key) and the server version, with the values as typed."""
        if not COMPATIBLE:
            return {"ok": False, "level": "error", "message": f"Needs {NEEDS_HUB}"}
        base = _base_url(values.get("IMMICH_URL"))
        key = str(values.get("IMMICH_API_KEY") or "").strip()
        if not base.lower().startswith(("http://", "https://")):
            return {"ok": False, "level": "error", "message": "The address must start with http:// or https://"}
        if not key:
            return {"ok": False, "level": "error", "message": "The API key is missing"}
        bad = [a for a in str(values.get("IMMICH_ALBUM_IDS") or "").replace(";", ",").split(",")
               if a.strip() and not ALBUM_ID_RE.match(a.strip())]
        if bad:
            return {"ok": False, "level": "error", "message": "Album IDs look like 3f0c9e1a-… (comma separated)"}
        headers = {"x-api-key": key, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=TEST_TIMEOUT, follow_redirects=False) as client:
                ping = await client.get(f"{base}/api/server/ping", headers=headers)
                if ping.status_code != 200 or not _is_json(ping, "res"):
                    return _ping_failed(ping)
                me = await client.get(f"{base}/api/users/me", headers=headers)
                version = await _server_version(client, base, headers)
        except httpx.TimeoutException:
            return {"ok": False, "level": "error", "message": "Immich did not answer in time. Check the address and network"}
        except httpx.HTTPError:
            return {"ok": False, "level": "error", "message": "Immich is not reachable. Check the address (no /api at the end)"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Immich test failed: %s", type(exc).__name__)
            return {"ok": False, "level": "error", "message": "The test failed unexpectedly"}
        if me.status_code in (401, 403):
            return {"ok": False, "level": "error",
                    "message": "Immich rejected the API key (wrong key or missing permissions: user.read)"}
        if me.status_code != 200:
            return {"ok": False, "level": "error", "message": f"Immich answers with error {me.status_code}"}
        try:
            data = me.json()
            name = str(data.get("name") or data.get("email") or "").strip() if isinstance(data, dict) else ""
        except ValueError:
            name = ""
        if not name:
            return {"ok": False, "level": "error", "message": "The answer does not look like Immich"}
        suffix = f", Immich {version}" if version else ""
        return {"ok": True, "level": "ok", "message": f"Connected as {name}{suffix}"}


def _is_json(response: httpx.Response, key: str) -> bool:
    try:
        data = response.json()
    except ValueError:
        return False
    return isinstance(data, dict) and key in data


def _ping_failed(response: httpx.Response) -> Dict[str, Any]:
    if response.status_code == 404 or response.status_code == 200:
        text = "This address does not look like Immich. Check the address (no /api at the end)"
    else:
        text = f"Immich answers with error {response.status_code}"
    return {"ok": False, "level": "error", "message": text}


async def _server_version(client: httpx.AsyncClient, base: str, headers: Dict[str, str]) -> str:
    """``1.2.3`` from ``/api/server/version`` (``/api/server-info/version`` on old servers), or ""."""
    for path in ("/api/server/version", "/api/server-info/version"):
        try:
            response = await client.get(f"{base}{path}", headers=headers)
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            continue
        if isinstance(data, dict) and all(isinstance(data.get(k), int) for k in ("major", "minor", "patch")):
            return f"{data['major']}.{data['minor']}.{data['patch']}"
    return ""
