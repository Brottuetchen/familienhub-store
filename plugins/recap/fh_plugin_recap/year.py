"""The annual recap ("Jahresrückblick"): numbers over a whole year, and its story with a design that remembers last year.

The year uses the monthly machinery (``story``: page template, palettes' shape/type styles, fit script, helpers) but has its own
slide keys, layout variants, headline texts and festive palettes, kept in dicts of its own (``YEAR_VARIANTS`` and
``YEAR_KICKERS``) so the choices of every monthly design stay exactly as they were. The year is chosen from its own random
stream and avoids what the previous year used.

``facts`` derives what only a year can show (hours per month, records, discoveries, trip) from the year's collected data and
the person's monthly snapshots; ``render`` turns the snapshot into the story page.
"""
import collections
import datetime as dt
import random

from . import story
from .story import MONTHS, de_date, de_num, e, get

MONTHS_SHORT = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
FOCUS = {"video": "Serien & Filme", "music": "Musik", "books": "Hörbücher", "pods": "Podcasts", "games": "Games", "photos": "Fotos"}
AREAS = [("series", "Serien"), ("movies", "Filme"), ("artists", "Künstler"), ("songs", "Songs"), ("books", "Hörbücher"),
         ("pods", "Podcasts"), ("games", "Games")]

# Festive palettes only the year uses: per slide (background, accent, shape 1, shape 2), like ``story.PALETTES``
YEAR_PALETTES = {
    "silvester": {"sets": [
        ("#0b1030", "#ffd166", "#3a2c8f", "#7b2cbf"), ("#150a2e", "#ffe27a", "#ff3cac", "#4a1d96"),
        ("#071a2e", "#ffe9a8", "#0e7490", "#1d4ed8"), ("#1f0a24", "#ffd1f0", "#c026d3", "#581c87"),
        ("#0a1d1a", "#f7e58a", "#0f766e", "#134e4a"), ("#1a1030", "#fff1b8", "#6d28d9", "#be185d")]},
    "konfetti": {"sets": [
        ("#2b0a3d", "#ff5e9c", "#ffd23f", "#3bceac"), ("#0d1b3e", "#ffd23f", "#ee4266", "#3bceac"),
        ("#3a0a2a", "#7cf7ff", "#ff5e9c", "#ffd23f"), ("#10243a", "#ff9ec7", "#3bceac", "#ffd23f"),
        ("#2a1245", "#c4ff3d", "#ff3cac", "#5b2bff"), ("#3d0e1e", "#ffe27a", "#ee4266", "#540d6e")]},
    "goldregen": {"sets": [
        ("#141008", "#ffcb47", "#8a5a00", "#3d2b00"), ("#1b0f08", "#ffd89b", "#a1470f", "#5a2a0a"),
        ("#0f1410", "#f6e27a", "#4d6b2f", "#2a3d18"), ("#180c14", "#ffc2d9", "#9d174d", "#4a0d2e"),
        ("#0c1219", "#ffe08a", "#3b5b7a", "#1c2f44"), ("#1a1208", "#fff0b3", "#b8860b", "#6b4a00")]},
}
YEAR_VARIANTS = {"yintro": 3, "ytotal": 3, "ycurve": 2, "ytop": 2, "yrecords": 2, "ynew": 2, "yphotos": 2, "yra": 2, "yfocus": 2,
                 "youtro": 2}
YEAR_KICKERS = {
    "yintro": ["Dein Jahr", "Das war dein", "Zwölf Monate", "Zurückgespult"],
    "ytotal": ["Das ganze Jahr", "Alles zusammen", "Unterm Strich", "Die große Zahl"],
    "ycurve": ["Dein Jahresverlauf", "Monat für Monat", "Auf und ab", "Die Kurve des Jahres"],
    "ytop": ["Top 5 · {a}", "Hitliste · {a}", "Die Besten · {a}"],
    "yrecords": ["Deine Rekorde", "Bestmarken", "Ganz weit vorn"],
    "ynew": ["Neu entdeckt", "Frisch dabei", "Neues in diesem Jahr"],
    "yphotos": ["Zwölf Momente", "Das Jahr im Bild", "Festgehalten", "Durch die Linse"],
    "yra": ["Achievements", "Trophäenjahr", "Erfolge im Jahr", "Retro-Bilanz"],
    "yfocus": ["Dein Monats-Ranking", "Was wann los war", "Monat für Monat im Fokus"],
    "youtro": ["Dein {y}", "Das war {y}", "{y} in Zahlen"],
}
LABELS = {"yintro": "Jahr · Intro", "ytotal": "Jahr · Gesamt", "ycurve": "Jahr · Verlauf", "ytop": "Jahr · Top 5", "yrecords": "Jahr · Rekorde",
          "ynew": "Jahr · Entdeckungen", "yphotos": "Jahr · Fotos", "yra": "Jahr · Achievements", "yfocus": "Jahr · Monats-Ranking",
          "youtro": "Jahr · Outro"}
