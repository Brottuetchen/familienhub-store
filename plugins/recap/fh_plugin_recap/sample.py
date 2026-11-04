"""Made-up data for the design preview: one complete month that fills every slide the story can show.

Nothing here is real: names are invented and every picture is a small SVG placeholder inlined as a data URI, so the
preview needs no files. The highlights come from the real ``story.highlights`` so they match what a build would find.
"""
import zlib
from urllib.parse import quote

from . import story, year as yearly

MONTH = "2026-10"
YEAR = 2026


def svg(title, w, h, scene=False):
    """A gradient tile with the initials of ``title`` (or a little landscape for photos) as an image data URI."""
    hue = zlib.crc32(title.encode("utf-8")) % 360
    body = (f'<circle cx="{w * .72:.0f}" cy="{h * .3:.0f}" r="{w * .12:.0f}" fill="#fff" opacity=".8"/>'
            f'<path d="M0 {h} L0 {h * .68:.0f} L{w * .3:.0f} {h * .45:.0f} L{w * .55:.0f} {h * .7:.0f} L{w * .75:.0f} {h * .55:.0f} '
            f'L{w} {h * .75:.0f} L{w} {h}Z" fill="#000" opacity=".35"/>' if scene else
            f'<text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle" font-family="sans-serif" font-weight="900" '
            f'font-size="{w * .36:.0f}" fill="#fff" opacity=".9">{story.e(story.initials(title))}</text>')
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><defs>'
         f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl({hue},70%,52%)"/>'
         f'<stop offset="1" stop-color="hsl({(hue + 50) % 360},65%,26%)"/></linearGradient></defs>'
         f'<rect width="{w}" height="{h}" fill="url(#g)"/>{body}</svg>')
    return "data:image/svg+xml," + quote(s)


def poster(title):
    return svg(title, 200, 300)


def square(title):
    return svg(title, 200, 200)


def photo(title):
    return svg(title, 300, 300, scene=True)


