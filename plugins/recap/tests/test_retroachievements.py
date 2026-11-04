"""Tests for the RetroAchievements section: the API calls and the month window, the aggregation, the slides, fail-soft
behaviour and that the API key never leaks. No network: the RA API and its picture host are simulated with respx."""

import datetime as dt
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from fh_plugin_recap import build, collectors, plugin as mod, store, story, views
from fh_plugin_recap.plugin import RecapPlugin

from helpers import ANNA, JPEG, install
from test_plugin import MONTH, TZ, build_for, people, run, services  # noqa: F401 - fixtures

KEY = "ra-secret-key-1"
RA = "https://retroachievements.org/API"
MEDIA = "https://media.retroachievements.org"
START, END = dt.datetime(2026, 10, 1, tzinfo=TZ), dt.datetime(2026, 11, 1, tzinfo=TZ)
CFG = collectors.Config(ra_username="retro_anna", ra_api_key=KEY)


def ach(n, game=1, points=10, true=20, day=5, hardcore=1, **kw):
    return {"Date": f"2026-10-{day:02d} 12:00:00", "HardcoreMode": hardcore, "AchievementID": n, "Title": f"Achievement {n}",
            "Description": f"Do thing {n}", "BadgeName": str(1000 + n), "Points": points, "TrueRatio": true, "GameID": game,
            "GameTitle": f"Game {game}", "GameIcon": f"/Images/00{game}.png", "ConsoleName": "SNES",
            "BadgeURL": f"/Badge/{1000 + n}.png", "GameURL": f"/game/{game}", "Type": None, **kw}


def award(game, kind="Mastery/Completion", extra=1, at="2026-10-20T10:00:00+00:00"):
    return {"AwardedAt": at, "AwardType": kind, "AwardData": game, "AwardDataExtra": extra, "Title": f"Game {game}",
            "ConsoleName": "SNES", "ImageIcon": f"/Images/00{game}.png"}


SAMPLE = [ach(1, 1, 10, 20), ach(2, 1, 5, 400, 6), ach(3, 1, 25, 40, 7, hardcore=0), ach(4, 2, 50, 60, 8), ach(5, 3, 3, 3, 9)]


def mock_ra(http, achievements=SAMPLE, awards=None, pictures=True):
    earned = http.get(f"{RA}/API_GetAchievementsEarnedBetween.php").mock(return_value=httpx.Response(200, json=achievements))
    got = http.get(f"{RA}/API_GetUserAwards.php").mock(return_value=httpx.Response(
        200, json={"TotalAwardsCount": 0, "VisibleUserAwards": awards or []}))
    http.get(host="media.retroachievements.org").mock(return_value=httpx.Response(
        200 if pictures else 404, content=JPEG, headers={"content-type": "image/jpeg"}))
    return earned, got


def collect(tmp_path, cfg=CFG, setup=mock_ra):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_retroachievements(client, cfg, START, END, tmp_path)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        calls = setup(http)
        return run(go()), calls


def ra_person(people):
    people[1] = {**ANNA, "RECAP_RA_USERNAME": "retro_anna", "RECAP_RA_API_KEY": KEY}


# --- settings ---

def test_feature_is_off_without_both_settings(people):
    assert "retroachievements" not in mod.load_config(1).sources()
    for values in ({"RECAP_RA_USERNAME": "retro_anna"}, {"RECAP_RA_API_KEY": KEY}):
        people[1] = {**ANNA, **values}
        assert "retroachievements" not in mod.load_config(1).sources()
    ra_person(people)
    cfg = mod.load_config(1)
    assert "retroachievements" in cfg.sources() and (cfg.ra_username, cfg.ra_api_key) == ("retro_anna", KEY)


def test_build_without_settings_has_no_slide_and_no_calls(people, services):
    route = services.get(f"{RA}/API_GetAchievementsEarnedBetween.php").mock(return_value=httpx.Response(200, json=SAMPLE))
    data = build_for()
    assert data["retroachievements"] == {} and "retroachievements" not in data["errors"] and route.call_count == 0
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "s-ra" not in page and "Achievements" not in page and "Erfolge" not in page
    assert "## Achievements" not in views.render_obsidian(data, "/x")


