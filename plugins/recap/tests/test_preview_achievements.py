"""Tests for the interim state of the running month (``preview=true``) and for achievements from Anker/PC games and PS4
trophies (read from ``_achievements`` in the device files, named through the Steam Web API). No network: respx."""

import datetime as dt
import json
import logging
import time

import httpx
import pytest
import respx

from fh_plugin_recap import build, collectors, plugin as mod, store, story, views

from helpers import ANNA, JPEG, install
from test_games import device_file, entry, gamedirs, write_device  # noqa: F401 - fixtures
from test_plugin import MONTH, TZ, build_for, client, fake_build, people, run, services  # noqa: F401 - fixtures
from test_retroachievements import KEY as RA_KEY, RA, SAMPLE, mock_ra

STEAM_KEY = "steam-secret-key-1"
SCHEMA = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
START, END = dt.datetime(2026, 10, 1, tzinfo=TZ), dt.datetime(2026, 11, 1, tzinfo=TZ)
T_OCT = int(dt.datetime(2026, 10, 12, 20, tzinfo=TZ).timestamp())
T_OCT2 = int(dt.datetime(2026, 10, 14, 20, tzinfo=TZ).timestamp())
T_SEP = int(dt.datetime(2026, 9, 28, 20, tzinfo=TZ).timestamp())
T_NOV = int(dt.datetime(2026, 11, 2, 20, tzinfo=TZ).timestamp())


def block(**extra):
    return {"anker:1773": {"title": "Cult of the Lamb", "steam_app_id": "1313140", "source": "anker", "unlocked": [
                {"id": "DEAL_WITH_THE_DEVIL", "time": T_OCT}, {"id": "OLD_ONE", "time": T_SEP}, {"id": "FUTURE", "time": T_NOV}]},
            "ps4:3972": {"title": "Bloodborne", "source": "ps4", "unlocked": [
                {"id": "000", "name": "Hunter of Hunters", "time": T_OCT2}, {"id": "001", "name": "Old", "time": 0}]},
            **extra}


def write_block(gamedirs, name="deck", ach=None, games=None):
    content = {**(games or {}), "_achievements": block() if ach is None else ach}
    write_device(gamedirs[0], name, content)


def mock_schema(http, ok=True):
    def answer(request):
        assert request.url.params["key"] == STEAM_KEY
        if not ok:
            return httpx.Response(500)
        return httpx.Response(200, json={"game": {"availableGameStats": {"achievements": [
            {"name": "DEAL_WITH_THE_DEVIL", "displayName": "Pakt mit dem Teufel", "description": "Schließe einen Pakt.",
             "icon": "https://cdn.example.org/ach.jpg"}]}}})
    http.get(SCHEMA).mock(side_effect=answer)
    http.get(host="cdn.example.org").mock(return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))


def collect(tmp_path, gamedirs, key=STEAM_KEY, setup=mock_schema):
    cfg = collectors.Config(games_dir=str(gamedirs[0]), games_devices=("deck",), steam_api_key=key)
    img = tmp_path / "img"
    img.mkdir(exist_ok=True)

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_pc_achievements(client, cfg, START, END, img)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        setup(http)
        return run(go())


# --- reading the device files ---

def test_read_achievements_merges_devices_and_survives_odd_data(tmp_path):
    write_device(tmp_path, "deck", {"_achievements": {**block(), "bad key": {"title": "x", "unlocked": []}, "anker:9": "no",
                                                       "anker:8": {"title": "", "unlocked": []}, "anker:7": {"title": "T", "unlocked": 5}}})
    write_device(tmp_path, "pc", {"_achievements": {"anker:1773": {"title": "Cult of the Lamb", "steam_app_id": "x1", "unlocked": [
        {"id": "DEAL_WITH_THE_DEVIL", "time": T_OCT - 600}, {"id": "", "time": 5}, "nope"]}}})
    write_device(tmp_path, "odd", {"_achievements": [1, 2]})
    (tmp_path / "broken.json").write_text("{kaputt")
    found = collectors.read_achievements(str(tmp_path), ("deck", "pc", "odd", "broken", "missing", "../x"))
    assert set(found) == {"anker:1773", "ps4:3972"}
    lamb = {u["id"]: u["time"] for u in found["anker:1773"]["unlocked"]}
    assert lamb["DEAL_WITH_THE_DEVIL"] == T_OCT - 600 and found["anker:1773"]["steam_app_id"] == "1313140"  # earliest time wins


