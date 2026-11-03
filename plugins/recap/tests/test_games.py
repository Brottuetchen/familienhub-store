"""Tests for the games section: ES-DE statistics, name and cover handling, the monthly difference of cumulative
numbers, the daily snapshots, the story and the settings. No network: Steam is simulated with respx."""

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from fh_plugin_recap import build, collectors, plugin as mod, store, story, views
from fh_plugin_recap.plugin import RecapPlugin

from helpers import ANNA, JPEG, install
from test_plugin import MONTH, TZ, build_for, people, run, services  # noqa: F401 - fixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 800
NFS = "Need for Speed - Underground 2 (Europe) (En,Fr,De,Es,It,Nl,Sv).iso"
MARIO = "Super Mario Maker 2/Super Mario Maker 2.xci"
WITCHER = "The Witcher 3 Wild Hunt — Remastered.desktop"
START, END = dt.datetime(2026, 10, 1, tzinfo=TZ), dt.datetime(2026, 11, 1, tzinfo=TZ)


def entry(playtime, playcount, last="20261025T114002"):
    return {"flags": {"lastplayed": [last, 1793085927]}, "playtime": playtime, "playcount": playcount}


def device_file(**games):
    """``games``: system -> {relative path: entry}; the ES-DE file format."""
    return {system: {"_system": {"flags": {}}, **{f"game:./{rel}": e for rel, e in entries.items()}}
            for system, entries in games.items()}


def write_device(directory, name, content):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(content), encoding="utf-8")


@pytest.fixture
def gamedirs(tmp_path, monkeypatch):
    games, roms = tmp_path / "games", tmp_path / "roms"
    monkeypatch.setenv("RECAP_GAMES_DIR", str(games))
    monkeypatch.setenv("RECAP_ROMS_DIR", str(roms))
    return games, roms


def rec(playtime, playcount, last="20261025T114002"):
    return {"playtime": playtime, "playcount": playcount, "lastplayed": last}


# --- names, systems, covers ---

@pytest.mark.parametrize("rel, name", [
    ("Need for Speed - Underground 2 (Europe) (En,Fr,De,Es,It,Nl,Sv).iso", "Need for Speed - Underground 2"),
    ("Simpsons, The - Hit & Run (USA).iso", "The Simpsons - Hit & Run"),
    ("Legend of Zelda, The (Rev 2) [!].nes", "The Legend of Zelda"),
    ("Final Fantasy VII (Disque 1) (v2.01).bin", "Final Fantasy VII"),
    ("Super Mario Maker 2/Super Mario Maker 2.xci", "Super Mario Maker 2"),
    ("The Witcher 3 Wild Hunt — Remastered.desktop", "The Witcher 3 Wild Hunt — Remastered"),
    ("Cyberpunk 2077.desktop", "Cyberpunk 2077"),
    ("Sonic Vol.2.zip", "Sonic Vol.2"),
    ("", ""),
])
def test_game_names_are_cleaned(rel, name):
    assert collectors.clean_game_name(rel) == name


def test_system_labels():
    assert collectors.system_label("ps2") == "PlayStation 2"
    assert collectors.system_label("windows") == "PC" and collectors.system_label("megadrive") == "Mega Drive"
    assert collectors.system_label("atarist") == "ATARIST"


def test_devices_are_plain_file_stems():
    assert collectors.parse_devices(" pc, deck,pc,../x,,a b") == ("pc", "deck")
    assert collectors.parse_devices(None) == ()


def test_cover_candidates_for_files_and_folders():
    assert collectors.game_cover_candidates("ps2", NFS) == ["ps2/covers/Need for Speed - Underground 2 (Europe) (En,Fr,De,Es,It,Nl,Sv).png"]
    assert collectors.game_cover_candidates("switch", MARIO) == [
        "switch/covers/Super Mario Maker 2/Super Mario Maker 2.png", "switch/covers/Super Mario Maker 2.png"]
    assert collectors.game_cover_candidates("x", "") == []