def test_settings_are_in_manifest_and_code():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "familienhub-plugin.json").read_text())
    mine = {s["key"]: s for s in manifest["personal_settings"]}
    assert mine["RECAP_RA_USERNAME"]["type"] == "text" and mine["RECAP_RA_API_KEY"]["type"] == "password"
    assert not mine["RECAP_RA_API_KEY"].get("default") and not mine["RECAP_RA_API_KEY"]["required"]
    assert {"retroachievements.org", "media.retroachievements.org"} <= set(manifest["hosts"])


# --- collector ---

def test_month_window_is_sent_in_the_hubs_timezone(tmp_path):
    out, (earned, _) = collect(tmp_path)
    params = dict(earned.calls[0].request.url.params)
    assert params["u"] == "retro_anna" and params["y"] == KEY
    assert params["f"] == str(int(dt.datetime(2026, 9, 30, 22, tzinfo=dt.timezone.utc).timestamp()))  # 1 Oct 00:00 Berlin
    assert params["t"] == str(int(dt.datetime(2026, 10, 31, 23, tzinfo=dt.timezone.utc).timestamp()))  # 1 Nov 00:00 Berlin
    assert out["total"] == 5


def test_aggregation(tmp_path):
    out, _ = collect(tmp_path)
    assert out["total"] == 5 and out["points"] == 93 and out["true_points"] == 523
    assert out["hardcore"] == 4 and out["softcore"] == 1 and out["games_count"] == 3
    assert [(g["name"], g["count"], g["points"]) for g in out["games"]] == [("Game 1", 3, 40), ("Game 2", 1, 50), ("Game 3", 1, 3)]
    assert [x["title"] for x in out["rarest"]][:2] == ["Achievement 2", "Achievement 1"]  # 400/5 and 20/10 per point
    assert all(x["badge"] and x["badge"].startswith("img/ra-b") for x in out["rarest"])
    assert all(g["icon"] and g["icon"].startswith("img/ra-g") for g in out["games"])
    assert not any(k.startswith("_") for x in out["rarest"] + out["games"] for k in x)
    assert len(list(tmp_path.glob("ra-*"))) == 5 + 3


def test_items_outside_the_month_and_duplicates_are_handled(tmp_path):
    items = [ach(1, 1, 10, 20, hardcore=0), ach(1, 1, 10, 20, hardcore=1), ach(2, day=30, Date="2026-09-30 21:59:59"),
             ach(3, Date="2026-10-31 23:00:00"), {"junk": True}, "x", ach(4, Title="  ")]
    out, _ = collect(tmp_path, setup=lambda http: mock_ra(http, items))
    assert out["total"] == 1 and out["hardcore"] == 1 and out["points"] == 10  # twice earned counts once, as hardcore


def test_awards_in_the_month_only(tmp_path):
    awards = [award(1), award(2, "Mastery/Completion", 0), award(3, "Game Beaten", 1), award(4, "Game Beaten", 0, "2026-09-30T20:00:00+00:00"),
              award(1), {"AwardType": "Site Award", "AwardedAt": "2026-10-02T00:00:00+00:00", "Title": "x"}]
    out, (_, got) = collect(tmp_path, setup=lambda http: mock_ra(http, awards=awards))
    assert [(a["title"], a["kind"], a["hardcore"]) for a in out["awards"]] == [
        ("Game 1", "mastered", True), ("Game 2", "completed", False), ("Game 3", "beaten", True)]
    assert all(a["icon"] and a["icon"].startswith("img/ra-a") for a in out["awards"]) and got.call_count == 1


def test_award_failure_keeps_the_achievements(tmp_path):
    def setup(http):
        mock_ra(http)
        http.get(f"{RA}/API_GetUserAwards.php").mock(return_value=httpx.Response(500))
    out, _ = collect(tmp_path, setup=setup)
    assert out["total"] == 5 and out["awards"] == []


