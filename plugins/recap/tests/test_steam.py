"""Tests for the Steam library in the games section: the owned-games request, the daily snapshots, the month
difference and its first-month fallback, the merge with ES-DE entries, the ranking, fail-soft behaviour and that the
API key never leaks. No network: the Steam API and its picture host are simulated with respx."""

import datetime as dt
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from fh_plugin_recap import build, collectors, plugin as mod, store, views
from fh_plugin_recap.plugin import RecapPlugin

from helpers import ANNA, JPEG, install
from test_games import NFS, WITCHER, device_file, entry, gamedirs, write_device  # noqa: F401 - fixtures
from test_plugin import MONTH, TZ, build_for, people, run, services  # noqa: F401 - fixtures

KEY = "steam-secret-key-1"
STEAM_ID = "76561190000000001"
API = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"
START, END = dt.datetime(2026, 10, 1, tzinfo=TZ), dt.datetime(2026, 11, 1, tzinfo=TZ)
CFG = collectors.Config(steam_id=STEAM_ID, steam_api_key=KEY)
LATE = int(dt.datetime(2026, 10, 20, 20, tzinfo=TZ).timestamp())  # last played inside October
OLD = int(dt.datetime(2026, 8, 3, tzinfo=TZ).timestamp())


def app(appid, name, forever, last=LATE, recent=0, deck=0, **kw):
    return {"appid": appid, "name": name, "img_icon_url": "abc", "playtime_forever": forever, "playtime_deck_forever": deck,
            "playtime_linux_forever": 0, "playtime_windows_forever": forever - deck, "playtime_mac_forever": 0,
            "playtime_disconnected": 0, "rtime_last_played": last, **({"playtime_2weeks": recent} if recent else {}), **kw}


def snap(*apps):
    """The stored form of an owned-games list."""
    return {str(a["appid"]): {"name": a["name"], "forever": a["playtime_forever"], "deck": a["playtime_deck_forever"],
                              "linux": 0, "windows": a["playtime_windows_forever"], "last_played": a["rtime_last_played"],
                              "recent": a.get("playtime_2weeks", 0)} for a in apps}


GAMES = [app(292030, "The Witcher 3: Wild Hunt", 6000, deck=1000), app(620, "Portal 2", 300), app(70, "Half-Life", 90)]


def mock_steam(http, games=GAMES, pictures=True):
    route = http.get(API).mock(return_value=httpx.Response(200, json={"response": {"game_count": len(games), "games": games}}))
    http.get(host="cdn.cloudflare.steamstatic.com").mock(return_value=httpx.Response(
        200 if pictures else 404, content=JPEG, headers={"content-type": "image/jpeg"}))
    http.get("https://store.steampowered.com/api/storesearch/").mock(return_value=httpx.Response(200, json={"items": []}))
    return route


def steam_person(people):
    people[1] = {**ANNA, "RECAP_STEAM_ID": STEAM_ID, "RECAP_STEAM_API_KEY": KEY}


def collect(tmp_path, base, final, as_of=END, cfg=CFG, setup=mock_steam, errors=None):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, cfg, START, END, tmp_path, None, None, None, (base, final, as_of), errors)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        calls = setup(http)
        return run(go()), calls


# --- settings ---

def test_feature_is_off_without_both_settings(people):
    assert "games" not in mod.load_config(1).sources()
    for values in ({"RECAP_STEAM_ID": STEAM_ID}, {"RECAP_STEAM_API_KEY": KEY}):
        people[1] = {**ANNA, **values}
        assert "games" not in mod.load_config(1).sources()
    steam_person(people)
    cfg = mod.load_config(1)
    assert "games" in cfg.sources() and (cfg.steam_id, cfg.steam_api_key) == (STEAM_ID, KEY)


def test_settings_are_in_manifest_and_code():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "familienhub-plugin.json").read_text())
    mine = {s["key"]: s for s in manifest["personal_settings"]}
    assert mine["RECAP_STEAM_ID"]["type"] == "text" and mine["RECAP_STEAM_API_KEY"]["type"] == "password"
    assert not mine["RECAP_STEAM_API_KEY"].get("default") and not mine["RECAP_STEAM_API_KEY"]["required"]
    assert {"api.steampowered.com", "cdn.cloudflare.steamstatic.com"} <= set(manifest["hosts"])
    assert RecapPlugin().version == manifest["version"] == "1.8.0"


def test_build_without_settings_makes_no_steam_call(people, services):
    route = mock_steam(services)
    data = build_for()
    assert not route.called and store.steam_days(1) == [] and "steam" not in data["errors"]