LATE_YEAR = ()  # slide types added to the year after its first release go here (own random stream, like ``story.LATE_SLIDES``)


# -- What only a year can show --------------------------------------------------------------------------------------

def _by_month(day_map, year, scale=1.0):
    out = [0.0] * 12
    for day, value in (day_map or {}).items():
        if str(day)[:4] == str(year):
            out[int(day[5:7]) - 1] += value * scale
    return out


def month_rows(data, snaps):
    """Twelve rows ``{month, hours, parts, focus, photos, source}``. A month with a monthly snapshot uses its figures
    (games included); one without them is derived from the year's own daily data (video hours, minutes of music, audiobooks and
    podcasts, photos per month), which cannot hold games."""
    year = int(data["year"])
    jf, mu, ab, pc, ph = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos"))
    video = _by_month(jf.get("days"), year)
    music, books, pods = (_by_month(x.get("day_minutes"), year, 1 / 60) for x in (mu, ab, pc))
    rows = []
    for n in range(1, 13):
        snap = snaps.get(n)
        if snap and snap.get("totals") and not snap.get("empty"):
            t = snap["totals"]
            parts = {"video": t.get("series_hours", 0) + t.get("movie_hours", 0), "music": t.get("music_hours", 0),
                     "books": t.get("audiobook_hours", 0), "pods": t.get("podcast_hours", 0), "games": t.get("games_hours", 0)}
            photos, source = (snap.get("photos") or {}).get("total", 0), "snapshot"
        else:
            parts = {"video": video[n - 1], "music": music[n - 1], "books": books[n - 1], "pods": pods[n - 1], "games": 0}
            photos, source = (ph.get("month_counts") or {}).get(f"{n:02d}", 0), "data"
        hours = round(sum(parts.values()), 1)
        focus = max(parts, key=lambda k: parts[k]) if hours > 0 else ("photos" if photos else None)
        rows.append({"month": n, "hours": hours, "photos": photos, "focus": focus, "source": source,
                     "parts": {k: round(v, 1) for k, v in parts.items()}})
    return rows


def records(data, snaps):
    """The longest couch day, gaming marathon and most-listened week of the year (only those the data can tell)."""
    out = {}
    jf, ga = data.get("jellyfin") or {}, data.get("games") or {}
    if jf.get("best_day"):
        out["binge"] = {k: jf["best_day"][k] for k in ("date", "hours", "titles")}
    cands = [(g["minutes"], n, g) for n, s in snaps.items() if (g := get(s, "games", "games", 0))]
    if cands:
        minutes, n, g = max(cands, key=lambda c: (c[0], -c[1]))
        out["game"] = {"name": g["name"], "hours": round(minutes / 60, 1), "sessions": g.get("sessions") or 0, "month": n}
    elif ga.get("games"):  # without monthly snapshots: the game of the year
        g = ga["games"][0]
        out["game"] = {"name": g["name"], "hours": round(g["minutes"] / 60, 1), "sessions": g.get("sessions") or 0, "month": None}
    if out.get("game") and out["game"]["hours"] <= 0:
        del out["game"]
    weeks = collections.Counter()
    for key in ("music", "audiobooks", "podcasts"):
        for day, minutes in (data.get(key) or {}).get("day_minutes", {}).items():
            d = dt.date.fromisoformat(day)
            weeks[d - dt.timedelta(days=d.weekday())] += minutes
    if weeks:
        monday, minutes = max(weeks.items(), key=lambda kv: (kv[1], kv[0]))
        out["week"] = {"from": monday.isoformat(), "to": (monday + dt.timedelta(days=6)).isoformat(), "hours": round(minutes / 60, 1)}
    return out


def discoveries(data, earlier):
    """Artists heard for the first time (ListenBrainz knows the whole history) and series that are new in the person's top lists
    compared with all earlier snapshots; without earlier snapshots there is no way to tell, so no series are named."""
    known = {s["name"] for h in earlier for s in (h.get("jellyfin") or {}).get("series") or []}
    series = [s["name"] for s in (data.get("jellyfin") or {}).get("series") or [] if s["name"] not in known][:5] if known else []
    return {"artists": [{"name": a["name"], "plays": a["plays"]} for a in (data.get("music") or {}).get("new_artists") or []][:5],
            "series": series}


def facts(data, snaps, earlier):
    """Adds everything derived to the year's data: ``months``, ``records``, ``discoveries`` and ``trip``. ``snaps`` are the
    year's monthly snapshots (``{month number: snapshot}``), ``earlier`` all snapshots before the year (oldest first)."""
    data["months"] = month_rows(data, snaps)
    data["records"] = records(data, snaps)
    data["discoveries"] = discoveries(data, earlier)
    data["trip"] = story.highlights(data, [h for h in earlier if h.get("photos")]).get("trip")
    return data


