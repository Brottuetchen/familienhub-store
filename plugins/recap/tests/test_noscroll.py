"""No slide of the story ever scrolls or runs off its box: every variant x type style x sample (normal and at its limits)
at phone and foldable sizes, rendered in a real browser. Skipped without Playwright + Chromium.
Run it as a script (``python tests/test_noscroll.py``) for a table of the measured overflow per slide."""
import re
import sys

import pytest

from fh_plugin_recap import gallery, story

sync_api = pytest.importorskip("playwright.sync_api")

VIEWPORTS = [(360, 640), (390, 844), (412, 915), (840, 900)]  # small phone, phone, large phone, foldable inner screen
MIN_FIT_NORMAL = .75  # the normal month must be laid out by the structure; the fit is the safety net
MIN_FIT_WORST = .5
FROZEN = "*,*::before,*::after{animation:none!important;transition:none!important}"
# Per slide: how far the content (everything but the blurred collage) is outside the slide box, scrolling, the fit scale and polaroid/text overlaps
MEASURE = """() => [...document.querySelectorAll('.slide')].map(s => {
  const b = s.getBoundingClientRect(), inner = s.querySelector('.inner'), r = {bottom: 0, top: 0, left: 0, right: 0}, ov = [];
  const own = e => !e.closest('.collage') && getComputedStyle(e).display !== 'none';
  for (const e of inner.querySelectorAll('*')) {
    if (!own(e)) continue;
    const q = e.getBoundingClientRect();
    if (!q.width && !q.height) continue;
    r.bottom = Math.max(r.bottom, q.bottom - b.bottom); r.top = Math.max(r.top, b.top - q.top);
    r.right = Math.max(r.right, q.right - b.right); r.left = Math.max(r.left, b.left - q.left);
  }
  for (const polas of inner.querySelectorAll('.polas, .fan')) {
    const figs = [...polas.querySelectorAll('figure')].map(f => f.getBoundingClientRect());
    for (const c of inner.children) {
      if (c === polas || !own(c)) continue;
      const q = c.getBoundingClientRect();
      if (figs.some(f => f.left < q.right && f.right > q.left && f.top < q.bottom - 1 && f.bottom > q.top + 1) && q.height) ov.push(c.className || c.tagName);
    }
  }
  return {key: /s-(\\w+)/.exec(s.className)[1], v: +/ v(\\d+)/.exec(s.className)[1], scroll: inner.scrollHeight - inner.clientHeight,
          fit: +(getComputedStyle(inner).getPropertyValue('--fit') || 1), overflow: r, overlap: ov, scrollable: getComputedStyle(inner).overflowY}
})"""


def settle(page):
    page.add_style_tag(content=FROZEN)
    page.evaluate("document.fonts.ready.then(() => window.recapFit && window.recapFit())")
    page.wait_for_timeout(30)
    page.evaluate("window.recapFit && window.recapFit()")


def problems(rows, min_fit, where):
    out = []
    for r in rows:
        o, name = r["overflow"], f'{where} {r["key"]}.v{r["v"]}'
        if r["scrollable"] in ("auto", "scroll"):
            out.append(f"{name}: inner may scroll")
        if r["scroll"] > 1:
            out.append(f'{name}: content {r["scroll"]}px taller than the box')
        if o["bottom"] > 1 or o["right"] > 1 or o["left"] > 1:
            out.append(f'{name}: content outside the slide (bottom {o["bottom"]:.0f}, right {o["right"]:.0f}, left {o["left"]:.0f})')
        if o["top"] > 1:
            out.append(f'{name}: content above the slide ({o["top"]:.0f})')
        if r["overlap"]:
            out.append(f'{name}: polaroids/fan overlap {r["overlap"]}')
        if r["fit"] < min_fit:
            out.append(f'{name}: fit scale {r["fit"]:.2f} < {min_fit}')
    return out


def story_page(type_, n, worst):
    sel = {**{k: min(n, c - 1) for k, c in story.VARIANTS.items()}, "worst": int(worst)}
    return gallery.sample_story("neon", "blobs", type_, 0, sel)