# --- request and snapshot ---

def test_owned_games_request_and_parsing():
    async def go():
        async with collectors.new_client() as client:
            return await collectors.steam_owned(client, CFG)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        route = mock_steam(http, GAMES + [{"name": "broken"}])
        state = run(go())
    params = dict(route.calls[0].request.url.params)
    assert params == {"key": KEY, "steamid": STEAM_ID, "include_appinfo": "1", "include_played_free_games": "1"}
    assert state["292030"] == {"name": "The Witcher 3: Wild Hunt", "forever": 6000, "deck": 1000, "linux": 0, "windows": 5000,
                               "last_played": LATE, "recent": 0}
    assert len(state) == 3


@pytest.mark.parametrize("payload", [{"response": {}}, {"response": {"games": []}}, {"response": None}, [1]])
def test_an_answer_without_games_is_an_error(payload):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.steam_owned(client, CFG)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        http.get(API).mock(return_value=httpx.Response(200, json=payload))
        with pytest.raises(LookupError):
            run(go())


def test_snapshot_is_written_once_per_day_and_only_when_enabled(people):
    with respx.mock(using="httpx", assert_all_called=False) as http:
        route = mock_steam(http)
        assert run(build.snapshot_steam(1, mod.load_config(1), TZ)) is False and not route.called  # not enabled
        steam_person(people)
        cfg = mod.load_config(1)
        assert run(build.snapshot_steam(1, cfg, TZ)) is True
        assert run(build.snapshot_steam(1, cfg, TZ)) is False and route.call_count == 1
    (day,) = store.steam_days(1)
    assert day == dt.datetime.now(TZ).date().isoformat()
    assert store.read_steam(1, day)["620"] == {"name": "Portal 2", "forever": 300, "deck": 0, "linux": 0, "windows": 300,
                                               "last_played": LATE, "recent": 0}
    assert KEY not in store.steam_path(1, day).read_text() and STEAM_ID not in store.steam_path(1, day).read_text()


def test_snapshot_is_silent_on_errors(people, monkeypatch):
    steam_person(people)
    cfg = mod.load_config(1)
    with respx.mock(using="httpx", assert_all_called=False) as http:
        http.get(API).mock(return_value=httpx.Response(500))
        assert run(build.snapshot_steam(1, cfg, TZ)) is False and store.steam_days(1) == []
        http.get(API).mock(return_value=httpx.Response(200, json={"response": {}}))  # private profile: never stored
        assert run(build.snapshot_steam(1, cfg, TZ)) is False and store.steam_days(1) == []
        mock_steam(http)
        monkeypatch.setattr(store, "write_steam", lambda *a: (_ for _ in ()).throw(OSError("full")))
        assert run(build.snapshot_steam(1, cfg, TZ)) is False


def test_scheduler_takes_the_steam_snapshot(people, monkeypatch):
    steam_person(people)
    monkeypatch.setattr(build, "busy", lambda: True)
    from test_plugin import FakeDB

    with respx.mock(using="httpx", assert_all_called=False) as http:
        route = mock_steam(http)
        run(RecapPlugin().scheduled(FakeDB([1, 2])))
    assert len(store.steam_days(1)) == 1 and store.steam_days(2) == [] and route.call_count == 1


def test_state_selection_around_the_month(people):
    for day, forever in (("2026-09-20", 100), ("2026-09-30", 200), ("2026-10-15", 300), ("2026-11-01", 400), ("2026-11-05", 500)):
        store.write_steam(1, day, {"1": {"forever": forever}})
    base, final, at = build.steam_states(1, START, END)
    assert base["1"]["forever"] == 200 and final["1"]["forever"] == 400 and at == dt.datetime(2026, 11, 1, tzinfo=TZ)
    assert build.steam_states(2, START, END) == (None, None, None)


# --- month difference ---

def test_delta_subtracts_the_baseline():
    base = snap(app(1, "A", 100), app(2, "B", 500), app(3, "Idle", 50))
    final = snap(app(1, "A", 160), app(2, "B", 500), app(3, "Idle", 50), app(4, "Fresh", 90), app(5, "Owned", 0))
    out = collectors.steam_delta(base, final, START, END, END)
    assert out == {"1": {"name": "A", "minutes": 60, "new": False}, "4": {"name": "Fresh", "minutes": 90, "new": True}}


def test_new_means_nothing_was_played_before():
    base = snap(app(1, "A", 0))
    assert collectors.steam_delta(base, snap(app(1, "A", 30)), START, END, END)["1"]["new"] is True


