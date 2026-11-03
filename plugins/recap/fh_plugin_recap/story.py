"""The story page of the monthly recap, with a design that has a memory so no month looks like the one before.

Every month gets a design (palette, background shape, type style, layout variant and text pool entry per slide).
It is chosen with the month and the person as the random seed and excludes what the person's earlier months used.
The choice is stored in the snapshot (`design`), so a recap stays the same when it is built again.
User-facing texts are German on purpose (follow-up: translate through the pools below).
"""
import datetime as dt
import html
import math
import random
import re
import zlib

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

# Palettes: per slide (background, accent, shape 1, shape 2). "months" = the months in which a palette is preferred.
PALETTES = {
    "neon": {"months": range(1, 13), "sets": [
        ("#2a0b4a", "#c4ff3d", "#ff3cac", "#5b2bff"), ("#101848", "#7cf7ff", "#3f51ff", "#a020f0"),
        ("#3a0a14", "#ffc857", "#e63946", "#7b1e2b"), ("#0b1f12", "#1ed760", "#1db954", "#0e4d2a"),
        ("#062a3a", "#9bf6ff", "#00b4d8", "#0077b6"), ("#2d0b3a", "#ff8ae2", "#b5179e", "#480ca8")]},
    "herbstlaub": {"months": (9, 10, 11), "sets": [
        ("#3b1d0e", "#ffb347", "#d9480f", "#7c2d12"), ("#2b2a10", "#e9ff70", "#9c6644", "#5a3e1b"),
        ("#40160f", "#ffd6a5", "#c1121f", "#780000"), ("#1f2d1b", "#ffd166", "#606c38", "#283618"),
        ("#3a2410", "#ffe8b6", "#bc6c25", "#6f1d1b"), ("#2a1a2e", "#f4a261", "#9d4edd", "#6a040f")]},
    "polarlicht": {"months": (11, 12, 1, 2), "sets": [
        ("#05162b", "#7dffb3", "#00a6a6", "#3a0ca3"), ("#0b1026", "#b8f2ff", "#4361ee", "#7209b7"),
        ("#06202b", "#a0ffe6", "#2ec4b6", "#011627"), ("#140b2e", "#ff9de2", "#5f0f40", "#0f4c5c"),
        ("#0a1f1a", "#d8ff70", "#1b998b", "#2d3047"), ("#101828", "#e0e1ff", "#3d5a80", "#98c1d9")]},
    "fruehling": {"months": (3, 4, 5), "sets": [
        ("#1d3b2a", "#f9f871", "#5fb49c", "#2e7d32"), ("#3d1f4f", "#ffc6ff", "#c77dff", "#7b2cbf"),
        ("#103c48", "#caffbf", "#4ecdc4", "#1a535c"), ("#4a1d33", "#ffadad", "#ff6b9a", "#8d0f4a"),
        ("#24304f", "#fdffb6", "#9bb1ff", "#3a4a8c"), ("#2f3b1d", "#e9ff9a", "#a7c957", "#386641")]},
    "sommer": {"months": (6, 7, 8), "sets": [
        ("#7a1c1a", "#fff275", "#ff5e5b", "#d7263d"), ("#05445e", "#fff275", "#00a6a6", "#016fb9"),
        ("#6b2d00", "#ffe066", "#ff7f11", "#ee4266"), ("#3c2a6b", "#ffca3a", "#ff595e", "#1982c4"),
        ("#073b4c", "#ffd166", "#06d6a0", "#118ab2"), ("#6d1430", "#ffffff", "#ef476f", "#ffd166")]},
    "pop": {"months": range(1, 13), "sets": [
        ("#111111", "#ff453a", "#ff453a", "#2c2c2e"), ("#111111", "#ffd60a", "#ffd60a", "#2c2c2e"),
        ("#111111", "#0a84ff", "#0a84ff", "#2c2c2e"), ("#111111", "#30d158", "#30d158", "#2c2c2e"),
        ("#111111", "#bf5af2", "#bf5af2", "#2c2c2e"), ("#111111", "#ff9f0a", "#ff9f0a", "#2c2c2e")]},
}
SHAPES = ["blobs", "stripes", "rings", "glow"]
TYPES = ["bold", "caps", "serif"]
VARIANTS = {"intro": 3, "total": 3, "series": 3, "binge": 2, "movies": 2, "music": 3, "books": 2, "pods": 2,
            "photos": 3, "games": 3, "outro": 2}
# Added after the first releases: chosen from an own random stream, so the choices for all other slides stay as they were
LATE_SLIDES = ("games",)
GAME_MARATHON_MINUTES = 600
KICKERS = {
    "intro": ["Dein Rückblick", "Das war dein", "Zurückgespult", "Zeit für den Rückblick"],
    "total": ["Insgesamt", "Alles zusammen", "Unterm Strich", "Die große Zahl"],
    "series": ["Deine Serie des Monats", "Hier bist du hängen geblieben", "Nicht mehr losgekommen", "Folge um Folge"],
    "binge": ["Dein Couch-Tag", "Der längste Abend", "Ein Tag, viele Folgen"],
    "movies": ["Filmabende", "Großes Kino", "Popcorn-Bilanz"],
    "music": ["Musik", "Dein Soundtrack", "Auf Dauerschleife", "Was in deinen Ohren lief"],
    "books": ["Hörbücher", "Zum Zuhören", "Kopfkino"],
    "pods": ["Podcasts", "Auf die Ohren", "Gesprächsstoff"],
    "photos": ["Festgehalten", "Deine Momente", "Durch die Linse", "Erinnerungen"],
    "games": ["Game on", "Controller in der Hand", "Zocker-Bilanz", "Level-Up", "Noch ein Versuch"],
    "outro": ["Dein {m} {y}", "Das war {m}", "{m} in Zahlen"],
}


def intro_leads(data, hl):
    """Intro lines from text blocks; every statement comes straight from the data."""
    jf, mu, ph, t = (data.get(k) or {} for k in ("jellyfin", "music", "photos", "totals"))
    serie = get(jf, "series", 0, "name")
    trip = get(hl, "trip", "places", 0, "name")
    out = {}
    if trip and serie:
        out["trip"] = f"Zwischen {serie} und {trip}: ein Monat mit {de_num(t['media_hours'])} Stunden Medien."
    if serie and ph.get("total"):
        out["serie"] = f"Am meisten lief {serie}. Dazu {ph['total']} Fotos und Videos."
    if mu.get("top_artists"):
        out["musik"] = f"{de_num(t['media_hours'])} Stunden Medien, und ganz vorne in deinen Ohren: {mu['top_artists'][0]['name']}."
    if hl.get("finished"):
        out["fertig"] = f"Ein Monat mit Abschluss: {hl['finished'][0]} hast du zu Ende geschaut."
    out["neutral"] = "Was du geschaut, gehört und festgehalten hast."
    return out


def pick_lead(data, hl, history):
    """An intro of a kind the previous month did not use."""
    last = ((history[-1].get("design") or {}).get("lead") if history else None)
    keys = [k for k in intro_leads(data, hl) if k != last] or ["neutral"]
    return random.Random(data["month"] + str(data.get("seed", "")) + "lead").choice(keys)


def e(x):
    return html.escape(str(x if x is not None else ""))


def de_num(x, digits=0):
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def de_date(d):
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]}"


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


# -- Choosing the design ------------------------------------------------------