def test_failing_pictures_give_placeholders(tmp_path):
    out, _ = collect(tmp_path, setup=lambda http: mock_ra(http, pictures=False))
    assert out["total"] == 5 and all(x["badge"] is None for x in out["rarest"]) and all(g["icon"] is None for g in out["games"])


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(401, json={"message": "bad key"}),
                                      httpx.Response(200, json={"message": "odd"}), httpx.Response(200, text="not json")])
def test_api_errors_raise_without_the_key(tmp_path, response):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_retroachievements(client, CFG, START, END, tmp_path)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        http.get(f"{RA}/API_GetAchievementsEarnedBetween.php").mock(return_value=response)
        with pytest.raises(Exception) as err:
            run(go())
    assert KEY not in collectors.describe_error(err.value)


# --- build ---

def test_build_stores_data_renders_slides_and_hides_the_key(people, services, caplog):
    ra_person(people)
    mock_ra(services, awards=[award(1)])
    with caplog.at_level(logging.DEBUG):
        data = build_for()
    ra = data["retroachievements"]
    assert ra["total"] == 5 and data["errors"] == {} and ra["awards"][0]["kind"] == "mastered"
    assert data["highlights"]["ra_awards"][0]["title"] == "Game 1"
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "s-ra" in page, data["errors"]
    assert "s-ra_awards" in page and "Game 1" in page and "gemeistert" in page
    assert (store.month_dir(1, MONTH) / "img" / ra["rarest"][0]["badge"].split("/")[1]).is_file()
    assert "media.retroachievements.org" not in page and "retroachievements.org" not in page
    obsidian = views.render_obsidian(data, "/x")
    assert "## Achievements" in obsidian and "erfolge: 5" in obsidian
    for blob in (json.dumps(data), json.dumps(store.read_snapshot(1, MONTH)), page, obsidian, caplog.text):
        assert KEY not in blob and "retro_anna" not in blob


def test_clips_stay_unique_with_the_new_slides(people, services):
    ra_person(people)
    mock_ra(services, awards=[award(1)])
    page = story.render(build_for(), None, [])
    clips = [c for _, c in re.findall(r'<section class="slide s-(\w+)[^"]*"[^>]*?data-clip="([^"]+)"', page)]
    assert "s-ra " in page and "s-ra_awards" in page and len(clips) == len(set(clips))


def test_api_error_is_soft_and_the_recap_still_builds(people, services):
    ra_person(people)
    services.get(f"{RA}/API_GetAchievementsEarnedBetween.php").mock(return_value=httpx.Response(500))
    data = build_for()
    assert data["errors"] == {"retroachievements": "HTTPStatusError 500"} and data["retroachievements"] == {}
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "s-ra" not in page and "Great Show" in page


def test_a_ra_only_person_gets_a_recap(people, services):
    people[2] = {"RECAP_RA_USERNAME": "retro_ben", "RECAP_RA_API_KEY": KEY}
    mock_ra(services)
    data = run(build.build_month(2, MONTH, mod.load_config(2), TZ))
    assert data["retroachievements"]["total"] == 5 and not data.get("empty") and store.has_story(2, MONTH)


def test_no_achievements_in_the_month_gives_no_slide(people, services):
    ra_person(people)
    mock_ra(services, achievements=[])
    data = build_for()
    assert data["retroachievements"]["total"] == 0 and "s-ra" not in (store.month_dir(1, MONTH) / "index.html").read_text()


# --- story ---

def sample_ra(awards=True):
    badge = lambda i: f"img/ra-b{i}.png"  # noqa: E731
    return {"total": 12, "points": 230, "true_points": 900, "hardcore": 12, "softcore": 0, "games_count": 2,
            "games": [{"name": "Alpha Quest", "console": "SNES", "count": 8, "points": 150, "icon": None},
                      {"name": "Beta Run", "console": "NES", "count": 4, "points": 80, "icon": "img/ra-g2.png"}],
            "rarest": [{"title": f"Rare {i}", "description": "d", "points": 10, "true_points": 100, "hardcore": True, "game": "Alpha Quest",
                        "badge": badge(i) if i % 2 else None} for i in range(6)],
            "awards": [{"title": "Alpha Quest", "console": "SNES", "kind": "mastered", "hardcore": True, "icon": None}] if awards else []}