# -- Design ---------------------------------------------------------------------------------------------------------

def choose_design(data, history, current=None):
    """Design of the year: nothing the previous year used (palette, shape, type, variants, texts). ``history`` are the person's
    earlier annual snapshots (oldest first); ``current`` is the year's own design on a redesign, avoided as well."""
    seed = f"{data['year']}{data.get('seed', '')}year" + (f":{data['design_round']}" if data.get("design_round") else "")
    rnd = random.Random(seed)
    last = ((history[-1].get("design") or {}) if history else {})
    current = current or {}
    old = lambda design, group, key: (design.get(group) or {}).get(key)  # noqa: E731
    pick = lambda options, *avoid: story.pick_with(rnd, options, *avoid)  # noqa: E731
    keys = [k for k in YEAR_VARIANTS if k not in LATE_YEAR]
    design = {
        "palette": pick(list(YEAR_PALETTES), last.get("palette"), current.get("palette")),
        "offset": rnd.randrange(6),
        "shape": pick(story.SHAPES, last.get("shape"), current.get("shape")),
        "type": pick(story.TYPES, last.get("type"), current.get("type")),
        "variants": {k: pick(range(YEAR_VARIANTS[k]), old(last, "variants", k), old(current, "variants", k)) for k in keys},
        "texts": {k: pick(range(len(YEAR_KICKERS[k])), old(last, "texts", k), old(current, "texts", k)) for k in keys},
    }
    late = random.Random(seed + "late")
    for k in LATE_YEAR:
        design["variants"][k] = story.pick_with(late, range(YEAR_VARIANTS[k]), old(last, "variants", k), old(current, "variants", k))
        design["texts"][k] = story.pick_with(late, range(len(YEAR_KICKERS[k])), old(last, "texts", k), old(current, "texts", k))
    return design


# -- Story ----------------------------------------------------------------------------------------------------------

YEAR_CSS = """
.s-yintro .outline{font-size:clamp(4.5rem,27vw,9rem);white-space:nowrap}.s-yintro.v2 .giant{font-size:clamp(2.4rem,12vw,4rem);letter-spacing:-.02em}
.s-yintro .inner{position:static}.s-yintro .inner>*:not(.collage){position:relative;z-index:1}
.ycurve{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:5px;align-items:end;height:min(30vh,240px);height:min(30cqh,240px);margin-top:16px}
.ycurve i{display:block;height:var(--h);min-height:4px;border-radius:5px 5px 2px 2px;background:var(--accent);opacity:.5}
.ycurve i.max{opacity:1}.ycurve i.min{opacity:1;background:transparent;outline:2px dashed var(--accent);outline-offset:-2px}
.slide.on .ycurve i{animation:up 1s .3s cubic-bezier(.2,.8,.2,1) both}@keyframes up{from{height:0}}
.ymon{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:5px;margin-top:6px;font-size:.6rem;font-weight:800;text-align:center;opacity:.8}
.yline{display:block;width:100%;height:auto;margin-top:14px;overflow:visible}
.yline polyline{fill:none;stroke:var(--accent);stroke-width:3;stroke-linejoin:round;stroke-linecap:round}
.yline circle{fill:var(--accent);opacity:.6}.yline circle.max{opacity:1;r:6}.yline circle.min{fill:var(--bg);stroke:var(--accent);stroke-width:3;opacity:1;r:5}
.yline text{fill:#fff;font:800 11px system-ui,sans-serif;text-anchor:middle;opacity:.8}
.ynote{font-size:.78rem;opacity:.75;margin-top:10px;font-style:italic}
.yrec{display:grid;gap:12px;margin-top:14px}
.yrec>div{background:rgba(0,0,0,.28);border-radius:16px;padding:12px 16px;min-width:0}
.yrec span.l{display:block;font-size:.72rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase;opacity:.8}
.yrec b{display:block;font-family:var(--head);font-size:2.2rem;font-weight:900;letter-spacing:-.03em;color:var(--accent);line-height:1.05}
.yrec em{display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden;-webkit-line-clamp:2;font-style:normal;font-size:.9rem;line-height:1.3}
.s-yrecords.v1 .yrec>div{display:grid;grid-template-columns:minmax(0,34%) minmax(0,1fr);column-gap:14px;align-items:center;background:none;border-top:1px solid rgba(255,255,255,.25);border-radius:0;padding:10px 0}
.s-yrecords.v1 .yrec b{grid-row:span 2;font-size:1.9rem}
.ymos{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:14px}
.ymos figure{position:relative}.ymos img{display:block;width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px}
.ymos figcaption{position:absolute;left:5px;bottom:4px;font-size:.62rem;font-weight:800;text-shadow:0 1px 3px #000}
.s-yphotos.v1 .ymos{gap:10px 8px;padding:6px 0}
.s-yphotos.v1 .ymos figure{background:#fafafa;padding:4px 4px 16px;border-radius:3px;box-shadow:0 8px 18px rgba(0,0,0,.45);transform:rotate(var(--r))}
.s-yphotos.v1 .ymos figcaption{left:0;right:0;bottom:3px;text-align:center;color:#333;text-shadow:none}
.yhard{display:grid;grid-template-columns:56px minmax(0,1fr);gap:12px;align-items:center;margin-top:8px}
.yhard img,.yhard .ph{width:56px;height:56px;border-radius:12px;object-fit:cover;box-shadow:0 8px 20px rgba(0,0,0,.4)}
.yhard b,.yhard span{display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden;-webkit-line-clamp:2}.yhard span{font-size:.85rem;opacity:.85}
.yrank{list-style:none;display:grid;gap:6px;margin-top:12px}
.yrank li{display:grid;grid-template-columns:6.4rem minmax(0,1fr);column-gap:10px;align-items:center;font-size:.85rem;font-weight:700}
.yrank i{display:block;height:12px;border-radius:99px;background:var(--accent);width:var(--w);min-width:6px;grid-column:2}
.yrank span{display:block;grid-column:2;font-size:.74rem;opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.yrank b{grid-row:span 2;font-weight:800}
.slide.on .yrank i{animation:grow 1s .3s cubic-bezier(.2,.8,.2,1) both}
.yfoc{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:14px}
.yfoc div{border-radius:10px;padding:7px 8px;background:color-mix(in srgb,var(--accent) calc(var(--a) * 70%),rgba(255,255,255,.08));min-width:0;color:#fff}
.yfoc b{display:block;font-size:.8rem}.yfoc span{display:block;font-size:.66rem;font-weight:700;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.yfoc div.top{outline:2px solid #fff;outline-offset:1px}
@container stage (max-height:760px){.ycurve{margin-top:10px}.yrec{gap:8px}.yrec>div{padding:8px 14px}.ymos{margin-top:8px;gap:4px}.yrank{gap:4px;margin-top:8px}.yfoc{gap:4px;margin-top:8px}}
"""