def sample_data():
    """The sample month (October 2026) as a snapshot, without ``design``."""
    series = [("Die Leuchtturm-Chronik", [1, 2, 3], 34, 27.5), ("Nachtschicht", [2], 12, 9.4), ("Kleine Wunder", [1], 8, 6.1),
              ("Das Tal der Stille", [1], 5, 4.0)]
    movies = ["Der letzte Zug", "Sommer in Blau", "Echo der Berge", "Kaffee & Kometen", "Das große Missverständnis", "Nordlicht"]
    artists = [("Lena Morgenrot", 96), ("The Paper Boats", 71), ("Kolibri", 58), ("Nordwind", 44), ("Mira Sonne", 31)]
    tracks = [("Goldene Stunde", "Lena Morgenrot", 21), ("Wellen", "The Paper Boats", 17), ("Papierflieger", "Kolibri", 15),
              ("Hinterm Horizont", "Nordwind", 12), ("Morgentau", "Mira Sonne", 11)]
    books = [("Die Stille der Berge", "Anna Beispiel", 11.2, True), ("Der Uhrmacher von Nebel", "Karl Muster", 7.5, False),
             ("Sternenstaub", "Eva Probe", 4.1, True), ("Das Haus am See", "Jonas Demo", 2.3, False)]
    pods = [("Wissen to go", 12, 5.6), ("Die Feierabend-Runde", 7, 4.2), ("Kurz erklärt", 9, 2.1), ("Nachtgedanken", 3, 1.4)]
    games = [("Hollow Lantern", "Steam", 960, 0, True), ("Pixel Farm Saga", "Nintendo Switch", 540, 14, False),
             ("Turbo Kart 64", "Nintendo 64", 215, 9, False), ("Cave Story Remix", "PC", 130, 6, True), ("Space Blocks", "Game Boy", 55, 3, False)]
    days = {f"2026-10-{d:02d}": h for d, h in [(3, 1.5), (4, 2.7), (5, 1.1), (6, .8), (7, 1.9), (8, 2.2), (9, 1.4), (10, 8.4), (11, 3.2),
                                               (12, 2.0), (15, 1.2), (16, 2.5), (17, 3.8), (20, .9), (24, 2.4), (25, 5.1), (26, 1.7)]}
    jf = {
        "series": [{"name": n, "poster": poster(n), "seasons": s, "episodes": ep, "hours": h} for n, s, ep, h in series],
        "series_hours": 47.0, "episodes": 59, "series_count": 6,
        "movies": [{"name": n, "poster": poster(n), "year": 2015 + i * 2} for i, n in enumerate(movies)],
        "movies_count": len(movies), "movie_hours": 11.5,
        "best_day": {"date": "2026-10-10", "hours": 8.4, "titles": ["Die Leuchtturm-Chronik", "Nachtschicht"]},
        "night_owl": {"title": "Nachtschicht - s02e05 - Doppelte Schicht", "time": "02:14", "date": "2026-10-17"},
        "days": days, "active_days": len(days), "finished_series": ["Das Tal der Stille", "Kleine Wunder"],
    }
    mu = {
        "plays": 612, "minutes": 1840, "artists_count": 38,
        "top_artists": [{"name": n, "plays": p, "cover": square(n)} for n, p in artists],
        "top_tracks": [{"name": n, "artist": a, "plays": p, "cover": square(n)} for n, a, p in tracks],
        "new_artists": [{"name": "Kolibri", "plays": 58}, {"name": "Mira Sonne", "plays": 31}, {"name": "Die Wolkenpiloten", "plays": 12}],
        "days": [],
    }
    ab = {
        "books": [{"title": t, "author": a, "hours": h, "cover": square(t), "finished": f} for t, a, h, f in books],
        "finished": 2, "minutes": 1500, "hours": 25.0, "titles_count": 4, "days": [],
    }
    pc = {
        "podcasts": [{"title": t, "episodes": n, "hours": h, "cover": square(t)} for t, n, h in pods],
        "minutes": 810, "hours": 13.5, "episodes": 31,
    }
    ph = {
        "total": 248, "photos": 226, "videos": 22, "days_with_photos": 19, "best_day": {"date": "2026-10-18"},
        "places": [{"name": n, "count": c} for n, c in [("Lissabon", 112), ("Musterstadt", 84), ("Sintra", 31), ("Cascais", 21)]],
        "place_geo": [{"name": "Musterstadt", "lat": 50.11, "lon": 8.68, "country": "Deutschland", "count": 84},
                      {"name": "Lissabon", "lat": 38.72, "lon": -9.14, "country": "Portugal", "count": 112},
                      {"name": "Sintra", "lat": 38.80, "lon": -9.38, "country": "Portugal", "count": 31}],
        "highlights": [{"img": photo(f"Foto {i}"), "date": f"2026-10-{d:02d}", "city": c}
                       for i, (d, c) in enumerate([(18, "Lissabon"), (18, "Lissabon"), (19, "Sintra"), (20, "Sintra"), (21, "Cascais"),
                                                   (22, "Lissabon"), (2, "Musterstadt"), (9, "Musterstadt"), (28, "")], 1)],
    }
    gm = [{"name": n, "system": s, "minutes": m, "sessions": ss, "cover": poster(n), "new": new} for n, s, m, ss, new in games]
    ga = {"games": gm, "games_count": 7, "systems_count": 4, "top_system": "Steam", "hours": 32.5, "minutes": 1950, "sessions": 32}
    ra = {
        "total": 37, "hardcore": 30, "points": 412, "games_count": 3,
        "rarest": [{"title": t, "game": g, "points": p, "badge": square(t)} for t, g, p in
                   [("Pilz-Meister", "Super Beispiel Land", 50), ("Ohne Kratzer", "Mega Muster Man", 25), ("Geheimer Raum", "Zelda-Verschnitt", 25),
                    ("Schnellste Runde", "Turbo Kart 64", 10), ("Kein Schaden", "Mega Muster Man", 10), ("Alle Münzen", "Super Beispiel Land", 5)]],
        "games": [{"name": n, "icon": square(n), "console": c, "count": k} for n, c, k in
                  [("Super Beispiel Land", "SNES", 19), ("Mega Muster Man", "Mega Drive", 12), ("Zelda-Verschnitt", "Game Boy", 6)]],
        "awards": [{"title": "Super Beispiel Land", "icon": square("Super Beispiel Land"), "console": "SNES", "kind": "mastered", "hardcore": True},
                   {"title": "Mega Muster Man", "icon": square("Mega Muster Man"), "console": "Mega Drive", "kind": "beaten", "hardcore": False}],
    }
    totals = {"media_hours": 160.2, "series_hours": 47.0, "movie_hours": 11.5, "music_hours": 30.7, "audiobook_hours": 25.0,
              "podcast_hours": 13.5, "games_hours": 32.5}
    return {"month": MONTH, "seed": "sample", "jellyfin": jf, "music": mu, "audiobooks": ab, "podcasts": pc, "photos": ph,
            "games": ga, "retroachievements": ra, "totals": totals, "soundtrack": [], "errors": {}}