def test_negative_and_absurd_deltas_are_not_counted():
    base = snap(app(1, "Reset", 500), app(2, "Huge", 10))
    final = snap(app(1, "Reset", 100), app(2, "Huge", 10 + 31 * 24 * 60 + 1), app(3, "Fine", 44641 - 1))
    out = collectors.steam_delta(base, final, START, END, END)
    assert list(out) == ["3"]  # a month has 44640 minutes: Fine is the most one game can have


def test_first_month_counts_only_apps_played_in_the_month_with_their_two_week_time():
    final = snap(app(1, "Recent", 5000, recent=240), app(2, "Old", 5000, last=OLD, recent=240), app(3, "NoRecent", 5000),
                 app(4, "Capped", 100, recent=500))
    out = collectors.steam_delta(None, final, START, END, END)
    assert out == {"1": {"name": "Recent", "minutes": 240, "new": False}, "4": {"name": "Capped", "minutes": 100, "new": True}}


def test_first_month_ignores_the_two_week_time_when_its_window_reaches_before_the_month():
    final = snap(app(1, "Recent", 5000, recent=240))
    assert collectors.steam_delta(None, final, START, END, dt.datetime(2026, 10, 10, tzinfo=TZ)) == {}  # window from Sep 26
    assert collectors.steam_delta(None, final, START, END, dt.datetime(2026, 10, 15, tzinfo=TZ)) != {}


def test_collect_first_month_without_any_snapshot_asks_steam_now(tmp_path):
    state = [app(1, "Recent", 5000, last=int(dt.datetime.now(TZ).timestamp()), recent=60)]
    cfg = collectors.Config(steam_id=STEAM_ID, steam_api_key=KEY)
    start, end = (dt.datetime.now(TZ) - dt.timedelta(days=20)), dt.datetime.now(TZ) + dt.timedelta(days=1)

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, cfg, start, end, tmp_path, None)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        route = mock_steam(http, state)
        out = run(go())
    assert route.call_count == 1 and out["minutes"] == 60 and out["games"][0]["name"] == "Recent"


# --- collector: ranking, covers, merge ---

def test_steam_games_enter_the_ranking_with_covers(tmp_path):
    base = snap(app(292030, "The Witcher 3: Wild Hunt", 6000), app(620, "Portal 2", 300), app(70, "Half-Life", 90))
    final = snap(app(292030, "The Witcher 3: Wild Hunt", 6180), app(620, "Portal 2", 420), app(70, "Half-Life", 90))

    def setup(http):
        route = http.get(f"{CDN}/292030/library_600x900.jpg").mock(return_value=httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}))
        http.get(f"{CDN}/620/library_600x900.jpg").mock(return_value=httpx.Response(404))
        header = http.get(f"{CDN}/620/header.jpg").mock(return_value=httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}))
        return route, header

    # the stored state is used, so Steam's API is not asked
    with respx.mock(using="httpx", assert_all_called=False) as http:
        api = http.get(API).mock(return_value=httpx.Response(500))
        route, header = setup(http)

        async def go():
            async with collectors.new_client() as client:
                return await collectors.collect_games(client, CFG, START, END, tmp_path, None, None, None, (base, final, END))

        out = run(go())
    assert not api.called and route.call_count == 1 and header.call_count == 1
    assert out["minutes"] == 300 and out["hours"] == 5.0 and out["games_count"] == 2 and out["top_system"] == "Steam"
    witcher, portal = out["games"]
    assert (witcher["name"], witcher["minutes"], witcher["system"], witcher["new"], witcher["sessions"]) == (
        "The Witcher 3: Wild Hunt", 180, "Steam", False, 0)
    assert witcher["cover"].endswith(".jpg") and portal["cover"].endswith(".jpg") and "_app" not in witcher and "_key" not in witcher
    assert out["systems"] == [{"name": "Steam", "minutes": 300}]


def test_cover_falls_back_to_the_store_search_then_a_placeholder(tmp_path):
    base, final = snap(app(1, "Alpha", 0), app(2, "Beta", 0)), snap(app(1, "Alpha", 60), app(2, "Beta", 30))

    def setup(http):
        http.get(host="cdn.cloudflare.steamstatic.com", path__regex=r"/steam/apps/1/.*").mock(return_value=httpx.Response(404))
        http.get(host="cdn.cloudflare.steamstatic.com", path__regex=r"/steam/apps/2/.*").mock(return_value=httpx.Response(404))
        http.get(host="cdn.cloudflare.steamstatic.com", path__regex=r"/steam/apps/99/.*").mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        http.get("https://store.steampowered.com/api/storesearch/").mock(side_effect=lambda r: httpx.Response(
            200, json={"items": [{"id": 99}] if r.url.params["term"] == "Alpha" else []}))

    out, _ = collect(tmp_path, base, final, setup=setup)
    alpha, beta = out["games"]
    assert alpha["cover"].endswith(".jpg") and beta["cover"] is None


