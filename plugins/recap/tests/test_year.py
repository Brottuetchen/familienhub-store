"""The annual recap: building a year from mocked sources, the January schedule, the archive, estimated game time, the
year's own design and that every monthly design and render stays exactly as it was."""

import asyncio
import hashlib
import json
import re
import time
from types import SimpleNamespace

import httpx
import pytest
import respx

from fh_plugin_recap import build, collectors, gallery, plugin as mod, sample, store, story, views, year as yearly
from fh_plugin_recap.plugin import RecapPlugin

from helpers import ANNA
from test_plugin import FakeDB, MONTH, TZ, client, page, people, run, services, wait_for_months  # noqa: F401 - fixtures
from test_steam import GAMES, KEY, STEAM_ID, app, mock_steam, snap, steam_person  # noqa: F401 - fixtures

YEAR = "2026"


def build_year_for(uid=1, year=YEAR, **kw):
    return run(build.build_year(uid, year, mod.load_config(uid), TZ, **kw))


# --- building a year ---

def test_year_build_end_to_end_with_mocked_sources(people, services):
    data = build_year_for()
    assert data["errors"] == {} and data["year"] == 2026 and "month" not in data
    folder = store.month_dir(1, YEAR)
    html = (folder / "index.html").read_text()
    assert store.read_snapshot(1, YEAR)["year"] == 2026 and store.has_story(1, YEAR)
    assert store.list_years(1) == [YEAR] and store.list_months(1) == []  # a year is not a month
    assert (store.user_dir(1) / "2026.json").is_file() and not list(store.user_dir(1).glob(".build-*"))
    # the collectors ran over the whole year
    assert data["jellyfin"]["series"][0]["name"] == "Great Show" and data["music"]["plays"] == 30
    assert data["audiobooks"]["books"][0]["title"] == "Long Book" and data["podcasts"]["podcasts"][0]["title"] == "Talk Show"
    assert data["photos"]["total"] == 5 and len(data["photos"]["highlights"]) == 1  # one picture per month that has photos
    assert data["photos"]["month_counts"]["10"] == 5 and data["music"]["day_minutes"] and data["audiobooks"]["day_minutes"]
    assert data["podcasts"]["day_minutes"]
    # what only a year can show
    months = data["months"]
    assert len(months) == 12 and [m["month"] for m in months] == list(range(1, 13))
    assert months[9]["hours"] > 0 and months[9]["source"] == "data" and months[9]["focus"] and months[0]["hours"] == 0
    assert data["records"]["binge"]["titles"] and data["records"]["week"]["hours"] > 0
    assert data["discoveries"]["series"] == [] and [a["name"] for a in data["discoveries"]["artists"]] == ["Band A", "Band B", "Band C"]  # series need earlier snapshots
    # the story
    keys = re.findall(r'<section class="slide s-(\w+)', html)
    assert keys[0] == "yintro" and keys[-1] == "youtro" and {"ytotal", "ytop", "yrecords", "yphotos"} <= set(keys)
    assert "ycurve" not in keys and "yfocus" not in keys  # one month with data: no curve, no ranking
    assert "Jahresrückblick 2026" in html and 'href="../"' in html and "data-clip=" in html
    assert not re.findall(r'(?:src|href)="https?://', html) and "url('http" not in html
    assert any(f.name.startswith("ph-") for f in (folder / "img").iterdir())


def test_year_build_uses_the_monthly_snapshots_for_the_curve(people, services):
    build_for = lambda month: run(build.build_month(1, month, mod.load_config(1), TZ))  # noqa: E731
    build_for("2026-10")
    first = store.read_snapshot(1, "2026-10")
    # a second, bigger month: copy the snapshot with other numbers (as a real second build would hold)
    second = json.loads(json.dumps(first))
    second.update(month="2026-11")
    second["totals"] = {"series_hours": 40.0, "movie_hours": 0, "music_hours": 5.0, "audiobook_hours": 0, "podcast_hours": 0, "games_hours": 0,
                        "media_hours": 45.0}
    store.write_snapshot(1, "2026-11", second)
    data = build_year_for()
    rows = {m["month"]: m for m in data["months"]}
    assert rows[10]["source"] == "snapshot" and rows[10]["hours"] == first["totals"]["media_hours"]
    assert rows[11]["hours"] == 45.0 and rows[11]["focus"] == "video"
    html = (store.month_dir(1, YEAR) / "index.html").read_text()
    assert "s-ycurve" in html and "s-yfocus" in html and "November war dein stärkster Monat" in html
    assert [h["month"] for h in store.history(1, "2027-01")] == ["2026-10", "2026-11"]  # the year is no month in the history