def approx_note(ga):
    """The honest note under games figures that are partly estimated."""
    if not ga.get("approx"):
        return ""
    since = ga.get("approx_since")
    month = MONTHS[int(since[5:7]) - 1] if since else None
    return f'<p class="ynote">Spielzeit {"vor " + month + " " if month else ""}geschätzt.</p>'


def _num(x, digits=1):
    return de_num(x, digits)


def _list(rows, cls="list thumbs"):
    """``rows``: (picture, title, meta)."""
    return f'<ol class="{cls}">' + "".join(
        f'<li>{story.img(i, "th", t)}<b>{e(t)}</b><span>{m}</span></li>' for i, t, m in rows) + "</ol>"


def top_areas(data):
    """``[(key, label, rows, foot)]`` for the areas with data; rows are (picture, title, meta) of the top five."""
    jf, mu, ab, pc, ga = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "games"))
    out = []
    if jf.get("series"):
        out.append(("series", "Serien", [(x.get("poster"), x["name"], f'{x["episodes"]} Folgen · {_num(x["hours"])} h') for x in jf["series"][:5]],
                    f'{jf["episodes"]} Folgen aus {jf["series_count"]} Serien · {_num(jf["series_hours"])} Stunden'))
    if jf.get("movies"):
        out.append(("movies", "Filme", [(x.get("poster"), x["name"] + story.year(x), f'{_num(x["hours"])} h' if x.get("hours") else "") for x in jf["movies"][:5]],
                    f'{jf["movies_count"]} Filme · {_num(jf["movie_hours"])} Stunden'))
    if mu.get("top_artists"):
        out.append(("artists", "Künstler", [(x.get("cover"), x["name"], f'{x["plays"]}×') for x in mu["top_artists"][:5]],
                    f'{de_num(mu["plays"])} Wiedergaben von {mu["artists_count"]} Künstlern'))
    if mu.get("top_tracks"):
        out.append(("songs", "Songs", [(x.get("cover"), x["name"], f'{e(x["artist"])} · {x["plays"]}×') for x in mu["top_tracks"][:5]],
                    f'{de_num(mu.get("tracks_count") or mu["plays"])} verschiedene Titel · {de_num(mu["minutes"])} Minuten'))
    if ab.get("books"):
        out.append(("books", "Hörbücher", [(x.get("cover"), x["title"], f'{e(x.get("author") or "")}{" · " if x.get("author") else ""}'
                                            f'{_num(x["hours"])} h{" · ✓ fertig" if x.get("finished") else ""}') for x in ab["books"][:5]],
                    f'{_num(ab["hours"])} Stunden, {ab["finished"]} zu Ende gehört'))
    if pc.get("podcasts"):
        out.append(("pods", "Podcasts", [(x.get("cover"), x["title"], f'{x["episodes"]} Folgen · {_num(x["hours"])} h') for x in pc["podcasts"][:5]],
                    f'{_num(pc["hours"])} Stunden mit {pc["episodes"]} Folgen'))
    if ga.get("games"):
        out.append(("games", "Games", [(x.get("cover"), x["name"], story.game_meta(x)) for x in ga["games"][:5]],
                    f'{ga["games_count"]} Spiele · {_num(ga["hours"])} Stunden'))
    return out


