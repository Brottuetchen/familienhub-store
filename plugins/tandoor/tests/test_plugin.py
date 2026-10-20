"""Tests for the Tandoor plugin. Run with the hub's backend on the path:

    DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest

Tandoor is simulated with respx (responses shaped like Tandoor 1.5 / 2.x API answers); nothing real is called.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_user
from app.plugins.manifest import validate_manifest
from fh_plugin_tandoor import plugin as mod
from fh_plugin_tandoor.plugin import TandoorPlugin, TandoorUnavailable

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://tandoor.example.org"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

SEARCH = {
    "count": 3, "next": None, "previous": None,
    "results": [
        {"id": 12, "name": "Potato gratin", "description": "Creamy", "image": f"{BASE}/media/recipes/gratin.jpg",
         "keywords": [], "working_time": 20, "waiting_time": 40, "rating": None, "internal": True},
        {"id": 13, "name": "Lentil soup", "description": "", "image": None, "keywords": [],
         "working_time": 15, "waiting_time": 0},
        {"id": 14, "name": "Pancakes", "image": "/media/recipes/pancakes.png", "working_time": 0, "waiting_time": 0},
    ],
}

DETAIL = {
    "id": 12, "name": "Potato gratin", "description": "Creamy", "image": f"{BASE}/media/recipes/gratin.jpg",
    "servings": 4, "servings_text": "", "working_time": 20, "waiting_time": 40,
    "source_url": None, "internal": True,
    "steps": [
        {"id": 2, "name": "Bake", "instruction": "Pour over the cream.\n\nBake for {{ scale(40) }} minutes.", "order": 1,
         "ingredients": [
             {"id": 5, "food": {"id": 3, "name": "cream", "plural_name": None}, "unit": {"id": 2, "name": "ml", "plural_name": None},
              "amount": 200.0, "note": "", "order": 0, "is_header": False, "no_amount": False},
         ]},
        {"id": 1, "name": "Prepare", "instruction": "Slice the potatoes thinly.", "order": 0,
         "ingredients": [
             {"id": 6, "food": None, "unit": None, "amount": 0, "note": "For the base", "order": 0, "is_header": True, "no_amount": True},
             {"id": 7, "food": {"id": 1, "name": "potato", "plural_name": "potatoes"}, "unit": {"id": 1, "name": "kg", "plural_name": None},
              "amount": 1.5, "note": "waxy", "order": 1, "is_header": False, "no_amount": False},
             {"id": 8, "food": {"id": 4, "name": "garlic clove", "plural_name": "garlic cloves"}, "unit": None,
              "amount": 2.0, "note": "", "order": 2, "is_header": False, "no_amount": False},
             {"id": 9, "food": {"id": 5, "name": "salt", "plural_name": None}, "unit": None,
              "amount": 0, "note": "to taste", "order": 3, "is_header": False, "no_amount": True},
             {"id": 10, "food": {"id": 6, "name": "nutmeg", "plural_name": None}, "unit": {"id": 3, "name": "pinch", "plural_name": "pinches"},
              "amount": 0.25, "note": "", "order": 4, "is_header": False, "no_amount": False},
         ]},
        {"id": 3, "name": "", "instruction": "", "order": 2, "ingredients": []},
    ],
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("TANDOOR_URL", BASE)
    monkeypatch.setenv("TANDOOR_TOKEN", "tda_test-token")
    mod.reset_caches()
    yield
    mod.reset_caches()


@pytest.fixture
def tandoor():
    with respx.mock(using="httpx", assert_all_called=False) as service:
        service.route(host="testserver").pass_through()
        yield service


@pytest.fixture
def client():
    app = FastAPI()
    for router in TandoorPlugin().routers():
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


def run(coro):
    return asyncio.run(coro)


# --- manifest and wiring ----------------------------------------------------------------------------------------


def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = TandoorPlugin()
    assert plugin.id == manifest["id"] == "tandoor"
    assert manifest["entry_point"] == "fh_plugin_tandoor.plugin:TandoorPlugin"
    assert manifest["min_hub_version"] == "1.0.0-beta.3" and manifest["version"] == "1.0.0"
    assert {s.key for s in plugin.settings} == {s["key"] for s in manifest["settings"]}
    assert {s["key"]: (s["type"], s["required"]) for s in manifest["settings"]} == {
        "TANDOOR_URL": ("url", True), "TANDOOR_TOKEN": ("password", True)}
    assert next(s for s in plugin.settings if s.key == "TANDOOR_TOKEN").secret
    assert set(plugin.capabilities) == set(manifest["capabilities"]) == {"meal_planning"}
    for router in plugin.routers():
        assert all(r.path.startswith("/api/plugins/tandoor/") for r in router.routes)


def test_plugin_is_a_recipe_source_of_the_hub():
    from app.plugins.sources import overrides

    plugin = TandoorPlugin()
    assert overrides(plugin, "recipe_search") and overrides(plugin, "recipe")
    assert not overrides(plugin, "recipe_to_shopping")  # the hub's own shopping list gets the ingredients


def test_needs_url_and_token(monkeypatch):
    assert TandoorPlugin().is_configured()
    monkeypatch.delenv("TANDOOR_TOKEN")
    assert not TandoorPlugin().is_configured()
    assert TandoorPlugin().missing_settings() == ["TANDOOR_TOKEN"]


# --- compatibility guard ----------------------------------------------------------------------------------------


def test_guard_on_old_hub(monkeypatch, tandoor):
    monkeypatch.setattr(mod, "compat_problem", lambda: mod.GUARD_TEXT)
    plugin = TandoorPlugin()
    assert plugin.is_configured() is False and plugin.is_set_up() is False
    assert run(plugin.recipe_search("gratin", 10)) == []
    assert run(plugin.recipe("12")) == {}
    result = run(plugin.test_settings({"TANDOOR_URL": BASE, "TANDOOR_TOKEN": "x"}))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]
    assert "1.0.0-beta.3" in run(plugin.check())["message"]
    assert not tandoor.calls  # nothing was contacted


def test_guard_detects_missing_hub_modules(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "app.plugins.api", None)  # import fails like on a hub without the module
    assert mod.compat_problem() == mod.GUARD_TEXT
    assert TandoorPlugin().is_configured() is False
    monkeypatch.undo()
    assert mod.compat_problem() is None


# --- search -----------------------------------------------------------------------------------------------------


def test_search_mapping(tandoor):
    route = tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    items = run(TandoorPlugin().recipe_search("gratin", 30))
    assert [i["id"] for i in items] == ["12", "13", "14"]
    first = items[0]
    assert first == {"id": "12", "name": "Potato gratin", "thumbnail": "/api/plugins/tandoor/image/12",
                     "url": f"{BASE}/view/recipe/12", "minutes": 60}
    assert items[1]["thumbnail"] is None and items[1]["minutes"] == 15
    assert items[2]["thumbnail"] == "/api/plugins/tandoor/image/14" and items[2]["minutes"] is None
    request = route.calls.last.request
    assert request.url.params["query"] == "gratin" and request.url.params["page_size"] == "30"
    assert request.headers["authorization"] == "Bearer tda_test-token"


def test_search_is_cached(tandoor):
    route = tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    plugin = TandoorPlugin()
    run(plugin.recipe_search("gratin", 30))
    run(plugin.recipe_search("Gratin", 30))
    assert route.call_count == 1


def test_search_skips_malformed_rows_and_respects_limit(tandoor):
    rows = [{"id": 1, "name": "A"}, "junk", {"id": None, "name": "B"}, {"id": 3, "name": " "}, {"id": "x", "name": "C"},
            {"id": 5, "name": "D"}, {"id": 6, "name": "E"}]
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json={"count": 7, "results": rows}))
    assert [i["id"] for i in run(TandoorPlugin().recipe_search("abc", 2))] == ["1", "5"]


@pytest.mark.parametrize("reply", [
    httpx.Response(500), httpx.Response(401, json={"detail": "bad"}), httpx.Response(200, text="<html>"),
    httpx.Response(200, json={"count": 1, "results": "no"}), httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
def test_search_failure_raises_a_clean_error(tandoor, reply):
    tandoor.get(f"{BASE}/api/recipe/").mock(side_effect=[reply] if isinstance(reply, Exception) else None,
                                            return_value=None if isinstance(reply, Exception) else reply)
    with pytest.raises(TandoorUnavailable) as err:
        run(TandoorPlugin().recipe_search("gratin", 10))
    assert "tda_test-token" not in str(err.value) and "example.org" not in str(err.value)


def test_search_through_the_hub_adapter_reports_unavailable(tandoor):
    from app.plugins.sources import PluginRecipes, PluginSourceError

    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(502))
    with pytest.raises(PluginSourceError):
        run(PluginRecipes(TandoorPlugin()).search("gratin"))
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    result = run(PluginRecipes(TandoorPlugin()).search("gratin"))
    assert result["total"] == 3 and result["items"][0]["minutes"] == 60


# --- recipe detail ----------------------------------------------------------------------------------------------


def test_recipe_detail_mapping(tandoor):
    route = tandoor.get(f"{BASE}/api/recipe/12/").mock(return_value=httpx.Response(200, json=DETAIL))
    r = run(TandoorPlugin().recipe("12"))
    assert r["id"] == "12" and r["name"] == "Potato gratin"
    assert r["image"] == "/api/plugins/tandoor/image/12"
    assert r["url"] == f"{BASE}/view/recipe/12"
    assert r["servings"] == 4 and r["minutes"] == 60
    assert r["ingredients"] == [
        "1.5 kg potatoes (waxy)",       # plural name, note, header skipped
        "2 garlic cloves",
        "salt (to taste)",              # no_amount
        "0.25 pinches nutmeg",               # fractional amount uses the plural
        "200 ml cream",
    ]
    assert r["steps"] == ["Slice the potatoes thinly.", "Pour over the cream. Bake for 40 minutes."]  # ordered, empty skipped
    assert route.calls.last.request.headers["authorization"] == "Bearer tda_test-token"


def test_recipe_is_cached(tandoor):
    route = tandoor.get(f"{BASE}/api/recipe/12/").mock(return_value=httpx.Response(200, json=DETAIL))
    plugin = TandoorPlugin()
    run(plugin.recipe("12"))
    run(plugin.recipe("12"))
    assert route.call_count == 1


def test_recipe_singular_when_amount_is_one(tandoor):
    detail = {"id": 1, "name": "X", "servings": 0, "steps": [{"instruction": "Go", "ingredients": [
        {"food": {"name": "onion", "plural_name": "onions"}, "unit": None, "amount": 1, "is_header": False}]}]}
    tandoor.get(f"{BASE}/api/recipe/1/").mock(return_value=httpx.Response(200, json=detail))
    r = run(TandoorPlugin().recipe("1"))
    assert r["ingredients"] == ["1 onion"] and r["servings"] is None and r["minutes"] is None and r["image"] is None


def test_recipe_with_embedded_sub_recipe(tandoor):
    detail = {"id": 2, "name": "Cake", "steps": [{"order": 0, "instruction": "Mix", "ingredients": [], "step_recipe_data": {
        "steps": [{"order": 0, "instruction": "Whisk the base", "ingredients": [
            {"food": {"name": "egg", "plural_name": "eggs"}, "amount": 3, "unit": None}]}]}}]}
    tandoor.get(f"{BASE}/api/recipe/2/").mock(return_value=httpx.Response(200, json=detail))
    r = run(TandoorPlugin().recipe("2"))
    assert r["ingredients"] == ["3 eggs"] and r["steps"] == ["Whisk the base", "Mix"]


def test_recipe_not_found_is_empty_and_hub_says_not_found(tandoor):
    from app.plugins.sources import PluginRecipes, PluginSourceError

    tandoor.get(f"{BASE}/api/recipe/99/").mock(return_value=httpx.Response(404, json={"detail": "Not found."}))
    assert run(TandoorPlugin().recipe("99")) == {}
    with pytest.raises(PluginSourceError):
        run(PluginRecipes(TandoorPlugin()).recipe("99"))


@pytest.mark.parametrize("reply", [httpx.Response(500), httpx.Response(403), httpx.Response(200, json=[1]),
                                   httpx.Response(200, json={"id": 1, "name": ""}), httpx.ConnectError("x")])
def test_recipe_failure_raises_a_clean_error(tandoor, reply):
    tandoor.get(f"{BASE}/api/recipe/5/").mock(side_effect=[reply] if isinstance(reply, Exception) else None,
                                              return_value=None if isinstance(reply, Exception) else reply)
    with pytest.raises(TandoorUnavailable):
        run(TandoorPlugin().recipe("5"))


@pytest.mark.parametrize("bad", ["", "abc", "1/../2", "12?x=1", "../12"])
def test_recipe_rejects_non_numeric_ids_without_a_request(tandoor, bad):
    assert run(TandoorPlugin().recipe(bad)) == {}
    assert not tandoor.calls


def test_hub_adapter_shapes_detail(tandoor):
    from app.plugins.sources import PluginRecipes

    tandoor.get(f"{BASE}/api/recipe/12/").mock(return_value=httpx.Response(200, json=DETAIL))
    r = run(PluginRecipes(TandoorPlugin()).recipe("12"))
    assert r["servings"] == 4 and r["minutes"] == 60 and len(r["ingredients"]) == 5 and len(r["steps"]) == 2


# --- images -----------------------------------------------------------------------------------------------------


def test_image_only_for_returned_ids(client, tandoor):
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    media = tandoor.get(f"{BASE}/media/recipes/gratin.jpg").mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"}))
    tandoor.get(f"{BASE}/api/recipe/12/").mock(return_value=httpx.Response(404))
    assert client.get("/api/plugins/tandoor/image/12").status_code == 404  # unknown to Tandoor: nothing to serve
    assert not media.called
    mod.reset_caches()
    run(TandoorPlugin().recipe_search("gratin", 30))
    r = client.get("/api/plugins/tandoor/image/12")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert "private" in r.headers["cache-control"]
    assert media.calls.last.request.headers["authorization"] == "Bearer tda_test-token"
    tandoor.get(f"{BASE}/api/recipe/13/").mock(return_value=httpx.Response(200, json={"id": 13, "name": "Lentil soup", "image": None}))
    assert client.get("/api/plugins/tandoor/image/13").status_code == 404  # returned, but has no image
    tandoor.get(f"{BASE}/api/recipe/999/").mock(return_value=httpx.Response(404))
    assert client.get("/api/plugins/tandoor/image/999").status_code == 404


def test_image_for_relative_path_and_from_recipe_detail(client, tandoor):
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    tandoor.get(f"{BASE}/media/recipes/pancakes.png").mock(return_value=httpx.Response(200, content=PNG))
    run(TandoorPlugin().recipe_search("pancakes", 30))
    assert client.get("/api/plugins/tandoor/image/14").status_code == 200
    tandoor.get(f"{BASE}/api/recipe/12/").mock(return_value=httpx.Response(200, json=DETAIL))
    tandoor.get(f"{BASE}/media/recipes/gratin.jpg").mock(return_value=httpx.Response(200, content=PNG))
    run(TandoorPlugin().recipe("12"))
    assert client.get("/api/plugins/tandoor/image/12").status_code == 200


def test_image_url_on_foreign_host_is_not_followed(client, tandoor):
    """A recipe whose image points elsewhere: only the path is used, always on the configured Tandoor address."""
    rows = {"count": 1, "results": [{"id": 7, "name": "Evil", "image": "https://evil.example.net/media/x.png?v=2"}]}
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=rows))
    evil = tandoor.get(host="evil.example.net").mock(return_value=httpx.Response(200, content=PNG))
    ours = tandoor.get(f"{BASE}/media/x.png").mock(return_value=httpx.Response(200, content=PNG))
    run(TandoorPlugin().recipe_search("evil", 5))
    assert client.get("/api/plugins/tandoor/image/7").status_code == 200
    assert ours.called and not evil.called


@pytest.mark.parametrize("image", ["javascript:alert(1)", "/media/../../etc/passwd", "relative.png", 5])
def test_unsafe_image_values_are_dropped(tandoor, image):
    rows = {"count": 1, "results": [{"id": 7, "name": "X", "image": image}]}
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=rows))
    assert run(TandoorPlugin().recipe_search("xyz", 5))[0]["thumbnail"] is None


def test_image_errors(client, tandoor):
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    run(TandoorPlugin().recipe_search("gratin", 30))
    media = tandoor.get(f"{BASE}/media/recipes/gratin.jpg")
    media.mock(return_value=httpx.Response(404))
    assert client.get("/api/plugins/tandoor/image/12").status_code == 404
    media.mock(return_value=httpx.Response(500))
    assert client.get("/api/plugins/tandoor/image/12").status_code == 502
    media.mock(side_effect=httpx.ConnectError("down"))
    assert client.get("/api/plugins/tandoor/image/12").status_code == 502
    media.mock(return_value=httpx.Response(200, content=b"<html>not an image</html>"))
    assert client.get("/api/plugins/tandoor/image/12").status_code == 415


def test_image_whitelist_follows_the_configured_address(client, tandoor, monkeypatch):
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json=SEARCH))
    run(TandoorPlugin().recipe_search("gratin", 30))
    monkeypatch.setenv("TANDOOR_URL", "https://other.example.org")
    tandoor.get("https://other.example.org/api/recipe/12/").mock(return_value=httpx.Response(404))
    old_media = tandoor.get(f"{BASE}/media/recipes/gratin.jpg")
    assert client.get("/api/plugins/tandoor/image/12").status_code == 404
    assert not old_media.called


def test_image_of_unknown_id_is_looked_up_once(client, tandoor):
    """After a restart the whitelist is empty: the recipe is fetched from Tandoor and its image path is used."""
    detail = tandoor.get(f"{BASE}/api/recipe/12/").mock(return_value=httpx.Response(200, json=DETAIL))
    media = tandoor.get(f"{BASE}/media/recipes/gratin.jpg").mock(return_value=httpx.Response(200, content=PNG))
    r = client.get("/api/plugins/tandoor/image/12")
    assert r.status_code == 200 and r.content == PNG
    assert detail.calls.last.request.headers["authorization"] == "Bearer tda_test-token"
    assert client.get("/api/plugins/tandoor/image/12").status_code == 200
    assert detail.call_count == 1 and media.call_count == 2  # registered now: no second lookup


def test_image_lookup_failures_are_404_and_negatively_cached(client, tandoor):
    gone = tandoor.get(f"{BASE}/api/recipe/77/").mock(return_value=httpx.Response(404))
    assert client.get("/api/plugins/tandoor/image/77").status_code == 404
    assert client.get("/api/plugins/tandoor/image/77").status_code == 404
    assert gone.call_count == 1
    down = tandoor.get(f"{BASE}/api/recipe/78/").mock(side_effect=httpx.ConnectError("x"))
    assert client.get("/api/plugins/tandoor/image/78").status_code == 404
    assert client.get("/api/plugins/tandoor/image/78").status_code == 404
    assert down.call_count == 1
    noimg = tandoor.get(f"{BASE}/api/recipe/79/").mock(return_value=httpx.Response(200, json={"id": 79, "name": "x", "image": None}))
    assert client.get("/api/plugins/tandoor/image/79").status_code == 404
    assert client.get("/api/plugins/tandoor/image/79").status_code == 404
    assert noimg.call_count == 1


def test_image_lookup_expires_negative_cache(client, tandoor, monkeypatch):
    route = tandoor.get(f"{BASE}/api/recipe/77/").mock(return_value=httpx.Response(404))
    assert client.get("/api/plugins/tandoor/image/77").status_code == 404
    monkeypatch.setattr(mod, "MISS_TTL", 0.0)
    client.get("/api/plugins/tandoor/image/77")
    assert route.call_count == 2


def test_image_lookup_ignores_foreign_host_and_non_numeric_ids(client, tandoor):
    tandoor.get(f"{BASE}/api/recipe/8/").mock(return_value=httpx.Response(200, json={"id": 8, "name": "E", "image": "https://evil.example.net/media/y.png"}))
    evil = tandoor.get(host="evil.example.net").mock(return_value=httpx.Response(200, content=PNG))
    ours = tandoor.get(f"{BASE}/media/y.png").mock(return_value=httpx.Response(200, content=PNG))
    assert client.get("/api/plugins/tandoor/image/8").status_code == 200
    assert ours.called and not evil.called
    upstream = lambda: [c for c in tandoor.calls if c.request.url.host != "testserver"]
    before = len(upstream())
    for bad in ("abc", "1x", "9" * 20):
        assert client.get(f"/api/plugins/tandoor/image/{bad}").status_code == 404
    assert len(upstream()) == before  # no request for non-numeric ids


def test_image_route_requires_login():
    app = FastAPI()
    for router in TandoorPlugin().routers():
        app.include_router(router)
    assert TestClient(app).get("/api/plugins/tandoor/image/12").status_code in (401, 403)


# --- connection test --------------------------------------------------------------------------------------------


def test_settings_connected_with_unsaved_values(tandoor, monkeypatch):
    monkeypatch.delenv("TANDOOR_URL")
    monkeypatch.delenv("TANDOOR_TOKEN")
    route = tandoor.get("https://typed.example.org/api/recipe/").mock(
        return_value=httpx.Response(200, json={"count": 42, "next": "x", "previous": None, "results": [SEARCH["results"][0]]}))
    result = run(TandoorPlugin().test_settings({"TANDOOR_URL": "https://typed.example.org/", "TANDOOR_TOKEN": "tda_typed"}))
    assert result == {"ok": True, "level": "ok", "message": "Connected, 42 recipes"}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer tda_typed" and request.url.params["page_size"] == "1"


def test_settings_singular_and_api_suffix(tandoor):
    tandoor.get(f"{BASE}/api/recipe/").mock(return_value=httpx.Response(200, json={"count": 1, "results": []}))
    result = run(TandoorPlugin().test_settings({"TANDOOR_URL": f"{BASE}/api/", "TANDOOR_TOKEN": "t"}))
    assert result["message"] == "Connected, 1 recipe"


@pytest.mark.parametrize("reply,text", [
    (httpx.Response(401), "rejected"), (httpx.Response(403), "rejected"), (httpx.Response(404), "No Tandoor API"),
    (httpx.Response(500), "error 500"), (httpx.Response(200, text="<html>login</html>"), "not reachable"),
    (httpx.Response(200, json={"hello": "world"}), "does not look like"),
    (httpx.ConnectError("x"), "not reachable"), (httpx.ReadTimeout("x"), "not reachable")])
def test_settings_error_paths(tandoor, reply, text):
    tandoor.get(f"{BASE}/api/recipe/").mock(side_effect=[reply] if isinstance(reply, Exception) else None,
                                            return_value=None if isinstance(reply, Exception) else reply)
    result = run(TandoorPlugin().test_settings({"TANDOOR_URL": BASE, "TANDOOR_TOKEN": "tda_secret"}))
    assert result["ok"] is False and result["level"] == "error" and text in result["message"]
    assert "tda_secret" not in result["message"] and "example.org" not in result["message"]


@pytest.mark.parametrize("values,text", [
    ({"TANDOOR_URL": "", "TANDOOR_TOKEN": "t"}, "address"), ({"TANDOOR_URL": "ftp://x", "TANDOOR_TOKEN": "t"}, "address"),
    ({"TANDOOR_URL": BASE, "TANDOOR_TOKEN": ""}, "token"), ({}, "address")])
def test_settings_validation(tandoor, values, text):
    result = run(TandoorPlugin().test_settings(values))
    assert result["ok"] is False and text in result["message"] and not tandoor.calls


def test_not_configured_means_no_requests(tandoor, monkeypatch):
    monkeypatch.delenv("TANDOOR_URL")
    plugin = TandoorPlugin()
    assert run(plugin.recipe_search("gratin", 5)) == [] and run(plugin.recipe("12")) == {}
    assert not tandoor.calls