def test_failing_sources_and_empty_years(people):
    with respx.mock(using="httpx", assert_all_called=False) as http:
        from helpers import install
        install(http, fail=("jellyfin", "music", "audiobooks", "podcasts", "photos"))
        with pytest.raises(build.BuildFailed):
            build_year_for()
    assert store.read_snapshot(1, YEAR) is None and not store.list_years(1)


def test_year_design_does_not_repeat_the_previous_years(people, services):
    first = build_year_for(year="2025")
    second = build_year_for(year=YEAR)
    a, b = first["design"], second["design"]
    assert a["palette"] != b["palette"] and a["shape"] != b["shape"] and a["type"] != b["type"]
    for group in ("variants", "texts"):
        assert all(a[group][k] != b[group][k] for k in a[group]), group
    assert build_year_for()["design"] == b  # a rebuild keeps the design
    assert build_year_for(redesign=True)["design"]["palette"] != b["palette"]


def test_year_design_covers_every_slide_type_and_avoids_the_monthly_streams():
    design = yearly.choose_design({"year": 2026, "seed": "1"}, [])
    assert set(design["variants"]) == set(yearly.YEAR_VARIANTS) == set(design["texts"]) and design["palette"] in yearly.YEAR_PALETTES
    assert all(n >= 2 for n in yearly.YEAR_VARIANTS.values()) and all(len(v) >= 2 for v in yearly.YEAR_KICKERS.values())
    assert not (set(yearly.YEAR_VARIANTS) & set(story.VARIANTS)) and not (set(yearly.YEAR_PALETTES) & set(story.PALETTES))


# --- slides ---

def test_sample_year_renders_every_slide_in_every_variant():
    for n in range(max(yearly.YEAR_VARIANTS.values())):
        sel = {**{k: min(n, c - 1) for k, c in yearly.YEAR_VARIANTS.items()}, "year": 1}
        html = gallery.sample_story("silvester", "blobs", "bold", 0, sel)
        keys = re.findall(r'<section class="slide s-(\w+) v(\d)', html)
        assert {k for k, _ in keys} == set(yearly.YEAR_VARIANTS)
        assert all(v == str(min(n, yearly.YEAR_VARIANTS[k] - 1)) for k, v in keys)
        assert html.count('class="slide s-ytop') == 7  # one per area
        assert "Spielzeit vor November geschätzt" in html and "Jahresrückblick" in html


def test_slides_without_data_are_skipped():
    data = sample.sample_year_input()
    data["design"] = yearly.choose_design(data, [])
    full = re.findall(r'<section class="slide s-(\w+)', yearly.render(data))
    for key in ("retroachievements", "photos", "games", "audiobooks", "podcasts"):
        data[key] = {}
    data["records"], data["discoveries"], data["trip"] = {}, {"artists": [], "series": []}, None
    data["months"] = [m for m in data["months"] if m["month"] == 3]
    keys = re.findall(r'<section class="slide s-(\w+) v\d(?: a-(\w+))?', yearly.render(data))
    assert {k for k, _ in keys} == {"yintro", "ytotal", "ytop", "youtro"} and {a for k, a in keys if k == "ytop"} == {"series", "movies", "artists", "songs"}
    assert set(full) > {k for k, _ in keys}


def test_curve_ranking_and_records_are_computed_from_months_and_data():
    data, snaps = sample.sample_year()
    months = {m["month"]: m for m in data["months"]}
    assert months[10]["hours"] == max(m["hours"] for m in months.values()) and months[10]["source"] == "snapshot"
    assert months[6]["source"] == "data" and months[6]["focus"] == "video"  # no monthly snapshot for June: from the year's daily data
    assert data["records"]["game"]["month"] == max(snaps, key=lambda n: snaps[n]["games"]["games"][0]["minutes"])
    week = data["records"]["week"]
    assert (time_to_date(week["to"]) - time_to_date(week["from"])).days == 6 and time_to_date(week["from"]).weekday() == 0
    assert data["discoveries"]["artists"] and "Nachtschicht" not in data["discoveries"]["series"] and "Kleine Wunder" in data["discoveries"]["series"]
    assert data["trip"]["home"] == "Musterstadt"


def time_to_date(text):
    import datetime as dt
    return dt.date.fromisoformat(text)