def test_local_cover_is_copied_and_checked(tmp_path):
    roms, img = tmp_path / "roms", tmp_path / "img"
    img.mkdir()
    (roms / "switch" / "covers").mkdir(parents=True)
    (roms / "switch" / "covers" / "Super Mario Maker 2.png").write_bytes(PNG)
    assert collectors.local_game_cover(str(roms), "switch", MARIO, img, "gm-mario") == "img/gm-mario.png"
    assert (img / "gm-mario.png").read_bytes() == PNG
    (roms / "switch" / "covers" / "Super Mario Maker 2").mkdir()
    (roms / "switch" / "covers" / "Super Mario Maker 2" / "Super Mario Maker 2.png").write_bytes(JPEG)  # the specific one wins
    assert collectors.local_game_cover(str(roms), "switch", MARIO, img, "gm-mario") == "img/gm-mario.jpg"
    (roms / "ps2" / "covers").mkdir(parents=True)
    (roms / "ps2" / "covers" / (NFS[:-4] + ".png")).write_bytes(b"not an image")
    assert collectors.local_game_cover(str(roms), "ps2", NFS, img, "x") is None  # content is checked, not the name
    assert collectors.local_game_cover(str(roms), "../etc", NFS, img, "x") is None
    assert collectors.local_game_cover(str(tmp_path / "missing"), "ps2", NFS, img, "x") is None


# --- reading the device files ---

def test_read_games_keeps_only_games_and_survives_odd_data(tmp_path):
    write_device(tmp_path, "pc", {**device_file(ps2={NFS: entry(821, 4)}, steam={WITCHER: entry(3600, 2)}),
                                  "bad": "x", "gba": {"game:./A.gba": {"playtime": "oops", "flags": []}, "other": {}}})
    (tmp_path / "broken.json").write_text("{nope")
    got = collectors.read_games(str(tmp_path), ("pc", "broken", "missing", "../pc"))
    assert list(got) == ["pc"]
    assert got["pc"][f"ps2/{NFS}"] == {"playtime": 821, "playcount": 4, "lastplayed": "20261025T114002"}
    assert got["pc"]["gba/A.gba"] == {"playtime": 0, "playcount": 0, "lastplayed": None}
    assert not any("_system" in k or "other" in k for k in got["pc"])


# --- the monthly difference ---

def test_delta_subtracts_the_baseline():
    base = {"pc": {"ps2/a.iso": rec(600, 2), "ps2/idle.iso": rec(900, 3)}}
    final = {"pc": {"ps2/a.iso": rec(2400, 5), "ps2/idle.iso": rec(900, 3), "ps2/new.iso": rec(300, 1)}}
    got = collectors.games_delta(base, final, START, END)
    assert got == {"ps2/a.iso": {"seconds": 1800, "sessions": 3, "new": False},
                   "ps2/new.iso": {"seconds": 300, "sessions": 1, "new": True}}


def test_first_month_without_snapshot_uses_the_last_played_date():
    final = {"pc": {"ps2/in.iso": rec(5000, 4, "20261025T114002"), "ps2/old.iso": rec(9000, 9, "20260915T100000"),
                    "ps2/next.iso": rec(100, 1, "20261102T100000"), "ps2/none.iso": rec(100, 1, None)}}
    got = collectors.games_delta(None, final, START, END)
    assert list(got) == ["ps2/in.iso"] and got["ps2/in.iso"]["seconds"] == 5000 and got["ps2/in.iso"]["new"] is True


