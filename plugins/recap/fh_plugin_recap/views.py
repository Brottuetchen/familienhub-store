"""The month index page and the Obsidian Markdown of a month (same content as the original standalone script)."""

from __future__ import annotations

import html
from typing import Any, Dict, List

from . import store
from .story import MONTHS, de_num

INDEX_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rückblicke</title>
<style>body{margin:0;background:#14051f;color:#fff;font-family:system-ui,sans-serif;padding:48px 16px}
main{max-width:520px;margin:0 auto}h1{font-size:2.6rem;font-weight:900;letter-spacing:-.04em;margin:0 0 24px}
a{display:block;padding:18px 20px;margin-bottom:12px;border-radius:16px;background:linear-gradient(120deg,#5b2bff,#ff3cac);color:#fff;text-decoration:none}
a b{display:block;font-size:1.4rem}a span{opacity:.9;font-size:.9rem}p{opacity:.85;line-height:1.5}</style></head>
<body><main><h1>Rückblicke</h1>{{CARDS}}</main></body></html>"""

EMPTY_TEXT = ("Noch kein Rückblick da. Sobald du unter „Mehr → Meine Verbindungen“ mindestens einen Dienst "
              "verbunden hast, entsteht am Monatsanfang dein erster Rückblick.")


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def month_label(month: str) -> str:
    year, number = month.split("-")
    return f"{MONTHS[int(number) - 1]} {int(year)}"


def render_index(user_id: int) -> str:
    cards: List[str] = []
    for month in store.list_months(user_id):
        data = store.read_snapshot(user_id, month) or {}
        totals = data.get("totals") or {}
        photos = (data.get("photos") or {}).get("total") or 0
        cards.append(f'<a href="{month}/"><b>{_e(month_label(month))}</b><span>{de_num(totals.get("media_hours") or 0)} h Medien · '
                     f'{_e(photos)} Fotos</span></a>')
    return INDEX_PAGE.replace("{{CARDS}}", "".join(cards) or f"<p>{_e(EMPTY_TEXT)}</p>")


def render_obsidian(data: Dict[str, Any], url: str) -> str:
    year, month = map(int, data["month"].split("-"))
    mname = MONTHS[month - 1]
    jf, mu, ab, pc, ph, t = (data.get(k) or {} for k in ("jellyfin", "music", "audiobooks", "podcasts", "photos", "totals"))
    lines = ["---", "type: rueckblick", "agent: ignore", f"monat: {data['month']}", "tags: [rückblick]",
             f"medien_stunden: {t.get('media_hours')}", f"serienfolgen: {jf.get('episodes', 0)}",
             f"filme: {jf.get('movies_count', 0)}", f"musik_minuten: {mu.get('minutes', 0)}",
             f"hoerbuch_stunden: {ab.get('hours', 0)}", f"podcast_stunden: {pc.get('hours', 0)}",
             f"fotos: {ph.get('total', 0)}", "---",
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
    lines += ["## Meine Notizen", "", ""]
    return "\n".join(lines)
