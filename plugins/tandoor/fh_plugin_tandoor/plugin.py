"""Tandoor Recipes as a recipe source ("Recipe from Tandoor").

Hooks used: ``recipe_search``, ``recipe``, ``test_settings``, ``check`` and a router that serves recipe images
through ``app.plugins.api.proxy_image``. ``recipe_to_shopping`` is deliberately not implemented: the hub then puts the
ingredients on its own shopping list (Tandoor's shopping list is not touched).
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.logging_config import get_logger
from app.core.security import get_current_user
from app.plugins import Plugin, Setting, get_setting

logger = get_logger("plugin.tandoor")

router = APIRouter(prefix="/api/plugins/tandoor", tags=["tandoor"])

TIMEOUT = 8.0  # seconds for every call to Tandoor
SEARCH_TTL = 60.0
DETAIL_TTL = 300.0
IMAGE_TTL = 7 * 24 * 3600.0  # how long a returned recipe id may be fetched as image
CACHE_MAX = 300
IMAGE_MAX = 3000
MAX_STEP_DEPTH = 2  # recipes embedded as a step of another recipe

GUARD_TEXT = "This plugin needs Familienhub 1.0.0-beta.3 or newer (recipe sources and image proxy). Please update the hub."


class TandoorUnavailable(Exception):
    """Tandoor is not reachable or answered nonsense. Carries no address and no token."""


# --- compatibility guard ----------------------------------------------------------------------------------------


def compat_problem() -> Optional[str]:
    """None when the hub has the hooks this plugin needs, else a short explanation. Never raises."""
    try:
        from app.plugins.api import proxy_image  # noqa: F401  (added in 1.0.0-beta.3)
        from app.plugins.sources import recipe_plugins  # noqa: F401
    except Exception:  # noqa: BLE001 - an older hub lacks these modules or names
        return GUARD_TEXT
    return None


# --- settings and HTTP ------------------------------------------------------------------------------------------


def _base(value: Any = None) -> str:
    raw = str(value if value is not None else (get_setting("TANDOOR_URL") or "")).strip().rstrip("/")
    if raw.lower().endswith("/api"):
        raw = raw[:-4]
    return raw if raw.lower().startswith(("http://", "https://")) else ""


def _headers(token: Any = None) -> Dict[str, str]:
    value = str(token if token is not None else (get_setting("TANDOOR_TOKEN") or "")).strip()
    return {"Authorization": f"Bearer {value}", "Accept": "application/json"} if value else {"Accept": "application/json"}


async def _get_json(base: str, path: str, params: Optional[dict] = None, token: Any = None) -> Tuple[int, Any]:
    """GET ``base + path``; returns (status, parsed JSON or None). Raises TandoorUnavailable on network errors."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            response = await client.get(f"{base}{path}", params=params, headers=_headers(token))
    except httpx.HTTPError as exc:
        logger.warning("Tandoor not reachable: %s", type(exc).__name__)  # never log the address or the token
        raise TandoorUnavailable("not reachable") from exc
    if response.status_code != 200:
        return response.status_code, None
    try:
        return 200, response.json()
    except ValueError as exc:
        logger.warning("Tandoor answered with invalid JSON")
        raise TandoorUnavailable("invalid answer") from exc


# --- caches (in memory, per process) ----------------------------------------------------------------------------

_search_cache: Dict[Tuple[str, str, int], Tuple[float, List[dict]]] = {}
_detail_cache: Dict[Tuple[str, str], Tuple[float, dict]] = {}
_images: Dict[str, Tuple[float, str, str]] = {}  # recipe id -> (stored at, base, image path): the whitelist
_image_misses: Dict[Tuple[str, str], Tuple[float, bool]] = {}  # (base, id) -> looked up, nothing to serve
MISS_TTL = 300.0


def reset_caches() -> None:
    _search_cache.clear()
    _detail_cache.clear()
    _images.clear()
    _image_misses.clear()


def _put(cache: dict, key, value, limit: int = CACHE_MAX) -> None:
    if len(cache) >= limit:
        cache.pop(min(cache, key=lambda k: cache[k][0]))
    cache[key] = (time.monotonic(), value)


def _fresh(cache: dict, key, ttl: float):
    hit = cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < ttl else None


# --- mapping ----------------------------------------------------------------------------------------------------