def test_two_devices_are_merged_and_a_late_device_does_not_count_its_history():
    base = {"pc": {"switch/m.xci": rec(1000, 1)}}
    final = {"pc": {"switch/m.xci": rec(1600, 2)},
             "deck": {"switch/m.xci": rec(7200, 5, "20260820T100000"), "steam/z.desktop": rec(1200, 2, "20261010T100000")}}
    got = collectors.games_delta(base, final, START, END)
    # deck had no snapshot: only what was played on it within the month, and the old Switch game stays out
    assert got == {"switch/m.xci": {"seconds": 600, "sessions": 1, "new": False},
                   "steam/z.desktop": {"seconds": 1200, "sessions": 2, "new": True}}
    both = {"pc": {"switch/m.xci": rec(1600, 2)}, "deck": {"switch/m.xci": rec(300, 1)}}
    base2 = {"pc": {"switch/m.xci": rec(1000, 1)}, "deck": {}}
    assert collectors.games_delta(base2, both, START, END)["switch/m.xci"] == {"seconds": 900, "sessions": 2, "new": False}


def test_collect_games_builds_the_numbers_and_covers(tmp_path, gamedirs):
    games, roms = gamedirs
    img = tmp_path / "img"
    img.mkdir()
    (roms / "ps2" / "covers").mkdir(parents=True)
    (roms / "ps2" / "covers" / (NFS[:-4] + ".png")).write_bytes(PNG)
    final = {"pc": {
        f"ps2/{NFS}": rec(7200, 4), f"steam/{WITCHER}": rec(3600, 2), f"switch/{MARIO}": rec(1800, 1), "ps3/Gone (USA).pkg": rec(600, 1)}}
    cfg = collectors.Config(games_dir=str(games), roms_dir=str(roms), games_devices=("pc",))

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, cfg, START, END, img, None, final)

    with respx.mock(using="httpx", assert_all_called=False) as http:
        search = http.get("https://store.steampowered.com/api/storesearch/").mock(side_effect=lambda r: httpx.Response(
            200, json={"items": [{"id": 292030}]} if "Witcher" in r.url.params["term"] else {"items": []}))
        http.get("https://cdn.cloudflare.steamstatic.com/steam/apps/292030/library_600x900.jpg").mock(return_value=httpx.Response(404))
        header = http.get("https://cdn.cloudflare.steamstatic.com/steam/apps/292030/header.jpg").mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        out = run(go())
    assert out["hours"] == 3.7 and out["minutes"] == 220 and out["sessions"] == 8 and out["games_count"] == 4
    assert out["systems_count"] == 4 and out["top_system"] == "PlayStation 2"
    assert [g["name"] for g in out["games"]] == ["Need for Speed - Underground 2", "The Witcher 3 Wild Hunt — Remastered",
                                                  "Super Mario Maker 2", "Gone"]
    nfs, witcher, mario, gone = out["games"]
    assert nfs["cover"] == "img/gm-need-for-speed-underground-2.png" and nfs["system"] == "PlayStation 2" and nfs["minutes"] == 120
    assert witcher["cover"] and witcher["cover"].endswith(".jpg") and header.call_count == 1  # Steam: portrait missing, header used
    assert mario["cover"] is None and gone["cover"] is None  # no local file and no Steam hit: the story shows a tile
    assert dict(search.calls[0].request.url.params)["cc"] == "DE"


def test_collect_games_without_readable_files_raises(tmp_path):
    cfg = collectors.Config(games_dir=str(tmp_path), games_devices=("pc",))

    async def go():
        async with collectors.new_client() as client:
            return await collectors.collect_games(client, cfg, START, END, tmp_path, None)

    with pytest.raises(LookupError):
        run(go())


# --- snapshots and the build ---

def pc_file(gamedirs, **kw):
    write_device(gamedirs[0], "pc", device_file(**kw))


def games_person(people):
    people[1] = {**ANNA, "RECAP_GAMES_DEVICES": "pc"}