def test_discoveries_need_earlier_snapshots_for_series():
    data = {"jellyfin": {"series": [{"name": "A"}, {"name": "B"}]}, "music": {"new_artists": [{"name": "X", "plays": 5}]}}
    assert yearly.discoveries(data, []) == {"artists": [{"name": "X", "plays": 5}], "series": []}
    assert yearly.discoveries(data, [{"jellyfin": {"series": [{"name": "A"}]}}])["series"] == ["B"]


# --- estimated game time ---

def test_steam_games_without_a_baseline_are_marked_approximate(people, services, monkeypatch):
    no_snapshot(monkeypatch)
    steam_person(people)
    mock_steam(services)
    # snapshots only began in November: the first one holds the time before it
    store.write_steam(1, "2026-11-03", snap(app(292030, "The Witcher 3: Wild Hunt", 5900, deck=1000), app(620, "Portal 2", 300),
                                          app(70, "Half-Life", 90)))
    data = build_year_for()
    ga = data["games"]
    assert ga["approx"] is True and ga["approx_since"] == "2026-11-03"
    top = {g["name"]: g["minutes"] for g in ga["games"]}
    assert top["The Witcher 3: Wild Hunt"] == 6000 and top["Portal 2"] == 300 and top["Half-Life"] == 90  # played in the year before the snapshot: playtime_forever
    html = (store.month_dir(1, YEAR) / "index.html").read_text()
    assert "Spielzeit vor November geschätzt" in html and html.count("Spielzeit vor November geschätzt") >= 2  # top 5 and receipt
    assert "geschätzt" in views.render_obsidian_year(data, "/x")


def no_snapshot(monkeypatch):
    """The build also takes today's Steam snapshot; the tests set the snapshots themselves."""
    async def nothing(*a, **kw):
        return False

    monkeypatch.setattr(build, "snapshot_steam", nothing)


def test_steam_games_with_a_baseline_before_the_year_are_exact(people, services, monkeypatch):
    no_snapshot(monkeypatch)
    steam_person(people)
    mock_steam(services)
    store.write_steam(1, "2025-12-31", snap(app(292030, "The Witcher 3: Wild Hunt", 5000, deck=1000), app(620, "Portal 2", 100)))
    ga = build_year_for()["games"]
    assert ga["approx"] is False
    assert {g["name"]: g["minutes"] for g in ga["games"]}["The Witcher 3: Wild Hunt"] == 1000 and {g["name"]: g["minutes"] for g in ga["games"]}["Portal 2"] == 200
    assert "geschätzt" not in (store.month_dir(1, YEAR) / "index.html").read_text()


def test_steam_year_delta_cases():
    start, end = build.year_bounds(2026, TZ)
    inside, before = int(start.timestamp()) + 86400 * 100, int(start.timestamp()) - 86400 * 400
    final = snap(app(1, "In Year", 500, last=inside), app(2, "Old Game", 900, last=before), app(3, "Grown", 700, last=inside),
                 app(4, "New After", 60, last=inside))
    first = snap(app(2, "Old Game", 900, last=before), app(3, "Grown", 600, last=before), app(1, "In Year", 450, last=inside))
    out, approx = collectors.steam_year_delta(None, first, final, start, end, end)
    assert {k: v["minutes"] for k, v in out.items()} == {"1": 500, "3": 100, "4": 60}  # 1: 450 before + 50 since; 3: only the growth
    assert approx is True
    out, approx = collectors.steam_year_delta(None, snap(app(3, "Grown", 600, last=before)), final, start, end, end)
    assert approx is False and out["3"]["minutes"] == 100  # nothing was played in the year before the first snapshot
    out, approx = collectors.steam_year_delta(None, None, final, start, end, end)  # no snapshot at all: playtime_forever of the apps played in the year
    assert {k: v["minutes"] for k, v in out.items()} == {"1": 500, "3": 700, "4": 60} and approx is True
    out, approx = collectors.steam_year_delta(snap(app(3, "Grown", 600)), first, final, start, end, end)
    assert approx is False and out["3"]["minutes"] == 100 and out["2"]["minutes"] == 900  # a real baseline: the plain difference (an app missing from it counts from zero)