def test_steam_titles_are_not_counted_twice_with_es_de(tmp_path):
    base = snap(app(292030, "The Witcher 3: Wild Hunt", 6000), app(620, "Portal 2", 300))
    final = snap(app(292030, "The Witcher 3: Wild Hunt", 6000 + 120), app(620, "Portal 2", 300 + 30))
    es_final = {"pc": {f"steam/{WITCHER}": {"playtime": 7200, "playcount": 3, "lastplayed": "20261020T200000"},
                       "windows/Portal 2.desktop": {"playtime": 5400, "playcount": 2, "lastplayed": "20261021T200000"},
                       "ps2/" + NFS: {"playtime": 3600, "playcount": 1, "lastplayed": "20261022T200000"}}}

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, CFG, START, END, tmp_path, None, es_final, None, (base, final, END))

    with respx.mock(using="httpx", assert_all_called=False) as http:
        mock_steam(http)
        out = run(go())
    by = {g["name"]: g for g in out["games"]}
    assert len(out["games"]) == 3 and out["games_count"] == 3
    # ES-DE saw 2 h (more than Steam's 2 h = equal) -> one row; Portal: ES-DE 1.5 h beats Steam's 30 min
    assert by["The Witcher 3: Wild Hunt"]["minutes"] == 120 and by["The Witcher 3: Wild Hunt"]["sessions"] == 3
    assert by["Portal 2"]["minutes"] == 90 and by["Portal 2"]["system"] == "Steam" and by["Portal 2"]["sessions"] == 2
    assert by["Need for Speed - Underground 2"]["system"] == "PlayStation 2"
    assert out["minutes"] == 120 + 90 + 60


def test_steam_wins_when_it_has_more_and_es_de_rows_of_one_title_are_summed(tmp_path):
    base, final = snap(app(1, "Cyberpunk 2077", 0)), snap(app(1, "Cyberpunk 2077", 300))
    es_final = {"pc": {"steam/Cyberpunk 2077.desktop": {"playtime": 3600, "playcount": 2, "lastplayed": "20261020T200000"},
                       "windows/Cyberpunk 2077 Edition.exe": {"playtime": 1800, "playcount": 1, "lastplayed": "20261021T200000"}}}

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, CFG, START, END, tmp_path, None, es_final, None, (base, final, END))

    with respx.mock(using="httpx", assert_all_called=False) as http:
        mock_steam(http)
        out = run(go())
    (top,) = out["games"]  # 60 + 30 min from ES-DE, 300 min from Steam: one row with the larger value
    assert top["minutes"] == 300 and top["sessions"] == 3 and top["system"] == "Steam" and out["minutes"] == 300


@pytest.mark.parametrize("a, b", [("The Witcher 3 Wild Hunt — Remastered", "Witcher 3: Wild Hunt"),
                                  ("DOOM™", "Doom"), ("Portal  2", "portal 2")])
def test_titles_match_loosely(a, b):
    assert collectors.title_key(a) == collectors.title_key(b)


def test_titles_of_other_games_do_not_match():
    assert collectors.title_key("Portal") != collectors.title_key("Portal 2")


def test_only_steam_and_pc_rows_merge_with_steam(tmp_path):
    base, final = snap(app(1, "Hades", 0)), snap(app(1, "Hades", 60))
    es_final = {"pc": {"switch/Hades.xci": {"playtime": 1200, "playcount": 1, "lastplayed": "20261020T200000"}}}

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, CFG, START, END, tmp_path, None, es_final, None, (base, final, END))

    with respx.mock(using="httpx", assert_all_called=False) as http:
        mock_steam(http)
        out = run(go())
    assert sorted((g["system"], g["minutes"]) for g in out["games"]) == [("Nintendo Switch", 20), ("Steam", 60)]


# --- fail-soft ---

def test_steam_error_keeps_the_es_de_games_and_is_recorded(tmp_path):
    es_final = {"pc": {f"ps2/{NFS}": {"playtime": 3600, "playcount": 1, "lastplayed": "20261020T200000"}}}
    errors = {}

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, CFG, START, END, tmp_path, None, es_final, None, (None, None, None), errors)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        http.get(API).mock(return_value=httpx.Response(403))
        out = run(go())
    assert errors == {"steam": "HTTPStatusError 403"} and out["games_count"] == 1 and KEY not in json.dumps(out) + json.dumps(errors)