def test_snapshot_is_written_once_per_day_and_only_when_enabled(people, gamedirs):
    pc_file(gamedirs, ps2={NFS: entry(821, 4)})
    cfg = mod.load_config(1)
    assert build.snapshot_games(1, cfg, TZ) is False and store.game_days(1) == []  # games not enabled for this person
    games_person(people)
    cfg = mod.load_config(1)
    assert cfg.games_devices == ("pc",) and "games" in cfg.sources()
    assert build.snapshot_games(1, cfg, TZ) is True
    assert build.snapshot_games(1, cfg, TZ) is False
    (day,) = store.game_days(1)
    assert day == dt.datetime.now(TZ).date().isoformat()
    assert store.read_games(1, day)["pc"][f"ps2/{NFS}"]["playtime"] == 821


def test_snapshot_is_silent_without_files_or_disk(people, gamedirs, monkeypatch):
    games_person(people)
    cfg = mod.load_config(1)
    assert build.snapshot_games(1, cfg, TZ) is False and store.game_days(1) == []  # nothing mounted
    pc_file(gamedirs, ps2={NFS: entry(1, 1)})
    monkeypatch.setattr(store, "write_games", lambda *a: (_ for _ in ()).throw(OSError("full")))
    assert build.snapshot_games(1, cfg, TZ) is False


def test_scheduler_takes_the_snapshot_even_while_a_build_runs(people, gamedirs, monkeypatch):
    games_person(people)
    pc_file(gamedirs, ps2={NFS: entry(821, 4)})
    monkeypatch.setattr(build, "busy", lambda: True)
    from test_plugin import FakeDB

    run(RecapPlugin().scheduled(FakeDB([1, 2])))
    assert len(store.game_days(1)) == 1 and store.game_days(2) == []


def test_state_selection_around_the_month(people):
    for day, playtime in (("2026-09-20", 100), ("2026-09-30", 200), ("2026-10-15", 300), ("2026-11-01", 400), ("2026-11-05", 500)):
        store.write_games(1, day, {"pc": {"ps2/a.iso": rec(playtime, 1)}})
    base, final = build.game_states(1, START, END)
    assert base["pc"]["ps2/a.iso"]["playtime"] == 200 and final["pc"]["ps2/a.iso"]["playtime"] == 400
    assert build.game_states(2, START, END) == (None, None)
    store.write_games(2, "2026-10-01", {"pc": {}})
    assert build.game_states(2, START, END)[0] == {"pc": {}}  # a snapshot on the first day itself is the baseline


def test_build_uses_snapshots_and_renders_the_games_slide(people, services, gamedirs):
    games_person(people)
    pc_file(gamedirs, ps2={NFS: entry(99999, 99)}, steam={WITCHER: entry(99999, 9, "20261201T100000")})  # December, must not count
    store.write_games(1, "2026-09-30", {"pc": {f"ps2/{NFS}": rec(600, 2)}})
    store.write_games(1, "2026-11-01", {"pc": {f"ps2/{NFS}": rec(600 + 3 * 3600, 6), f"steam/{WITCHER}": rec(7200, 3, "20261020T200000")}})
    (gamedirs[1] / "ps2" / "covers").mkdir(parents=True)
    (gamedirs[1] / "ps2" / "covers" / (NFS[:-4] + ".png")).write_bytes(PNG)
    services.get("https://store.steampowered.com/api/storesearch/").mock(return_value=httpx.Response(200, json={"items": []}))
    data = build_for()
    ga = data["games"]
    assert ga["hours"] == 5.0 and ga["sessions"] == 7 and ga["games_count"] == 2 and ga["top_system"] == "PlayStation 2"
    assert [g["name"] for g in ga["games"]] == ["Need for Speed - Underground 2", "The Witcher 3 Wild Hunt — Remastered"]
    assert [g["new"] for g in ga["games"]] == [False, True]
    assert data["totals"]["games_hours"] == 5.0 and data["highlights"].get("first_games") is True
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "s-games" in page and "Need for Speed" in page and 'data-clip' in page
    assert (store.month_dir(1, MONTH) / "img" / "gm-need-for-speed-underground-2.png").is_file()
    assert "Games" in views.render_obsidian(data, "/x") and "## Games" in views.render_obsidian(data, "/x")
    assert "spiele_stunden: 5.0" in views.render_obsidian(data, "/x")