def test_esde_year_delta_cases():
    start, end = build.year_bounds(2026, TZ)
    rec = lambda play, count, last: {"playtime": play, "playcount": count, "lastplayed": last}  # noqa: E731
    final = {"pc": {"a/Mario.zip": rec(7200, 12, "20261120T100000"), "a/Old.zip": rec(3600, 3, "20240101T100000"), "a/New.zip": rec(600, 2, "20261201T100000")}}
    first = {"pc": {"a/Mario.zip": rec(3600, 8, "20260315T100000"), "a/Old.zip": rec(3600, 3, "20240101T100000")}}
    played, approx = collectors.games_year_delta(None, first, final, start, end)
    assert approx is True and played["a/Mario.zip"]["seconds"] == 7200 and played["a/Mario.zip"]["sessions"] == 12  # 3600 before the snapshot + 3600 since
    assert "a/Old.zip" not in played and played["a/New.zip"]["seconds"] == 600 and played["a/New.zip"]["new"] is True
    played, approx = collectors.games_year_delta(None, None, final, start, end)  # a device without any snapshot
    assert approx is True and set(played) == {"a/Mario.zip", "a/New.zip"}
    played, approx = collectors.games_year_delta({"pc": {"a/Mario.zip": rec(1000, 1, None)}}, first, final, start, end)
    assert approx is False and played["a/Mario.zip"]["seconds"] == 6200


# --- January schedule, archive, endpoints ---

@pytest.fixture
def fake_builds(monkeypatch):
    calls, sent = [], []

    def fake_for(kind):
        async def fake(uid, period, cfg, tz, redesign=False):
            calls.append((kind, uid, period))
            await asyncio.sleep(0)
            store.month_dir(uid, period).mkdir(parents=True, exist_ok=True)
            (store.month_dir(uid, period) / "index.html").write_text("<html></html>")
            data = {("year" if kind == "year" else "month"): int(period) if kind == "year" else period,
                    "totals": {"media_hours": 3}, "errors": {}, "notified": False}
            store.write_snapshot(uid, period, data)
            return data
        return fake

    monkeypatch.setattr(build, "build_month", fake_for("month"))
    monkeypatch.setattr(build, "build_year", fake_for("year"))

    async def fake_notify(plugin_id, **kw):
        sent.append(kw)
        return {"recipients": 1, "delivered": 1, "sent": True, "reason": None}

    monkeypatch.setattr(mod, "notify", fake_notify)
    return calls, sent


def january(monkeypatch):
    monkeypatch.setattr(mod, "last_month", lambda: "2026-12")
    monkeypatch.setattr(mod, "last_year", lambda: YEAR)


def test_the_annual_build_starts_once_per_person_on_the_first_of_january(people, fake_builds, monkeypatch):
    calls, sent = fake_builds
    january(monkeypatch)
    people[3] = dict(ANNA)
    plugin = RecapPlugin()

    async def tick():
        await plugin.scheduled(FakeDB([1, 2, 3]))
        await build.wait_all()

    for _ in range(8):
        run(tick())
    # December first (one person per run), then the year (one person per run); nobody twice, nobody without sources
    assert calls == [("month", 1, "2026-12"), ("month", 3, "2026-12"), ("year", 1, YEAR), ("year", 3, YEAR)]
    titles = [k["title"] for k in sent]
    assert titles.count("Dein Jahresrückblick 2026 ist da") == 2 and titles.count("Dein Dezember-Rückblick ist da") == 2
    assert all(k["data"]["route"] in (f"/plugin-page/recap/recaps?path={p}/" for p in ("2026-12", YEAR)) for k in sent)
    assert store.read_snapshot(1, YEAR)["notified"] is True


def test_two_runs_in_a_row_never_start_the_year_twice(people, fake_builds, monkeypatch):
    calls, sent = fake_builds
    january(monkeypatch)
    store.write_snapshot(1, "2026-12", {"month": "2026-12", "notified": True, "totals": {}})
    store.month_dir(1, "2026-12").mkdir(parents=True)
    (store.month_dir(1, "2026-12") / "index.html").write_text("<html></html>")
    plugin = RecapPlugin()

    async def twice():
        await plugin.scheduled(FakeDB([1]))
        await plugin.scheduled(FakeDB([1]))  # while the first build still runs
        await build.wait_all()
        await plugin.scheduled(FakeDB([1]))  # and after it is done
        await build.wait_all()

    run(twice())
    assert calls == [("year", 1, YEAR)]