def _image_path(value: Any) -> Optional[str]:
    """The path (and query) of a Tandoor image address. Only the path is kept: it is joined onto the configured
    address later, so a recipe can never make the proxy contact another host or receive the token."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    path = parts.path
    if not path.startswith("/") or path.startswith("//") or ".." in path or "\\" in path:
        return None
    return path + (f"?{parts.query}" if parts.query else "")


def _register_image(recipe_id: str, base: str, raw_image: Any) -> Optional[str]:
    path = _image_path(raw_image)
    if path is None:
        return None
    if recipe_id not in _images and len(_images) >= IMAGE_MAX:
        _images.pop(min(_images, key=lambda k: _images[k][0]))
    _images[recipe_id] = (time.monotonic(), base, path)
    return f"/api/plugins/tandoor/image/{recipe_id}"


def _minutes(recipe: dict) -> Optional[int]:
    total = 0
    for key in ("working_time", "waiting_time"):
        value = recipe.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            total += int(value)
    return total or None


def _number(amount: float) -> str:
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _name(obj: Any, plural: bool) -> str:
    if not isinstance(obj, dict):
        return ""
    plural_name = obj.get("plural_name")
    chosen = plural_name if plural and isinstance(plural_name, str) and plural_name.strip() else obj.get("name")
    return str(chosen or "").strip()


def ingredient_line(ing: Any) -> Optional[str]:
    """``{"amount": 200, "unit": {"name": "g"}, "food": {"name": "flour"}, "note": ""}`` -> ``"200 g flour"``."""
    if not isinstance(ing, dict) or ing.get("is_header"):
        return None
    amount = ing.get("amount")
    has_amount = (isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0
                  and not ing.get("no_amount"))
    plural = has_amount and abs(float(amount) - 1) > 1e-9
    food = _name(ing.get("food"), plural)
    parts: List[str] = []
    if has_amount:
        parts.append(_number(float(amount)))
        unit = _name(ing.get("unit"), plural)
        if unit:
            parts.append(unit)
    if food:
        parts.append(food)
    line = " ".join(parts)
    note = str(ing.get("note") or "").strip()
    if note:
        line = f"{line} ({note})" if line else note
    if not line:
        line = str(ing.get("original_text") or "").strip()
    return line or None


_SCALE = re.compile(r"\{\{\s*scale\(\s*([0-9.,]+)\s*\)\s*\}\}")


def _instruction(text: Any) -> str:
    return " ".join(_SCALE.sub(r"\1", str(text or "")).split())


def _walk_steps(steps: Any, ingredients: List[str], instructions: List[str], depth: int = 0) -> None:
    if not isinstance(steps, list):
        return
    ordered = sorted((s for s in steps if isinstance(s, dict)), key=lambda s: s.get("order") if isinstance(s.get("order"), (int, float)) else 0)
    for step in ordered:
        sub = step.get("step_recipe_data")
        if depth < MAX_STEP_DEPTH and isinstance(sub, dict):
            _walk_steps(sub.get("steps"), ingredients, instructions, depth + 1)
        raw = [i for i in step.get("ingredients") or [] if isinstance(i, dict)]
        raw.sort(key=lambda i: i.get("order") if isinstance(i.get("order"), (int, float)) else 0)
        for ing in raw:
            line = ingredient_line(ing)
            if line:
                ingredients.append(line)
        text = _instruction(step.get("instruction"))
        if text:
            instructions.append(text)


def _recipe_url(base: str, recipe_id: str) -> str:
    return f"{base}/view/recipe/{recipe_id}"


def _valid_id(recipe_id: Any) -> bool:
    return isinstance(recipe_id, str) and recipe_id.isdigit() and len(recipe_id) <= 12


# --- image endpoint ---------------------------------------------------------------------------------------------


@router.get("/image/{recipe_id}")
async def image(recipe_id: str, request: Request, _user=Depends(get_current_user)):
    """Recipe picture through the hub. Only numeric recipe ids; the address is built from the configured Tandoor
    address plus the path Tandoor gave for that recipe (from a recent search or recipe call, else looked up once),
    never from the request."""
    try:
        from app.plugins.api import proxy_image
    except Exception as exc:  # noqa: BLE001 - hub older than 1.0.0-beta.3
        raise HTTPException(404, "Not available") from exc
    base = _base()
    if not base or not _valid_id(recipe_id):
        raise HTTPException(404, "Image not found")
    entry = _images.get(recipe_id)
    if entry is None or time.monotonic() - entry[0] > IMAGE_TTL or entry[1] != base:
        entry = await _resolve_image(base, recipe_id)  # e.g. after a restart: ask Tandoor for the recipe itself
    if entry is None:
        raise HTTPException(404, "Image not found")
    return await proxy_image(f"{base}{entry[2]}", _headers(), request=request, timeout=TIMEOUT)


async def _resolve_image(base: str, recipe_id: str) -> Optional[Tuple[float, str, str]]:
    """Image path of a recipe this process has not returned (yet): fetch the recipe from the configured Tandoor.
    Only the path Tandoor reports is used. A failed lookup or a recipe without picture is remembered for a while."""
    key = (base, recipe_id)
    if _fresh(_image_misses, key, MISS_TTL):
        return None
    try:
        status, data = await _get_json(base, f"/api/recipe/{recipe_id}/")
    except TandoorUnavailable:
        status, data = 0, None
    if status == 200 and isinstance(data, dict) and _register_image(recipe_id, base, data.get("image")):
        return _images[recipe_id]
    _put(_image_misses, key, True)
    return None


# --- the plugin -------------------------------------------------------------------------------------------------


class TandoorPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(
            id="tandoor",
            name="Tandoor Recipes",
            version="1.0.0",
            description="Search, view and plan recipes from your Tandoor server",
            capabilities=frozenset({"meal_planning"}),
            settings=(
                Setting("TANDOOR_URL", "Tandoor address"),
                Setting("TANDOOR_TOKEN", "API token", secret=True),
            ),
        )

    def routers(self):
        return [router]

    def is_configured(self) -> bool:
        """Needs the settings and a hub that has the recipe-source hooks (older hubs: not configured, no crash)."""
        try:
            return compat_problem() is None and super().is_configured()
        except Exception:  # noqa: BLE001
            return False

    def is_set_up(self) -> bool:
        return self.is_configured()

    async def check(self):
        problem = compat_problem()
        if problem:
            return {"status": "error", "message": problem}
        return None

    # --- recipe source ---

    async def recipe_search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        base = _base()
        query = str(query or "").strip()
        if compat_problem() or not base or not query:
            return []
        size = max(1, min(int(limit or 30), 100))
        key = (base, query.lower(), size)
        cached = _fresh(_search_cache, key, SEARCH_TTL)
        if cached is not None:
            return cached
        status, data = await _get_json(base, "/api/recipe/", {"query": query, "page_size": size})
        if status != 200:
            logger.warning("Tandoor search answered HTTP %d", status)
            raise TandoorUnavailable("search failed")
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise TandoorUnavailable("invalid answer")
        items: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") in (None, "") or not str(row.get("name") or "").strip():
                continue
            rid = str(row["id"])
            if not _valid_id(rid):
                continue
            items.append({"id": rid, "name": str(row["name"]).strip(), "thumbnail": _register_image(rid, base, row.get("image")),
                          "url": _recipe_url(base, rid), "minutes": _minutes(row)})
            if len(items) >= size:
                break
        _put(_search_cache, key, items)
        return items

    async def recipe(self, recipe_id: str) -> Dict[str, Any]:
        base = _base()
        rid = str(recipe_id or "")
        if compat_problem() or not base or not _valid_id(rid):
            return {}
        cached = _fresh(_detail_cache, (base, rid), DETAIL_TTL)
        if cached is not None:
            return cached
        status, data = await _get_json(base, f"/api/recipe/{quote(rid)}/")
        if status == 404:
            return {}
        if status != 200:
            logger.warning("Tandoor recipe answered HTTP %d", status)
            raise TandoorUnavailable("recipe failed")
        if not isinstance(data, dict) or not str(data.get("name") or "").strip():
            raise TandoorUnavailable("invalid answer")
        ingredients: List[str] = []
        steps: List[str] = []
        _walk_steps(data.get("steps"), ingredients, steps)
        servings = data.get("servings")
        result = {
            "id": rid,
            "name": str(data["name"]).strip(),
            "image": _register_image(rid, base, data.get("image")),
            "url": _recipe_url(base, rid),
            "servings": servings if isinstance(servings, (int, float)) and not isinstance(servings, bool) and servings > 0 else None,
            "minutes": _minutes(data),
            "ingredients": ingredients,
            "steps": steps,
        }
        _put(_detail_cache, (base, rid), result)
        return result

    # --- connection test ---

    async def test_settings(self, values):
        problem = compat_problem()
        if problem:
            return {"ok": False, "level": "error", "message": problem}
        base = _base(values.get("TANDOOR_URL") or "")
        token = str(values.get("TANDOOR_TOKEN") or "").strip()
        if not base:
            return {"ok": False, "level": "error", "message": "Enter the address of Tandoor (http:// or https://)"}
        if not token:
            return {"ok": False, "level": "error", "message": "Enter an API token"}
        try:
            status, data = await _get_json(base, "/api/recipe/", {"page_size": 1}, token=token)
        except TandoorUnavailable:
            return {"ok": False, "level": "error", "message": "Tandoor is not reachable"}
        if status in (401, 403):
            return {"ok": False, "level": "error", "message": "Tandoor rejected the API token (it needs read access)"}
        if status == 404:
            return {"ok": False, "level": "error", "message": "No Tandoor API found at this address"}
        if status != 200:
            return {"ok": False, "level": "error", "message": f"Tandoor answers with error {status}"}
        if not isinstance(data, dict) or not isinstance(data.get("results"), list) or not isinstance(data.get("count"), int):
            return {"ok": False, "level": "error", "message": "The address does not look like a Tandoor API"}
        count = data["count"]
        return {"ok": True, "level": "ok", "message": f"Connected, {count} recipe{'s' if count != 1 else ''}"}