LONG = ["Die unglaublich lange Geschichte vom Leuchtturmwärter und der Donaudampfschifffahrtsgesellschaft",
        "Außergewöhnliche Abenteuer des Hauptmanns Freiherr von Münchhausen-Schwarzenberg", "Wo die wilden Kerle wohnen und andere Weltuntergänge",
        "Räuber Hotzenplotz und die Mondrakete im Hinterhof", "Kommissar Dobermann ermittelt: Der Fall des verschwundenen Rosenkavaliers",
        "Zwischen Rhabarberschorle und Weltraumschrott", "Donaudampfschifffahrtselektrizitätenhauptbetriebswerkbauunterbeamtengesellschaft",
        "Lieder aus dem Hinterland: Gesamtaufnahme in drei Akten"]


def long_name(i):
    return LONG[i % len(LONG)]


def worst_data():
    """The same month at its limits: every list at its maximum length, long German titles (and one word that cannot break),
    big numbers, many photos. The layouts must hold even this without anything scrolling or running off the slide."""
    d = sample_data()
    L = long_name
    jf, mu, ab, pc, ph, ga, ra = (d[k] for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "games", "retroachievements"))
    jf["series"] = [{"name": L(i), "poster": poster(L(i)), "seasons": list(range(1, 10 - i)), "episodes": 340 - i * 31, "hours": 276.5 - i * 20} for i in range(5)]
    jf.update(series_hours=1234.5, episodes=1999, series_count=88, movies_count=88, movie_hours=222.5,
              movies=[{"name": L(i + 2), "poster": poster(L(i + 2)), "year": 1990 + i} for i in range(8)],
              finished_series=[L(i) for i in range(4)])
    jf["best_day"] = {"date": "2026-10-10", "hours": 18.4, "titles": [L(0), L(1)]}
    jf["night_owl"] = {"title": L(5) + " - s02e05 - " + L(3), "time": "02:14", "date": "2026-10-17"}
    mu.update(plays=12612, minutes=41840, artists_count=388,
              top_artists=[{"name": L(i), "plays": 1096 - i, "cover": square(L(i))} for i in range(5)],
              top_tracks=[{"name": L(i + 3), "artist": L(i + 1), "plays": 321 - i, "cover": square(L(i + 3))} for i in range(5)],
              new_artists=[{"name": L(i + 4), "plays": 58 - i} for i in range(5)])
    ab["books"] = [{"title": L(i), "author": L(i + 1), "hours": 111.2 - i, "cover": square(L(i)), "finished": i % 2 == 0} for i in range(5)]
    ab.update(hours=225.0, titles_count=15, finished=3)
    pc["podcasts"] = [{"title": L(i + 1), "episodes": 112 - i, "hours": 55.6 - i, "cover": square(L(i + 1))} for i in range(5)]
    pc.update(hours=133.5, episodes=331)
    ph.update(total=2480, photos=2260, videos=220, places=[{"name": L(i + 5), "count": 112 - i} for i in range(5)])
    ph["highlights"] = [{"img": photo(f"Foto {i}"), "date": f"2026-10-{(i * 2) % 28 + 1:02d}", "city": L(i + 5)} for i in range(12)]
    ph["place_geo"] = [{"name": L(i + 5), "lat": 38.7 + i, "lon": -9.1 + i, "country": "Vereinigte Arabische Emirate", "count": 31 + i} for i in range(3)] + ph["place_geo"][:1]
    ga["games"] = [{"name": L(i), "system": "Nintendo Entertainment System Classic Edition", "minutes": 4960 - i * 400, "sessions": 114 - i,
                    "cover": poster(L(i)), "new": i in (1, 2)} for i in range(8)]
    ga.update(games_count=88, systems_count=14, top_system="Nintendo Entertainment System Classic Edition", hours=1132.5, minutes=51950, sessions=3200)
    ra.update(total=3700, hardcore=3000, points=41200, games_count=33)
    ra["rarest"] = [{"title": L(i), "game": L(i + 1), "points": 150 - i, "badge": square(L(i))} for i in range(6)]
    ra["games"] = [{"name": L(i + 2), "icon": square(L(i + 2)), "console": "Nintendo Entertainment System", "count": 119 - i} for i in range(5)]
    ra["awards"] = [{"title": L(i + 3), "icon": square(L(i + 3)), "console": "Super Nintendo Entertainment System", "kind": k, "hardcore": i % 2 == 0}
                    for i, k in enumerate(("mastered", "beaten", "completed", "beaten_soft"))]
    d["totals"] = {"media_hours": 3180.2, "series_hours": 1247.0, "movie_hours": 111.5, "music_hours": 330.7, "audiobook_hours": 225.0,
                   "podcast_hours": 133.5, "games_hours": 1132.5}
    return d