def test_no_annual_build_outside_january_and_it_waits_for_december(people, fake_builds, monkeypatch):
    calls, _ = fake_builds
    monkeypatch.setattr(mod, "last_month", lambda: "2026-10")
    monkeypatch.setattr(mod, "last_year", lambda: None)  # any other month
    run(RecapPlugin().scheduled(FakeDB([1])))
    run(build.wait_all())
    assert calls == [("month", 1, "2026-10")]
    # January, December not built yet and paused between attempts: the year waits; after the last attempt it goes ahead
    calls.clear()
    january(monkeypatch)
    store.record_attempt(1, "2026-12", time.time())
    run(RecapPlugin().scheduled(FakeDB([1])))
    run(build.wait_all())
    assert calls == []
    for _ in range(build.MAX_ATTEMPTS):
        store.record_attempt(1, "2026-12", time.time())
    run(RecapPlugin().scheduled(FakeDB([1])))
    run(build.wait_all())
    assert calls == [("year", 1, YEAR)]


def test_failed_annual_builds_back_off(people, monkeypatch):
    january(monkeypatch)
    store.write_snapshot(1, "2026-12", {"month": "2026-12", "notified": True})
    store.month_dir(1, "2026-12").mkdir(parents=True)
    (store.month_dir(1, "2026-12") / "index.html").write_text("x")
    calls = []

    async def broken(uid, period, cfg, tz, redesign=False):
        calls.append(period)
        raise build.BuildFailed("nothing")

    monkeypatch.setattr(build, "build_year", broken)
    for _ in range(6):
        run(RecapPlugin().scheduled(FakeDB([1])))
        run(build.wait_all())
    assert calls == [YEAR] and store.attempts(1, YEAR)["count"] == 1  # the next try is hours away


def test_the_archive_lists_the_year_above_the_months(people, services):
    run(build.build_month(1, MONTH, mod.load_config(1), TZ))
    build_year_for()
    _, index = page(RecapPlugin(), 1, "")
    text = index.decode()
    assert "Jahresrückblick 2026" in text and 'href="2026/"' in text and 'href="2026/neu"' in text
    assert text.index("Jahresrückblick 2026") < text.index("Oktober 2026")
    assert views.month_label(YEAR) == "Jahresrückblick 2026"


def test_the_year_story_and_assets_are_served_and_confined(people, services):
    build_year_for()
    plugin = RecapPlugin()
    ctype, body = page(plugin, 1, "2026/")
    assert ctype.startswith("text/html") and b"Jahresr" in body and b"<base" not in body
    assert b'<base href="2026/">' in page(plugin, 1, "2026")[1]
    asset = next(f.name for f in (store.month_dir(1, YEAR) / "img").iterdir() if f.suffix == ".jpg")
    assert page(plugin, 1, f"2026/img/{asset}")[0] == "image/jpeg"
    assert page(plugin, 2, "2026/") is None  # someone else's year
    for bad in ("2026/../2025", "2026/img/../index.html", "20261/", "2026/img/x.exe", "../2026"):
        assert page(plugin, 1, bad) is None
    kind, file = store.resolve(1, "2026/index.html")
    assert store.user_dir(1).resolve() in file.resolve().parents


def test_redesign_of_a_year_from_the_archive(people, services, monkeypatch):
    build_year_for()
    plugin = RecapPlugin()
    started = []
    monkeypatch.setattr(build, "start", lambda uid, period, cfg, tz, **kw: started.append((uid, period, kw["redesign"])) or "started")
    ctype, body = page(plugin, 1, "2026/neu")
    assert started == [(1, YEAR, True)] and "Jahresrückblick 2026 bekommt ein neues Design".encode() in body
    assert page(plugin, 1, "2025/neu") is None and json.loads(page(plugin, 1, "2026/status")[1]) == {"running": False}


def test_year_endpoints(client, people, services, monkeypatch):
    monkeypatch.setattr(mod, "current_year", lambda: 2027)
    body = client.post("/api/plugins/recap/build?year=2026").json()
    assert body["status"] == "started" and body["month"] == YEAR and body["route"].endswith(f"path={YEAR}/")
    deadline = time.time() + 20
    while time.time() < deadline and not store.list_years(1):
        time.sleep(.05)
    listing = client.get("/api/plugins/recap/months").json()
    assert listing["years"][0]["year"] == YEAR and listing["years"][0]["label"] == "Jahresrückblick 2026" and listing["items"] == []
    assert client.post("/api/plugins/recap/build?year=2026").json()["status"] == "exists"
    for bad in ("year=2027", "year=2030", "year=26", "year=../x", "year=2026&month=2026-10"):
        assert client.post(f"/api/plugins/recap/build?{bad}").status_code == 422, bad
    text = client.get("/api/plugins/recap/markdown/2026")
    assert text.status_code == 200 and text.text.startswith("---\ntype: jahresrueckblick") and "# Jahresrückblick 2026" in text.text
    assert f"(/plugin-page/recap/recaps?path={YEAR}/)" in text.text and "Great Show" in text.text and "## Monate" in text.text
    assert client.get("/api/plugins/recap/markdown/2025").status_code == 404