# --- collecting ---

def test_only_unlocks_in_the_period_count_and_names_come_from_steam(tmp_path, gamedirs):
    write_block(gamedirs)
    out = collect(tmp_path, gamedirs)
    assert out["total"] == 2 and out["points"] == 0 and out["games_count"] == 2
    titles = {x["title"]: x for x in out["rarest"]}
    assert set(titles) == {"Pakt mit dem Teufel", "Hunter of Hunters"}  # not the September, November or time-less ones
    pakt = titles["Pakt mit dem Teufel"]
    assert pakt["description"] == "Schließe einen Pakt." and pakt["source"] == "anker" and pakt["badge"].startswith("img/pc-b")
    assert (tmp_path / pakt["badge"]).exists() and titles["Hunter of Hunters"]["source"] == "ps4"
    assert {(g["name"], g["console"], g["count"]) for g in out["games"]} == {("Cult of the Lamb", "PC", 1), ("Bloodborne", "PS4", 1)}
    assert [x["title"] for x in out["rarest"]] == ["Hunter of Hunters", "Pakt mit dem Teufel"]  # newest first
    assert STEAM_KEY not in json.dumps(out)


@pytest.mark.parametrize("setup, key", [(mock_schema, ""), (lambda http: mock_schema(http, ok=False), STEAM_KEY)])
def test_without_key_or_schema_the_raw_name_is_shown(tmp_path, gamedirs, setup, key):
    write_block(gamedirs)
    out = collect(tmp_path, gamedirs, key=key, setup=setup if key else (lambda http: None))
    assert {x["title"] for x in out["rarest"]} == {"Deal With The Devil", "Hunter of Hunters"}
    assert all(x["badge"] is None for x in out["rarest"])


def test_nothing_in_the_period_gives_nothing(tmp_path, gamedirs):
    write_block(gamedirs, ach={"anker:1": {"title": "G", "unlocked": [{"id": "A", "time": T_SEP}]}})
    assert collect(tmp_path, gamedirs)["total"] == 0
    assert collectors.merge_achievements({"total": 3, "points": 5}, collect(tmp_path, gamedirs)) == {"total": 3, "points": 5}


def test_merge_keeps_retroachievements_and_names_the_source():
    ra = {"total": 4, "points": 40, "hardcore": 4, "games_count": 2, "games": [{"name": "Zelda", "console": "SNES", "count": 4, "points": 30}],
          "rarest": [{"title": f"R{i}", "game": "Zelda", "points": 10, "badge": None} for i in range(6)], "awards": []}
    pc = {"total": 3, "points": 0, "games_count": 1, "games": [{"name": "Lamb", "console": "PC", "count": 3, "points": 0, "source": "anker"}],
          "rarest": [{"title": f"P{i}", "game": "Lamb", "points": 0, "badge": None, "source": "anker"} for i in range(3)], "awards": []}
    merged = collectors.merge_achievements(ra, pc)
    assert (merged["total"], merged["ra_total"], merged["pc_total"], merged["games_count"]) == (7, 4, 3, 3)
    assert merged["points"] == 40 and len(merged["rarest"]) == 6
    assert [x.get("source") for x in merged["games"]] == ["ra", "anker"] and sum(1 for x in merged["rarest"] if x.get("source") == "anker") == 2
    only = collectors.merge_achievements({}, pc)
    assert (only["total"], only["ra_total"], only["pc_total"]) == (3, 0, 3)
    assert collectors.merge_achievements(ra, {}) is ra


