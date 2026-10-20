"""Tests for the Immich plugin. Run with the hub's backend on the path:

    pip install pytest respx httpx fastapi
    DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest

The hub is never started and no real Immich is called: Immich is simulated with respx.
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_user
from app.plugins import Plugin
from app.plugins.hooks import sanitize_cards
from app.plugins.manifest import hub_version_ok, validate_manifest
from fh_plugin_immich import plugin as mod
from fh_plugin_immich.plugin import ImmichPlugin

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://photos.example.org"
KEY = "test-key-123"
TODAY = date(2026, 10, 20)
A1, A2, A3, A4 = ("11111111-aaaa-4aaa-8aaa-000000000001", "11111111-aaaa-4aaa-8aaa-000000000002",
                  "11111111-aaaa-4aaa-8aaa-000000000003", "11111111-aaaa-4aaa-8aaa-000000000004")
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def asset(asset_id, taken="2023-10-20T09:15:00.000Z", **extra):
    return {"id": asset_id, "type": "IMAGE", "isFavorite": False, "isArchived": False, "isTrashed": False,
            "localDateTime": taken, "fileCreatedAt": taken, **extra}


def memories(*groups):
    return [{"id": f"m{i}", "type": "on_this_day", "data": {"year": year}, "assets": assets}
            for i, (year, assets) in enumerate(groups)]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("IMMICH_URL", BASE)
    monkeypatch.setenv("IMMICH_API_KEY", KEY)
    monkeypatch.setattr(mod, "date", type("D", (date,), {"today": classmethod(lambda cls: TODAY)}))


@pytest.fixture
def immich():
    with respx.mock(using="httpx", assert_all_called=False) as service:
        service.route(host="testserver").pass_through()
        yield service


@pytest.fixture
def client(configured):
    app = FastAPI()
    for router in ImmichPlugin().routers():
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


def cards(user=None):
    return asyncio.run(ImmichPlugin().today_cards(user or object(), None))


# --- manifest and loading ---

def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = ImmichPlugin()
    assert plugin.id == manifest["id"] == "immich"
    assert manifest["version"] == "1.0.0" and manifest["min_hub_version"] == "1.0.0-beta.3"
    assert manifest["capabilities"] == [] and set(plugin.capabilities) <= set(manifest["capabilities"])
    assert {s.key for s in plugin.settings} == {s["key"] for s in manifest["settings"]}
    by_key = {s["key"]: s for s in manifest["settings"]}
    assert by_key["IMMICH_URL"]["required"] and by_key["IMMICH_URL"]["type"] == "url"
    assert by_key["IMMICH_API_KEY"]["required"] and by_key["IMMICH_API_KEY"]["secret"]
    assert not by_key["IMMICH_ALBUM_IDS"]["required"] and not by_key["IMMICH_INCLUDE_ARCHIVED"]["required"]
    assert by_key["IMMICH_MAX_PHOTOS"]["default"] == "3"
    for router in plugin.routers():
        assert all(r.path.startswith("/api/plugins/immich/") for r in router.routes)
    assert 'immich = "fh_plugin_immich.plugin:ImmichPlugin"' in (ROOT / "pyproject.toml").read_text()


def test_min_hub_version_excludes_older_hubs():
    assert not hub_version_ok("1.0.0-beta.3", "1.0.0-beta.2")
    assert hub_version_ok("1.0.0-beta.3", "1.0.0-beta.3") and hub_version_ok("1.0.0-beta.3", "1.0.0")


def test_loads_like_an_installed_plugin(tmp_path):
    """The hub's own loader (same checks as after a real install), in a fresh interpreter: the loader refuses a
    package name that is already importable, which it is inside this test run."""
    backend = [p for p in sys.path if (Path(p) / "app" / "plugins" / "installer.py").is_file()][0]
    script = textwrap.dedent(f"""
        import json
        from pathlib import Path
        from fastapi import FastAPI
        from app.plugins import installer
        from app.plugins.manifest import validate_manifest
        root = Path({str(ROOT)!r})
        manifest = validate_manifest(json.loads((root / "familienhub-plugin.json").read_text()))
        app = FastAPI()
        plugin = installer.load_plugin(app, manifest, root)
        paths = sorted(p for p in app.openapi()["paths"] if p.startswith("/api/plugins/"))
        assert paths == ["/api/plugins/immich/thumbnail/{{asset_id}}"], paths
        assert plugin.id == "immich" and plugin.has_test and plugin.active is False
        print("LOADED")
    """)
    env = {**os.environ, "PYTHONPATH": backend, "DATA_DIR": str(tmp_path / "data"), "SECRET_KEY": "t" * 40,
           "DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONHOME", None)
    run = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert "LOADED" in run.stdout, run.stdout[-1500:] + run.stderr[-2500:]


# --- compatibility guard ---

def test_old_hub_without_hooks_is_not_set_up_and_explains(configured, monkeypatch):
    monkeypatch.setattr(mod, "COMPATIBLE", False)
    plugin = ImmichPlugin()
    assert plugin.is_configured() is False
    assert plugin.missing_settings() == ["Needs Familienhub 1.0.0-beta.3 or newer"]
    assert asyncio.run(plugin.check())["message"] == "Needs Familienhub 1.0.0-beta.3 or newer"
    assert asyncio.run(plugin.today_cards(object(), None)) == []
    result = asyncio.run(plugin.test_settings({"IMMICH_URL": BASE, "IMMICH_API_KEY": KEY}))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]


def test_this_hub_is_compatible(configured):
    assert mod.COMPATIBLE is True and hasattr(Plugin, "today_cards")
    plugin = ImmichPlugin()
    assert plugin.is_configured() and plugin.missing_settings() == [] and asyncio.run(plugin.check()) is None


def test_missing_settings_are_reported(monkeypatch):
    plugin = ImmichPlugin()
    assert plugin.is_configured() is False
    assert plugin.missing_settings() == ["IMMICH_URL", "IMMICH_API_KEY"]


# --- Today cards ---

def test_cards_from_memories(configured, immich):
    route = immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories(
        (2023, [asset(A1, isFavorite=True), asset("11111111-aaaa-4aaa-8aaa-0000000000v1", type="VIDEO")]),
        (2021, [asset(A2, "2021-10-20T08:00:00.000Z", exifInfo={"description": "Mia's first bike"})]),
    )))
    result = cards(type("U", (), {"language": "en"})())
    assert route.calls.last.request.headers["x-api-key"] == KEY
    assert route.calls.last.request.url.params["for"].startswith("2026-10-20")
    assert [c["id"] for c in result] == ["on-this-day", "on-this-day-2"]
    first, second = result
    assert first["title"] == "On this day, 3 years ago" and first["icon"] == "photo"
    assert first["lines"] == [{"text": "3 years ago", "secondary": "2023-10-20"}]
    assert first["image"] == f"/api/plugins/immich/thumbnail/{A1}"
    assert first["link"] == f"{BASE}/photos/{A1}"
    assert second["title"] == "On this day, 5 years ago"
    assert second["lines"][0] == {"text": "5 years ago"} and second["lines"][1]["text"] == "Mia's first bike"


def test_german_texts_by_default(configured, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2025, [asset(A1)]))))
    card = cards()[0]
    assert card["title"] == "An diesem Tag, vor 1 Jahr" and card["lines"][0]["secondary"] == "20.10.2023"


def test_cards_survive_the_hubs_validation(configured, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    plugin = ImmichPlugin()
    clean = sanitize_cards(plugin, cards())
    assert clean[0]["key"] == "plugin:immich:on-this-day"
    assert clean[0]["image"] == f"/api/plugins/immich/thumbnail/{A1}" and clean[0]["link"] == f"{BASE}/photos/{A1}"
    assert clean[0]["icon"] == "photo"


def test_no_link_for_a_plain_http_address(configured, immich, monkeypatch):
    monkeypatch.setenv("IMMICH_URL", "http://photos.lan:2283")
    immich.get("http://photos.lan:2283/api/memories").mock(
        return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    card = cards()[0]
    assert "link" not in card and card["image"].startswith("/api/plugins/immich/thumbnail/")


def test_a_url_with_api_suffix_still_works(configured, immich, monkeypatch):
    monkeypatch.setenv("IMMICH_URL", BASE + "/api/")
    route = immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    assert len(cards()) == 1 and route.called


def test_favourites_first_and_limit(configured, immich, monkeypatch):
    monkeypatch.setenv("IMMICH_MAX_PHOTOS", "2")
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories(
        (2024, [asset(A1, "2024-10-20T10:00:00.000Z")]),
        (2023, [asset(A2, isFavorite=True)]),
        (2022, [asset(A3, "2022-10-20T10:00:00.000Z", isFavorite=True)]),
        (2021, [asset(A4, "2021-10-20T10:00:00.000Z")]),
    )))
    result = cards()
    assert [c["image"].rsplit("/", 1)[1] for c in result] == [A2, A3]  # only the favourites, oldest last


def test_different_years_before_a_second_photo_of_one_year(configured, immich, monkeypatch):
    monkeypatch.setenv("IMMICH_MAX_PHOTOS", "2")
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories(
        (2023, [asset(A1, isFavorite=True), asset(A2, isFavorite=True)]),
        (2022, [asset(A3, "2022-10-20T10:00:00.000Z")]),
    )))
    assert {c["image"].rsplit("/", 1)[1] for c in cards()} == {A1, A3}


def test_max_photos_is_clamped(configured, monkeypatch):
    for raw, expected in (("0", 1), ("99", 5), ("abc", 3), ("2.0", 2), ("", 3)):
        monkeypatch.setenv("IMMICH_MAX_PHOTOS", raw)
        assert mod._Config.load().max_photos == expected  # noqa: SLF001


def test_archived_photos_are_skipped_unless_included(configured, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories(
        (2023, [asset(A1, isArchived=True)]))))
    immich.post(f"{BASE}/api/search/metadata").mock(return_value=httpx.Response(200, json={"assets": {"items": []}}))
    assert cards() == []


def test_include_archived_uses_the_search(configured, immich, monkeypatch):
    monkeypatch.setenv("IMMICH_INCLUDE_ARCHIVED", "true")
    memo = immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(500))
    search = immich.post(f"{BASE}/api/search/metadata").mock(side_effect=lambda req: httpx.Response(200, json={
        "assets": {"items": [asset(A1, "2024-10-20T09:00:00.000Z", isArchived=True)]
                   if '"2024-10-19' in req.content.decode() else []}}))
    result = cards()
    assert not memo.called
    assert len(result) == 1 and result[0]["title"].endswith("2 years ago") or "vor 2 Jahren" in result[0]["title"]
    sent = json.loads(search.calls[0].request.content)
    assert sent["withArchived"] is True and sent["type"] == "IMAGE" and "albumIds" not in sent


def test_search_fallback_when_there_are_no_memories(configured, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=[]))
    search = immich.post(f"{BASE}/api/search/metadata").mock(side_effect=lambda req: httpx.Response(200, json={
        "assets": {"items": [asset(A1, "2023-10-20T22:30:00.000Z"), asset(A2, "2023-10-21T00:10:00.000Z")]
                   if '"takenAfter":"2023-10-19' in req.content.decode().replace(" ", "") else []}}))
    result = cards()
    assert [c["image"].rsplit("/", 1)[1] for c in result] == [A1]  # the neighbouring day's photo is filtered out
    assert search.call_count == mod.MAX_YEARS_BACK


def test_album_filter_goes_to_the_search_with_album_ids(configured, immich, monkeypatch):
    album = "3f0c9e1a-1111-4222-8333-444455556666"
    monkeypatch.setenv("IMMICH_ALBUM_IDS", f" {album.upper()} , not valid!, ")
    memo = immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    search = immich.post(f"{BASE}/api/search/metadata").mock(side_effect=lambda req: httpx.Response(200, json={
        "assets": {"items": [asset(A2, "2024-10-20T09:00:00.000Z")] if '"2024-10-19' in req.content.decode() else []}}))
    result = cards()
    assert not memo.called and len(result) == 1
    assert json.loads(search.calls[0].request.content)["albumIds"] == [album]


def test_old_server_memory_lane(configured, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(404))
    lane = immich.get(f"{BASE}/api/assets/memory-lane").mock(return_value=httpx.Response(200, json=[
        {"yearsAgo": 4, "title": "4 years ago", "assets": [asset(A1, "2022-10-20T10:00:00.000Z")]}]))
    result = cards()
    assert lane.calls.last.request.url.params["day"] == "20" and lane.calls.last.request.url.params["month"] == "10"
    assert result[0]["title"] == "An diesem Tag, vor 4 Jahren"


def test_leap_day_skips_years_without_it(configured, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=[]))
    search = immich.post(f"{BASE}/api/search/metadata").mock(return_value=httpx.Response(200, json={"assets": {"items": []}}))
    asyncio.run(mod._photos_for_today(mod._Config.load(), date(2028, 2, 29)))  # noqa: SLF001
    assert search.call_count == 3  # 2024, 2020 and 2016 are leap years among the last 15


def test_result_is_cached_per_day(configured, immich):
    route = immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    assert len(cards()) == 1 and len(cards()) == 1
    assert route.call_count == 1
    asyncio.run(mod._photos_for_today(mod._Config.load(), date(2026, 10, 21)))  # noqa: SLF001
    assert route.call_count == 2


def test_a_failure_is_cached_briefly(configured, immich):
    route = immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(500))
    assert cards() == [] and cards() == []
    assert route.call_count == 1


@pytest.mark.parametrize("answer", [
    httpx.Response(401), httpx.Response(500), httpx.Response(200, text="not json"),
    httpx.Response(200, json={"unexpected": "shape"}), httpx.Response(200, json=["junk", 5, {"assets": "x"}]),
    httpx.TimeoutException("slow"), httpx.ConnectError("down"),
])
def test_a_broken_immich_gives_no_card(configured, immich, answer):
    immich.get(f"{BASE}/api/memories").mock(side_effect=[answer])
    immich.post(f"{BASE}/api/search/metadata").mock(side_effect=httpx.ConnectError("down"))
    assert cards() == []


def test_no_card_without_configuration():
    assert cards() == []


def test_secrets_never_reach_the_log(configured, immich, caplog):
    immich.get(f"{BASE}/api/memories").mock(side_effect=httpx.ConnectError(f"cannot reach {BASE} with {KEY}"))
    immich.post(f"{BASE}/api/search/metadata").mock(side_effect=httpx.ConnectError(KEY))
    with caplog.at_level("DEBUG"):
        cards()
    assert KEY not in caplog.text and BASE not in caplog.text


# --- thumbnail route ---

def test_thumbnail_only_for_todays_photos(client, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    thumb = immich.get(f"{BASE}/api/assets/{A1}/thumbnail").mock(return_value=httpx.Response(200, content=JPEG))
    other = immich.get(f"{BASE}/api/assets/{A2}/thumbnail").mock(return_value=httpx.Response(200, content=JPEG))
    assert client.get(f"/api/plugins/immich/thumbnail/{A1}").status_code == 404  # nothing picked yet
    cards()
    ok = client.get(f"/api/plugins/immich/thumbnail/{A1}")
    assert ok.status_code == 200 and ok.content == JPEG and ok.headers["content-type"] == "image/jpeg"
    request = thumb.calls.last.request
    assert request.headers["x-api-key"] == KEY and request.url.params["size"] == "preview"
    assert client.get(f"/api/plugins/immich/thumbnail/{A2}").status_code == 404
    assert not other.called


@pytest.mark.parametrize("asset_id", ["..%2F..%2Fserver%2Fabout", "a/b", "x", "%2e%2e", "11111111-aaaa-4aaa-8aaa-0000000000zz"])
def test_thumbnail_is_not_an_open_proxy(client, immich, asset_id):
    immich.route(host="photos.example.org").mock(return_value=httpx.Response(200, content=JPEG))
    assert client.get(f"/api/plugins/immich/thumbnail/{asset_id}").status_code == 404
    assert not [c for c in immich.calls if c.request.url.host == "photos.example.org"]


def test_thumbnail_with_a_non_image_or_broken_upstream(client, immich):
    immich.get(f"{BASE}/api/memories").mock(return_value=httpx.Response(200, json=memories((2023, [asset(A1)]))))
    cards()
    route = immich.get(f"{BASE}/api/assets/{A1}/thumbnail")
    route.mock(return_value=httpx.Response(200, content=b"<html>login</html>"))
    assert client.get(f"/api/plugins/immich/thumbnail/{A1}").status_code == 415
    route.mock(return_value=httpx.Response(500))
    assert client.get(f"/api/plugins/immich/thumbnail/{A1}").status_code == 502
    route.mock(side_effect=httpx.ConnectError("down"))
    assert client.get(f"/api/plugins/immich/thumbnail/{A1}").status_code == 502


# --- connection test ---

def run_test(values):
    return asyncio.run(ImmichPlugin().test_settings(values))


def good(immich, name="Alex"):
    immich.get(f"{BASE}/api/server/ping").mock(return_value=httpx.Response(200, json={"res": "pong"}))
    immich.get(f"{BASE}/api/users/me").mock(return_value=httpx.Response(200, json={"name": name, "email": "a@example.org"}))
    immich.get(f"{BASE}/api/server/version").mock(return_value=httpx.Response(200, json={"major": 1, "minor": 135, "patch": 3}))


def test_connection_ok_with_the_typed_values(immich):
    good(immich)
    result = run_test({"IMMICH_URL": BASE + "/", "IMMICH_API_KEY": "typed-key"})
    assert result == {"ok": True, "level": "ok", "message": "Connected as Alex, Immich 1.135.3"}
    assert [c.request.headers["x-api-key"] for c in immich.calls if c.request.url.host == "photos.example.org"] == ["typed-key"] * 3


def test_connection_ok_on_an_old_server_version_path(immich):
    good(immich)
    immich.get(f"{BASE}/api/server/version").mock(return_value=httpx.Response(404))
    immich.get(f"{BASE}/api/server-info/version").mock(return_value=httpx.Response(200, json={"major": 1, "minor": 90, "patch": 0}))
    assert run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})["message"] == "Connected as Alex, Immich 1.90.0"


def test_connection_ok_without_a_version(immich):
    good(immich)
    immich.get(f"{BASE}/api/server/version").mock(return_value=httpx.Response(500))
    immich.get(f"{BASE}/api/server-info/version").mock(return_value=httpx.Response(500))
    assert run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})["message"] == "Connected as Alex"


@pytest.mark.parametrize("values, fragment", [
    ({"IMMICH_URL": "photos.example.org", "IMMICH_API_KEY": "k"}, "http://"),
    ({"IMMICH_URL": BASE, "IMMICH_API_KEY": ""}, "API key is missing"),
    ({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k", "IMMICH_ALBUM_IDS": "ok-album-id-1, no way!"}, "Album IDs"),
])
def test_connection_input_errors_need_no_request(immich, values, fragment):
    result = run_test(values)
    assert result["ok"] is False and result["level"] == "error" and fragment in result["message"]
    assert not [c for c in immich.calls if c.request.url.host == "photos.example.org"]


def test_connection_wrong_key(immich):
    good(immich)
    immich.get(f"{BASE}/api/users/me").mock(return_value=httpx.Response(401, json={"message": "Invalid API key"}))
    result = run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "s3cret-typed"})
    assert result["ok"] is False and "rejected the API key" in result["message"] and "s3cret" not in result["message"]


def test_connection_wrong_address_is_not_immich(immich):
    immich.get(f"{BASE}/api/server/ping").mock(return_value=httpx.Response(200, text="<html>Welcome</html>"))
    result = run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})
    assert result["ok"] is False and "does not look like Immich" in result["message"]
    immich.get(f"{BASE}/api/server/ping").mock(return_value=httpx.Response(404))
    assert "does not look like Immich" in run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})["message"]


@pytest.mark.parametrize("answer, fragment", [
    (httpx.ConnectError("down"), "not reachable"),
    (httpx.ReadTimeout("slow"), "did not answer in time"),
    (httpx.Response(502), "error 502"),
])
def test_connection_unreachable(immich, answer, fragment):
    immich.get(f"{BASE}/api/server/ping").mock(side_effect=[answer])
    result = run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})
    assert result["ok"] is False and fragment in result["message"]
    assert BASE not in result["message"]


def test_connection_me_server_error_and_malformed(immich):
    good(immich)
    immich.get(f"{BASE}/api/users/me").mock(return_value=httpx.Response(500))
    assert "error 500" in run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})["message"]
    immich.get(f"{BASE}/api/users/me").mock(return_value=httpx.Response(200, text="nope"))
    assert run_test({"IMMICH_URL": BASE, "IMMICH_API_KEY": "k"})["ok"] is False