def choose_design(data, history, current=None):
    """Design with memory: nothing the previous month (for palettes: the last two) already had. ``current`` is the
    month's own design on a redesign: its choices are avoided as well."""
    month = int(data["month"][5:])
    seed = data["month"] + str(data.get("seed", "")) + (f":{data['design_round']}" if data.get("design_round") else "")
    rnd = random.Random(seed)
    past = [h.get("design") or {} for h in history]
    last = past[-1] if past else {}
    current = current or {}

    recent = {p.get("palette") for p in past[-2:]} | {current.get("palette")}
    seasonal = [k for k, v in PALETTES.items() if month in v["months"] and len(v["months"]) < 12 and k not in recent]
    anytime = [k for k in PALETTES if k not in recent]
    palette = rnd.choice(seasonal or anytime or list(PALETTES))

    def pick(options, *avoid):
        return pick_with(rnd, options, *avoid)

    def old(design, group, key):
        return (design.get(group) or {}).get(key)

    design = {
        "palette": palette,
        "offset": rnd.randrange(6),
        "shape": pick(SHAPES, last.get("shape"), current.get("shape")),
        "type": pick(TYPES, last.get("type"), current.get("type")),
        "variants": {k: pick(range(n), old(last, "variants", k), old(current, "variants", k))
                     for k, n in VARIANTS.items() if k not in LATE_SLIDES},
        "texts": {k: pick(range(len(v)), old(last, "texts", k), old(current, "texts", k))
                  for k, v in KICKERS.items() if k not in LATE_SLIDES},
    }
    late = random.Random(seed + "late")
    for k in LATE_SLIDES:
        design["variants"][k] = pick_with(late, range(VARIANTS[k]), old(last, "variants", k), old(current, "variants", k))
        design["texts"][k] = pick_with(late, range(len(KICKERS[k])), old(last, "texts", k), old(current, "texts", k))
    return design


def pick_with(rnd, options, *avoid):
    """Random choice that avoids the given values; the previous month's value is the one avoided first."""
    options = list(options)
    rest = [o for o in options if o not in avoid] or [o for o in options if o != avoid[0]] or options
    return rnd.choice(rest)


def highlights(data, history):
    """Special events that get a slide of their own."""
    jf, mu, ab, pc, ph, t = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "totals"))
    out = {}
    before = [h["totals"]["media_hours"] for h in history if h.get("totals")]
    if len(before) >= 3 and t.get("media_hours", 0) > max(before):  # a record needs three months to compare with
        out["record"] = {"hours": t["media_hours"], "old": max(before)}
    if jf.get("finished_series"):
        out["finished"] = jf["finished_series"][:4]
    if mu.get("new_artists"):
        out["new_artists"] = mu["new_artists"][:5]
    # Trip: photo places far from home (home = the most frequent photo place over all months)
    geo = {}
    for snap in history + [data]:
        for p in (snap.get("photos") or {}).get("place_geo") or []:
            g = geo.setdefault(p["name"], {"count": 0, "lat": p["lat"], "lon": p["lon"], "country": p.get("country")})
            g["count"] += p["count"]
    if geo:
        home_name, home = max(geo.items(), key=lambda kv: kv[1]["count"])
        trips = [(p["name"], p.get("country"), round(km((home["lat"], home["lon"]), (p["lat"], p["lon"]))), p["count"])
                 for p in ph.get("place_geo") or []]
        trips = [x for x in trips if x[2] >= 150]
        if trips:
            trips.sort(key=lambda x: -x[2])
            out["trip"] = {"home": home_name, "places": [{"name": n, "country": c, "km": d, "count": k} for n, c, d, k in trips[:3]]}
    # Longest streak of days with a series, music or an audiobook
    days = set(jf.get("days") or {}) | set(mu.get("days") or []) | set(ab.get("days") or [])
    if days:
        ds = sorted(dt.date.fromisoformat(d) for d in days)
        best, run, start, best_start = 1, 1, ds[0], ds[0]
        for a, b in zip(ds, ds[1:]):
            if (b - a).days == 1:
                run += 1
            else:
                run, start = 1, b
            if run > best:
                best, best_start = run, start
        if best >= 7:
            out["streak"] = {"days": best, "from": best_start.isoformat()}
    if pc.get("podcasts") and not any((h.get("podcasts") or {}).get("podcasts") for h in history):
        out["first_podcasts"] = True
    ga = data.get("games") or {}
    if ga.get("games"):
        if not any((h.get("games") or {}).get("games") for h in history):
            out["first_games"] = True
        top = ga["games"][0]
        if top["minutes"] >= GAME_MARATHON_MINUTES:  # one game for ten hours or more in a single month
            out["game_marathon"] = {"name": top["name"], "hours": round(top["minutes"] / 60, 1), "sessions": top["sessions"]}
    return out


# -- Slides -------------------------------------------------------------------──

def delta(cur, prev):
    if prev is None or cur is None:
        return ""
    if not prev:
        return '<p class="delta up">neu dabei</p>' if cur else ""
    pct = round((cur - prev) / prev * 100)
    if abs(pct) < 3:
        return '<p class="delta">etwa so viel wie im Vormonat</p>'
    cls = "up" if pct > 0 else "down"
    return f'<p class="delta {cls}">{"▲" if pct > 0 else "▼"} {abs(pct)} % {"mehr" if pct > 0 else "weniger"} als im Vormonat</p>'


def get(d, *path):
    for p in path:
        if isinstance(d, list) and isinstance(p, int):
            d = d[p] if len(d) > p else None
        elif isinstance(d, dict):
            d = d.get(p)
        else:
            return None
    return d


def game_time(minutes):
    return f"{minutes} Min." if minutes < 60 else f"{de_num(minutes / 60, 1)} h"


def game_meta(x):
    starts = f" · {x['sessions']} {'Start' if x['sessions'] == 1 else 'Starts'}" if x.get("sessions") else ""
    return f"{e(x.get('system') or '')} · {game_time(x['minutes'])}{starts}"


def year(x):
    return f" ({x['year']})" if x.get("year") else ""


def count(value, digits=0):
    return f'<span class="count" data-to="{value}" data-dec="{digits}">{de_num(value, digits)}</span>'


_SKIP_WORDS = {"the", "a", "an", "der", "die", "das", "ein", "eine", "le", "la", "les", "el"}


def initials(name):
    """Up to two capital letters for the placeholder tile (never empty)."""
    words = re.findall(r"[^\W_]+", str(name or ""))
    if len(words) > 1 and words[0].lower() in _SKIP_WORDS:
        words = words[1:]
    letters = (words[0][0] + words[1][0]) if len(words) > 1 else (words[0][:2] if words else "")
    return letters.upper() or "•"


def img(src, cls="", name=""):
    """The picture, or (when there is none) a generated tile with the initials of ``name``. The tile is plain CSS on the
    slide's own accent colours; the angle of its gradient comes from the name so neighbours do not look alike."""
    if src:
        return f'<img class="{cls}" src="{e(src)}" alt="">'
    angle = 115 + zlib.crc32(str(name).encode("utf-8")) % 90
    return f'<div class="ph {cls}" style="--pa:{angle}deg"><strong>{e(initials(name))}</strong></div>'