def test_a_games_only_person_gets_a_recap(people, gamedirs, services):
    people[2] = {"RECAP_GAMES_DEVICES": "pc"}
    pc_file(gamedirs, ps2={NFS: entry(7200, 3, "20261012T100000")})
    data = run(build.build_month(2, MONTH, mod.load_config(2), TZ))
    assert data["games"]["hours"] == 2.0 and not data.get("empty") and store.has_story(2, MONTH)


# --- the story ---

def sample_games():
    names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]
    return {"hours": 30.5, "minutes": 1830, "sessions": 22, "games_count": 6, "systems_count": 2,
            "systems": [{"name": "PC", "minutes": 1500}, {"name": "SNES", "minutes": 330}], "top_system": "PC",
            "games": [{"name": n, "system": "PC", "minutes": 700 - 100 * i, "sessions": 5 - i % 3, "cover": None, "new": i == 5}
                      for i, n in enumerate(names)]}


def story_data(month="2026-10", with_games=True, **design):
    data = {"month": month, "seed": "1", "errors": {}, "soundtrack": [], "jellyfin": {}, "music": {}, "audiobooks": {},
            "podcasts": {}, "photos": {}, "games": sample_games() if with_games else {}}
    data["totals"] = build.compute_totals(data)
    data["highlights"] = story.highlights(data, [])
    data["design"] = story.choose_design(data, [])
    data["design"]["lead"] = story.pick_lead(data, data["highlights"], [])
    data["design"].update(design)
    return data


@pytest.mark.parametrize("variant", [0, 1, 2])
def test_each_games_variant_renders(variant):
    data = story_data()
    data["design"]["variants"]["games"] = variant
    page = story.render(data, None, [])
    assert "s-games" in page and "Alpha" in page and "Stunden gespielt" in page and "30,5" in page
    assert "Zeta" not in page.split("s-games", 1)[1].split("</section>")[0]  # only the top five
    assert "Dauerbrenner" in page  # 700 minutes of one game: the marathon highlight
    assert page.count('class="ph ') >= 1  # no covers: generated tiles


def test_story_without_games_is_unchanged_by_the_feature():
    old = story_data(with_games=False)
    page = story.render(old, None, [])
    assert "s-games" not in page and "Games" not in page.split("Alle Rückblicke")[0].split('class="slide s-total')[1].split("</section>")[0]
    # a design stored before games existed (no entry for them) renders, with or without games
    for with_games in (False, True):
        data = story_data(with_games=with_games)
        del data["design"]["variants"]["games"]
        del data["design"]["texts"]["games"]
        assert "s-outro" in story.render(data, None, [])


def test_design_choice_for_other_slides_is_untouched_and_games_do_not_repeat():
    data = story_data(with_games=False)
    first = story.choose_design(data, [])
    other = story.choose_design({**data, "month": "2026-11"}, [{**data, "design": first}])
    assert other["variants"]["games"] != first["variants"]["games"] and other["texts"]["games"] != first["texts"]["games"]
    for key in story.VARIANTS:
        if key not in story.LATE_SLIDES:
            assert 0 <= other["variants"][key] < story.VARIANTS[key]
    assert first["palette"] in story.PALETTES and first["offset"] in range(6)
    assert list(first["variants"]) == [k for k in story.VARIANTS if k not in story.LATE_SLIDES] + ["games"]


def test_highlights_for_games():
    data = story_data()
    assert data["highlights"]["first_games"] is True and data["highlights"]["game_marathon"]["name"] == "Alpha"
    later = story.highlights(data, [data])
    assert "first_games" not in later
    data["games"]["games"][0]["minutes"] = 599
    assert "game_marathon" not in story.highlights(data, [])
    assert "first_games" not in story.highlights(story_data(with_games=False), [])