def render(data):
    d = data["design"]
    y = int(data["year"])
    jf, mu, ab, pc, ph, t = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "totals"))
    ga, ra = data.get("games") or {}, data.get("retroachievements") or {}
    months, rec, disc = data.get("months") or [], data.get("records") or {}, data.get("discoveries") or {}
    V, T = d["variants"], d["texts"]
    kick = lambda k, **f: f'<p class="kicker">{e(YEAR_KICKERS[k][T.get(k, 0)].format(y=y, **f))}</p>'  # noqa: E731
    count, delta_hours = story.count, t.get("media_hours", 0)
    content = []  # (key, html, backdrop, area)

    def add(key, inner, backdrop=None, area=None):
        content.append((key, inner, backdrop, area))

    # 1. Intro
    pairs = [(s.get("poster"), s["name"]) for s in jf.get("series") or []] + [(x.get("poster"), x["name"]) for x in jf.get("movies") or []] \
        + [(x.get("cover"), x["name"]) for x in mu.get("top_tracks") or []] + [(x.get("cover"), x["title"]) for x in ab.get("books") or []] \
        + [(x.get("cover"), x["name"]) for x in ga.get("games") or []]
    images = [p for p in pairs if p[0]][:6]
    if images:
        images += [p for p in pairs if not p[0]][:max(0, 6 - len(images))]
    bits = [f"{de_num(delta_hours)} Stunden Medien"] if delta_hours else []
    if ph.get("total"):
        bits.append(f"{de_num(ph['total'])} Fotos und Videos")
    if ra.get("total"):
        bits.append(f"{de_num(ra['total'])} Erfolge")
    if bits:
        lead = e((", ".join(bits[:-1]) + " und " + bits[-1] if len(bits) > 1 else bits[0]) + ": zwölf Monate in Zahlen.")
    else:
        lead = "Was du geschaut, gehört und festgehalten hast."
    if V["yintro"] == 1 and images:
        collage = "".join(f'<div class="cl" style="--r:{(k * 37) % 24 - 12}deg">{story.img(i, "", n)}</div>' for k, (i, n) in enumerate(images))
        add("yintro", f'<div class="collage">{collage}</div>{kick("yintro")}<h1 class="giant">{y}</h1><p class="lead">{lead}</p>')
    elif V["yintro"] == 2:
        add("yintro", f'<p class="outline">{y}</p>{kick("yintro")}<h1 class="giant">Rückblick</h1><p class="lead">{lead}</p>')
    else:
        add("yintro", f'{kick("yintro")}<h1 class="giant">{y}</h1><p class="lead">{lead}</p>')

    # 2. Total
    parts = [(n, t.get(k, 0)) for n, k in (("Serien", "series_hours"), ("Filme", "movie_hours"), ("Musik", "music_hours"),
                                            ("Hörbücher", "audiobook_hours"), ("Podcasts", "podcast_hours"), ("Games", "games_hours"))]
    parts = [(n, v) for n, v in parts if v]
    mx, tot = max([v for _, v in parts] + [1]), delta_hours or 1
    note = approx_note(ga) if t.get("games_hours") else ""
    big = f'<p class="big">{count(round(delta_hours))}<small> Stunden</small></p>'
    days = f"Medien in einem Jahr, das sind <b>{de_num(delta_hours / 24, 1)} Tage</b> am Stück."
    if V["ytotal"] == 1:
        seg = "".join(f'<span style="--w:{v / tot * 100:.1f}%;--o:{1 - k * .16:.2f}"></span>' for k, (n, v) in enumerate(parts))
        legend = "".join(f'<li style="--o:{1 - k * .16:.2f}"><i></i>{n}<b>{de_num(v, 1)} h</b></li>' for k, (n, v) in enumerate(parts))
        body = f'<div class="stack">{seg}</div><ul class="legend">{legend}</ul>'
    elif V["ytotal"] == 2:
        body = "".join(f'<p class="mini"><b>{de_num(v, 1)} h</b> {n}</p>' for n, v in parts)
        big = f'<p class="big">{count(round(delta_hours / 24, 1), 1)}<small> Tage am Stück</small></p>'
        days = f"Das sind <b>{de_num(delta_hours)} Stunden</b> Serien, Filme, Musik und mehr."
    else:
        body = '<div class="bars">' + "".join(
            f'<div class="bar"><span class="bl">{n}</span><span class="bt" style="--w:{v / mx * 100:.0f}%"></span>'
            f'<span class="bv">{de_num(v, 1)} h</span></div>' for n, v in parts) + "</div>"
    add("ytotal", f'{kick("ytotal")}{big}<p class="lead">{days}</p>{body}{note}')

    # 3. Year curve
    live = [m for m in months if m["hours"] > 0]
    if len(live) >= 2:
        hi, lo = max(live, key=lambda m: (m["hours"], -m["month"])), min(live, key=lambda m: (m["hours"], m["month"]))
        top = hi["hours"] or 1
        names = "".join(f"<span>{MONTHS_SHORT[m['month'] - 1][0]}</span>" for m in months)
        if V["ycurve"] == 1:
            pts = [(18 + k * 29.5, 96 - m["hours"] / top * 84, m) for k, m in enumerate(months)]
            cls = lambda m: " max" if m is hi else " min" if m is lo else ""  # noqa: E731
            svg = ('<svg class="yline" viewBox="0 0 360 122" role="img" aria-label="Stunden pro Monat"><polyline points="'
                   + " ".join(f"{x:.1f},{yy:.1f}" for x, yy, _ in pts) + '"/>'
                   + "".join(f'<circle class="{cls(m).strip()}" cx="{x:.1f}" cy="{yy:.1f}" r="4"/>' for x, yy, m in pts)
                   + "".join(f'<text x="{x:.1f}" y="118">{MONTHS_SHORT[m["month"] - 1][0]}</text>' for x, _, m in pts) + "</svg>")
            chart = svg
        else:
            chart = ('<div class="ycurve">' + "".join(
                f'<i class="{"max" if m is hi else "min" if m is lo else ""}" style="--h:{m["hours"] / top * 100:.0f}%" title="{MONTHS[m["month"] - 1]}"></i>'
                for m in months) + f'</div><div class="ymon">{names}</div>')
        add("ycurve", f'{kick("ycurve")}<h2 class="title">{MONTHS[hi["month"] - 1]} war dein stärkster Monat</h2>'
            f'<p class="lead"><b>{de_num(hi["hours"])} Stunden</b>. Am ruhigsten: {MONTHS[lo["month"] - 1]} mit {de_num(lo["hours"])} Stunden.</p>{chart}')

    # 4. Top 5 per area
    for key, label, rows, foot in top_areas(data):
        if not rows:
            continue
        head = kick("ytop", a=label)
        foot_html = f'<p class="foot">{e(foot)}</p>' + (approx_note(ga) if key == "games" else "")
        first = rows[0]
        if V["ytop"] == 1 and first[0]:
            add("ytop", f'{head}{story.img(first[0], "poster square")}<h2 class="title">{e(first[1])}</h2><p class="lead">{first[2]}</p>'
                f'{_list(rows[1:], "list thumbs")}{foot_html}', first[0], key)
        else:
            add("ytop", f'{head}{_list(rows)}{foot_html}', None, key)

    # 5. Records
    cards = []
    if rec.get("binge"):
        b = rec["binge"]
        cards.append(("Längster Couch-Tag", f'{_num(b["hours"])} h', f'{de_date(dt.date.fromisoformat(b["date"]))} · ' + e(" und ".join(b["titles"]))))
    if rec.get("game"):
        g = rec["game"]
        when = f' im {MONTHS[g["month"] - 1]}' if g.get("month") else " im Jahr"
        starts = f' · {g["sessions"]} {"Start" if g["sessions"] == 1 else "Starts"}' if g.get("sessions") else ""
        cards.append(("Längster Gaming-Marathon", f'{_num(g["hours"])} h', f'{e(g["name"])}{when}{starts}'))
    if rec.get("week"):
        w = rec["week"]
        a, z = dt.date.fromisoformat(w["from"]), dt.date.fromisoformat(w["to"])
        span = f'{a.day}. {MONTHS[a.month - 1]} bis {z.day}. {MONTHS[z.month - 1]}' if a.month != z.month else f'{a.day}. bis {z.day}. {MONTHS[z.month - 1]}'
        cards.append(("Meistgehörte Woche", f'{_num(w["hours"])} h', f'{span} · Musik, Hörbücher und Podcasts'))
    if cards:
        add("yrecords", f'{kick("yrecords")}<div class="yrec">' + "".join(
            f'<div><span class="l">{l}</span><b>{big_}</b><em>{small}</em></div>' for l, big_, small in cards) + "</div>")

    # 6. Discoveries
    if disc.get("artists") or disc.get("series"):
        blocks = []
        if disc.get("artists"):
            blocks.append(("Künstler", "".join(f'<li><b>{e(a["name"])}</b><span>{a["plays"]}× gehört</span></li>' for a in disc["artists"])))
        if disc.get("series"):
            blocks.append(("Serien", "".join(f'<li><b>{e(s)}</b><span>neu in deiner Hitliste</span></li>' for s in disc["series"])))
        first = (disc["artists"][0]["name"] if disc.get("artists") else disc["series"][0])
        lists = "".join(f'<p class="sub">{n}</p><ol class="list plain">{li}</ol>' for n, li in blocks)
        if V["ynew"] == 1:
            add("ynew", f'{kick("ynew")}<h2 class="title">{e(first)}</h2><p class="lead">Zum ersten Mal in diesem Jahr dabei.</p>{lists}')
        else:
            add("ynew", f'{kick("ynew")}<p class="lead">Das ist {y} zum ersten Mal in deinem Leben aufgetaucht.</p>{lists}')

    # 7. Photos
    shots = (ph.get("highlights") or [])[:12]
    if ph.get("total") and shots:
        places = ", ".join(e(p["name"]) for p in (ph.get("places") or [])[:4])
        trip = data.get("trip")
        tp = (f'<p class="foot">Am weitesten weg von {e(trip["home"])}: <b>{e(trip["places"][0]["name"])}</b> ({de_num(trip["places"][0]["km"])} km).</p>'
              if trip else (f"<p class=foot>Unterwegs in {places}</p>" if places else ""))
        figs = "".join(f'<figure style="--r:{((k * 53) % 9) - 4}deg"><img src="{e(x["img"])}" alt="" loading="lazy">'
                       f'<figcaption>{MONTHS_SHORT[int(x["date"][5:7]) - 1]}</figcaption></figure>' for k, x in enumerate(shots))
        add("yphotos", f'{kick("yphotos")}<p class="big">{count(ph["total"])}<small> Fotos &amp; Videos</small></p>'
            f'<p class="lead">{ph["photos"]} Fotos, {ph["videos"]} Videos an {ph["days_with_photos"]} Tagen.</p><div class="ymos">{figs}</div>{tp}')

    # 8. RetroAchievements
    if ra.get("total"):
        n, hc = ra["total"], ra.get("hardcore", 0)
        big_ = f'<p class="big">{count(n)}<small> {"Erfolg" if n == 1 else "Erfolge"}</small></p>'
        pts = f'<p class="lead">{story.ach_lead(ra)}</p>'
        hard = (ra.get("rarest") or [None])[0]
        hard_html = (f'<p class="sub">{"Dein schwerster Erfolg" if hard and hard.get("points") else "Ein Erfolg des Jahres"}</p>'
                     f'<div class="yhard">{story.img(hard.get("badge"), "", hard["title"])}'
                     f'<div><b>{e(hard["title"])}</b><span>{e(hard["game"])} · {e(story.ach_meta(hard))}</span></div></div>' if hard else "")
        names = {"mastered": "gemeistert", "completed": "abgeschlossen", "beaten": "durchgespielt", "beaten_soft": "durchgespielt"}
        aw = ra.get("awards") or []
        aw_html = ("<p class=\"sub\">Geschafft</p>" + _list([(a.get("icon"), a["title"], f'{e(a.get("console") or "")} · {names.get(a["kind"], "geschafft")}')
                                                             for a in aw[:3]])) if aw else ""
        rg = [(x.get("icon"), x["name"], f'{e(x.get("console") or "")} · {x["count"]} {"Erfolg" if x["count"] == 1 else "Erfolge"}')
              for x in (ra.get("games") or [])[:3]]
        if V["yra"] == 1 and rg:
            add("yra", f'{kick("yra")}{big_}{pts}{_list(rg)}{hard_html}')
        else:
            add("yra", f'{kick("yra")}{big_}{pts}{hard_html}{aw_html}')

    # 9. Month ranking
    ranked = sorted([m for m in months if m["hours"] > 0 or m["photos"]], key=lambda m: (-m["hours"], m["month"]))
    if len(ranked) >= 2:
        peak = ranked[0]["hours"] or 1
        label = lambda m: (FOCUS.get(m["focus"], "") if m["focus"] else "")  # noqa: E731
        if V["yfocus"] == 1:
            cells = "".join(f'<div class="{"top" if m is ranked[0] else ""}" style="--a:{m["hours"] / peak:.2f}"><b>{MONTHS_SHORT[m["month"] - 1]}</b>'
                            f'<span>{e(label(m)) or "–"}</span><span>{de_num(m["hours"])} h</span></div>'
                            for m in sorted(months, key=lambda m: m["month"]))
            add("yfocus", f'{kick("yfocus")}<h2 class="title">Was wann im Fokus stand</h2><div class="yfoc">{cells}</div>')
        else:
            rows = "".join(f'<li><b>{MONTHS[m["month"] - 1]}</b><i style="--w:{m["hours"] / peak * 100:.0f}%"></i>'
                           f'<span>{e(label(m))}{" · " if label(m) else ""}{de_num(m["hours"])} h</span></li>' for m in ranked)
            add("yfocus", f'{kick("yfocus")}<h2 class="title">{MONTHS[ranked[0]["month"] - 1]} vorn</h2><ol class="yrank">{rows}</ol>')

    # 10. Outro: the year as a receipt
    facts_ = [("Stunden Medien", de_num(delta_hours)), ("Serienfolgen", jf.get("episodes", 0)), ("Filme", jf.get("movies_count", 0)),
              ("Musik-Minuten", de_num(mu.get("minutes", 0))), ("Hörbuch-Stunden", de_num(ab.get("hours", 0), 1)),
              ("Fotos & Videos", de_num(ph.get("total", 0)))]
    if ga.get("games"):
        facts_ += [("Spiele", ga["games_count"]), ("Stunden gespielt", de_num(ga["hours"], 1))]
    if ra.get("total"):
        facts_ += [("Erfolge", de_num(ra["total"]))]
    tops = [("Serie", get(jf, "series", 0, "name")), ("Künstler", get(mu, "top_artists", 0, "name")), ("Hörbuch", get(ab, "books", 0, "title")),
            ("Podcast", get(pc, "podcasts", 0, "title")), ("Spiel", get(ga, "games", 0, "name"))]
    link = '<p class="foot"><a href="../">Alle Rückblicke</a></p>'
    if V["youtro"] == 0:
        rows = "".join(f'<li><span>{k}</span><b>{v}</b></li>' for k, v in facts_) + '<li class="sep"></li>' + \
            "".join(f'<li><span>Top-{k}</span><b>{e(v)}</b></li>' for k, v in tops if v)
        add("youtro", f'<div class="receipt"><p class="rh">JAHRESRÜCKBLICK · {y}</p><ul>{rows}</ul><p class="rf">Danke für dein Jahr</p></div>{approx_note(ga)}{link}')
    else:
        add("youtro", f'{kick("youtro")}<div class="card"><div class="facts">{"".join(f"<div><b>{v}</b><span>{k}</span></div>" for k, v in facts_)}</div>'
            f'<ul class="tops">{"".join(f"<li><span>{k}</span><b>{e(v)}</b></li>" for k, v in tops if v)}</ul></div>{approx_note(ga)}{link}')

    # Colours, music and assembly (like the month: every slide its own song, the top artist's clip on its own slide)
    sets = YEAR_PALETTES[d["palette"]]["sets"] if d["palette"] in YEAR_PALETTES else story.PALETTES[d["palette"]]["sets"]
    clips = data.get("soundtrack") or []
    others = iter(clips[1:] or clips)
    html_slides = []
    for k, (key, inner, backdrop, area) in enumerate(content):
        bg, ac, b1, b2 = sets[(k + d["offset"]) % len(sets)]
        attr = f' style="--bg:{bg};--accent:{ac};--blob:{b1};--blob2:{b2}"'
        label = ""
        c = clips[0] if clips and area == "artists" else next(others, None)
        if c:
            attr += f' data-clip="{e(c["src"])}"'
            label = f'<p class="np">♪ {e(c["title"])} · {e(c["artist"])}</p>'
        bd = f'<div class="bd" style="background-image:url(\'{e(backdrop)}\')"></div>' if backdrop else ""
        extra = f" a-{area}" if area else ""
        html_slides.append(f'<section class="slide s-{key} v{V.get(key, 0)}{extra}"{attr}>{bd}<div class="inner">{inner}</div>{label}</section>')
    bars = "".join("<i><b></b></i>" for _ in content)
    sound = '<button class="snd" aria-label="Ton an/aus">🔊</button>' if clips else ""
    return (page().replace("{{FIT}}", story.FIT).replace("{{TITLE}}", f"Jahresrückblick {y}").replace("{{BARS}}", bars)
            .replace("{{SLIDES}}", "\n".join(html_slides)).replace("{{SOUND}}", sound)
            .replace("{{BODY}}", f'shape-{d["shape"]} type-{d["type"]}'))


def page():
    """The story's page template with the year's few extra styles."""
    return story.PAGE.replace("</style>", YEAR_CSS + "</style>", 1)
