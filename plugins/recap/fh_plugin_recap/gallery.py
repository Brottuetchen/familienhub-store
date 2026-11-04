"""Design preview: every slide of the story in every layout variant, with the sample month from ``sample.py``.

The frames are the real slides: ``story.render`` builds the sample story and the gallery cuts the slide sections out of
it, each into its own phone-sized iframe with the story's own page CSS (so nothing is duplicated here).
Animations are switched off and end in their final state.
"""
import html
import re

from . import sample, story

DEFAULTS = {"palette": "neon", "shape": "blobs", "type": "bold", "offset": 0}
LABELS = {"intro": "Intro", "total": "Gesamt", "series": "Serie", "binge": "Couch-Tag", "movies": "Filme", "music": "Musik",
          "books": "Hörbücher", "pods": "Podcasts", "photos": "Fotos", "games": "Games", "ra": "Achievements", "outro": "Outro"}
SPECIAL = {"record": "Rekord", "finished": "Durchgeschaut", "new_artists": "Neu entdeckt", "trip": "Unterwegs",
           "marathon": "Dauerbrenner", "ra_awards": "Geschafft", "streak": "Am Stück"}
FRAME_W, FRAME_H, SCALE = 360, 640, .75  # the frame is laid out like a phone and shown at 270 x 480

# Gallery only: no motion, no progress bars, no tap zones; the equaliser bars would all sit at one height without their animation
STATIC_CSS = (""".progress,.tap,.snd{display:none!important}*,*::before,*::after{animation:none!important;transition:none!important}
.slide{opacity:1;pointer-events:none}.eq i:nth-child(4n+1){height:45%}.eq i:nth-child(4n+2){height:85%}.eq i:nth-child(4n+3){height:60%}.eq i:nth-child(4n){height:100%}""")
SECTION = re.compile(r'<section class="slide s-(\w+) v(\d+)".*?</section>', re.S)
SCRIPT = re.compile(r"<script>.*?</script>", re.S)


def params(palette=None, shape=None, type_=None, offset=None, v=""):
    """Cleans the choices from the address: unknown values fall back to the defaults. ``v`` is ``key<variant>`` joined by dots."""
    palette = palette if palette in story.PALETTES else DEFAULTS["palette"]
    shape = shape if shape in story.SHAPES else DEFAULTS["shape"]
    type_ = type_ if type_ in story.TYPES else DEFAULTS["type"]
    try:
        offset = int(offset) % 6
    except (TypeError, ValueError):
        offset = DEFAULTS["offset"]
    sel = {}
    for part in str(v or "").split("."):
        m = re.fullmatch(r"([a-z]+?)(\d)", part)
        if m and m[1] in story.VARIANTS and int(m[2]) < story.VARIANTS[m[1]]:
            sel[m[1]] = int(m[2])
        elif m and m[1] == "worst" and m[2] == "1":  # not a variant: the sample month at its limits
            sel["worst"] = 1
    return palette, shape, type_, offset, sel


def encode_sel(sel):
    return ".".join(f"{k}{n}" for k, n in sel.items())


def sample_story(palette, shape, type_, offset, sel):
    """The sample month rendered with the given design; ``sel`` maps slides to a variant (the rest use the first), ``worst`` = 1 picks the month at its limits."""
    data, prev, history = sample.sample_story_input(worst=bool(sel.get("worst")))
    data["design"].update(palette=palette, shape=shape, type=type_, offset=offset,
                          variants={k: sel.get(k, 0) for k in story.VARIANTS}, texts={k: 0 for k in story.KICKERS})
    return story.render(data, prev, history)


def frame_page(section, palette, shape, type_, offset):
    """A one-slide page made of the story's own PAGE template, shown in its final state."""
    page = SCRIPT.sub("", story.PAGE).replace("{{FIT}}", story.FIT).replace("</style>", STATIC_CSS + "</style>", 1)
    return (page.replace("{{TITLE}}", "Vorschau").replace("{{BARS}}", "").replace("{{SOUND}}", "")
            .replace("{{BODY}}", f"shape-{shape} type-{type_}")
            .replace("{{SLIDES}}", section.replace('<section class="slide', '<section class="slide on', 1)))


def sections(palette, shape, type_, offset, worst=False):
    """``{(key, variant): section html}`` for every slide in every variant, special slides as ``(key, 0)``."""
    out = {}
    for n in range(max(story.VARIANTS.values())):
        page = sample_story(palette, shape, type_, offset, {**{k: min(n, c - 1) for k, c in story.VARIANTS.items()}, "worst": int(worst)})
        for m in SECTION.finditer(page):
            key, v = m[1], int(m[2])
            if (key in story.VARIANTS and v == n) or (key not in story.VARIANTS and n == 0):
                out[(key, v if key in story.VARIANTS else 0)] = m[0]
    return out


# -- the gallery page -----------------------------------------------------------------------------------------