def test_texts_for_every_mix():
    pc = {"pc_total": 3, "pc_games_count": 2}
    assert story.ach_lead({"total": 5, "points": 50, "hardcore": 5, "games_count": 2}, plain=True) == "50 Punkte, alle im Hardcore-Modus in 2 Spielen."
    mixed = story.ach_lead({"total": 8, "points": 50, "hardcore": 2, "ra_games_count": 2, "games_count": 4, **pc}, plain=True)
    assert mixed == "50 Punkte, davon 2 im Hardcore-Modus in 2 Spielen bei RetroAchievements. Dazu 3 Erfolge in 2 Spielen auf PC und PS4."
    assert story.ach_lead({"total": 3, "points": 0, "hardcore": 0, "ra_total": 0, **pc}, plain=True) == "Freigeschaltet in 2 Spielen auf PC und PS4."
    assert story.ach_meta({"points": 7}) == "7 Punkte" and story.ach_meta({"points": 0, "source": "ps4"}) == "PS4"


# --- the build ---

def pc_person(people, ra=False):
    people[1] = {**ANNA, "RECAP_GAMES_DEVICES": "deck", "RECAP_STEAM_API_KEY": STEAM_KEY, "RECAP_STEAM_ID": "76561190000000000",
                 **({"RECAP_RA_USERNAME": "retro_anna", "RECAP_RA_API_KEY": RA_KEY} if ra else {})}


def test_build_shows_pc_and_retroachievements_together(people, services, gamedirs):
    write_block(gamedirs, games={"switch": {"_system": {"flags": {}}}})
    pc_person(people, ra=True)
    mock_ra(services)
    mock_schema(services)
    services.get(host="api.steampowered.com", path="/IPlayerService/GetOwnedGames/v1/").mock(return_value=httpx.Response(500))
    data = build_for()
    ra = data["retroachievements"]
    assert ra["total"] == 5 + 2 and (ra["ra_total"], ra["pc_total"]) == (5, 2)
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "Pakt mit dem Teufel" in page and "Hunter of Hunters" in page and "bei RetroAchievements. Dazu 2 Erfolge in 2 Spielen auf PC und PS4" in page
    assert "PS4" in page and "Cult of the Lamb" in page
    assert STEAM_KEY not in page and STEAM_KEY not in json.dumps(data)
    assert "Dazu 2 Erfolge" in views.render_obsidian(data, "/x")


def test_pc_achievements_alone_make_the_slide(people, services, gamedirs):
    write_block(gamedirs, games={"switch": {"_system": {"flags": {}}}})
    pc_person(people)
    mock_schema(services)
    services.get(host="api.steampowered.com", path="/IPlayerService/GetOwnedGames/v1/").mock(return_value=httpx.Response(500))
    data = build_for()
    assert data["retroachievements"]["total"] == 2
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "Freigeschaltet in 2 Spielen auf PC und PS4" in page and "Hunter of Hunters" in page


# --- the interim state ---

def preview_period():
    return store.preview_period(MONTH)


def test_store_knows_the_interim_state(tmp_path):
    p = store.preview_period("2026-11")
    assert p == "2026-11-preview" and store.is_preview(p) and store.preview_month(p) == "2026-11"
    assert not store.is_preview("2026-11") and not store.is_preview("2026-13-preview") and not store.is_preview("../2026-11-preview")
    assert store.month_dir(1, p).name == p and store.snapshot_path(1, p).name == f"{p}.json"
    assert views.month_label(p) == "November 2026 (Zwischenstand)"
    with pytest.raises(ValueError):
        store.month_dir(1, "2026-11-previews")