def sample_history():
    """Three earlier months: enough for a record and a "home" for the trip slide."""
    home = [{"name": "Musterstadt", "lat": 50.11, "lon": 8.68, "country": "Deutschland", "count": 120}]
    return [{"month": f"2026-0{m}", "totals": {"media_hours": h}, "photos": {"place_geo": home}, "design": {}} for m, h in ((7, 64), (8, 78), (9, 91))]


def sample_prev():
    """September in the figures the deltas compare with: some up, some down, one new, one about equal."""
    return {"totals": {"media_hours": 140}, "jellyfin": {"series_hours": 31.0, "movie_hours": 14.0}, "music": {"minutes": 1810},
            "audiobooks": {"minutes": 700}, "podcasts": {"minutes": 0}, "photos": {"total": 110}, "games": {"minutes": 2400}}


def sample_story_input(worst=False):
    """``(data, prev, history)`` for ``story.render``; the caller adds ``design``. ``worst``: the month at its limits."""
    data, history = (worst_data() if worst else sample_data()), sample_history()
    data["highlights"] = story.highlights(data, history)
    data["design"] = {"lead": "trip"}
    return data, sample_prev(), history


# -- The sample year (annual recap) --------------------------------------------------------------------------------------

def sample_year_months():
    """Twelve made-up monthly snapshots (only what the annual recap reads), with a few quiet months and one without data."""
    hours = [64, 52, 78, 71, 58, 33, 41, 46, 91, 160, 118, 87]
    out = {}
    for n, h in enumerate(hours, 1):
        if n == 6:
            continue  # a month without a monthly recap: derived from the year's daily data
        share = {"series_hours": .3, "movie_hours": .08, "music_hours": .22, "audiobook_hours": .15, "podcast_hours": .1, "games_hours": .15}
        if n % 4 == 0:
            share.update(music_hours=.45, series_hours=.1)
        t = {k: round(h * v, 1) for k, v in share.items()}
        t["media_hours"] = round(sum(t.values()), 1)
        out[n] = {"month": f"2026-{n:02d}", "totals": t, "photos": {"total": 40 + n * 9},
                  "games": {"games": [{"name": ["Hollow Lantern", "Pixel Farm Saga", "Turbo Kart 64"][n % 3], "minutes": 120 + n * 41, "sessions": n + 3}]}}
    return out


def _spread(total_by_month, fn):
    """Daily values over the year: ``fn(month, day) -> value or None``."""
    return {f"2026-{m:02d}-{d:02d}": v for m in range(1, 13) for d in range(1, 29) if (v := fn(m, d)) is not None}