# --- the design preview shows the year ---

def test_preview_has_a_year_toggle_and_shows_every_year_slide():
    link = lambda kind, palette, shape, type_, offset, sel: f"{kind}/{palette}/{shape}/{type_}/{offset}/{gallery.encode_sel(sel)}"  # noqa: E731
    month = gallery.render_gallery("neon", "blobs", "bold", 0, {}, link)
    assert ">Jahr</a>" in month and "g/neon/blobs/bold/0/year1" in month and "Sonderfolien" in month
    year = gallery.render_gallery("silvester", "blobs", "bold", 0, {"year": 1}, link)
    cells = re.findall(r'data-slide="(\w+)" data-variant="(\d)"', year)
    assert {(k, str(v)) for k, n in yearly.YEAR_VARIANTS.items() for v in range(n)} == set(cells)
    assert "Sonderfolien" not in year and "silvester" in year and "goldregen" in year
    assert "g/silvester/blobs/bold/0/" in year and ">Monat</a>" in year
    for kicker in yearly.YEAR_KICKERS["yintro"]:
        assert kicker in year
    assert gallery.params("goldregen", v="year1.ytop1.yintro2")[4] == {"year": 1, "ytop": 1, "yintro": 2}
    assert gallery.params("goldregen", v="ytop1")[4] == {}  # year slides only count in year mode
    assert gallery.params(None, v="year1.series2")[4] == {"year": 1}
    worst = gallery.render_gallery("silvester", "blobs", "bold", 0, {"year": 1, "worst": 1}, link)
    assert "Donaudampf" in worst and "Grenzfall" in worst


def test_preview_page_shows_the_year_for_admins():
    admin = SimpleNamespace(id=1, is_admin=True, role="admin")
    ctype, body = asyncio.run(RecapPlugin().page(admin, "recaps", "preview/s~konfetti~rings~serif~2~year1.yintro1"))
    text = body.decode()
    assert 'class="slide s-yintro v1' in text and "type-serif" in text and "Jahresrückblick 2026" in text


# --- the months stay exactly as they were ---

GOLDEN = {  # sha256 prefixes of the sample month's render and of the monthly design choices, taken before the annual recap existed
    "neon-blobs-bold-0-[]": "8166ed6774ed2c17",
    "herbstlaub-rings-serif-3-[('movies', 1), ('photos', 2), ('series', 2)]": "f50bc51a0b9bd16f",
    "pop-stripes-caps-5-[('games', 1), ('music', 1), ('worst', 1)]": "e539022fd9338da6",
    "designs": "8e90389617517124",
}


def test_monthly_designs_and_renders_are_byte_identical_to_before():
    got = {}
    for pal, shape, typ, off, sel in [("neon", "blobs", "bold", 0, {}), ("herbstlaub", "rings", "serif", 3, {"movies": 1, "series": 2, "photos": 2}),
                                      ("pop", "stripes", "caps", 5, {"worst": 1, "music": 1, "games": 1})]:
        got[f"{pal}-{shape}-{typ}-{off}-{sorted(sel.items())}"] = hashlib.sha256(
            gallery.sample_story(pal, shape, typ, off, sel).encode()).hexdigest()[:16]
    data, prev, history = sample.sample_story_input()
    designs = [story.choose_design(dict(data, month=f"2026-{m:02d}", seed="42"), []) for m in range(1, 13)]
    got["designs"] = hashlib.sha256(json.dumps(designs, sort_keys=True).encode()).hexdigest()[:16]
    assert got == GOLDEN


def test_year_data_never_leaks_into_the_monthly_collectors(people, services):
    """A monthly build asks the collectors for exactly what it always did (no daily minutes, no per-month photos)."""
    data = run(build.build_month(1, MONTH, mod.load_config(1), TZ))
    assert "day_minutes" not in data["music"] and "day_minutes" not in data["audiobooks"] and "month_counts" not in data["photos"]
    assert "approx" not in (data.get("games") or {})