def test_steam_error_without_es_de_raises_without_the_key(tmp_path):
    with pytest.raises(httpx.HTTPStatusError) as caught:
        collect(tmp_path, None, None, setup=lambda http: http.get(API).mock(return_value=httpx.Response(500)))
    assert KEY not in collectors.describe_error(caught.value)


# --- build ---

def test_build_with_snapshots_ranks_steam_and_hides_the_key(people, services, gamedirs, caplog):
    steam_person(people)
    people[1]["RECAP_GAMES_DEVICES"] = "pc"
    write_device(gamedirs[0], "pc", device_file(ps2={NFS: entry(99999, 99)}))
    store.write_games(1, "2026-09-30", {"pc": {f"ps2/{NFS}": {"playtime": 600, "playcount": 2, "lastplayed": None}}})
    store.write_games(1, "2026-11-01", {"pc": {f"ps2/{NFS}": {"playtime": 600 + 3600, "playcount": 3, "lastplayed": "20261020T100000"}}})
    store.write_steam(1, "2026-09-30", snap(*GAMES))
    store.write_steam(1, "2026-11-01", snap(app(292030, "The Witcher 3: Wild Hunt", 6000 + 180, deck=1000), *GAMES[1:]))
    route = mock_steam(services)
    with caplog.at_level(logging.DEBUG):
        data = build_for()
    ga = data["games"]
    assert data["errors"] == {} and ga["hours"] == 4.0 and ga["games_count"] == 2 and ga["top_system"] == "Steam"
    assert [g["name"] for g in ga["games"]] == ["The Witcher 3: Wild Hunt", "Need for Speed - Underground 2"]
    assert ga["games"][0]["cover"] and data["totals"]["games_hours"] == 4.0
    assert not [c for c in route.calls if c.request.url.params.get("steamid") != STEAM_ID]
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    obsidian = views.render_obsidian(data, "/x")
    assert "s-games" in page and "Witcher" in page and "Steam" in obsidian
    stored = "".join(p.read_text() for p in store.user_dir(1).rglob("*.json"))
    for blob in (json.dumps(data), stored, page, obsidian, caplog.text):
        assert KEY not in blob


def test_a_steam_only_person_gets_a_recap_and_a_snapshot(people, services):
    people[2] = {"RECAP_STEAM_ID": STEAM_ID, "RECAP_STEAM_API_KEY": KEY}
    store.write_steam(2, "2026-09-30", snap(app(620, "Portal 2", 100)))
    store.write_steam(2, "2026-11-01", snap(app(620, "Portal 2", 300)))
    mock_steam(services)
    data = run(build.build_month(2, MONTH, mod.load_config(2), TZ))
    assert data["games"]["minutes"] == 200 and not data.get("empty") and store.has_story(2, MONTH)
    assert len(store.steam_days(2)) == 3  # the build on demand also kept today's state


def test_api_error_is_soft_and_the_recap_still_builds(people, services):
    steam_person(people)
    services.get(API).mock(return_value=httpx.Response(500))
    data = build_for()
    assert data["errors"] == {"games": "HTTPStatusError 500"} and data["games"] == {}
    assert "Great Show" in (store.month_dir(1, MONTH) / "index.html").read_text()


def test_steam_error_next_to_es_de_is_recorded_as_steam(people, services, gamedirs):
    steam_person(people)
    people[1]["RECAP_GAMES_DEVICES"] = "pc"
    write_device(gamedirs[0], "pc", device_file(ps2={NFS: entry(7200, 3, "20261012T100000")}))
    services.get(API).mock(return_value=httpx.Response(500))
    data = build_for()
    assert data["errors"] == {"steam": "HTTPStatusError 500"} and data["games"]["games_count"] == 1


# --- connection test ---

def test_connection_test(people, services):
    values = {"RECAP_STEAM_ID": STEAM_ID, "RECAP_STEAM_API_KEY": KEY}
    route = mock_steam(services)
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), values))
    assert result["ok"] and "Steam: connected" in result["message"] and KEY not in result["message"]
    assert dict(route.calls[0].request.url.params)["key"] == KEY
    route.mock(return_value=httpx.Response(403))
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), values))
    assert not result["ok"] and "Steam: login rejected" in result["message"] and KEY not in result["message"]
    route.mock(return_value=httpx.Response(200, json={"response": {}}))
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), values))
    assert not result["ok"] and "Steam: no games visible" in result["message"]
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), {"RECAP_STEAM_ID": STEAM_ID}))
    assert "Steam: incomplete" in result["message"]