CSS = """:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#15121c;color:#f2f0f7;font-family:system-ui,sans-serif;line-height:1.4}
header{position:sticky;top:0;z-index:5;background:#15121cee;backdrop-filter:blur(8px);padding:12px 16px;border-bottom:1px solid #ffffff22}
h1{font-size:1.25rem;margin:0 0 8px}h2{font-size:1.15rem;margin:36px 0 4px}h2 small{opacity:.6;font-weight:400}
.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:4px 0;font-size:.85rem}.row>b{min-width:5.5rem;opacity:.7;font-weight:600}
.chip{padding:3px 11px;border-radius:99px;background:#ffffff14;color:inherit;text-decoration:none}.chip.on{background:#c4ff3d;color:#111;font-weight:700}
.chip.go{background:#ff3cac;color:#fff;font-weight:700}main{padding:0 16px 60px}.hint{opacity:.7;font-size:.85rem;margin:2px 0 10px}
.frames{display:flex;flex-wrap:wrap;gap:22px;align-items:flex-start}.cell{width:270px}.cell.sel .shot{outline:3px solid #c4ff3d}
.cell .lab{font-size:.85rem;margin:0 0 6px}.cell .lab b{display:block;font-size:.95rem}.cell .lab a{color:#c4ff3d}.cell .lab span{opacity:.6}
.shot{width:270px;height:480px;overflow:hidden;border-radius:18px;background:#000;position:relative}
.shot iframe{width:%dpx;height:%dpx;border:0;transform:scale(%s);transform-origin:0 0}
.kick{font-size:.75rem;opacity:.65;margin:6px 0 0}""" % (FRAME_W, FRAME_H, SCALE)


def render_gallery(palette, shape, type_, offset, sel, link):
    """The gallery page. ``link(kind, palette, shape, type_, offset, sel)`` makes the address of the gallery (``"g"``) or of the full story (``"s"``)."""
    worst = bool(sel.get("worst"))
    secs = sections(palette, shape, type_, offset, worst)

    def chips(name, options, current, make):
        return (f'<div class="row"><b>{name}</b>' + "".join(
            f'<a class="chip{" on" if o == current else ""}" href="{html.escape(make(o))}">{html.escape(str(o))}</a>' for o in options) + "</div>")

    def cell(key, v, label, n_variants):
        section = secs.get((key, v))
        if section is None:
            return ""
        chosen = key in story.VARIANTS and sel.get(key, 0) == v
        pick = (f'<a href="{html.escape(link("g", palette, shape, type_, offset, {**sel, key: v}))}">für die Story wählen</a>'
                if key in story.VARIANTS and n_variants > 1 else "")
        doc = html.escape(frame_page(section, palette, shape, type_, offset), quote=True)
        return (f'<div class="cell{" sel" if chosen else ""}" data-slide="{key}" data-variant="{v}"><p class="lab"><b>{html.escape(label)}</b>{pick}</p>'
                f'<div class="shot"><iframe srcdoc="{doc}" title="{html.escape(label)}" loading="lazy" scrolling="no" tabindex="-1"></iframe></div></div>')

    body = []
    for key, count in story.VARIANTS.items():
        kicks = " · ".join(html.escape(x) for x in story.KICKERS[key])
        cells = "".join(cell(key, v, f"{key} · Variante {v + 1}", count) for v in range(count))
        body.append(f'<section id="{key}"><h2>{LABELS[key]} <small>{key} · {count} {"Variante" if count == 1 else "Varianten"}</small></h2>'
                    f'<p class="hint">Überschriften-Texte: {kicks}</p><div class="frames">{cells}</div></section>')
    extra = "".join(cell(k, 0, f"{k} · {name}", 1) for k, name in SPECIAL.items())
    body.append(f'<section id="special"><h2>Sonderfolien <small>erscheinen nur bei passenden Ereignissen</small></h2><div class="frames">{extra}</div></section>')

    head = (f'<header><h1>Design-Vorschau</h1>'
            + chips("Palette", story.PALETTES, palette, lambda o: link("g", o, shape, type_, offset, sel))
            + chips("Form", story.SHAPES, shape, lambda o: link("g", palette, o, type_, offset, sel))
            + chips("Schrift", story.TYPES, type_, lambda o: link("g", palette, shape, o, offset, sel))
            + chips("Farbwechsel", range(6), offset, lambda o: link("g", palette, shape, type_, o, sel))
            + chips("Daten", ("normal", "Grenzfall"), "Grenzfall" if worst else "normal",
                    lambda o: link("g", palette, shape, type_, offset, {**{k: v for k, v in sel.items() if k != "worst"}, **({"worst": 1} if o == "Grenzfall" else {})}))
            + f'<div class="row"><b>Story</b><a class="chip go" href="{html.escape(link("s", palette, shape, type_, offset, sel))}">Beispiel-Story mit der Auswahl öffnen</a>'
              f'<span class="hint">Auswahl: {html.escape(encode_sel(sel) or "überall Variante 1")}. Innerhalb einer Palette wechseln die Farben von Folie zu Folie.</span></div></header>')
    return (f'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Design-Vorschau</title><style>{CSS}</style></head><body>{head}<main>{"".join(body)}</main></body></html>')


def render_story(palette, shape, type_, offset, sel, back):
    """The full swipeable sample story with a way back to the gallery."""
    page = sample_story(palette, shape, type_, offset, sel)
    pill = (f'<a href="{html.escape(back)}" style="position:fixed;z-index:9;left:12px;bottom:calc(env(safe-area-inset-bottom) + 12px);'
            f'padding:6px 12px;border-radius:99px;background:rgba(0,0,0,.5);color:#fff;font:700 .8rem system-ui;text-decoration:none">← Design-Vorschau</a>')
    return page.replace("</body>", pill + "</body>", 1)
