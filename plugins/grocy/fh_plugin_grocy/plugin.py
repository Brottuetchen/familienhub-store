"""Grocy plugin: shopping list source, expiring stock on Today, daily hints for parents.

What it uses (docs/plugins.md): the shopping connector (``shopping_connector``), ``today_cards``,
``scheduled`` plus ``app.plugins.api.notify``, and ``test_settings``. Everything fails softly: Grocy being
down gives empty data, never an exception into the UI. Secrets and addresses are never logged.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.logging_config import get_logger
from app.plugins import Plugin, Setting, get_setting

logger = get_logger("plugin.grocy")

# Hubs older than 1.0.0-beta.3 lack the shopping connector and the notify API. The manifest's
# min_hub_version makes the installer refuse them; this guard covers custom images and hand-copied installs.
try:
    from app.plugins.api import notify
    from app.plugins.sources import ShoppingConnector

    _HUB_OK = True
except ImportError:  # pragma: no cover - exercised by tests through monkeypatching
    notify = None  # type: ignore[assignment]
    ShoppingConnector = object  # type: ignore[assignment,misc]
    _HUB_OK = False

HUB_TOO_OLD = {
    "de": "Dieses Plugin braucht Familienhub 1.0.0-beta.3 oder neuer.",
    "en": "This plugin needs Familienhub 1.0.0-beta.3 or newer.",
}

TIMEOUT = 6.0  # seconds per request, always below 8
CARD_TIMEOUT = 3.0
VOLATILE_TTL = 60.0
PRODUCTS_TTL = 300.0
NEVER = "2999-12-31"  # Grocy's "does not expire"

_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}


class GrocyError(Exception):
    """Grocy did not deliver. The message never contains addresses or keys."""


def reset_cache() -> None:
    _cache.clear()


def _cached(key: Tuple[str, str], ttl: float) -> Any:
    hit = _cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < ttl else None


def _store(key: Tuple[str, str], value: Any) -> Any:
    _cache[key] = (time.monotonic(), value)
    return value


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return min(max(number, lo), hi)


def _flag(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _base() -> str:
    return str(get_setting("GROCY_URL") or "").strip().rstrip("/")


def _key() -> str:
    return str(get_setting("GROCY_API_KEY") or "")


def _list_id() -> int:
    return _int(get_setting("GROCY_SHOPPING_LIST_ID"), 1, 1, 10**9)


def _due_days() -> int:
    return _int(get_setting("GROCY_DUE_DAYS"), 2, 0, 365)


def _lang(user: Any = None) -> str:
    return "de" if str(getattr(user, "language", "") or "").lower().startswith("de") else "en"


async def _request(base: str, key: str, method: str, path: str, *, json_body: Any = None,
                   params: Any = None, timeout: float = TIMEOUT) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            return await client.request(method, f"{base}{path}", json=json_body, params=params,
                                        headers={"GROCY-API-KEY": key, "Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.warning("Grocy request failed: %s", exc.__class__.__name__)
        raise GrocyError("Grocy not reachable") from exc


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GrocyError("Grocy answered with something unexpected") from exc


async def _get_json(base: str, key: str, path: str, *, params: Any = None, timeout: float = TIMEOUT) -> Any:
    response = await _request(base, key, "GET", path, params=params, timeout=timeout)
    if response.status_code != 200:
        raise GrocyError(f"Grocy answered with error {response.status_code}")
    return _json(response)


def _num(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _fmt_amount(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _done(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true")


# --- products and units (cached) ---------------------------------------------------------------------------


async def _products(base: str, key: str, timeout: float = TIMEOUT) -> Dict[str, Dict[str, Any]]:
    hit = _cached((base, "products"), PRODUCTS_TTL)
    if hit is not None:
        return hit
    rows = await _get_json(base, key, "/api/objects/products", timeout=timeout)
    if not isinstance(rows, list):
        raise GrocyError("Grocy answered with something unexpected")
    return _store((base, "products"), {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id") is not None})


async def _units(base: str, key: str) -> Dict[str, Dict[str, Any]]:
    hit = _cached((base, "units"), PRODUCTS_TTL)
    if hit is not None:
        return hit
    try:
        rows = await _get_json(base, key, "/api/objects/quantity_units")
    except GrocyError:
        return {}  # units are a nicety; amounts still show
    if not isinstance(rows, list):
        return {}
    return _store((base, "units"), {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id") is not None})


def _unit_name(unit: Optional[Dict[str, Any]], amount: Optional[float]) -> str:
    if not unit:
        return ""
    plural = unit.get("name_plural")
    return str(plural if plural and amount is not None and amount != 1 else unit.get("name") or "")


# --- shopping connector ------------------------------------------------------------------------------------


class GrocyList(ShoppingConnector):  # type: ignore[misc,valid-type]
    def __init__(self, base: str, key: str, list_id: int):
        self.base, self.key, self.list_id = base, key, list_id

    async def _rows(self) -> List[Dict[str, Any]]:
        rows = await _get_json(self.base, self.key, "/api/objects/shopping_list",
                               params=[("query[]", f"shopping_list_id={self.list_id}")])
        if not isinstance(rows, list):
            raise GrocyError("Grocy answered with something unexpected")
        # Grocy applies the filter itself; checking again keeps a server that ignores it from leaking other lists.
        return [r for r in rows if isinstance(r, dict) and str(r.get("shopping_list_id", self.list_id)) == str(self.list_id)]

    async def list_items(self) -> List[Dict[str, Any]]:
        rows = await self._rows()
        products = await _products(self.base, self.key) if any(r.get("product_id") for r in rows) else {}
        units = await _units(self.base, self.key)
        items = []
        for row in rows:
            if row.get("id") is None:
                continue
            note = str(row.get("note") or "").strip()
            product = products.get(str(row["product_id"])) if row.get("product_id") else None
            if row.get("product_id") and product is None:
                summary = note or f"Product {row['product_id']}"
            else:
                summary = str(product["name"]) if product else note
            amount = _num(row.get("amount"))
            unit_id = row.get("qu_id") or (product or {}).get("qu_id_purchase") if amount and amount != 1 else row.get("qu_id")
            unit = _unit_name(units.get(str(unit_id)), amount) if unit_id else ""
            parts = []
            if amount and (amount != 1 or unit):
                parts.append(f"{_fmt_amount(amount)} {unit}".strip())
            if product and note:
                parts.append(note)
            item = {"uid": str(row["id"]), "summary": summary,
                    "status": "completed" if _done(row.get("done")) else "needs_action"}
            if parts:
                item["description"] = " · ".join(parts)
            items.append(item)
        return items

    async def _match_product(self, summary: str) -> Optional[Dict[str, Any]]:
        try:
            products = await _products(self.base, self.key)
        except GrocyError:
            return None  # without the product list the item goes on as free text
        wanted = summary.casefold()
        for product in products.values():
            if str(product.get("name") or "").strip().casefold() == wanted and _flag(product.get("active"), True):
                return product
        return None

    async def add(self, summary: str, user_id: Optional[int] = None, description: Optional[str] = None) -> bool:
        summary = summary.strip()
        if not summary:
            return False
        description = (description or "").strip()
        product = await self._match_product(summary)
        for row in await self._rows():
            same = (str(row.get("product_id")) == str(product["id"]) if product and row.get("product_id")
                    else not row.get("product_id") and not product
                    and str(row.get("note") or "").strip().casefold() == summary.casefold())
            if not same or row.get("id") is None:
                continue
            if _done(row.get("done")):  # bought earlier and still listed: put it back instead of a second entry
                return await self.set_done(str(row["id"]), False)
            return True  # already open: no duplicate
        amount = _num(description)
        body: Dict[str, Any] = {"shopping_list_id": self.list_id, "amount": amount if amount and amount > 0 else 1}
        extra = "" if amount and amount > 0 else description
        if product:
            body["product_id"] = int(product["id"])
            if extra:
                body["note"] = extra
        else:
            body["note"] = f"{summary} ({extra})" if extra else summary
        response = await _request(self.base, self.key, "POST", "/api/objects/shopping_list", json_body=body)
        if response.status_code not in (200, 201):
            raise GrocyError(f"Grocy answered with error {response.status_code}")
        return True

    @staticmethod
    def _valid(uid: str) -> bool:
        return isinstance(uid, str) and uid.isdigit()

    async def set_done(self, uid: str, done: bool) -> bool:
        if not self._valid(uid):
            return False
        response = await _request(self.base, self.key, "PUT", f"/api/objects/shopping_list/{uid}",
                                  json_body={"done": 1 if done else 0})
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 204):
            raise GrocyError(f"Grocy answered with error {response.status_code}")
        return True

    async def delete(self, uid: str) -> bool:
        if not self._valid(uid):
            return False
        response = await _request(self.base, self.key, "DELETE", f"/api/objects/shopping_list/{uid}")
        if response.status_code == 404:
            return False
        if response.status_code not in (200, 204):
            raise GrocyError(f"Grocy answered with error {response.status_code}")
        return True

    async def rename(self, uid: str, summary: str) -> bool:
        """Free-text entries only: a product entry is named by its product."""
        if not self._valid(uid) or not summary.strip():
            return False
        response = await _request(self.base, self.key, "GET", f"/api/objects/shopping_list/{uid}")
        if response.status_code != 200:
            return False
        row = _json(response)
        if not isinstance(row, dict) or row.get("product_id"):
            return False
        put = await _request(self.base, self.key, "PUT", f"/api/objects/shopping_list/{uid}",
                             json_body={"note": summary.strip()})
        return put.status_code in (200, 204)


# --- stock -------------------------------------------------------------------------------------------------


def _parse_day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _entry(row: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    product = row.get("product") if isinstance(row.get("product"), dict) else {}
    name = str(product.get("name") or row.get("name") or "").strip()
    pid = product.get("id", row.get("id", row.get("product_id")))
    if not name or pid is None:
        return None
    due = str(row.get("best_before_date") or row.get("next_due_date") or "")[:10]
    return {"id": str(pid), "name": name, "due": due if _parse_day(due) and due != NEVER else ""}


async def fetch_volatile(base: str, key: str, days: int, timeout: float = TIMEOUT) -> Dict[str, List[Dict[str, Any]]]:
    hit = _cached((base, f"volatile{days}"), VOLATILE_TTL)
    if hit is not None:
        return hit
    data = await _get_json(base, key, "/api/stock/volatile", params={"due_soon_days": days}, timeout=timeout)
    if not isinstance(data, dict):
        raise GrocyError("Grocy answered with something unexpected")

    def rows(name: str) -> List[Dict[str, Any]]:
        raw = data.get(name)
        return [e for e in map(_entry, raw if isinstance(raw, list) else []) if e]

    result = {"due": sorted(rows("due_products"), key=lambda e: e["due"] or NEVER),
              "past": sorted(rows("overdue_products") + rows("expired_products"), key=lambda e: e["due"] or NEVER),
              "missing": rows("missing_products")}
    return _store((base, f"volatile{days}"), result)


def _when(due: str, today: date, lang: str) -> str:
    day = _parse_day(due)
    if day is None:
        return ""
    diff = (day - today).days
    if diff <= 0:
        return "heute" if lang == "de" else "today"
    if diff == 1:
        return "morgen" if lang == "de" else "tomorrow"
    return f"{diff} Tagen" if lang == "de" else f"{diff} days"


def _with_when(entry: Dict[str, Any], today: date, lang: str) -> str:
    when = _when(entry["due"], today, lang)
    if when and lang == "de" and when.endswith("Tagen"):
        when = "in " + when
    return f"{entry['name']} ({when})" if when else entry["name"]


def _join(texts: List[str], lang: str, budget: int = 120) -> str:
    out: List[str] = []
    for index, text in enumerate(texts):
        if out and len(", ".join(out + [text])) > budget:
            more = len(texts) - index
            return ", ".join(out) + f" +{more}"
        out.append(text)
    return ", ".join(out)


def _product_count(count: int, lang: str) -> str:
    if lang == "de":
        return f"{count} Produkt" if count == 1 else f"{count} Produkte"
    return f"{count} product" if count == 1 else f"{count} products"


def build_card(volatile: Dict[str, List[Dict[str, Any]]], today: date, lang: str) -> List[Dict[str, Any]]:
    due, past, missing = volatile["due"], volatile["past"], volatile["missing"]
    if not (due or past or missing):
        return []
    de = lang == "de"
    lines = []
    if due:
        lines.append({"text": f"{'Bald ablaufend' if de else 'Expiring soon'}: "
                              + _join([_with_when(e, today, lang) for e in due], lang)})
    if past:
        lines.append({"text": f"{'Abgelaufen' if de else 'Past date'}: " + _join([e["name"] for e in past], lang)})
    if missing:
        lines.append({"text": f"{'Fehlt' if de else 'Missing'}: {_product_count(len(missing), lang)}"})
    count = len(due) + len(past) or len(missing)
    return [{"id": "stock", "title": "Grocy" if not de else "Vorräte", "icon": "warning" if (due or past) else "shopping_cart",
             "badge": str(count), "lines": lines}]


# --- notification state ------------------------------------------------------------------------------------


def _state_path() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "grocy_state.json"


def _load_state() -> Dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"notified": dict(data.get("notified") or {}), "missing_day": str(data.get("missing_day") or "")}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {"notified": {}, "missing_day": ""}


def _save_state(state: Dict[str, Any]) -> bool:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".grocy_state-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("Grocy state not saved: %s", exc.__class__.__name__)
        return False


# --- the plugin --------------------------------------------------------------------------------------------


class GrocyPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(
            id="grocy",
            name="Grocy",
            version="1.0.0",
            description="Shopping list, expiring stock and missing products from Grocy",
            capabilities=frozenset({"shopping"}),
            settings=(
                Setting("GROCY_URL", "Grocy address"),
                Setting("GROCY_API_KEY", "API key", secret=True),
                Setting("GROCY_SHOPPING_LIST_ID", "Shopping list ID", required=False),
                Setting("GROCY_DUE_DAYS", "Expiring soon: days", required=False),
                Setting("GROCY_NOTIFY", "Notify parents", required=False),
                Setting("GROCY_NOTIFY_HOUR", "Notification hour (0-23)", required=False),
                Setting("GROCY_ADD_MISSING", "Add missing products to the list automatically", required=False),
            ),
        )
        self.schedule_minutes = 30

    def is_configured(self) -> bool:
        return _HUB_OK and super().is_configured()

    async def check(self) -> Optional[Dict[str, Any]]:
        if not _HUB_OK:
            return {"status": "error", "message": HUB_TOO_OLD["en"]}
        return None

    def shopping_connector(self, db):
        if not self.is_configured():
            return None
        return GrocyList(_base(), _key(), _list_id())

    async def today_cards(self, user, db):
        if not self.is_configured():
            return []
        try:
            volatile = await fetch_volatile(_base(), _key(), _due_days(), timeout=CARD_TIMEOUT)
        except (GrocyError, KeyError, TypeError, ValueError):
            return []
        return build_card(volatile, date.today(), _lang(user))

    async def test_settings(self, values):
        if not _HUB_OK:
            return {"ok": False, "level": "error", "message": HUB_TOO_OLD["en"]}
        base = str(values.get("GROCY_URL") or "").strip().rstrip("/")
        key = str(values.get("GROCY_API_KEY") or "").strip()
        if not base.startswith(("http://", "https://")):
            return {"ok": False, "level": "error", "message": "The Grocy address must start with http:// or https://"}
        if not key:
            return {"ok": False, "level": "error", "message": "The API key is missing"}
        list_id = _int(values.get("GROCY_SHOPPING_LIST_ID"), 1, 1, 10**9)
        try:
            response = await _request(base, key, "GET", "/api/system/info")
        except GrocyError:
            return {"ok": False, "level": "error", "message": "Grocy is not reachable"}
        if response.status_code in (401, 403):
            return {"ok": False, "level": "error", "message": "Grocy rejected the API key"}
        if response.status_code != 200:
            return {"ok": False, "level": "error", "message": f"Grocy answers with error {response.status_code}"}
        try:
            info = response.json()
            version = str(info["grocy_version"]["Version"])
        except (ValueError, KeyError, TypeError):
            return {"ok": False, "level": "error", "message": "This does not look like Grocy (unexpected answer)"}
        try:
            lists = await _get_json(base, key, "/api/objects/shopping_lists")
            known = {str(row.get("id")) for row in lists if isinstance(row, dict)}
        except (GrocyError, TypeError):
            return {"ok": True, "level": "warn",
                    "message": f"Connected, Grocy {version}. The shopping list could not be checked."}
        if str(list_id) not in known:
            return {"ok": False, "level": "error",
                    "message": f"Connected, Grocy {version}, but shopping list {list_id} does not exist"}
        return {"ok": True, "level": "ok", "message": f"Connected, Grocy {version}"}

    # --- daily job ---

    async def scheduled(self, db) -> None:
        if not self.is_configured():
            return
        base, key = _base(), _key()
        today = date.today()
        if _flag(get_setting("GROCY_NOTIFY"), True) and datetime.now().hour >= _int(get_setting("GROCY_NOTIFY_HOUR"), 8, 0, 23):
            await self._notify_due(db, base, key, today)
        if _flag(get_setting("GROCY_ADD_MISSING"), False):
            await self._add_missing(base, key, today)

    async def _notify_due(self, db, base: str, key: str, today: date) -> None:
        try:
            volatile = await fetch_volatile(base, key, _due_days())
        except GrocyError:
            return
        state = _load_state()
        cutoff = today.toordinal() - 7
        notified = {k: v for k, v in state["notified"].items()
                    if (_parse_day(k.rsplit("|", 1)[-1]) or today).toordinal() >= cutoff}
        fresh = [e for e in volatile["due"] if e["due"] and f"{e['id']}|{e['due']}" not in notified]
        if not fresh:
            return
        sent = False
        for lang, user_ids in self._parents_by_language(db).items():
            de = lang == "de"
            names = _join([_with_when(e, today, lang) for e in fresh], lang, 300)
            title = "Vorräte laufen bald ab" if de else "Stock expiring soon"
            result = await notify("grocy", user_ids=user_ids, title=title, body=names, data={"route": "/shopping"})
            sent = sent or bool(result.get("sent"))
        if sent:  # nothing sent (no recipients, rate limit): try again at the next run
            for entry in fresh:
                notified[f"{entry['id']}|{entry['due']}"] = today.isoformat()
            state["notified"] = notified
            _save_state(state)

    @staticmethod
    def _parents_by_language(db) -> Dict[str, List[int]]:
        from app.models.user import User

        groups: Dict[str, List[int]] = {}
        for user in db.query(User).filter(User.is_active == True).order_by(User.id).all():  # noqa: E712
            if (user.role or "").lower() in ("admin", "partner") or user.is_admin:
                groups.setdefault(_lang(user), []).append(user.id)
        return groups

    async def _add_missing(self, base: str, key: str, today: date) -> None:
        state = _load_state()
        if state["missing_day"] == today.isoformat():
            return
        try:
            response = await _request(base, key, "POST", "/api/stock/shoppinglist/add-missing-products",
                                      json_body={"list_id": _list_id()})
        except GrocyError:
            return
        if response.status_code in (200, 204):
            state["missing_day"] = today.isoformat()
            _save_state(state)
        else:
            logger.warning("Grocy: adding missing products failed with status %s", response.status_code)
