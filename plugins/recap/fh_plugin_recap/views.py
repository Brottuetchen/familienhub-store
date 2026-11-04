"""The month index page and the Obsidian Markdown of a month (same content as the original standalone script)."""

from __future__ import annotations

import html
from typing import Any, Dict, List

from . import store
from .story import MONTHS, de_num
from .year import FOCUS as FOCUS_NAMES

INDEX_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rückblicke</title>
<style>body{margin:0;background:#14051f;color:#fff;font-family:system-ui,sans-serif;padding:48px 16px}
main{max-width:520px;margin:0 auto}h1{font-size:2.6rem;font-weight:900;letter-spacing:-.04em;margin:0 0 24px}
.card{position:relative;margin-bottom:12px}
.card>a.go{display:block;padding:18px 20px;border-radius:16px;background:linear-gradient(120deg,#5b2bff,#ff3cac);color:#fff;text-decoration:none}
.card>a.go b{display:block;font-size:1.4rem}.card>a.go span{opacity:.9;font-size:.9rem}
.card>a.redo{position:absolute;right:14px;top:50%;transform:translateY(-50%);padding:8px 12px;border-radius:999px;
background:rgba(0,0,0,.25);color:#fff;text-decoration:none;font-size:.8rem}p{opacity:.85;line-height:1.5}
.card.year>a.go{background:linear-gradient(120deg,#b8860b,#ff3cac 55%,#5b2bff)}
.pv{display:inline-block;margin-top:14px;color:#fff;opacity:.8;font-size:.9rem}</style></head>
<body><main><h1>Rückblicke</h1>{{CARDS}}{{PREVIEW}}</main></body></html>"""

EMPTY_TEXT = ("Noch kein Rückblick da. Sobald du unter „Mehr → Meine Verbindungen“ mindestens einen Dienst "
              "verbunden hast, entsteht am Monatsanfang dein erster Rückblick.")


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def month_label(month: str) -> str:
    if store.is_year(month):
        return year_label(month)
    year, number = month.split("-")
    return f"{MONTHS[int(number) - 1]} {int(year)}"


PREVIEW_LINK = '<a class="pv" href="preview/">🎨 Design-Vorschau</a>'  # admins only


def year_label(year: str) -> str:
    return f"Jahresrückblick {int(year)}"


def render_index(user_id: int, admin: bool = False) -> str:
    cards: List[str] = []
    for year in store.list_years(user_id):  # the annual recaps first, each as an entry of its own
        data = store.read_snapshot(user_id, year) or {}
        totals = data.get("totals") or {}
        photos = (data.get("photos") or {}).get("total") or 0
        cards.append(f'<div class="card year"><a class="go" href="{year}/"><b>{_e(year_label(year))}</b>'
                     f'<span>{de_num(totals.get("media_hours") or 0)} h Medien · {_e(photos)} Fotos</span></a>'
                     f'<a class="redo" href="{year}/neu" onclick="return confirm(\'{_e(year_label(year))} neu gestalten? '
                     f'Das bisherige Design wird ersetzt.\')">✨ Neu gestalten</a></div>')
    for month in store.list_months(user_id):
        data = store.read_snapshot(user_id, month) or {}
        totals = data.get("totals") or {}
        photos = (data.get("photos") or {}).get("total") or 0
        cards.append(f'<div class="card"><a class="go" href="{month}/"><b>{_e(month_label(month))}</b>'
                     f'<span>{de_num(totals.get("media_hours") or 0)} h Medien · {_e(photos)} Fotos</span></a>'
                     f'<a class="redo" href="{month}/neu" onclick="return confirm(\'{_e(month_label(month))} neu gestalten? '
                     f'Das bisherige Design wird ersetzt.\')">✨ Neu gestalten</a></div>')
    return INDEX_PAGE.replace("{{CARDS}}", "".join(cards) or f"<p>{_e(EMPTY_TEXT)}</p>").replace(
        "{{PREVIEW}}", PREVIEW_LINK if admin else "")


REDESIGN_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Neu gestalten</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#14051f;color:#fff;font-family:system-ui,sans-serif;padding:16px;text-align:center}
.spin{width:56px;height:56px;margin:0 auto 24px;border-radius:50%;border:5px solid rgba(255,255,255,.2);border-top-color:#ff3cac;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}a{color:#fff}p{opacity:.85;line-height:1.5}</style></head>
<body><main>{{BODY}}</main>{{SCRIPT}}</body></html>"""

REDESIGN_TEXT = {
    "started": "{label} bekommt ein neues Design … das dauert etwa eine Minute.",
    "running": "{label} wird gerade neu gebaut … gleich geht es weiter.",
    "busy": "Gerade entsteht ein anderer Rückblick. Versuch es in ein, zwei Minuten noch einmal.",
    "no_sources": "Für einen Neubau ist unter „Mehr → Meine Verbindungen“ kein Dienst mehr verbunden.",
}

# Asks for the build state every two seconds and opens the month's story once it is done
REDESIGN_POLL = """<script>(function(){const t=setInterval(()=>fetch('status',{cache:'no-store'}).then(r=>r.json())
.then(d=>{if(!d.running){clearInterval(t);location.replace('./')}}).catch(()=>{}),2000)})()</script>"""


def render_redesign(month: str, status: str) -> str:
    waiting = status in ("started", "running")
    text = _e(REDESIGN_TEXT.get(status, REDESIGN_TEXT["busy"]).format(label=month_label(month)))
    body = (f'<div class="spin"></div><p>{text}</p>' if waiting
            else f'<p>{text}</p><p><a href="../">Zurück zu den Rückblicken</a></p>')
    return REDESIGN_PAGE.replace("{{BODY}}", body).replace("{{SCRIPT}}", REDESIGN_POLL if waiting else "")


def render_obsidian_year(data: Dict[str, Any], url: str) -> str:
    """The Obsidian note of a year: the same kind of sections as a month, plus the year's own (months, records)."""
    year = int(data["year"])
    jf, mu, ab, pc, ph, ga, ra, t = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "games",
                                                                 "retroachievements", "totals"))
    lines = ["---", "type: jahresrueckblick", "agent: ignore", f"jahr: {year}", "tags: [rückblick, jahresrückblick]",
             f"medien_stunden: {t.get('media_hours')}", f"serienfolgen: {jf.get('episodes', 0)}", f"filme: {jf.get('movies_count', 0)}",
             f"musik_minuten: {mu.get('minutes', 0)}", f"hoerbuch_stunden: {ab.get('hours', 0)}", f"podcast_stunden: {pc.get('hours', 0)}",
             f"fotos: {ph.get('total', 0)}", *([f"spiele_stunden: {ga['hours']}"] if ga.get("games") else []),
             *([f"erfolge: {ra['total']}"] if ra.get("total") else []), "---",
             f"# Jahresrückblick {year}", "", f"**[▶ Jahresrückblick als Story ansehen]({url})**", "",
             f"Insgesamt **{de_num(t.get('media_hours', 0))} Stunden** Medien.", ""]
    months = [m for m in data.get("months") or [] if m["hours"] or m["photos"]]
    if months:
        lines += ["## Monate", *[f"{i}. **{MONTHS[m['month'] - 1]}** – {de_num(m['hours'])} h"
                                 + (f", Schwerpunkt {FOCUS_NAMES[m['focus']]}" if m.get("focus") else "")
                                 for i, m in enumerate(sorted(months, key=lambda m: (-m["hours"], m["month"])), 1)], ""]
    rec = data.get("records") or {}
    if rec:
        lines.append("## Rekorde")
        if rec.get("binge"):
            lines.append(f"- Längster Couch-Tag: {rec['binge']['date']}, {de_num(rec['binge']['hours'], 1)} h ({', '.join(rec['binge']['titles'])})")
        if rec.get("game"):
            lines.append(f"- Längster Gaming-Marathon: {rec['game']['name']}, {de_num(rec['game']['hours'], 1)} h")
        if rec.get("week"):
            lines.append(f"- Meistgehörte Woche: {rec['week']['from']} bis {rec['week']['to']}, {de_num(rec['week']['hours'], 1)} h")
        lines.append("")
    if jf.get("series"):
        lines += ["## Serien & Filme", f"{jf['episodes']} Folgen ({de_num(jf['series_hours'], 1)} h), {jf['movies_count']} Filme ({de_num(jf['movie_hours'], 1)} h).", ""]
        lines += [f"{i}. **{s['name']}** – {s['episodes']} Folgen, {de_num(s['hours'], 1)} h" for i, s in enumerate(jf["series"][:5], 1)]
        if jf.get("movies"):
            lines += ["", "Filme: " + ", ".join(x["name"] for x in jf["movies"][:5])]
        lines.append("")
    if mu.get("plays"):
        lines += ["## Musik", f"{de_num(mu['plays'])} Wiedergaben, {de_num(mu['minutes'])} Minuten, {mu['artists_count']} Künstler.", ""]
        lines += [f"{i}. {x['name']} ({x['plays']}×)" for i, x in enumerate(mu["top_artists"], 1)]
        lines += ["", "Top-Songs:", *[f"{i}. {x['name']} – {x['artist']} ({x['plays']}×)" for i, x in enumerate(mu["top_tracks"], 1)], ""]
    if ab.get("books"):
        lines += ["## Hörbücher", f"{de_num(ab['hours'], 1)} Stunden, {ab['finished']} beendet.", ""]
        lines += [f"- {x['title']} – {x['author']} ({de_num(x['hours'], 1)} h){' ✓' if x['finished'] else ''}" for x in ab["books"]]
        lines.append("")
    if pc.get("podcasts"):
        lines += ["## Podcasts", f"{de_num(pc['hours'], 1)} Stunden, {pc['episodes']} Folgen.", ""]
        lines += [f"- {x['title']} ({x['episodes']} Folgen, {de_num(x['hours'], 1)} h)" for x in pc["podcasts"]]
        lines.append("")
    if ph.get("total"):
        lines += ["## Fotos", f"{ph['photos']} Fotos und {ph['videos']} Videos an {ph['days_with_photos']} Tagen."]
        if ph.get("places"):
            lines.append("Orte: " + ", ".join(f"{p['name']} ({p['count']})" for p in ph["places"]))
        lines.append("")
    if ga.get("games"):
        lines += ["## Games", f"{de_num(ga['hours'], 1)} Stunden, {ga['games_count']} Spiele auf {ga['systems_count']} Systemen."
                  + (" Spielzeit vor " + MONTHS[int(ga['approx_since'][5:7]) - 1] + " geschätzt." if ga.get("approx") and ga.get("approx_since")
                     else " Spielzeit geschätzt." if ga.get("approx") else ""), ""]
        lines += [f"{i}. **{x['name']}** ({x['system']}) – {de_num(x['minutes'] / 60, 1)} h" for i, x in enumerate(ga["games"][:5], 1)]
        lines.append("")
    if ra.get("total"):
        lines += ["## Achievements", f"{ra['total']} Erfolge, {de_num(ra['points'])} Punkte in {ra['games_count']} Spielen.", ""]
        if ra.get("rarest"):
            lines += ["Schwerster Erfolg: " + f"{ra['rarest'][0]['title']} ({ra['rarest'][0]['game']})"]
        if ra.get("awards"):
            lines += ["", "Geschafft: " + ", ".join(a["title"] for a in ra["awards"])]
        lines.append("")
    lines += ["## Meine Notizen", "", ""]
    return "\n".join(lines)


def render_obsidian(data: Dict[str, Any], url: str) -> str:
    year, month = map(int, data["month"].split("-"))
    mname = MONTHS[month - 1]
    jf, mu, ab, pc, ph, t = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "totals"))
    lines = ["---", "type: rueckblick", "agent: ignore", f"monat: {data['month']}", "tags: [rückblick]",
             f"medien_stunden: {t.get('media_hours')}", f"serienfolgen: {jf.get('episodes', 0)}",
             f"filme: {jf.get('movies_count', 0)}", f"musik_minuten: {mu.get('minutes', 0)}",
             f"hoerbuch_stunden: {ab.get('hours', 0)}", f"podcast_stunden: {pc.get('hours', 0)}",
             f"fotos: {ph.get('total', 0)}", *([f"spiele_stunden: {data['games']['hours']}"] if (data.get("games") or {}).get("games") else []),
             *([f"erfolge: {data['retroachievements']['total']}"] if (data.get("retroachievements") or {}).get("total") else []),
             "---",
             f"# Rückblick {mname} {year}", "", f"**[▶ Rückblick als Story ansehen]({url})**", "",
             f"Insgesamt **{de_num(t.get('media_hours', 0))} Stunden** Medien.", ""]
    if jf.get("series"):
        lines += ["## Serien & Filme", f"{jf['episodes']} Folgen ({de_num(jf['series_hours'], 1)} h), "
                  f"{jf['movies_count']} Filme ({de_num(jf['movie_hours'], 1)} h).", ""]
        lines += [f"{i}. **{s['name']}** – {s['episodes']} Folgen, {de_num(s['hours'], 1)} h"
                  for i, s in enumerate(jf["series"][:5], 1)]
        if jf.get("movies"):
            lines += ["", "Filme: " + ", ".join(x["name"] for x in jf["movies"])]
        lines.append("")
    if mu.get("plays"):
        lines += ["## Musik", f"{mu['plays']} Wiedergaben, {de_num(mu['minutes'])} Minuten, {mu['artists_count']} Künstler.", ""]
        lines += [f"{i}. {x['name']} – {x['artist']} ({x['plays']}×)" for i, x in enumerate(mu["top_tracks"], 1)]
        lines.append("")
    if ab.get("books"):
        lines += ["## Hörbücher", f"{de_num(ab['hours'], 1)} Stunden, {ab['finished']} beendet.", ""]
        lines += [f"- {x['title']} – {x['author']} ({de_num(x['hours'], 1)} h){' ✓' if x['finished'] else ''}" for x in ab["books"]]
        lines.append("")
    if pc.get("podcasts"):
        lines += ["## Podcasts", f"{de_num(pc['hours'], 1)} Stunden, {pc['episodes']} Folgen.", ""]
        lines += [f"- {x['title']} ({x['episodes']} Folgen, {de_num(x['hours'], 1)} h)" for x in pc["podcasts"]]
        lines.append("")
    if ph.get("total"):
        lines += ["## Fotos", f"{ph['photos']} Fotos und {ph['videos']} Videos an {ph['days_with_photos']} Tagen."]
        if ph.get("places"):
            lines.append("Orte: " + ", ".join(f"{p['name']} ({p['count']})" for p in ph["places"]))
        lines.append("")
    ga = data.get("games") or {}
    if ga.get("games"):
        lines += ["## Games", f"{de_num(ga['hours'], 1)} Stunden, {ga['sessions']} Starts, {ga['games_count']} Spiele auf "
                  f"{ga['systems_count']} Systemen" + (f", am meisten auf {ga['top_system']}." if ga.get("top_system") else "."), ""]
        lines += [f"{i}. **{x['name']}** ({x['system']}) – {de_num(x['minutes'] / 60, 1)} h, {x['sessions']} Starts"
                  for i, x in enumerate(ga["games"][:5], 1)]
        lines.append("")
    ra = data.get("retroachievements") or {}
    if ra.get("total"):
        lines += ["## Achievements", f"{ra['total']} Erfolge, {de_num(ra['points'])} Punkte ({ra['hardcore']} im Hardcore-Modus) in "
                  f"{ra['games_count']} Spielen.", ""]
        lines += [f"{i}. **{x['name']}** ({x['console']}) – {x['count']} Erfolge" for i, x in enumerate(ra.get("games") or [], 1)]
        if ra.get("rarest"):
            lines += ["", "Seltenste Erfolge: " + ", ".join(f"{x['title']} ({x['game']})" for x in ra["rarest"][:3])]
        if ra.get("awards"):
            lines += ["", "Geschafft: " + ", ".join(a["title"] for a in ra["awards"])]
        lines.append("")
    lines += ["## Meine Notizen", "", ""]
    return "\n".join(lines)