def test_preview_bounds_end_tomorrow_at_the_latest():
    now = dt.datetime.now(TZ)
    start, end = build.preview_bounds(store.preview_period(now.strftime("%Y-%m")), TZ)
    assert (start.year, start.month, start.day) == (now.year, now.month, 1)
    assert end == min(build.month_bounds(now.strftime("%Y-%m"), TZ)[1],
                      (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    old = build.preview_bounds("2026-10-preview", TZ)
    assert old == (START, END)


def test_preview_is_kept_apart_marked_and_never_part_of_the_history(people, services):
    data = build_for(month=preview_period())
    assert data["preview"] is True and data["month"] == MONTH and data["until"] == "2026-10-31" and "record" not in data["highlights"]
    page = (store.month_dir(1, preview_period()) / "index.html").read_text()
    assert "Zwischenstand bis 31. Oktober" in page and "ZWISCHENSTAND" in page or "Zwischenstand" in page
    assert "<title>Rückblick Oktober 2026 (Zwischenstand)</title>" in page
    assert store.read_snapshot(1, MONTH) is None and not store.has_story(1, MONTH)
    assert store.list_months(1) == [] and store.list_previews(1) == [preview_period()]
    assert store.history(1, "2026-11") == []  # the interim state is no earlier month
    assert store.resolve(1, f"{preview_period()}/")[0] == "story" and store.resolve(1, f"{preview_period()}/img/../../x.png") is None


def test_real_build_is_not_blocked_and_removes_the_interim_state(people, services):
    build_for(month=preview_period())
    build_for()
    real = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "Zwischenstand" not in real
    assert store.list_previews(1) == [] and not store.month_dir(1, preview_period()).exists()
    assert not store.snapshot_path(1, preview_period()).exists()
    assert store.list_months(1) == [MONTH]


def test_preview_does_not_change_the_design_memory(people, services):
    build_for(month="2026-09")
    before = store.history(1, "2026-10")
    build_for(month=preview_period())
    assert store.history(1, "2026-10") == before and [h["month"] for h in store.history(1, "2026-12")] == ["2026-09"]


def test_preview_sends_no_message(client, people, services, monkeypatch):
    sent = []

    async def fake_notify(plugin_id, **kw):
        sent.append(kw)
        return {"sent": True}

    monkeypatch.setattr(mod, "notify", fake_notify)
    monkeypatch.setattr(mod, "this_month", lambda: MONTH)
    body = client.post("/api/plugins/recap/build?preview=true").json()
    assert body["status"] == "started" and body["month"] == preview_period() and body["preview"] is True
    assert body["route"].endswith(f"path={preview_period()}/")
    for _ in range(200):
        months = client.get("/api/plugins/recap/months").json()
        if months["previews"]:
            break
        time.sleep(0.05)
    assert months["items"] == [] and months["previews"][0]["label"] == "Oktober 2026 (Zwischenstand)"
    assert months["previews"][0]["until"] == "2026-10-31" and months["previews"][0]["route"] == body["route"]
    assert sent == []
    # every call rebuilds (no "exists"), and the real month can still be built next
    assert client.post(f"/api/plugins/recap/build?month={MONTH}&preview=true").json()["status"] == "started"
    time.sleep(1.5)
    assert client.post("/api/plugins/recap/build?month=2026-10").json()["status"] in ("started", "busy", "running")


def test_preview_endpoint_rules(client, people, fake_build, monkeypatch):
    monkeypatch.setattr(mod, "this_month", lambda: MONTH)
    assert client.post("/api/plugins/recap/build?month=2026-08&preview=true").status_code == 422  # not the running month
    assert client.post("/api/plugins/recap/build?year=2025&preview=true").status_code == 422
    assert client.post("/api/plugins/recap/build?month=nonsense&preview=true").status_code == 422
    assert client.post("/api/plugins/recap/build?month=2030-01").status_code == 422  # the old rule stands without preview
    people[1].clear()
    assert client.post("/api/plugins/recap/build?preview=true").json()["status"] == "no_sources"


def test_index_page_lists_the_interim_state_first(people, services):
    build_for(month="2026-09")
    build_for(month=preview_period())
    page = views.render_index(1)
    assert "Oktober 2026 (Zwischenstand)" in page and page.index("Zwischenstand") < page.index("September 2026")
    assert f'href="{preview_period()}/"' in page and f"{preview_period()}/neu" not in page


def test_page_serves_the_interim_story(people, services):
    from types import SimpleNamespace

    build_for(month=preview_period())
    mime, body = run(mod.RecapPlugin().page(SimpleNamespace(id=1, is_admin=False), "recaps", preview_period()))
    assert mime.startswith("text/html") and b"Zwischenstand" in body and f'<base href="{preview_period()}/">'.encode() in body

