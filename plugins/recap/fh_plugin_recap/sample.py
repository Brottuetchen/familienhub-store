"""Made-up data for the design preview: one complete month that fills every slide the story can show.

Nothing here is real: names are invented and every picture is a small SVG placeholder inlined as a data URI, so the
preview needs no files. The highlights come from the real ``story.highlights`` so they match what a build would find.
"""
import zlib
from urllib.parse import quote

from . import story

MONTH = "2026-10"


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


def sample_history():
    """Three earlier months: enough for a record and a "home" for the trip slide."""
    home = [{"name": "Musterstadt", "lat": 50.11, "lon": 8.68, "country": "Deutschland", "count": 120}]
    return [{"month": f"2026-0{m}", "totals": {"media_hours": h}, "photos": {"place_geo": home}, "design": {}} for m, h in ((7, 64), (8, 78), (9, 91))]


def sample_prev():
    """September in the figures the deltas compare with: some up, some down, one new, one about equal."""
    return {"totals": {"media_hours": 140}, "jellyfin": {"series_hours": 31.0, "movie_hours": 14.0}, "music": {"minutes": 1810},
            "audiobooks": {"minutes": 700}, "podcasts": {"minutes": 0}, "photos": {"total": 110}, "games": {"minutes": 2400}}


def sample_story_input():
    """``(data, prev, history)`` for ``story.render``; the caller adds ``design``."""
    data, history = sample_data(), sample_history()
    data["highlights"] = story.highlights(data, history)
    data["design"] = {"lead": "trip"}
    return data, sample_prev(), history