def ra_story(ra=None, soundtrack=0, **design):
    data = {"month": "2026-10", "seed": "1", "errors": {}, "soundtrack": [
        {"src": f"img/clip-{i:02d}.mp3", "title": f"Song {i}", "artist": "Band"} for i in range(soundtrack)],
        "jellyfin": {}, "music": {}, "audiobooks": {}, "podcasts": {}, "photos": {}, "games": {},
        "retroachievements": sample_ra() if ra is None else ra}
    data["totals"] = build.compute_totals(data)
    data["highlights"] = story.highlights(data, [])
    data["design"] = story.choose_design(data, [])
    data["design"].update(design)
    return data


@pytest.mark.parametrize("variant", [0, 1])
def test_each_ra_variant_renders(variant):
    data = ra_story()
    data["design"]["variants"]["ra"] = variant
    page = story.render(data, None, [])
    slide = page.split("s-ra ", 1)[1].split("</section>")[0]
    assert "230" in slide and "Alpha Quest" in slide and ("Rare 0" in slide if variant == 0 else "Beta Run" in slide)
    assert "alle im Hardcore-Modus" in slide and 'class="ph ' in slide  # missing badges: generated tiles
    assert "s-ra_awards" in page and "gemeistert" in page


def test_slides_follow_the_games_and_are_absent_without_data():
    data = ra_story()
    data["games"] = {"hours": 2.0, "minutes": 120, "sessions": 2, "games_count": 1, "systems_count": 1, "systems": [], "top_system": "SNES",
                     "games": [{"name": "Zed", "system": "SNES", "minutes": 120, "sessions": 2, "cover": None, "new": False}]}
    data["totals"] = build.compute_totals(data)
    page = story.render(data, None, [])
    assert page.index("s-games") < page.index("s-ra ") < page.index("s-ra_awards") < page.index("s-outro")
    for empty in ({}, {"total": 0}):
        assert "s-ra" not in story.render(ra_story(empty), None, [])
    assert "s-ra_awards" not in story.render(ra_story(sample_ra(awards=False)), None, [])


def test_design_stored_before_ra_still_renders():
    data = ra_story()
    del data["design"]["variants"]["ra"]
    del data["design"]["texts"]["ra"]
    assert "s-ra" in story.render(data, None, [])


def test_other_design_choices_are_unchanged_by_the_new_slide():
    games_only = story.choose_design({"month": "2026-10", "seed": "1"}, [])
    assert games_only["variants"]["games"] in range(3) and games_only["variants"]["ra"] in range(2)
    assert list(games_only["variants"])[-2:] == ["games", "ra"]


@pytest.mark.parametrize("count", [3, 22])
def test_clips_never_repeat_with_ra_slides(count):
    page = story.render(ra_story(soundtrack=count), None, [])
    used = [c for _, c in re.findall(r'<section class="slide s-(\w+)[^"]*"[^>]*?data-clip="([^"]+)"', page)]
    assert "s-ra " in page and used and len(used) == len(set(used))


def test_outro_counts_the_achievements():
    assert "Erfolge" in story.render(ra_story(), None, []).split("s-outro", 1)[1]


# --- connection test ---

def test_connection_test(people, services):
    values = {"RECAP_RA_USERNAME": "retro_anna", "RECAP_RA_API_KEY": KEY}
    route = services.get(f"{RA}/API_GetUserProfile.php").mock(return_value=httpx.Response(200, json={"User": "retro_anna"}))
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), values))
    assert result["ok"] and "RetroAchievements: connected" in result["message"] and KEY not in result["message"]
    assert dict(route.calls[0].request.url.params)["y"] == KEY
    route.mock(return_value=httpx.Response(401))
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), values))
    assert not result["ok"] and "RetroAchievements: login rejected" in result["message"] and KEY not in result["message"]
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), {"RECAP_RA_USERNAME": "retro_anna"}))
    assert "incomplete" in result["message"]