def sample_year(worst=False):
    """``(data, snaps, earlier)`` for ``yearly.facts``/``yearly.render``: the sample month's lists at year scale, 12 photos (one per month),
    some games with the estimated-time note. ``worst`` takes the month's limit sample as the base (long titles, big numbers)."""
    d = worst_data() if worst else sample_data()
    d.pop("month"), d.pop("highlights", None)
    d["year"] = YEAR
    jf, mu, ab, pc, ph, ga, ra = (d[k] for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "games", "retroachievements"))
    jf.update(series_hours=round(jf["series_hours"] * 9, 1), episodes=jf["episodes"] * 9, movie_hours=round(jf["movie_hours"] * 8, 1), movies_count=jf["movies_count"] * 8,
              series_count=jf["series_count"] * 8, finished_series=jf["finished_series"],
              best_day={"date": "2026-11-22", "hours": 11.6 if not worst else 21.9, "titles": jf["best_day"]["titles"]},
              days=_spread(None, lambda m, dd: round(.5 + (m * dd) % 5 * .6, 1) if (m + dd) % 3 else None))
    jf["active_days"] = len(jf["days"])
    for x in jf["series"]:
        x.update(episodes=x["episodes"] * 6, hours=round(x["hours"] * 6, 1))
    for i, x in enumerate(jf["movies"]):
        x["hours"] = round(2.4 - i * .2, 1)
    mu.update(plays=mu["plays"] * 9, minutes=mu["minutes"] * 9, artists_count=mu["artists_count"] * 5, tracks_count=640,
              day_minutes=_spread(None, lambda m, dd: float(20 + (m * 7 + dd * 3) % 70) if dd % 2 else None))
    for x in mu["top_artists"] + mu["top_tracks"]:
        x["plays"] *= 8
    ab.update(hours=round(ab["hours"] * 7, 1), minutes=ab["minutes"] * 7, finished=9, titles_count=22, day_minutes=_spread(None, lambda m, dd: 35.0 if dd in (3, 4, 5, 17) else None))
    pc.update(hours=round(pc["hours"] * 8, 1), minutes=pc["minutes"] * 8, episodes=pc["episodes"] * 8, day_minutes=_spread(None, lambda m, dd: 28.0 if dd in (9, 10) else None))
    ph.update(total=ph["total"] * (9 if not worst else 3), photos=ph["photos"] * 9, videos=ph["videos"] * 9, days_with_photos=140)
    cities = ["Musterstadt", "Lissabon", "Sintra", "Cascais", "Hamburg", "Wien", "Berlin", "Rom", "Nizza", "Oslo", "Prag", "Bremen"]
    ph["highlights"] = [{"img": photo(f"Foto {i}"), "date": f"2026-{i:02d}-{(i * 5) % 27 + 1:02d}",
                         "city": long_name(i) if worst else cities[i - 1]} for i in range(1, 13)]
    ph["month_counts"] = {f"{n:02d}": 40 + n * 9 for n in range(1, 13)}
    ga.update(hours=round(ga["hours"] * 6, 1), minutes=ga["minutes"] * 6, games_count=ga["games_count"] * 5, approx=True, approx_since="2026-11-03")
    for x in ga["games"]:
        x["minutes"] *= 6
    ra.update(total=ra["total"] * 8, points=ra["points"] * 8, hardcore=ra["hardcore"] * 8, games_count=ra["games_count"] * 4)
    for x in ra["games"]:
        x["count"] *= 6
    t = d["totals"]
    for k in list(t):
        t[k] = round(t[k] * 7, 1)
    snaps = sample_year_months()
    earlier = [{"month": "2025-12", "jellyfin": {"series": [{"name": jf["series"][1]["name"]}, {"name": "Eine alte Serie"}]},
                "photos": {"place_geo": [{"name": "Musterstadt", "lat": 50.11, "lon": 8.68, "country": "Deutschland", "count": 900}]}}]
    return yearly.facts(d, snaps, earlier), snaps


def sample_year_input(worst=False):
    """``data`` for ``yearly.render`` with the sample design's lead-free parts filled; the caller adds ``design``."""
    data, _ = sample_year(worst)
    data["design"] = {}
    return data
