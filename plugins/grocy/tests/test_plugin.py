"""Tests for the Grocy plugin. Run with the hub's backend on the path:

    DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest

Grocy is simulated with respx (realistic answers of Grocy 4.x, written from its API documentation). The hub's own
notification service is the only thing replaced besides Grocy: ``notify`` is recorded, and its answer is the
hub's documented ``{"sent", ...}`` shape. What gets sent, and when, is decided by the plugin.
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fh_plugin_grocy.plugin as mod
from app.config import settings as hub_settings
from app.models.user import User
from app.plugins.manifest import validate_manifest
from fh_plugin_grocy.plugin import GrocyPlugin

ROOT = Path(__file__).resolve().parents[1]
GROCY = "https://grocy.example.org"
KEY = "test-api-key"
TODAY = date.today()


def day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


class Clock(datetime):
    hour_now = 9

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 20, cls.hour_now, 0)


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("GROCY_URL", GROCY + "/")
    monkeypatch.setenv("GROCY_API_KEY", KEY)
    for k in ("GROCY_SHOPPING_LIST_ID", "GROCY_DUE_DAYS", "GROCY_NOTIFY", "GROCY_NOTIFY_HOUR", "GROCY_ADD_MISSING"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(hub_settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(mod, "datetime", Clock)  # the job's wall clock; 09:00 unless a test says otherwise
    Clock.hour_now = 9
    mod.reset_cache()


@pytest.fixture
def grocy():
    with respx.mock(using="httpx", assert_all_called=False) as service:
        yield service


def run(coro):
    return asyncio.run(coro)


PRODUCTS = [
    {"id": "3", "name": "Milk", "active": "1", "qu_id_purchase": "2"},
    {"id": "4", "name": "Yoghurt", "active": "1", "qu_id_purchase": "1"},
    {"id": "9", "name": "Old thing", "active": "0", "qu_id_purchase": "1"},
]
UNITS = [{"id": "1", "name": "Piece", "name_plural": "Pieces"}, {"id": "2", "name": "Bottle", "name_plural": "Bottles"}]


def row(id, note=None, product_id=None, amount="1", done="0", list_id="1", qu_id=None):
    return {"id": str(id), "product_id": product_id, "note": note, "amount": amount, "shopping_list_id": list_id,
            "done": done, "qu_id": qu_id, "row_created_timestamp": "2026-10-19 18:02:11", "userfields": None}


def mock_catalog(grocy):
    grocy.get(f"{GROCY}/api/objects/products").mock(return_value=httpx.Response(200, json=PRODUCTS))
    grocy.get(f"{GROCY}/api/objects/quantity_units").mock(return_value=httpx.Response(200, json=UNITS))


def connector():
    return GrocyPlugin().shopping_connector(None)


# --- manifest and wiring ---


def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = GrocyPlugin()
    assert plugin.id == manifest["id"] == "grocy"
    assert manifest["min_hub_version"] == "1.0.0-beta.3" and manifest["license"] == "MIT"
    assert manifest["entry_point"] == "fh_plugin_grocy.plugin:GrocyPlugin"
    by_key = {s["key"]: s for s in manifest["settings"]}
    assert {s.key for s in plugin.settings} == set(by_key)
    assert by_key["GROCY_API_KEY"]["secret"] is True and by_key["GROCY_API_KEY"]["required"] is True
    assert by_key["GROCY_URL"]["required"] is True and by_key["GROCY_URL"]["type"] == "url"
    assert [k for k, s in by_key.items() if s["required"]] == ["GROCY_URL", "GROCY_API_KEY"]
    assert set(plugin.capabilities) <= set(manifest["capabilities"]) == {"shopping"}
    defaults = {k: str(s["default"]).lower() for k, s in by_key.items() if s.get("default") is not None}  # the hub normalises to text
    assert defaults == {"GROCY_SHOPPING_LIST_ID": "1", "GROCY_DUE_DAYS": "2", "GROCY_NOTIFY": "true",
                        "GROCY_NOTIFY_HOUR": "8", "GROCY_ADD_MISSING": "false"}


def test_plugin_needs_url_and_key(monkeypatch):
    plugin = GrocyPlugin()
    assert plugin.is_configured()
    monkeypatch.delenv("GROCY_API_KEY")
    assert not plugin.is_configured() and plugin.missing_settings() == ["GROCY_API_KEY"]
    assert plugin.shopping_connector(None) is None


def test_guard_for_hubs_without_the_hooks(monkeypatch, grocy):
    monkeypatch.setattr(mod, "_HUB_OK", False)
    plugin = GrocyPlugin()
    assert plugin.is_configured() is False
    assert "1.0.0-beta.3" in run(plugin.check())["message"]
    assert plugin.shopping_connector(None) is None
    assert run(plugin.today_cards(object(), None)) == []
    assert run(plugin.scheduled(None)) is None
    result = run(plugin.test_settings({"GROCY_URL": GROCY, "GROCY_API_KEY": KEY}))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]
    assert not grocy.calls  # nothing was sent to Grocy


def test_import_guard_survives_a_missing_hub_module():
    """The module itself must import on an old hub: simulate it in a fresh interpreter."""
    import subprocess
    import sys
    code = (
        "import sys, types, importlib.util, pathlib\n"
        # an old hub: app.plugins exists without the beta.3 modules (api, sources)
        "spec = importlib.util.find_spec('app')\n"
        "pkg = types.ModuleType('app.plugins'); pkg.__path__ = [str(pathlib.Path(spec.submodule_search_locations[0]) / 'plugins')]\n"
        "sys.modules['app.plugins'] = pkg\n"
        "for name in ('app.plugins.api', 'app.plugins.sources'): sys.modules[name] = None\n"
        "from app.plugins.base import Plugin, Setting, get_setting\n"
        "pkg.Plugin, pkg.Setting, pkg.get_setting = Plugin, Setting, get_setting\n"
        "import fh_plugin_grocy.plugin as m\n"
        "p = m.GrocyPlugin()\n"
        "assert m._HUB_OK is False and p.is_configured() is False\n"
    )
    import os
    env = {**os.environ, "GROCY_URL": GROCY, "GROCY_API_KEY": KEY,
           "PYTHONPATH": os.pathsep.join(sys.path)}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
    assert done.returncode == 0, done.stderr


# --- shopping connector ---


def test_list_maps_products_free_text_and_status(grocy):
    mock_catalog(grocy)
    route = grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[
        row(1, product_id="3", amount="2", qu_id="2"),
        row(2, note="Birthday candles"),
        row(3, product_id="4", amount="6"),
        row(4, note="Bread", done="1"),
        row(5, product_id="3", amount="1", note="the green cap", qu_id=None),
        row(6, note="Other list item", list_id="2"),
    ]))
    items = run(connector().list_items())
    request = route.calls.last.request
    assert request.headers["GROCY-API-KEY"] == KEY
    assert "query%5B%5D=shopping_list_id%3D1" in str(request.url) or "query[]=shopping_list_id=1" in str(request.url)
    assert items == [
        {"uid": "1", "summary": "Milk", "status": "needs_action", "description": "2 Bottles"},
        {"uid": "2", "summary": "Birthday candles", "status": "needs_action"},
        {"uid": "3", "summary": "Yoghurt", "status": "needs_action", "description": "6 Pieces"},
        {"uid": "4", "summary": "Bread", "status": "completed"},
        {"uid": "5", "summary": "Milk", "status": "needs_action", "description": "the green cap"},
    ]


def test_list_uses_the_configured_list_id(grocy, monkeypatch):
    monkeypatch.setenv("GROCY_SHOPPING_LIST_ID", "2")
    grocy.get(f"{GROCY}/api/objects/quantity_units").mock(return_value=httpx.Response(200, json=UNITS))
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[
        row(6, note="Other list item", list_id="2"), row(1, note="First list")]))
    assert [i["summary"] for i in run(connector().list_items())] == ["Other list item"]


def test_list_errors_raise_for_the_hub_to_report_unavailable(grocy):
    for answer in (httpx.Response(500), httpx.Response(401), httpx.Response(200, text="<html>"),
                   httpx.Response(200, json={"a": 1}), httpx.ConnectError("down"), httpx.ReadTimeout("slow")):
        grocy.get(f"{GROCY}/api/objects/shopping_list").mock(side_effect=[answer])
        with pytest.raises(mod.GrocyError) as err:
            run(connector().list_items())
        assert KEY not in str(err.value) and "grocy.example.org" not in str(err.value)


def test_list_survives_a_failing_unit_lookup(grocy):
    grocy.get(f"{GROCY}/api/objects/quantity_units").mock(return_value=httpx.Response(500))
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[row(2, note="Candles")]))
    assert run(connector().list_items())[0]["summary"] == "Candles"


def test_add_free_text(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[]))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={"created_object_id": 12}))
    assert run(connector().add("Candles", 1)) is True
    assert json.loads(post.calls.last.request.content) == {"shopping_list_id": 1, "amount": 1, "note": "Candles"}


def test_add_matches_a_product_case_insensitively_with_amount(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[]))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={"created_object_id": 13}))
    assert run(connector().add("  mILK ", None, "2")) is True
    assert json.loads(post.calls.last.request.content) == {"shopping_list_id": 1, "amount": 2, "product_id": 3}


def test_add_text_description_goes_into_the_note(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[]))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={}))
    run(connector().add("Milk", None, "lactose free"))
    run(connector().add("Flour", None, "500 g"))
    bodies = [json.loads(c.request.content) for c in post.calls]
    assert bodies[0] == {"shopping_list_id": 1, "amount": 1, "product_id": 3, "note": "lactose free"}
    assert bodies[1] == {"shopping_list_id": 1, "amount": 1, "note": "Flour (500 g)"}  # "500 g" is not a bare number


def test_inactive_products_are_not_matched(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[]))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={}))
    run(connector().add("Old thing", None))
    assert json.loads(post.calls.last.request.content)["note"] == "Old thing"


def test_add_does_not_create_a_duplicate_of_an_open_entry(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[
        row(1, product_id="3"), row(2, note="Candles")]))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={}))
    assert run(connector().add("milk", None)) is True
    assert run(connector().add("CANDLES", None)) is True
    assert not post.called


def test_add_unchecks_a_checked_duplicate(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[row(7, note="Bread", done="1")]))
    put = grocy.put(f"{GROCY}/api/objects/shopping_list/7").mock(return_value=httpx.Response(204))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={}))
    assert run(connector().add("bread", None)) is True
    assert json.loads(put.calls.last.request.content) == {"done": 0} and not post.called


def test_add_failure_raises_and_blank_is_refused(grocy):
    mock_catalog(grocy)
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[]))
    grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(400, json={"error_message": "x"}))
    with pytest.raises(mod.GrocyError):
        run(connector().add("Candles", None))
    assert run(connector().add("   ", None)) is False


def test_add_works_without_the_product_list(grocy):
    grocy.get(f"{GROCY}/api/objects/products").mock(return_value=httpx.Response(500))
    grocy.get(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json=[]))
    post = grocy.post(f"{GROCY}/api/objects/shopping_list").mock(return_value=httpx.Response(200, json={}))
    assert run(connector().add("Milk", None)) is True
    assert json.loads(post.calls.last.request.content)["note"] == "Milk"


@pytest.mark.parametrize("done, value", [(True, 1), (False, 0)])
def test_check_and_uncheck(grocy, done, value):
    put = grocy.put(f"{GROCY}/api/objects/shopping_list/5").mock(return_value=httpx.Response(204))
    assert run(connector().set_done("5", done)) is True
    assert json.loads(put.calls.last.request.content) == {"done": value}
    assert put.calls.last.request.headers["GROCY-API-KEY"] == KEY


def test_check_and_delete_not_found_and_bad_ids(grocy):
    grocy.put(f"{GROCY}/api/objects/shopping_list/99").mock(return_value=httpx.Response(404))
    grocy.delete(f"{GROCY}/api/objects/shopping_list/99").mock(return_value=httpx.Response(404))
    assert run(connector().set_done("99", True)) is False
    assert run(connector().delete("99")) is False
    assert run(connector().delete("../stock")) is False  # never builds a URL from a non-numeric uid
    assert run(connector().set_done("1/2", True)) is False
    assert not [c for c in grocy.calls if "stock" in str(c.request.url)]


def test_delete_and_server_error(grocy):
    grocy.delete(f"{GROCY}/api/objects/shopping_list/5").mock(return_value=httpx.Response(204))
    assert run(connector().delete("5")) is True
    grocy.delete(f"{GROCY}/api/objects/shopping_list/6").mock(return_value=httpx.Response(500))
    with pytest.raises(mod.GrocyError):
        run(connector().delete("6"))
    grocy.put(f"{GROCY}/api/objects/shopping_list/6").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(mod.GrocyError):
        run(connector().set_done("6", True))


def test_rename_free_text_only(grocy):
    grocy.get(f"{GROCY}/api/objects/shopping_list/2").mock(return_value=httpx.Response(200, json=row(2, note="Candles")))
    grocy.get(f"{GROCY}/api/objects/shopping_list/1").mock(return_value=httpx.Response(200, json=row(1, product_id="3")))
    put = grocy.put(f"{GROCY}/api/objects/shopping_list/2").mock(return_value=httpx.Response(204))
    assert run(connector().rename("2", "Candles, 20")) is True
    assert json.loads(put.calls.last.request.content) == {"note": "Candles, 20"}
    assert run(connector().rename("1", "Whole milk")) is False


# --- Today card ---


def volatile(due=(), overdue=(), expired=(), missing=()):
    def product(pid, name, best_before):
        return {"id": str(pid), "amount": "1", "amount_aggregated": "1", "amount_opened": "0",
                "best_before_date": best_before, "is_aggregated_amount": "0",
                "product": {"id": str(pid), "name": name, "qu_id_stock": "1"}}
    return {"due_products": [product(*d) for d in due], "overdue_products": [product(*d) for d in overdue],
            "expired_products": [product(*d) for d in expired],
            "missing_products": [{"id": str(m[0]), "name": m[1], "amount_missing": "2", "is_partly_in_stock": "0"} for m in missing]}


def mock_volatile(grocy, payload):
    return grocy.get(f"{GROCY}/api/stock/volatile").mock(return_value=httpx.Response(200, json=payload))


def test_card_expiring_and_missing_in_english(grocy):
    route = mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1)), (3, "Milk", day(2))], missing=[(5, "Eggs"), (6, "Rice"), (7, "Tea")]))
    cards = run(GrocyPlugin().today_cards(object(), None))
    assert route.calls.last.request.url.params["due_soon_days"] == "2"
    assert cards == [{"id": "stock", "title": "Grocy", "icon": "warning", "badge": "2", "lines": [
        {"text": "Expiring soon: Yoghurt (tomorrow), Milk (2 days)"}, {"text": "Missing: 3 products"}]}]


def test_card_in_german_orders_by_date_and_shows_past_dates(grocy):
    class De:
        language = "de"
    mock_volatile(grocy, volatile(due=[(3, "Milch", day(2)), (4, "Joghurt", day(0))], expired=[(8, "Käse", day(-3))], missing=[(5, "Eier")]))
    (card,) = run(GrocyPlugin().today_cards(De(), None))
    assert card["title"] == "Vorräte" and card["badge"] == "3"
    assert [l["text"] for l in card["lines"]] == [
        "Bald ablaufend: Joghurt (heute), Milch (in 2 Tagen)", "Abgelaufen: Käse", "Fehlt: 1 Produkt"]


def test_card_only_missing_shows_the_missing_count_as_badge(grocy):
    mock_volatile(grocy, volatile(missing=[(5, "Eggs")]))
    (card,) = run(GrocyPlugin().today_cards(object(), None))
    assert card["badge"] == "1" and card["icon"] == "shopping_cart" and card["lines"] == [{"text": "Missing: 1 product"}]


def test_no_card_when_nothing_is_relevant(grocy):
    mock_volatile(grocy, volatile())
    assert run(GrocyPlugin().today_cards(object(), None)) == []


def test_card_ignores_products_that_never_expire(grocy):
    mock_volatile(grocy, volatile(due=[(4, "Salt", "2999-12-31")]))
    (card,) = run(GrocyPlugin().today_cards(object(), None))
    assert card["lines"] == [{"text": "Expiring soon: Salt"}]


def test_card_line_stays_short_with_many_products(grocy):
    mock_volatile(grocy, volatile(due=[(i, f"Product number {i}", day(1)) for i in range(30)]))
    (card,) = run(GrocyPlugin().today_cards(object(), None))
    text = card["lines"][0]["text"]
    assert len(text) <= 160 and text.rsplit(" +", 1)[1].isdigit()


@pytest.mark.parametrize("answer", [httpx.Response(500), httpx.Response(200, text="nope"), httpx.Response(200, json=[1]),
                                    httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
def test_card_fails_softly(grocy, answer):
    grocy.get(f"{GROCY}/api/stock/volatile").mock(side_effect=[answer])
    assert run(GrocyPlugin().today_cards(object(), None)) == []


def test_card_garbage_rows_are_dropped(grocy):
    mock_volatile(grocy, {"due_products": ["x", {"product": {}}, None, {"best_before_date": day(1)}], "missing_products": 5})
    assert run(GrocyPlugin().today_cards(object(), None)) == []


def test_volatile_answers_are_cached_briefly(grocy):
    route = mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    plugin = GrocyPlugin()
    run(plugin.today_cards(object(), None))
    run(plugin.today_cards(object(), None))
    assert route.call_count == 1


def test_due_days_setting_is_used_and_clamped(grocy, monkeypatch):
    route = mock_volatile(grocy, volatile())
    monkeypatch.setenv("GROCY_DUE_DAYS", "5")
    run(GrocyPlugin().today_cards(object(), None))
    assert route.calls.last.request.url.params["due_soon_days"] == "5"
    monkeypatch.setenv("GROCY_DUE_DAYS", "garbage")
    mod.reset_cache()
    run(GrocyPlugin().today_cards(object(), None))
    assert route.calls.last.request.url.params["due_soon_days"] == "2"


# --- scheduled job ---


@pytest.fixture
def hub_db():
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    with sessionmaker(bind=engine)() as db:
        db.add_all([
            User(username="alex", email="a@x.test", hashed_password="x", role="admin", is_admin=True, language="en"),
            User(username="sam", email="s@x.test", hashed_password="x", role="partner", language="de"),
            User(username="mia", email="m@x.test", hashed_password="x", role="child", language="de"),
            User(username="old", email="o@x.test", hashed_password="x", role="partner", is_active=False),
        ])
        db.commit()
        yield db


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(plugin_id, **kwargs):
        calls.append({"plugin_id": plugin_id, **kwargs})
        return {"recipients": len(kwargs.get("user_ids") or []), "delivered": 1, "sent": True, "reason": None}

    monkeypatch.setattr(mod, "notify", fake_notify)
    return calls


def test_notifies_parents_once_per_product_and_date(grocy, hub_db, sent):
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1)), (3, "Milk", day(2))]))
    plugin = GrocyPlugin()
    run(plugin.scheduled(hub_db))
    assert len(sent) == 2  # one per language group of the parents: alex (en), sam (de); the child gets nothing
    by_title = {c["title"]: c for c in sent}
    assert by_title["Stock expiring soon"]["body"] == "Yoghurt (tomorrow), Milk (2 days)"
    assert by_title["Vorräte laufen bald ab"]["body"] == "Yoghurt (morgen), Milk (in 2 Tagen)"
    ids = {c["title"]: c["user_ids"] for c in sent}
    assert ids["Stock expiring soon"] == [1] and ids["Vorräte laufen bald ab"] == [2]
    state = json.loads((Path(hub_settings.data_dir) / "grocy_state.json").read_text())
    assert set(state["notified"]) == {f"4|{day(1)}", f"3|{day(2)}"}

    sent.clear()
    mod.reset_cache()
    run(plugin.scheduled(hub_db))
    assert sent == []  # same products, same dates: no second message


def test_a_new_product_or_a_new_date_is_notified_again(grocy, hub_db, sent):
    plugin = GrocyPlugin()
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    run(plugin.scheduled(hub_db))
    sent.clear()
    mod.reset_cache()
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1)), (4, "Yoghurt", day(2)), (6, "Cheese", day(1))]))
    run(plugin.scheduled(hub_db))
    bodies = " ".join(c["body"] for c in sent)
    assert "Cheese" in bodies and "Yoghurt (2 days)" in bodies and "Yoghurt (tomorrow)" not in bodies


def test_nothing_before_the_notify_hour(grocy, hub_db, sent, monkeypatch):
    Clock.hour_now = 7
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    run(GrocyPlugin().scheduled(hub_db))
    assert sent == []
    Clock.hour_now = 9
    run(GrocyPlugin().scheduled(hub_db))
    assert len(sent) == 2


def test_notify_hour_setting(grocy, hub_db, sent, monkeypatch):
    monkeypatch.setenv("GROCY_NOTIFY_HOUR", "18")
    Clock.hour_now = 17
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    run(GrocyPlugin().scheduled(hub_db))
    assert sent == []


def test_notifications_can_be_switched_off(grocy, hub_db, sent, monkeypatch):
    monkeypatch.setenv("GROCY_NOTIFY", "false")
    route = mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    run(GrocyPlugin().scheduled(hub_db))
    assert sent == [] and not route.called


def test_failed_delivery_is_retried_later(grocy, hub_db, monkeypatch):
    async def nobody(plugin_id, **kwargs):
        return {"recipients": 0, "delivered": 0, "sent": False, "reason": "rate_limited"}

    monkeypatch.setattr(mod, "notify", nobody)
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    run(GrocyPlugin().scheduled(hub_db))
    assert not (Path(hub_settings.data_dir) / "grocy_state.json").exists()


def test_old_state_entries_are_pruned_and_a_broken_state_file_is_ignored(grocy, hub_db, sent):
    path = Path(hub_settings.data_dir) / "grocy_state.json"
    path.write_text("{not json")
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1))]))
    run(GrocyPlugin().scheduled(hub_db))
    assert len(sent) == 2
    path.write_text(json.dumps({"notified": {f"1|{day(-30)}": "x", f"4|{day(1)}": "x"}}))
    sent.clear()
    mod.reset_cache()
    mock_volatile(grocy, volatile(due=[(4, "Yoghurt", day(1)), (6, "Cheese", day(1))]))
    run(GrocyPlugin().scheduled(hub_db))
    assert all("Yoghurt" not in c["body"] for c in sent) and sent  # 4|tomorrow was already notified
    assert set(json.loads(path.read_text())["notified"]) == {f"4|{day(1)}", f"6|{day(1)}"}  # 30-day-old entry pruned


def test_job_survives_grocy_down(grocy, hub_db, sent):
    grocy.get(f"{GROCY}/api/stock/volatile").mock(side_effect=httpx.ConnectError("down"))
    run(GrocyPlugin().scheduled(hub_db))
    assert sent == []


def test_add_missing_products_once_a_day(grocy, hub_db, sent, monkeypatch):
    monkeypatch.setenv("GROCY_ADD_MISSING", "true")
    monkeypatch.setenv("GROCY_SHOPPING_LIST_ID", "2")
    mock_volatile(grocy, volatile())
    post = grocy.post(f"{GROCY}/api/stock/shoppinglist/add-missing-products").mock(return_value=httpx.Response(204))
    run(GrocyPlugin().scheduled(hub_db))
    run(GrocyPlugin().scheduled(hub_db))
    assert post.call_count == 1
    assert json.loads(post.calls.last.request.content) == {"list_id": 2}


def test_add_missing_is_off_by_default_and_retried_after_failure(grocy, hub_db, sent, monkeypatch):
    mock_volatile(grocy, volatile())
    post = grocy.post(f"{GROCY}/api/stock/shoppinglist/add-missing-products").mock(return_value=httpx.Response(500))
    run(GrocyPlugin().scheduled(hub_db))
    assert not post.called
    monkeypatch.setenv("GROCY_ADD_MISSING", "true")
    run(GrocyPlugin().scheduled(hub_db))
    run(GrocyPlugin().scheduled(hub_db))
    assert post.call_count == 2  # a failed run does not count as done


def test_the_job_only_runs_for_a_configured_plugin(grocy, hub_db, sent, monkeypatch):
    monkeypatch.delenv("GROCY_URL")
    run(GrocyPlugin().scheduled(hub_db))
    assert not grocy.calls and GrocyPlugin().schedule_minutes == 30


# --- connection test ---


SYSTEM_INFO = {"grocy_version": {"Version": "4.2.0", "ReleaseDate": "2024-04-01"}, "php_version": "8.2.12", "sqlite_version": "3.41.2"}
LISTS = [{"id": "1", "name": "Shopping list"}, {"id": "2", "name": "Garden"}]


def test_connection_test_with_unsaved_values(grocy):
    info = grocy.get("https://typed.example.org/api/system/info").mock(return_value=httpx.Response(200, json=SYSTEM_INFO))
    grocy.get("https://typed.example.org/api/objects/shopping_lists").mock(return_value=httpx.Response(200, json=LISTS))
    result = run(GrocyPlugin().test_settings({"GROCY_URL": "https://typed.example.org/", "GROCY_API_KEY": "typed-key", "GROCY_SHOPPING_LIST_ID": "2"}))
    assert result == {"ok": True, "level": "ok", "message": "Connected, Grocy 4.2.0"}
    assert info.calls.last.request.headers["GROCY-API-KEY"] == "typed-key"
    assert not [c for c in grocy.calls if "grocy.example.org" in str(c.request.url)]  # saved values are not used


def test_connection_test_reports_a_missing_list(grocy):
    grocy.get(f"{GROCY}/api/system/info").mock(return_value=httpx.Response(200, json=SYSTEM_INFO))
    grocy.get(f"{GROCY}/api/objects/shopping_lists").mock(return_value=httpx.Response(200, json=LISTS))
    result = run(GrocyPlugin().test_settings({"GROCY_URL": GROCY, "GROCY_API_KEY": KEY, "GROCY_SHOPPING_LIST_ID": "7"}))
    assert result["ok"] is False and result["level"] == "error" and "list 7 does not exist" in result["message"]


def test_connection_test_default_list_is_one(grocy):
    grocy.get(f"{GROCY}/api/system/info").mock(return_value=httpx.Response(200, json=SYSTEM_INFO))
    grocy.get(f"{GROCY}/api/objects/shopping_lists").mock(return_value=httpx.Response(200, json=LISTS))
    assert run(GrocyPlugin().test_settings({"GROCY_URL": GROCY, "GROCY_API_KEY": KEY}))["ok"] is True


def test_connection_test_list_check_failing_is_a_warning(grocy):
    grocy.get(f"{GROCY}/api/system/info").mock(return_value=httpx.Response(200, json=SYSTEM_INFO))
    grocy.get(f"{GROCY}/api/objects/shopping_lists").mock(return_value=httpx.Response(500))
    result = run(GrocyPlugin().test_settings({"GROCY_URL": GROCY, "GROCY_API_KEY": KEY}))
    assert result["ok"] is True and result["level"] == "warn" and "Grocy 4.2.0" in result["message"]


@pytest.mark.parametrize("answer, text", [
    (httpx.Response(401, json={"error_message": "No valid api key"}), "rejected the API key"),
    (httpx.Response(403), "rejected the API key"),
    (httpx.Response(500), "error 500"),
    (httpx.Response(200, json={"hello": "world"}), "does not look like Grocy"),
    (httpx.Response(200, text="<html>login</html>"), "does not look like Grocy"),
    (httpx.ConnectError("down"), "not reachable"),
    (httpx.ReadTimeout("slow"), "not reachable"),
])
def test_connection_test_errors(grocy, answer, text):
    grocy.get(f"{GROCY}/api/system/info").mock(side_effect=[answer])
    result = run(GrocyPlugin().test_settings({"GROCY_URL": GROCY, "GROCY_API_KEY": KEY}))
    assert result["ok"] is False and result["level"] == "error" and text in result["message"]
    assert KEY not in result["message"] and "example.org" not in result["message"]


@pytest.mark.parametrize("values, text", [
    ({"GROCY_URL": "grocy.lan", "GROCY_API_KEY": KEY}, "http://"),
    ({"GROCY_URL": "", "GROCY_API_KEY": KEY}, "http://"),
    ({"GROCY_URL": GROCY, "GROCY_API_KEY": ""}, "API key is missing"),
    ({"GROCY_URL": GROCY}, "API key is missing"),
])
def test_connection_test_validates_input_before_any_request(grocy, values, text):
    result = run(GrocyPlugin().test_settings(values))
    assert result["ok"] is False and text in result["message"] and not grocy.calls


def test_every_request_has_a_timeout_below_eight_seconds():
    assert mod.TIMEOUT <= 8 and mod.CARD_TIMEOUT <= 8
