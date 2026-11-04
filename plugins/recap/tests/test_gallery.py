"""The design preview: every slide in every variant, the sample story, the routes and their login."""
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_user
from fh_plugin_recap import gallery, sample, story
from fh_plugin_recap.plugin import RecapPlugin

link = lambda kind, palette, shape, type_, offset, sel: f"{kind}/{palette}/{shape}/{type_}/{offset}/{gallery.encode_sel(sel)}"  # noqa: E731


def cells(page):
    return re.findall(r'data-slide="(\w+)" data-variant="(\d)"', page)


def test_sample_story_renders_every_slide_type():
    html = gallery.sample_story("neon", "blobs", "bold", 0, {})
    keys = set(re.findall(r'<section class="slide s-(\w+)', html))
    assert set(story.VARIANTS) <= keys and set(gallery.SPECIAL) <= keys
    assert "neu dabei" in html and "▲" in html and "▼" in html and "etwa so viel" in html  # all kinds of deltas
    assert "Rekord" in html and "Steam" in html


def test_worst_case_sample_is_at_the_limits():
    html = gallery.sample_story("neon", "blobs", "bold", 0, {"worst": 1})
    assert "Donaudampfschifffahrtselektrizitätenhauptbetriebswerkbauunterbeamtengesellschaft" in html
    assert set(story.VARIANTS) <= set(re.findall(r'<section class="slide s-(\w+)', html))
    assert html.count("<figure") and "s-photos" in html
    assert gallery.params(v="worst1.series1")[4] == {"worst": 1, "series": 1} and gallery.params(v="worst2")[4] == {}
    page = gallery.render_gallery("neon", "blobs", "bold", 0, {"worst": 1}, link)
    assert "Grenzfall" in page and "Donaudampf" in page and len(cells(page)) >= sum(story.VARIANTS.values())


def test_sample_pictures_are_inline_only():
    html = gallery.sample_story("neon", "blobs", "bold", 0, {})
    assert "http://" not in html.replace("http://www.w3.org", "").replace("%3A//www.w3.org", "")
    assert "data:image/svg+xml" in html and 'src=""' not in html
    assert sample.svg("Der Test", 20, 30).startswith("data:image/svg+xml,%3Csvg")


def test_gallery_has_every_slide_in_every_variant():
    page = gallery.render_gallery("neon", "blobs", "bold", 0, {}, link)
    got = cells(page)
    for key, count in story.VARIANTS.items():
        for v in range(count):
            assert (key, str(v)) in got, (key, v)
            assert f"{key} · Variante {v + 1}" in page
        assert (key, str(count)) not in got
        for text in story.KICKERS[key]:
            assert text.replace("&", "&amp;") in page
    for key in gallery.SPECIAL:
        assert (key, "0") in got


def test_frames_are_static_and_reuse_the_story_css():
    page = gallery.render_gallery("neon", "blobs", "bold", 0, {}, link)
    assert page.count("<iframe") == len(cells(page))
    doc = gallery.frame_page(gallery.sections("neon", "blobs", "bold", 0)[("total", 0)], "neon", "blobs", "bold", 0)
    assert doc.count("<script") == 1 and "recapFit" in doc and "animation:none!important" in doc and 'class="slide on s-total' in doc
    assert story.PAGE.split("</style>")[0].split("<style>")[1] in doc  # the story's own CSS, unchanged
    assert 'class="progress"></div>' in doc  # no progress bars


@pytest.mark.parametrize("palette", list(story.PALETTES))
def test_every_palette_works(palette):
    page = gallery.render_gallery(palette, "rings", "serif", 3, {}, link)
    assert "type-serif" in page and "shape-rings" in page
    first = story.PALETTES[palette]["sets"][0][1]
    assert any(x in page for x in {s[0] for s in story.PALETTES[palette]["sets"]}) and first
    assert len(cells(page)) >= sum(story.VARIANTS.values())


@pytest.mark.parametrize("shape,type_", [(s, t) for s in story.SHAPES for t in story.TYPES])
def test_every_shape_and_type_renders(shape, type_):
    assert f"shape-{shape} type-{type_}" in gallery.sample_story("pop", shape, type_, 0, {})


def test_variant_selection_changes_the_story():
    one = gallery.sample_story("neon", "blobs", "bold", 0, {"movies": 1})
    assert 'class="slide s-movies v1"' in one and 'class="fan"' in one
    palette, shape, type_, offset, sel = gallery.params("nope", "x", "y", "9", "movies1.movies9.bogus2.series2")
    assert (palette, shape, type_, offset) == ("neon", "blobs", "bold", 3) and sel == {"movies": 1, "series": 2}
    page = gallery.render_gallery("neon", "blobs", "bold", 0, {"movies": 1}, link)
    assert 'class="cell sel" data-slide="movies" data-variant="1"' in page and "s/neon/blobs/bold/0/movies1" in page


# --- routes ---

def make_client(logged_in):
    app = FastAPI()
    for router in RecapPlugin().routers():
        app.include_router(router)
    if logged_in:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    return TestClient(app)


def test_preview_routes_need_a_login():
    client = make_client(False)
    for path in ("/api/plugins/recap/preview", "/api/plugins/recap/preview/story"):
        assert client.get(path).status_code in (401, 403)


def test_preview_routes_serve_the_gallery_and_the_story():
    client = make_client(True)
    r = client.get("/api/plugins/recap/preview", params={"palette": "polarlicht", "shape": "glow", "type": "caps", "v": "series2"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Design-Vorschau" in r.text and "type-caps" in r.text and "shape-glow" in r.text
    assert "/api/plugins/recap/preview/story?palette=polarlicht" in r.text
    r = client.get("/api/plugins/recap/preview/story", params={"v": "intro2.music1"})
    assert r.status_code == 200 and 'class="slide s-music v1"' in r.text and "← Design-Vorschau" in r.text
    assert client.get("/api/plugins/recap/preview", params={"palette": "kaputt"}).status_code == 200  # falls back to the default


def run_page(path):
    import asyncio
    return asyncio.run(RecapPlugin().page(SimpleNamespace(id=1), "recaps", path))


def test_preview_pages_work_through_the_signed_link():
    ctype, body = run_page("preview/")
    assert ctype.startswith("text/html") and "Design-Vorschau" in body.decode() and "<base" not in body.decode()
    assert b'href="g~herbstlaub~blobs~bold~0~"' in body
    assert b'<base href="preview/">' in run_page("preview")[1]
    ctype, body = run_page("preview/s~herbstlaub~stripes~serif~2~books1")
    assert 'class="slide s-books v1"' in body.decode() and "type-serif" in body.decode() and 'href="g~herbstlaub~stripes~serif~2~books1"' in body.decode()
    assert run_page("preview/g~neon~glow~caps~0~")[0].startswith("text/html")
    assert run_page("preview/unsinn") is None


def test_index_links_to_the_preview(tmp_path):
    from fh_plugin_recap import views
    assert 'href="preview/"' in views.render_index(1) and "Design-Vorschau" in views.render_index(1)