def test_totals_count_games_hours():
    totals = build.compute_totals({"games": {"hours": 2.5}, "music": {"hours": 1.0}})
    assert totals["games_hours"] == 2.5 and totals["media_hours"] == 3.5
    assert build.compute_totals({})["games_hours"] == 0


def test_intro_collage_may_show_game_covers():
    data = story_data()
    for g in data["games"]["games"]:
        g["cover"] = "img/gm-x.png"
    data["design"]["variants"]["intro"] = 1
    assert 'class="collage"' in story.render(data, None, [])


# --- settings and connection test ---

def test_settings_are_in_manifest_and_code():
    from pathlib import Path

    manifest = json.loads((Path(__file__).resolve().parents[1] / "familienhub-plugin.json").read_text())
    admin = {s["key"]: s for s in manifest["settings"]}
    assert admin["RECAP_GAMES_DIR"]["default"] == "/app/external/games" and admin["RECAP_ROMS_DIR"]["default"] == "/app/external/roms"
    mine = {s["key"]: s for s in manifest["personal_settings"]}
    assert mine["RECAP_GAMES_DEVICES"]["label"]["de"] == "Spielgeräte (ES-DE)" and mine["RECAP_GAMES_DEVICES"]["type"] == "text"
    assert {"store.steampowered.com", "cdn.cloudflare.steamstatic.com"} <= set(manifest["hosts"])


def test_config_defaults_and_overrides(people, monkeypatch):
    cfg = mod.load_config(1)
    assert cfg.games_dir == "/app/external/games" and cfg.roms_dir == "/app/external/roms" and cfg.games_devices == ()
    monkeypatch.setenv("RECAP_GAMES_DIR", "/data/g")
    games_person(people)
    assert mod.load_config(1).games_dir == "/data/g" and mod.load_config(1).sources()[-1] == "games"
    assert mod.load_config(1, overrides={"RECAP_GAMES_DEVICES": "pc, deck"}).games_devices == ("pc", "deck")


def test_connection_test_for_games(people, gamedirs, services):
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), {"RECAP_GAMES_DEVICES": "pc"}))
    assert not result["ok"] and "Games: no device file readable" in result["message"]
    pc_file(gamedirs, ps2={NFS: entry(1, 1)})
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), {"RECAP_GAMES_DEVICES": "deck, pc"}))
    assert result["ok"] and "Games: connected" in result["message"] and str(gamedirs[0]) not in result["message"]
    assert run(RecapPlugin().test_personal_settings(SimpleNamespace(id=2), {}))["level"] == "info"


def test_steam_terms_loosen_dash_and_edition_suffixes():
    assert collectors.steam_terms("The Witcher 3 Wild Hunt — Remastered") == [
        "The Witcher 3 Wild Hunt — Remastered", "The Witcher 3 Wild Hunt"]
    assert collectors.steam_terms("Crimson Desert Enhanced") == ["Crimson Desert Enhanced", "Crimson Desert"]
    assert collectors.steam_terms("Cyberpunk 2077") == ["Cyberpunk 2077"]


def test_steam_cover_retries_with_a_looser_term(tmp_path):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.steam_cover(client, tmp_path, "The Witcher 3 Wild Hunt — Remastered", "gm-w3")

    with respx.mock(using="httpx", assert_all_called=False) as http:
        search = http.get("https://store.steampowered.com/api/storesearch/").mock(side_effect=lambda r: httpx.Response(
            200, json={"items": [{"id": 292030}]} if r.url.params["term"] == "The Witcher 3 Wild Hunt" else {"items": []}))
        http.get("https://cdn.cloudflare.steamstatic.com/steam/apps/292030/library_600x900.jpg").mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        out = run(go())
    assert out == "img/gm-w3.jpg" and search.call_count == 2