def render(data, prev, history):
    d = data["design"]
    hl = data.get("highlights") or {}
    y, m = map(int, data["month"].split("-"))
    mname = MONTHS[m - 1]
    jf, mu, ab, pc, ph, t = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "totals"))
    ga = data.get("games") or {}
    P = lambda *p: get(prev, *p) if prev else None  # noqa: E731
    V, T = d["variants"], d["texts"]  # designs stored before a slide type existed lack its entries: first variant and text
    kick = lambda k: f'<p class="kicker">{e(KICKERS[k][T.get(k, 0)].format(m=mname, y=y))}</p>'  # noqa: E731
    content = []  # (key, weight, html, backdrop image)

    def add(key, weight, inner, backdrop=None):
        content.append((key, weight, inner, backdrop))

    # Intro
    pairs = [(s.get("poster"), s["name"]) for s in jf.get("series") or []] + [(x.get("poster"), x["name"]) for x in jf.get("movies") or []] \
        + [(x.get("cover"), x["name"]) for x in mu.get("top_tracks") or []] + [(x.get("cover"), x["title"]) for x in ab.get("books") or []] \
        + [(x.get("cover"), x["name"]) for x in ga.get("games") or []]
    images = [p for p in pairs if p[0]][:6]
    if images:  # a few missing pictures become placeholder tiles, so the collage keeps its shape
        images += [p for p in pairs if not p[0]][:max(0, 6 - len(images))]
    leads = intro_leads(data, hl)
    lead = e(leads.get(d.get("lead"), leads["neutral"]))
    if V["intro"] == 1 and images:
        collage = "".join(f'<div class="cl" style="--r:{(k * 37) % 24 - 12}deg">{img(i, "", n)}</div>' for k, (i, n) in enumerate(images))
        intro = f'<div class="collage">{collage}</div>{kick("intro")}<h1 class="giant">{mname}</h1><p class="lead">{lead}</p>'
    elif V["intro"] == 2:
        intro = f'<p class="outline">{y}</p>{kick("intro")}<h1 class="giant">{mname}</h1><p class="lead">{lead}</p>'
    else:
        intro = f'{kick("intro")}<h1 class="giant">{mname}<br><span class="year">{y}</span></h1><p class="lead">{lead}</p>'

    # Total
    parts = [("Serien", t["series_hours"]), ("Filme", t["movie_hours"]), ("Musik", t["music_hours"]),
             ("Hörbücher", t["audiobook_hours"]), ("Podcasts", t["podcast_hours"]), ("Games", t.get("games_hours", 0))]
    parts = [(n, v) for n, v in parts if v]
    mx = max([v for _, v in parts] + [1])
    tot = t["media_hours"] or 1
    if V["total"] == 1:
        seg = "".join(f'<span style="--w:{v / tot * 100:.1f}%;--o:{1 - k * .16:.2f}"></span>' for k, (n, v) in enumerate(parts))
        legend = "".join(f'<li style="--o:{1 - k * .16:.2f}"><i></i>{n}<b>{de_num(v, 1)} h</b></li>' for k, (n, v) in enumerate(parts))
        body = f'<div class="stack">{seg}</div><ul class="legend">{legend}</ul>'
        big = f'<p class="big">{count(round(t["media_hours"]))}<small> Stunden</small></p>'
    elif V["total"] == 2:
        body = "".join(f'<p class="mini"><b>{de_num(v, 1)} h</b> {n}</p>' for n, v in parts)
        big = f'<p class="big">{count(round(t["media_hours"] / 24, 1), 1)}<small> Tage am Stück</small></p>'
    else:
        body = '<div class="bars">' + "".join(
            f'<div class="bar"><span class="bl">{n}</span><span class="bt" style="--w:{v / mx * 100:.0f}%"></span>'
            f'<span class="bv">{de_num(v, 1)} h</span></div>' for n, v in parts) + "</div>"
        big = f'<p class="big">{count(round(t["media_hours"]))}<small> Stunden</small></p>'
    total_lead = (f"So viel wie noch nie, bisher lag dein Rekord bei {de_num(hl['record']['old'])} Stunden." if hl.get("record")
                  else f"Medien in einem Monat, das sind <b>{de_num(t['media_hours'] / 24, 1)} Tage</b> am Stück."
                  if V["total"] != 2 else f"Das sind <b>{de_num(t['media_hours'])} Stunden</b> Serien, Filme, Musik und mehr.")
    total = f'{kick("total")}{big}<p class="lead">{total_lead}</p>{body}{delta(t["media_hours"], P("totals", "media_hours"))}'

    # Series
    if jf.get("series"):
        top = jf["series"][0]
        se = top["seasons"]
        season_txt = (f"Staffel {se[0]} bis {se[-1]}" if len(se) > 1 else f"Staffel {se[0]}") if se else ""
        rest = "".join(f'<li>{img(s.get("poster"), "th", s["name"])}<b>{e(s["name"])}</b><span>{s["episodes"]} Folgen · {de_num(s["hours"], 1)} h</span></li>'
                       for s in jf["series"][1:4])
        stats = f'<b>{top["episodes"]} Folgen</b> · {de_num(top["hours"], 1)} Stunden{" · " + season_txt if season_txt else ""}'
        dl = delta(jf["series_hours"], P("jellyfin", "series_hours"))
        if V["series"] == 1 and top.get("poster"):
            add("series", t["series_hours"], f'{kick("series")}<p class="big">{count(top["episodes"])}<small> Folgen</small></p>'
                f'<h2 class="title">{e(top["name"])}</h2><p class="lead">{de_num(top["hours"], 1)} Stunden'
                f'{" · " + season_txt if season_txt else ""}</p><ol class="list thumbs">{rest}</ol>{dl}', top["poster"])
        elif V["series"] == 2 and len([s for s in jf["series"][:3] if s.get("poster")]) >= 2:
            pod = "".join(f'<figure class="p{k + 1}">{img(s.get("poster"), "", s["name"])}<b>{k + 1}</b><figcaption>{e(s["name"])}'
                          f'<span>{s["episodes"]} Folgen</span></figcaption></figure>' for k, s in enumerate(jf["series"][:3]))
            add("series", t["series_hours"], f'{kick("series")}<h2 class="title">{e(top["name"])}</h2><p class="lead">{stats}</p>'
                f'<div class="podium">{pod}</div><p class="foot">{jf["episodes"]} Folgen aus {jf["series_count"]} Serien</p>{dl}')
        else:
            add("series", t["series_hours"], f'{kick("series")}{img(top.get("poster"), "poster tilt") if top.get("poster") else ""}'
                f'<h2 class="title">{e(top["name"])}</h2><p class="lead">{stats}</p><ol class="list thumbs">{rest}</ol>'
                f'<p class="foot">{jf["episodes"]} Folgen aus {jf["series_count"]} Serien</p>{dl}')

    # Couch day
    if jf.get("best_day"):
        bd = jf["best_day"]
        day = dt.date.fromisoformat(bd["date"])
        owl = ""
        if jf.get("night_owl"):
            no = jf["night_owl"]
            owl = (f'<p class="foot">Nachteulen-Moment: <b>{e(no["title"].split(" - ")[0])}</b> um {no["time"]} Uhr '
                   f'am {dt.date.fromisoformat(no["date"]).day}. {mname}.</p>')
        what = " und ".join(f"<b>{e(x)}</b>" for x in bd["titles"])
        if V["binge"] == 1 and jf.get("days"):
            first = dt.date(y, m, 1)
            ndays = (dt.date(y + (m == 12), m % 12 + 1, 1) - first).days
            peak = max(jf["days"].values()) or 1
            cells = "".join('<i class="pad"></i>' for _ in range(first.weekday()))
            for k in range(ndays):
                dd = first + dt.timedelta(days=k)
                v = jf["days"].get(dd.isoformat(), 0)
                cells += f'<i class="{"hot" if dd == day else ""}" style="--a:{v / peak:.2f}" title="{dd.day}."><span>{dd.day}</span></i>'
            add("binge", -1, f'{kick("binge")}<h2 class="title">{de_date(day)}</h2><p class="lead">{count(bd["hours"], 1)} Stunden, '
                f'vor allem mit {what}.</p><div class="cal"><b>Mo</b><b>Di</b><b>Mi</b><b>Do</b><b>Fr</b><b>Sa</b><b>So</b>{cells}</div>{owl}')
        else:
            add("binge", -1, f'{kick("binge")}<h2 class="title">{de_date(day)}</h2><p class="big">{count(bd["hours"], 1)}'
                f'<small> Stunden</small></p><p class="lead">vor allem mit {what}.</p><p class="foot">An {jf["active_days"]} Tagen '
                f'lief Jellyfin.</p>{owl}')

    # Movies
    if jf.get("movies"):
        n = jf["movies_count"]
        head = f'{kick("movies")}<p class="big">{count(n)}<small> {"Film" if n == 1 else "Filme"}</small></p>'
        dl = delta(jf["movie_hours"], P("jellyfin", "movie_hours"))
        if V["movies"] == 1:
            fan = "".join(f'<figure style="--i:{k};--n:{len(jf["movies"][:5])}">{img(x.get("poster"), "", x["name"])}</figure>' for k, x in enumerate(jf["movies"][:5]))
            names = ", ".join(e(x["name"]) for x in jf["movies"])
            add("movies", t["movie_hours"], f'{head}<div class="fan">{fan}</div><p class="lead">{names}</p>{dl}')
        else:
            grid = "".join(f'<figure>{img(x.get("poster"), "", x["name"])}<figcaption>{e(x["name"])}{year(x)}'
                           f'</figcaption></figure>' for x in jf["movies"])
            add("movies", t["movie_hours"], f'{head}<div class="posters">{grid}</div>{dl}')

    # Music
    if mu.get("plays"):
        ta = mu["top_artists"][0]
        dl = delta(mu["minutes"], P("music", "minutes"))
        cover = next((x.get("cover") for x in mu["top_tracks"] if x.get("cover")), None) or ta.get("cover")
        if V["music"] == 1:
            ranks = "".join(f'<li><b>{k + 1}</b>{img(a.get("cover"), "th", a["name"])}<span>{e(a["name"])}</span><em>{a["plays"]}×</em></li>' for k, a in enumerate(mu["top_artists"]))
            add("music", t["music_hours"], f'{kick("music")}<p class="lead">{de_num(mu["plays"])} Wiedergaben, {de_num(mu["minutes"])} Minuten</p>'
                f'<p class="sub">Deine Top-Künstler</p><ol class="ranks thumbs">{ranks}</ol>{dl}', cover)
        elif V["music"] == 2:
            eq = "".join(f'<i style="--d:{(k * 0.13) % 1:.2f}s"></i>' for k in range(16))
            add("music", t["music_hours"], f'{kick("music")}<div class="eq">{eq}</div><p class="big">{count(mu["minutes"])}<small> Minuten</small></p>'
                f'<p class="lead">Ganz vorne: <b>{e(ta["name"])}</b> mit {ta["plays"]} Wiedergaben. Dazu {mu["artists_count"] - 1} '
                f'weitere Künstler.</p>{dl}')
        else:
            tracks = "".join(f'<li>{img(x.get("cover"), "cv", x["name"])}<b>{e(x["name"])}</b><span>{e(x["artist"])} · {x["plays"]}×</span></li>'
                             for x in mu["top_tracks"])
            add("music", t["music_hours"], f'{kick("music")}<p class="big">{count(mu["minutes"])}<small> Minuten</small></p>'
                f'<p class="lead">{de_num(mu["plays"])} Wiedergaben von {mu["artists_count"]} Künstlern. Ganz vorne: '
                f'<b>{e(ta["name"])}</b> ({ta["plays"]}×).</p><p class="sub">Deine Top-Titel</p><ol class="tracks">{tracks}</ol>{dl}')

    # Audiobooks and podcasts (same layout)
    def listen_slide(key, items, hours, minutes, sub, prev_minutes, foot=""):
        b = items[0]
        title = b.get("title")
        meta = f'{e(b.get("author") or "")}{" · " if b.get("author") else ""}<b>{de_num(b["hours"], 1)} Stunden</b>'
        rest = "".join(f'<li>{img(x.get("cover"), "th", x["title"])}<b>{e(x["title"])}</b><span>{e(x.get("author") or str(x.get("episodes", "")) + " Folgen")}'
                       f' · {de_num(x["hours"], 1)} h{" · ✓ fertig" if x.get("finished") else ""}</span></li>' for x in items[1:4])
        dl = delta(minutes, prev_minutes)
        if V[key] == 1 and b.get("cover"):
            add(key, hours, f'{kick(key)}{img(b["cover"], "poster square")}<h2 class="title">{e(title)}</h2><p class="lead">{meta}</p>'
                f'<p class="foot">{sub}</p>{foot}{dl}', b["cover"])
        else:
            add(key, hours, f'{kick(key)}{img(b.get("cover"), "poster square tilt") if b.get("cover") else ""}<h2 class="title">{e(title)}</h2>'
                f'<p class="lead">{meta}</p><ol class="list thumbs">{rest}</ol><p class="foot">{sub}</p>{foot}{dl}')

    if ab.get("books"):
        fin = f'<p class="foot">{ab["finished"]} {"Hörbuch" if ab["finished"] == 1 else "Hörbücher"} zu Ende gehört.</p>' if ab["finished"] else ""
        listen_slide("books", ab["books"], t["audiobook_hours"], ab["minutes"],
                     f'Insgesamt {de_num(ab["hours"], 1)} Stunden mit {ab["titles_count"]} Titeln.', P("audiobooks", "minutes"), fin)
    if pc.get("podcasts"):
        listen_slide("pods", pc["podcasts"], t["podcast_hours"], pc["minutes"],
                     f'Insgesamt {de_num(pc["hours"], 1)} Stunden mit {pc["episodes"]} Folgen.', P("podcasts", "minutes"))

    # Photos
    if ph.get("total"):
        bd = ph.get("best_day")
        places = ", ".join(e(p["name"]) for p in ph["places"][:4])
        head = f'{kick("photos")}<p class="big">{count(ph["total"])}<small> Fotos &amp; Videos</small></p>'
        info = (f'<p class="lead">{ph["photos"]} Fotos, {ph["videos"]} Videos.'
                f'{" Am meisten am <b>" + de_date(dt.date.fromisoformat(bd["date"])) + "</b>." if bd else ""}</p>'
                f'{f"<p class=sub>Unterwegs in {places}</p>" if places else ""}')
        hs = ph.get("highlights") or []
        cap = lambda x: f'{dt.date.fromisoformat(x["date"]).day}.{m}.{" · " + e(x["city"]) if x.get("city") else ""}'  # noqa: E731
        if V["photos"] == 1 and hs:
            pol = "".join(f'<figure style="--r:{((k * 53) % 22) - 11}deg;--x:{(k % 3) * 31}%;--y:{(k // 3) * 46}%">'
                          f'<img src="{e(x["img"])}" alt=""><figcaption>{cap(x)}</figcaption></figure>' for k, x in enumerate(hs))
            body = f'<div class="polas">{pol}</div>'
        elif V["photos"] == 2 and hs:
            hero = hs[len(hs) // 2]
            strip = "".join(f'<img src="{e(x["img"])}" alt="">' for x in hs if x is not hero)
            body = f'<figure class="hero"><img src="{e(hero["img"])}" alt=""><figcaption>{cap(hero)}</figcaption></figure><div class="strip">{strip}</div>'
        else:
            body = '<div class="grid">' + "".join(f'<figure><img src="{e(x["img"])}" alt="" loading="lazy"><figcaption>{cap(x)}</figcaption></figure>' for x in hs) + "</div>"
        add("photos", ph["total"] / 20, f'{head}{info}{body}{delta(ph["total"], P("photos", "total"))}')

    # Games
    if ga.get("games"):
        gs = ga["games"][:5]
        top = gs[0]
        n = ga["games_count"]
        gv = V.get("games", 0)
        dl = delta(ga["minutes"], P("games", "minutes"))
        news = [x["name"] for x in gs if x.get("new")]
        extra = (f'<p class="foot">Zum ersten Mal Spiele in deinem Rückblick.</p>' if hl.get("first_games")
                 else f'<p class="foot">Neu im Regal: {", ".join(e(x) for x in news[:3])}.</p>' if news and len(news) < len(gs) else "")
        big = f'<p class="big">{count(ga["hours"], 1)}<small> Stunden gespielt</small></p>'
        sys_txt = (f'{n} {"Spiel" if n == 1 else "Spiele"} auf {ga["systems_count"]} {"System" if ga["systems_count"] == 1 else "Systemen"}'
                   f', am meisten auf <b>{e(ga["top_system"])}</b>.')
        rows = "".join(f'<li>{img(x.get("cover"), "th", x["name"])}<b>{e(x["name"])}</b><span>{game_meta(x)}</span></li>' for x in gs)
        if gv == 1 and top.get("cover"):
            add("games", t.get("games_hours", 0), f'{kick("games")}{big}<p class="lead">{sys_txt}</p>'
                f'<ol class="list thumbs">{rows}</ol>{extra}{dl}', top["cover"])
        elif gv == 2 and len(gs) >= 3:
            pod = "".join(f'<figure class="p{k + 1}">{img(x.get("cover"), "", x["name"])}<b>{k + 1}</b><figcaption>{e(x["name"])}'
                          f'<span>{game_time(x["minutes"])}</span></figcaption></figure>' for k, x in enumerate(gs[:3]))
            add("games", t.get("games_hours", 0), f'{kick("games")}{big}<div class="podium">{pod}</div>'
                f'<p class="foot">{sys_txt}</p>{extra}{dl}')
        else:
            fan = "".join(f'<figure style="--i:{k};--n:{len(gs)}">{img(x.get("cover"), "", x["name"])}</figure>' for k, x in enumerate(gs))
            add("games", t.get("games_hours", 0), f'{kick("games")}{big}<div class="fan">{fan}</div>'
                f'<p class="lead">Am längsten gespielt: <b>{e(top["name"])}</b> ({game_time(top["minutes"])}).</p>'
                f'<p class="foot">{sys_txt}</p>{extra}{dl}')

    # Highlight slides
    special = {}
    if hl.get("record"):
        special["total"] = [("record", f'<p class="kicker">Neuer Rekord</p><p class="big">{count(round(hl["record"]["hours"]))}<small> Stunden</small></p>'
                             f'<p class="lead">Noch nie hast du in einem Monat so viel geschaut und gehört. Der alte Bestwert: '
                             f'{de_num(hl["record"]["old"])} Stunden.</p>', None)]
    if hl.get("finished"):
        posters = {s["name"]: s.get("poster") for s in jf.get("series") or []}
        names = hl["finished"]
        special.setdefault("series", []).append(("finished", f'<p class="kicker">Durchgeschaut</p><h2 class="title">{e(names[0])}</h2>'
                                                 f'<p class="lead">{"Bis zur letzten Folge." if len(names) == 1 else "Und dazu: " + ", ".join(e(n) for n in names[1:]) + "."}</p>'
                                                 f'{img(posters.get(names[0]), "poster tilt") if posters.get(names[0]) else ""}', None))
    if hl.get("new_artists"):
        li = "".join(f'<li><b>{e(a["name"])}</b><span>{a["plays"]}× gehört</span></li>' for a in hl["new_artists"])
        special.setdefault("music", []).append(("new_artists", f'<p class="kicker">Neu entdeckt</p><h2 class="title">'
                                                f'{e(hl["new_artists"][0]["name"])}</h2><p class="lead">Diesen Monat zum ersten Mal in deinen Ohren.</p>'
                                                f'<ol class="list plain">{li}</ol>', None))
    if hl.get("trip"):
        tp = hl["trip"]["places"]
        li = "".join(f'<li><b>{e(p["name"])}</b><span>{e(p.get("country") or "")} · {de_num(p["km"])} km · {p["count"]} Bilder</span></li>' for p in tp)
        special.setdefault("photos", []).append(("trip", f'<p class="kicker">Unterwegs</p><p class="big">{count(tp[0]["km"])}<small> km</small></p>'
                                                 f'<p class="lead">So weit weg von {e(hl["trip"]["home"])} waren deine Fotos, '
                                                 f'bis nach <b>{e(tp[0]["name"])}</b>.</p><ol class="list plain">{li}</ol>', None))
    if hl.get("game_marathon"):
        gm = hl["game_marathon"]
        cover = (ga.get("games") or [{}])[0].get("cover")
        starts = f" in {gm['sessions']} {'Start' if gm['sessions'] == 1 else 'Starts'}" if gm.get("sessions") else ""
        special.setdefault("games", []).append(("marathon", f'<p class="kicker">Dauerbrenner</p><p class="big">{count(gm["hours"], 1)}<small> Stunden</small></p>'
                                                f'<h2 class="title">{e(gm["name"])}</h2><p class="lead">So lange hast du dieses Spiel in einem Monat gespielt{starts}.</p>'
                                                f'{img(cover, "poster tilt") if cover else ""}', None))
    if hl.get("streak"):
        sd = dt.date.fromisoformat(hl["streak"]["from"])
        special.setdefault("binge", []).append(("streak", f'<p class="kicker">Am Stück</p><p class="big">{count(hl["streak"]["days"])}<small> Tage</small></p>'
                                                f'<p class="lead">Ab dem {sd.day}. {MONTHS[sd.month - 1]} lief jeden Tag etwas: Serie, Musik oder Hörbuch.</p>', None))

    # Order: what shaped the month first; the couch day right after the series
    main = sorted([c for c in content if c[1] >= 0], key=lambda c: -c[1])
    order = []
    for c in main:
        order.append(c)
        if c[0] == "series":
            order += [x for x in content if x[0] == "binge"]
    order += [c for c in content if c[1] < 0 and c not in order]
    slides = [("intro", intro, None), ("total", total, None)]
    slides += [(k, h, None) for k, h, _ in special.get("total", [])]
    for key, _, inner, bd in order:
        slides.append((key, inner, bd))
        slides += [(k, h, b) for k, h, b in special.get(key, [])]
    for key in ("series", "binge", "music", "photos", "games"):
        if key in special and not any(c[0] == key for c in order):
            slides += [(k, h, b) for k, h, b in special[key]]

    # Outro
    facts = [("Stunden Medien", de_num(t["media_hours"])), ("Serienfolgen", jf.get("episodes", 0)),
             ("Filme", jf.get("movies_count", 0)), ("Musik-Minuten", de_num(mu.get("minutes", 0))),
             ("Hörbuch-Stunden", de_num(ab.get("hours", 0), 1)), ("Fotos & Videos", ph.get("total", 0))]
    if ga.get("games"):
        facts += [("Spiele", ga["games_count"]), ("Stunden gespielt", de_num(ga["hours"], 1))]
    tops = [("Serie", get(jf, "series", 0, "name")), ("Künstler", get(mu, "top_artists", 0, "name")),
            ("Hörbuch", get(ab, "books", 0, "title")), ("Podcast", get(pc, "podcasts", 0, "title")), ("Spiel", get(ga, "games", 0, "name"))]
    if V["outro"] == 1:
        rows = "".join(f'<li><span>{k}</span><b>{v}</b></li>' for k, v in facts) + '<li class="sep"></li>' + \
            "".join(f'<li><span>Top-{k}</span><b>{e(v)}</b></li>' for k, v in tops if v)
        outro = (f'<div class="receipt"><p class="rh">RÜCKBLICK · {mname.upper()} {y}</p><ul>{rows}</ul>'
                 f'<p class="rf">Danke für deinen Besuch</p></div><p class="foot"><a href="../">Alle Rückblicke</a></p>')
    else:
        outro = (f'{kick("outro")}<div class="card"><div class="facts">{"".join(f"<div><b>{v}</b><span>{k}</span></div>" for k, v in facts)}</div>'
                 f'<ul class="tops">{"".join(f"<li><span>{k}</span><b>{e(v)}</b></li>" for k, v in tops if v)}</ul></div>'
                 f'<p class="foot"><a href="../">Alle Rückblicke</a></p>')
    slides.append(("outro", outro, None))

    # Colours, music and assembly
    sets = PALETTES[d["palette"]]["sets"]
    clips = data.get("soundtrack") or []
    others, n = clips[1:] or clips, 0
    html_slides = []
    for k, (key, inner, backdrop) in enumerate(slides):
        bg, ac, b1, b2 = sets[(k + d["offset"]) % len(sets)]
        attr = f' style="--bg:{bg};--accent:{ac};--blob:{b1};--blob2:{b2}"'
        label = ""
        if clips:
            if key == "music":
                c = clips[0]
            else:
                c, n = others[n % len(others)], n + 1
            attr += f' data-clip="{e(c["src"])}"'
            label = f'<p class="np">♪ {e(c["title"])} · {e(c["artist"])}</p>'
        bd = f'<div class="bd" style="background-image:url(\'{e(backdrop)}\')"></div>' if backdrop else ""
        html_slides.append(f'<section class="slide s-{key} v{V.get(key, 0)}"{attr}>{bd}<div class="inner">{inner}</div>{label}</section>')
    bars = "".join("<i><b></b></i>" for _ in slides)
    sound = '<button class="snd" aria-label="Ton an/aus">🔊</button>' if clips else ""
    return (PAGE.replace("{{TITLE}}", f"Rückblick {mname} {y}").replace("{{BARS}}", bars)
            .replace("{{SLIDES}}", "\n".join(html_slides)).replace("{{SOUND}}", sound)
            .replace("{{BODY}}", f'shape-{d["shape"]} type-{d["type"]}'))


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111">
<title>{{TITLE}}</title>
<style>
:root{--ink:#fff;--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;--head:var(--font)}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#000;color:var(--ink);font-family:var(--font);overflow:hidden;-webkit-tap-highlight-color:transparent}
.type-serif{--head:ui-serif,Georgia,"Times New Roman",serif}
.type-caps .giant,.type-caps .title,.type-caps .big small{text-transform:uppercase;letter-spacing:-.01em}
.stage{position:fixed;inset:0;max-width:520px;margin:0 auto}
.slide{position:absolute;inset:0;display:flex;align-items:center;opacity:0;pointer-events:none;transition:opacity .45s ease;overflow:hidden;background:var(--bg)}
.slide.on{opacity:1;pointer-events:auto}
.shape-blobs .slide::before{content:"";position:absolute;width:140vmax;height:140vmax;border-radius:46%;top:-70vmax;right:-80vmax;background:var(--blob);opacity:.55;animation:spin 26s linear infinite}
.shape-blobs .slide::after{content:"";position:absolute;width:90vmax;height:90vmax;border-radius:40%;bottom:-60vmax;left:-50vmax;background:var(--blob2);opacity:.5;animation:spin 34s linear infinite reverse}
.shape-stripes .slide::before{content:"";position:absolute;inset:-50%;background:repeating-linear-gradient(-35deg,var(--blob) 0 34px,transparent 34px 92px);opacity:.28;animation:slide 18s linear infinite}
.shape-stripes .slide::after{content:"";position:absolute;width:70vmax;height:70vmax;border-radius:50%;bottom:-40vmax;right:-30vmax;background:var(--blob2);opacity:.6}
.shape-rings .slide::before{content:"";position:absolute;width:170vmax;height:170vmax;left:50%;top:50%;transform:translate(-50%,-50%);background:repeating-radial-gradient(circle,var(--blob) 0 18px,transparent 18px 70px);opacity:.25;animation:pulse 9s ease-in-out infinite}
.shape-glow .slide::before{content:"";position:absolute;width:80vmax;height:80vmax;border-radius:50%;top:-25vmax;left:-25vmax;background:radial-gradient(circle,var(--blob),transparent 65%);opacity:.75;animation:drift 14s ease-in-out infinite alternate}
.shape-glow .slide::after{content:"";position:absolute;width:90vmax;height:90vmax;border-radius:50%;bottom:-35vmax;right:-35vmax;background:radial-gradient(circle,var(--blob2),transparent 65%);opacity:.7;animation:drift 17s ease-in-out infinite alternate-reverse}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes slide{to{transform:translateX(184px)}}
@keyframes pulse{50%{transform:translate(-50%,-50%) scale(1.12)}}
@keyframes drift{to{transform:translate(12vmax,10vmax)}}
.bd{position:absolute;inset:-40px;background-size:cover;background-position:center;filter:blur(28px) saturate(1.4) brightness(.5);transform:scale(1.15);z-index:0}
.slide:has(.bd)::before,.slide:has(.bd)::after{display:none}
.inner{position:relative;z-index:1;width:100%;padding:72px 26px 52px;max-height:100%;overflow-y:auto}
.slide.on .inner>*{animation:rise .7s cubic-bezier(.2,.8,.2,1) both}
.slide.on .inner>*:nth-child(2){animation-delay:.08s}.slide.on .inner>*:nth-child(3){animation-delay:.16s}
.slide.on .inner>*:nth-child(4){animation-delay:.24s}.slide.on .inner>*:nth-child(5){animation-delay:.32s}
.slide.on .inner>*:nth-child(n+6){animation-delay:.4s}
@keyframes rise{from{opacity:0;transform:translateY(22px)}to{opacity:1;transform:none}}
.kicker{font-size:.8rem;font-weight:800;letter-spacing:.18em;text-transform:uppercase;opacity:.85;margin-bottom:14px}
.giant{font-family:var(--head);font-size:clamp(3.4rem,17vw,5.6rem);font-weight:900;line-height:.92;letter-spacing:-.04em}
.year{color:var(--accent)}
.outline{font-family:var(--head);font-size:clamp(6rem,38vw,11rem);font-weight:900;line-height:.8;color:transparent;-webkit-text-stroke:2px var(--accent);opacity:.9;margin-bottom:10px;letter-spacing:-.05em}
.big{font-family:var(--head);font-size:clamp(3.6rem,19vw,6rem);font-weight:900;line-height:1;letter-spacing:-.045em;color:var(--accent);margin:4px 0 12px}
.big small{font-family:var(--font);font-size:.28em;letter-spacing:0;color:var(--ink);font-weight:800}
.title{font-family:var(--head);font-size:clamp(1.9rem,8.5vw,2.7rem);font-weight:900;line-height:1.05;letter-spacing:-.03em;margin:6px 0 10px}
.lead{font-size:1.12rem;line-height:1.45;max-width:32ch}
.lead b,.list b,.foot b{color:var(--accent)}
.sub{margin:22px 0 8px;font-weight:800;font-size:.85rem;letter-spacing:.12em;text-transform:uppercase;opacity:.85}
.foot{margin-top:16px;font-size:.95rem;opacity:.9;line-height:1.4}
.foot a{color:var(--ink)}
.mini{font-size:1.05rem;margin-top:6px}.mini b{color:var(--accent);font-size:1.3rem}
.delta{display:inline-block;margin-top:18px;padding:6px 12px;border-radius:99px;background:rgba(0,0,0,.28);font-size:.85rem;font-weight:700}
.delta.up{color:#b8ff6b}.delta.down{color:#ffb4a8}
.bars{margin-top:22px;display:grid;gap:10px}
.bar{display:grid;grid-template-columns:90px 1fr auto;align-items:center;gap:10px;font-weight:700;font-size:.92rem}
.bt{height:14px;border-radius:99px;background:var(--accent);width:var(--w);min-width:6px}
.slide.on .bt{animation:grow 1s .3s cubic-bezier(.2,.8,.2,1) both}
@keyframes grow{from{width:0}}
.stack{display:flex;height:26px;border-radius:99px;overflow:hidden;margin-top:22px;background:rgba(255,255,255,.12)}
.stack span{width:var(--w);background:var(--accent);opacity:var(--o);border-right:2px solid var(--bg)}
.slide.on .stack span{animation:grow 1s .3s cubic-bezier(.2,.8,.2,1) both}
.legend{list-style:none;margin-top:14px;display:grid;gap:6px}
.legend li{display:flex;align-items:center;gap:8px;font-weight:700}.legend b{margin-left:auto}
.legend i{width:12px;height:12px;border-radius:3px;background:var(--accent);opacity:var(--o)}
.poster{display:block;height:min(36vh,300px);aspect-ratio:2/3;object-fit:cover;border-radius:10px;box-shadow:0 18px 40px rgba(0,0,0,.45);margin:4px 0 18px}
.poster.square{aspect-ratio:1}
.tilt{transform:rotate(-3deg)}
.list{list-style:none;margin-top:18px;display:grid;gap:10px;counter-reset:n 1}
.list li{counter-increment:n;display:grid;grid-template-columns:28px 1fr;column-gap:8px;line-height:1.25}
.list li::before{content:counter(n);grid-row:span 2;font-weight:900;font-size:1.3rem;opacity:.6}
.list.plain{counter-reset:n 0}
.list span,.tracks span{font-size:.88rem;opacity:.85}
.tracks{list-style:none;display:grid;gap:10px}
.tracks li{display:grid;grid-template-columns:48px 1fr;column-gap:12px;align-items:center;line-height:1.25}
.tracks img,.tracks .cv{grid-row:span 2;width:48px;height:48px;border-radius:6px;object-fit:cover;background:rgba(255,255,255,.18)}
.ranks{list-style:none;display:grid;gap:4px}
.ranks li{display:grid;grid-template-columns:44px 1fr auto;align-items:baseline;gap:8px}
.ranks b{font-family:var(--head);font-size:2.4rem;font-weight:900;color:var(--accent);line-height:1}
.ranks span{font-size:1.25rem;font-weight:800;line-height:1.15}.ranks em{font-style:normal;opacity:.8;font-weight:700}
.ranks li:first-child span{font-size:1.6rem}
.eq{display:flex;gap:5px;align-items:flex-end;height:70px;margin:6px 0 4px}
.eq i{flex:1;background:var(--accent);border-radius:3px;height:30%;animation:eq .9s ease-in-out var(--d) infinite alternate}
@keyframes eq{to{height:100%}}
.posters{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}
.posters img,.posters .ph{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:8px;background:rgba(255,255,255,.15);box-shadow:0 10px 24px rgba(0,0,0,.35)}
.posters figcaption{font-size:.72rem;line-height:1.2;margin-top:4px;font-weight:600}
.fan{position:relative;height:min(34vh,260px);margin:10px 0 18px}
.fan figure{position:absolute;left:50%;bottom:0;width:38%;transform-origin:50% 120%;transform:translateX(-50%) rotate(calc((var(--i) - (var(--n) - 1) / 2) * 11deg))}
.fan img,.fan .ph{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:8px;box-shadow:0 12px 26px rgba(0,0,0,.5);background:rgba(255,255,255,.15)}
.podium{display:grid;grid-template-columns:1fr 1.2fr 1fr;align-items:end;gap:8px;margin-top:16px}
.podium figure{text-align:center;position:relative}
.podium .p1{order:2}.podium .p2{order:1}.podium .p3{order:3}
.podium img,.podium .ph{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:8px;box-shadow:0 10px 24px rgba(0,0,0,.4);background:rgba(255,255,255,.15)}
.podium b{position:absolute;top:-14px;left:50%;transform:translateX(-50%);width:30px;height:30px;border-radius:50%;background:var(--accent);color:#111;font-weight:900;line-height:30px}
.podium figcaption{font-size:.75rem;font-weight:700;margin-top:6px;line-height:1.2}
.podium figcaption span{display:block;opacity:.75;font-weight:600}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-top:16px}
.cal>b{font-size:.68rem;text-align:center;opacity:.7}
.cal i{aspect-ratio:1;border-radius:6px;background:color-mix(in srgb,var(--accent) calc(var(--a) * 100%),rgba(255,255,255,.08));position:relative}
.cal i span{position:absolute;inset:0;display:grid;place-items:center;font-size:.62rem;font-style:normal;font-weight:700;opacity:.75;color:#fff;mix-blend-mode:difference}
.cal i.pad{background:none}
.cal i.hot{outline:3px solid #fff;outline-offset:1px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:14px}
.grid figure{position:relative}
.grid img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;display:block}
.grid figcaption,.hero figcaption{position:absolute;left:6px;bottom:5px;font-size:.66rem;font-weight:700;text-shadow:0 1px 3px #000}
.polas{position:relative;height:min(42vh,330px);margin-top:14px}
.polas figure{position:absolute;left:var(--x);top:var(--y);width:40%;background:#fafafa;padding:6px 6px 22px;border-radius:3px;box-shadow:0 10px 24px rgba(0,0,0,.45);transform:rotate(var(--r))}
.polas img{display:block;width:100%;aspect-ratio:1;object-fit:cover}
.polas figcaption{position:absolute;left:0;right:0;bottom:4px;text-align:center;color:#333;font-size:.62rem;font-weight:700}
.hero{position:relative;margin-top:14px}
.hero img{display:block;width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:12px}
.strip{display:flex;gap:6px;margin-top:6px}
.strip img{flex:1;min-width:0;aspect-ratio:1;object-fit:cover;border-radius:8px}
.s-intro .inner{position:static}.s-intro .inner>*:not(.collage){position:relative;z-index:1}
.collage{position:absolute;inset:0;z-index:0;display:grid;grid-template-columns:repeat(3,1fr);align-content:center;gap:12px;padding:70px 14px;filter:brightness(.32) saturate(1.15);pointer-events:none}
.slide.on .inner>.collage{animation:dim 1.2s ease both}
@keyframes dim{from{opacity:0}to{opacity:1}}
.collage .cl{transform:rotate(var(--r))}
.collage img,.collage .ph{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:8px}
.s-intro .inner{position:relative}
.card{background:rgba(0,0,0,.32);border-radius:20px;padding:22px;backdrop-filter:blur(6px)}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:16px 12px}
.facts b{display:block;font-family:var(--head);font-size:1.9rem;font-weight:900;letter-spacing:-.03em;color:var(--accent);line-height:1}
.facts span{font-size:.78rem;font-weight:700;opacity:.85}
.tops{list-style:none;margin-top:20px;display:grid;gap:8px;border-top:1px solid rgba(255,255,255,.25);padding-top:16px}
.tops li{display:flex;gap:10px;justify-content:space-between;font-size:.92rem}
.tops span{opacity:.75}.tops b{text-align:right}
.receipt{background:#fbfaf5;color:#1b1b1b;font-family:ui-monospace,"Courier New",monospace;padding:22px 18px 18px;border-radius:4px;box-shadow:0 18px 40px rgba(0,0,0,.45);transform:rotate(-1.5deg);
 -webkit-mask:linear-gradient(#000 0 0) top/100% calc(100% - 10px) no-repeat,radial-gradient(circle at 6px 100%,transparent 6px,#000 6.5px) bottom/12px 10px repeat-x}
.receipt .rh{text-align:center;font-weight:800;letter-spacing:.08em;border-bottom:1px dashed #999;padding-bottom:10px;margin-bottom:10px;font-size:.85rem}
.receipt ul{list-style:none;display:grid;gap:6px;font-size:.85rem}
.receipt li{display:flex;justify-content:space-between;gap:10px}.receipt li b{text-align:right}
.receipt li.sep{border-top:1px dashed #999;margin:4px 0}
.receipt .rf{text-align:center;margin-top:12px;font-size:.75rem;opacity:.7}
.progress{position:fixed;top:0;left:0;right:0;max-width:520px;margin:0 auto;display:flex;gap:4px;padding:calc(env(safe-area-inset-top) + 12px) 12px 0;z-index:5}
.progress i{flex:1;height:3px;border-radius:3px;background:rgba(255,255,255,.3);overflow:hidden}
.progress b{display:block;height:100%;width:0;background:#fff}
.progress i.done b{width:100%}
.progress i.cur b{animation:fill var(--dur) linear forwards}
.paused .progress i.cur b{animation-play-state:paused}
@keyframes fill{to{width:100%}}
.tap{position:fixed;top:0;bottom:0;z-index:4;width:34%}
.tap.l{left:0}.tap.r{right:0;width:66%}
.np{position:absolute;left:0;right:0;bottom:calc(env(safe-area-inset-bottom) + 14px);z-index:2;text-align:center;font-size:.78rem;font-weight:700;opacity:.75;padding:0 70px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.snd{position:fixed;z-index:6;top:calc(env(safe-area-inset-top) + 24px);right:max(12px,calc(50vw - 248px));width:40px;height:40px;border-radius:50%;border:0;background:rgba(0,0,0,.35);color:#fff;font-size:1.1rem;cursor:pointer}
.thumbs .th{width:44px;height:44px;border-radius:10px;object-fit:cover;background:rgba(255,255,255,.18);align-self:center}
.list.thumbs li{grid-template-columns:28px 44px 1fr;align-items:center}
.list.thumbs .th{grid-row:span 2}
.ranks.thumbs li{grid-template-columns:44px 44px 1fr auto;align-items:center}
.slide .ph{container-type:inline-size;display:grid;place-items:center;overflow:hidden;color:#111;
 background:linear-gradient(var(--pa,135deg),var(--accent),color-mix(in srgb,var(--accent) 38%,var(--blob2)))}
.slide .ph strong{font-size:1.3rem;font-size:34cqw;font-weight:900;letter-spacing:-.02em;line-height:1;opacity:.8}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style></head>
<body class="{{BODY}}">
<div class="progress">{{BARS}}</div>
<main class="stage">
{{SLIDES}}
</main>
<div class="tap l" aria-label="Zurück"></div><div class="tap r" aria-label="Weiter"></div>
{{SOUND}}
<script>
(()=>{const S=[...document.querySelectorAll('.slide')],B=[...document.querySelectorAll('.progress i')],DUR=9000;let i=0,t;
document.documentElement.style.setProperty('--dur',DUR+'ms');
const fmt=(v,d)=>v.toLocaleString('de-DE',{minimumFractionDigits:d,maximumFractionDigits:d});
function count(sl){sl.querySelectorAll('.count').forEach(el=>{const to=+el.dataset.to,d=+(el.dataset.dec||0),t0=performance.now();
 const st=n=>{const p=Math.min(1,(n-t0)/1100),q=1-Math.pow(1-p,3);el.textContent=fmt(to*q,d);if(p<1)requestAnimationFrame(st)};requestAnimationFrame(st)})}
// Zwei Player für einen echten Crossfade. Der Wechsel beginnt kurz vor dem Folienwechsel,
// der erste Titel startet sofort in voller Lautstärke.
const P=[new Audio(),new Audio()],btn=document.querySelector('.snd'),VOL=.9,XF=900,MANUAL=450;let sound=true,cur=0,blocked=false,pre;
P.forEach(a=>{a.loop=true;a.preload='auto'});
S.forEach(s=>{if(s.dataset.clip){const l=document.createElement('link');l.rel='prefetch';l.href=s.dataset.clip;document.head.appendChild(l)}});
function ramp(a,to,ms){clearInterval(a._f);const from=a.volume,t0=performance.now();
 a._f=setInterval(()=>{const p=Math.min(1,(performance.now()-t0)/ms),q=p*p*(3-2*p);a.volume=Math.max(0,Math.min(1,from+(to-from)*q));
  if(p>=1){clearInterval(a._f);if(to===0)a.pause()}},30)}
function crossTo(src,ms){if(!sound||!src)return;const abs=new URL(src,location.href).href,A=P[cur];
 if(A.src===abs&&!A.paused)return;const first=A.paused,N=P[cur^1];cur^=1;if(!first)ramp(A,0,ms);
 clearInterval(N._f);N.src=src;N.currentTime=0;N.volume=first?VOL:0;
 N.play().then(()=>{blocked=false;if(!first)ramp(N,VOL,ms)}).catch(()=>{blocked=true})}
function setSound(on){sound=on;if(btn)btn.textContent=on?'🔊':'🔇';if(on)crossTo(S[i].dataset.clip,MANUAL);else P.forEach(a=>ramp(a,0,300))}
if(btn)btn.onclick=e=>{e.stopPropagation();setSound(!sound)};
// Blockiert der Browser den Ton ohne Berührung, startet ihn die erste Berührung
const unlock=()=>{if(blocked&&sound){blocked=false;P.forEach(a=>{a.pause();a.removeAttribute('src')});cur=0;crossTo(S[i].dataset.clip,0)}};
addEventListener('pointerdown',unlock,true);addEventListener('keydown',unlock,true);
function timers(ms){clearTimeout(t);clearTimeout(pre);if(i>=S.length-1||document.body.classList.contains('paused'))return;
 t=setTimeout(()=>show(i+1,true),ms);pre=setTimeout(()=>crossTo(S[i+1].dataset.clip,XF),Math.max(0,ms-XF*0.6))}
function show(n,auto){i=Math.max(0,Math.min(S.length-1,n));crossTo(S[i].dataset.clip,auto?XF:MANUAL);S.forEach((s,k)=>s.classList.toggle('on',k===i));
 B.forEach((b,k)=>{b.className=k<i?'done':k===i?'cur':'';void b.offsetWidth});count(S[i]);timers(DUR);history.replaceState(null,'','#'+(i+1))}
document.querySelector('.tap.r').onclick=()=>show(i+1);document.querySelector('.tap.l').onclick=()=>show(i-1);
addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key===' ')show(i+1);if(e.key==='ArrowLeft')show(i-1)});
let hold;addEventListener('pointerdown',()=>{hold=setTimeout(()=>{document.body.classList.add('paused');clearTimeout(t);clearTimeout(pre);if(sound)P[cur].pause()},250)});
addEventListener('pointerup',()=>{clearTimeout(hold);if(document.body.classList.contains('paused')){document.body.classList.remove('paused');if(sound)P[cur].play().catch(()=>{});timers(DUR/2)}});
let x0;addEventListener('touchstart',e=>x0=e.touches[0].clientX,{passive:true});
addEventListener('touchend',e=>{const dx=e.changedTouches[0].clientX-x0;if(Math.abs(dx)>60)show(i+(dx<0?1:-1))});
show((parseInt(location.hash.slice(1))||1)-1)})();
</script>
</body></html>
"""