def measure_story(page, html, size):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.set_content(html)
    settle(page)
    return page.evaluate(MEASURE)


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as ex:  # no browser installed
            pytest.skip(f"no Chromium: {ex}")
        pg = browser.new_page()
        yield pg
        browser.close()


@pytest.mark.parametrize("worst", [False, True], ids=["normal", "worst"])
@pytest.mark.parametrize("type_", story.TYPES)
def test_no_slide_overflows_in_the_story(page, type_, worst):
    bad, fits = [], []
    for n in range(max(story.VARIANTS.values())):
        html = story_page(type_, n, worst)
        for size in VIEWPORTS:
            rows = measure_story(page, html, size)
            assert len(rows) >= 14
            bad += problems(rows, MIN_FIT_WORST if worst else MIN_FIT_NORMAL, f"{type_} n{n} {size[0]}x{size[1]}")
            fits += [r["fit"] for r in rows]
    assert not bad, "\n".join(bad[:25])
    print(f"min fit {type_} {'worst' if worst else 'normal'}: {min(fits):.2f}")


@pytest.mark.parametrize("worst", [False, True], ids=["normal", "worst"])
@pytest.mark.parametrize("type_", story.TYPES)
def test_no_gallery_frame_overflows(page, type_, worst):
    page.set_viewport_size({"width": gallery.FRAME_W, "height": gallery.FRAME_H})
    bad = []
    for (key, v), section in gallery.sections("neon", "blobs", type_, 0, worst).items():
        page.set_content(gallery.frame_page(section, "neon", "blobs", type_, 0))
        settle(page)
        bad += problems(page.evaluate(MEASURE), MIN_FIT_WORST if worst else MIN_FIT_NORMAL, f"{type_} frame")
    assert not bad, "\n".join(bad[:25])


def test_fit_shrinks_content_that_is_too_tall_instead_of_scrolling(page):
    page.set_viewport_size({"width": 360, "height": 640})
    page.set_content(story_page("bold", 0, False))
    page.add_style_tag(content=FROZEN)
    page.evaluate("""() => { for (const n of document.querySelectorAll('.s-series .inner'))
        for (let k = 0; k < 2; k++) n.insertAdjacentHTML('beforeend', '<p class="lead">Noch eine Zeile, die nirgends mehr hinpasst.</p>') }""")
    page.evaluate("window.recapFit()")
    row = next(r for r in page.evaluate(MEASURE) if r["key"] == "series")
    assert .4 <= row["fit"] < .9 and not problems([row], .4, "forced")
    page.set_viewport_size({"width": 412, "height": 915})  # the fit follows the box on a resize
    page.wait_for_timeout(100)
    row = next(r for r in page.evaluate(MEASURE) if r["key"] == "series")
    assert row["fit"] > .9 and not problems([row], .4, "resized")


def test_scripts_in_frames_are_only_the_fit():
    doc = gallery.frame_page(gallery.sections("neon", "blobs", "bold", 0)[("total", 0)], "neon", "blobs", "bold", 0)
    assert len(re.findall(r"<script", doc)) == 1 and "recapFit" in doc


if __name__ == "__main__":  # measured overflow per slide, to compare before and after a change
    with sync_api.sync_playwright() as p:
        pg = p.chromium.launch().new_page()
        seen = {}
        for worst in (False, True):
            for type_ in story.TYPES:
                for n in range(3):
                    html = story_page(type_, n, worst)
                    for size in VIEWPORTS:
                        for r in measure_story(pg, html, size):
                            k = (("worst" if worst else "normal"), r["key"], r["v"])
                            worst_px = max(r["scroll"], r["overflow"]["bottom"], 0)
                            s = seen.setdefault(k, [0, 1.0, ""])
                            if worst_px > s[0]:
                                s[0], s[2] = worst_px, f"{type_} {size[0]}x{size[1]}"
                            s[1] = min(s[1], r["fit"])
        for k, (px, fit, at) in sorted(seen.items()):
            print(f"{k[0]:6} {k[1]:8} v{k[2]}  overflow {px:6.0f}px  min fit {fit:.2f}  worst at {at}")
        sys.exit(0)
