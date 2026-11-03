"""Tests for the cover fallback chains, their limits and the placeholder tiles of the story. Services are simulated."""

import asyncio
import json
import re
import time

import httpx
import pytest
import respx

from fh_plugin_recap import build, collectors, store, story
from fh_plugin_recap.collectors import Config, Covers

from helpers import ABS, JF, JPEG, LB, NAVI, install
from test_plugin import MONTH, TZ, build_for, people, run, services  # noqa: F401 - fixtures

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 800
TMDB = "https://image.tmdb.org/t/p"
CFG = Config(jellyfin_url=JF, jellyfin_token="server-token-xyz", navidrome_url=NAVI, navidrome_user="anna",
             navidrome_password="navi-secret-1", abs_url=ABS, abs_token="abs-secret-1")
ID1, ID2 = "1" * 32, "2" * 32


def mock():
    return respx.mock(using="httpx", assert_all_called=False)


def poster(tmp_path, kind="Series", item_id=ID1, name="Family Guy", year=None):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.jellyfin_poster(client, CFG, tmp_path, kind, item_id, name, year)
    return run(go())


def image(content=JPEG, kind="image/jpeg"):
    return httpx.Response(200, content=content, headers={"content-type": kind})


# --- Jellyfin: item image -> library search -> remote search ---

def test_poster_from_the_item_image(tmp_path):
    with mock() as http:
        route = http.get(f"{JF}/Items/{ID1}/Images/Primary").mock(return_value=image())
        assert poster(tmp_path) == "img/jf-series-family-guy.jpg" and route.call_count == 1
    assert (tmp_path / "jf-series-family-guy.jpg").read_bytes() == JPEG


def test_poster_falls_back_to_the_library_search_when_the_item_is_gone(tmp_path):
    with mock() as http:
        http.get(f"{JF}/Items/{ID1}/Images/Primary").mock(return_value=httpx.Response(404))
        search = http.get(f"{JF}/Items").mock(return_value=httpx.Response(200, json={"Items": [
            {"Id": ID2, "Name": "Other", "ImageTags": {"Primary": "x"}}, {"Id": ID1, "Name": "Family Guy", "ImageTags": {"Primary": "y"}}]}))
        second = http.get(f"{JF}/Items/{ID2}/Images/Primary").mock(return_value=image())
        remote = http.post(f"{JF}/Items/RemoteSearch/Series").mock(return_value=httpx.Response(200, json=[]))
        assert poster(tmp_path, kind="Series") == "img/jf-series-family-guy.jpg"
    params = dict(search.calls[0].request.url.params)
    assert params["searchTerm"] == "Family Guy" and params["IncludeItemTypes"] == "Series" and params["Recursive"] == "true"
    assert second.call_count == 1 and remote.call_count == 0  # the exact name match was not tried twice, no remote search needed


def test_movie_library_search_uses_the_year(tmp_path):
    with mock() as http:
        http.get(f"{JF}/Items/{ID1}/Images/Primary").mock(return_value=httpx.Response(404))
        search = http.get(f"{JF}/Items").mock(return_value=httpx.Response(200, json={"Items": [
            {"Id": ID2, "Name": "Casino Royale", "ImageTags": {"Primary": "x"}}]}))
        http.get(f"{JF}/Items/{ID2}/Images/Primary").mock(return_value=image())
        assert poster(tmp_path, "Movie", ID1, "Casino Royale", 2006) == "img/jf-movie-casino-royale.jpg"
    assert search.calls[0].request.url.params["Years"] == "2006" and search.calls[0].request.url.params["IncludeItemTypes"] == "Movie"


def test_poster_falls_back_to_the_remote_search_and_never_sends_the_server_key_to_the_image_host(tmp_path):
    with mock() as http:
        http.get(f"{JF}/Items/{ID1}/Images/Primary").mock(return_value=httpx.Response(404))
        http.get(f"{JF}/Items").mock(return_value=httpx.Response(200, json={"Items": []}))
        remote = http.post(f"{JF}/Items/RemoteSearch/Movie").mock(return_value=httpx.Response(200, json=[
            {"Name": "Casino Royale", "ImageUrl": "https://artworks.thetvdb.com/banners/images/missing/movie.jpg"},
            {"Name": "Casino Royale", "ImageUrl": "https://m.media-amazon.com/images/x.jpg"},
            {"Name": "James Bond 007 - Casino Royale", "ImageUrl": f"{TMDB}/original/abc.jpg"},
            {"Name": "No picture"}]))
        tmdb = http.get(f"{TMDB}/w500/abc.jpg").mock(return_value=image())
        amazon = http.get("https://m.media-amazon.com/images/x.jpg").mock(return_value=image())
        missing = http.get("https://artworks.thetvdb.com/banners/images/missing/movie.jpg").mock(return_value=image())
        assert poster(tmp_path, "Movie", None, "Casino Royale", 2006) == "img/jf-movie-casino-royale.jpg"
    assert json.loads(remote.calls[0].request.content) == {"SearchInfo": {"Name": "Casino Royale", "Year": 2006},
                                                           "IncludeDisabledProviders": False}
    assert 'Token="server-token-xyz"' in remote.calls[0].request.headers["authorization"]
    assert tmdb.call_count == 1 and amazon.call_count == 0 and missing.call_count == 0
    assert "authorization" not in tmdb.calls[0].request.headers  # TMDb is a third party


def test_poster_is_none_when_every_step_fails(tmp_path):
    with mock() as http:
        http.get(f"{JF}/Items/{ID1}/Images/Primary").mock(return_value=httpx.Response(404))
        http.get(f"{JF}/Items").mock(return_value=httpx.Response(500))
        http.post(f"{JF}/Items/RemoteSearch/Series").mock(return_value=httpx.Response(200, json=[
            {"ImageUrl": "ftp://example.org/x.jpg"}, {"ImageUrl": f"{TMDB}/w500/dead.jpg"}]))
        http.get(f"{TMDB}/w500/dead.jpg").mock(return_value=httpx.Response(404))
        assert poster(tmp_path) is None
    assert not list(tmp_path.iterdir())


# --- images: what is accepted ---

@pytest.mark.parametrize("response", [
    httpx.Response(200, content=JPEG, headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"x" * 900, headers={"content-type": "image/jpeg"}),  # not an image
    httpx.Response(200, content=JPEG + b"0" * collectors.MAX_IMAGE_BYTES, headers={"content-type": "image/jpeg"}),
    httpx.Response(200, content=b"\xff\xd8\xff"),  # too small
])
def test_images_that_are_not_acceptable_are_refused(tmp_path, response):
    async def go(url):
        async with collectors.new_client() as client:
            return await collectors.save_image(client, url, tmp_path, "x")
    with mock() as http:
        http.get("https://img.example.org/a").mock(return_value=response)
        assert run(go("https://img.example.org/a")) is None
        assert run(go("file:///etc/passwd")) is None and run(go("ftp://img.example.org/a")) is None
    assert not list(tmp_path.iterdir())


def test_png_and_missing_content_type_are_accepted(tmp_path):
    async def go():
        async with collectors.new_client() as client:
            return await collectors.save_image(client, "https://img.example.org/a", tmp_path, "x")
    with mock() as http:
        http.get("https://img.example.org/a").mock(return_value=httpx.Response(200, content=PNG))
        assert run(go()) == "img/x.png"


# --- music ---

def listens(*tracks):
    return [{"listened_at": int(time.time()) - 60, "track_metadata": {
        "track_name": t, "artist_name": a, "release_name": "R " + a,
        "mbid_mapping": {"caa_release_mbid": "11111111-2222-3333-4444-555555555555"}}} for t, a in tracks]


def lb(http, items):
    http.get(f"{LB}/1/user/anna_lb/listens").mock(return_value=httpx.Response(200, json={"payload": {"listens": items}}))
    http.get(f"{LB}/1/stats/user/anna_lb/artists").mock(return_value=httpx.Response(200, json={"payload": {"artists": []}}))


MUSIC_CFG = Config(**{**CFG.__dict__, "listenbrainz_user": "anna_lb"})


def test_track_cover_falls_back_to_navidrome_when_the_cover_art_archive_has_nothing(tmp_path):
    with mock() as http:
        lb(http, listens(("#DIY", "Trettmann"), ("Wahnsinn", "01099")))
        http.get(host="coverartarchive.org").mock(return_value=httpx.Response(404))
        search = http.get(f"{NAVI}/rest/search3").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "searchResult3": {"song": [
                {"id": "s0", "title": "#DIY (Remix)", "artist": "Trettmann", "coverArt": "mf-wrong"},
                {"id": "s1", "title": "#DIY", "artist": "trettmann", "coverArt": "al-diy"},
                {"id": "s2", "title": "Wahnsinn", "artist": "01099", "albumId": "al-wahn"}]}}}))
        art = http.get(f"{NAVI}/rest/getCoverArt.view").mock(return_value=image())
        data = run(_music(tmp_path))
    covers_ = {t["name"]: t["cover"] for t in data["top_tracks"]}
    assert covers_["#DIY"] and covers_["Wahnsinn"] and search.call_count >= 2
    asked = sorted(c.request.url.params["id"] for c in art.calls)
    assert {"al-diy", "al-wahn"} <= set(asked) and all(c.request.url.params["size"] == "500" for c in art.calls)


def _music(tmp_path, covers=None):
    import datetime as dt

    async def go():
        async with collectors.new_client() as client:
            now = dt.datetime.now(TZ)
            return await collectors.collect_music(client, MUSIC_CFG, now - dt.timedelta(days=2), now + dt.timedelta(days=2),
                                                  tmp_path, TZ, covers)
    return go()


def test_cover_art_archive_hit_does_not_ask_navidrome(tmp_path):
    with mock() as http:
        lb(http, listens(("Song", "Band")))
        http.get(host="coverartarchive.org").mock(return_value=image())
        search = http.get(f"{NAVI}/rest/search3").mock(return_value=httpx.Response(500))
        data = run(_music(tmp_path))
    assert data["top_tracks"][0]["cover"].startswith("img/mu-")
    assert data["top_artists"][0]["cover"]  # the artist: Navidrome fails, so the release of the month is used
    assert search.call_count == 1  # only the artist lookup


def test_artist_picture_from_get_artist_info2(tmp_path):
    with mock() as http:
        http.get(f"{NAVI}/rest/search3").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "searchResult3": {"artist": [{"id": "ar-other", "name": "Other"}, {"id": "ar-1", "name": "Trettmann"}]}}}))
        info = http.get(f"{NAVI}/rest/getArtistInfo2.view").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "artistInfo2": {"largeImageUrl": "https://lastfm.example.org/i/" + collectors.LASTFM_PLACEHOLDER + ".png",
                                            "mediumImageUrl": "https://img.example.org/trettmann.jpg"}}}))
        pic = http.get("https://img.example.org/trettmann.jpg").mock(return_value=image())
        star = http.get(host="lastfm.example.org").mock(return_value=image())

        async def go():
            async with collectors.new_client() as client:
                return await collectors.navi_artist_cover(client, CFG, tmp_path, "trettmann", "ar-trettmann")
        assert run(go()) == "img/ar-trettmann.jpg"
    assert info.calls[0].request.url.params["id"] == "ar-1" and pic.call_count == 1 and star.call_count == 0


def test_artist_without_info_image_uses_the_most_played_album_then_song(tmp_path):
    with mock() as http:
        http.get(f"{NAVI}/rest/search3").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "searchResult3": {"artist": [{"id": "ar-1", "name": "Band"}]}}}))
        http.get(f"{NAVI}/rest/getArtistInfo2.view").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "artistInfo2": {}}}))
        http.get(f"{NAVI}/rest/getArtist.view").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "artist": {"album": [{"id": "al-1", "coverArt": "al-quiet", "playCount": 2},
                                                 {"id": "al-2", "coverArt": "al-loud", "playCount": 90}]}}}))
        art = http.get(f"{NAVI}/rest/getCoverArt.view").mock(return_value=image())

        async def go():
            async with collectors.new_client() as client:
                return await collectors.navi_artist_cover(client, CFG, tmp_path, "Band", "ar-band")
        assert run(go()) == "img/ar-band.jpg"
    assert art.calls[0].request.url.params["id"] == "al-loud"


def test_artist_falls_back_to_the_cover_of_its_top_song(tmp_path):
    with mock() as http:
        def search(request):
            if request.url.params.get("artistCount") == "5":
                return httpx.Response(200, json={"subsonic-response": {"status": "ok", "searchResult3": {}}})
            return httpx.Response(200, json={"subsonic-response": {"status": "ok", "searchResult3": {"song": [
                {"id": "a", "artist": "Band", "playCount": 1, "coverArt": "mf-low"},
                {"id": "b", "artist": "Band", "playCount": 8, "coverArt": "mf-top"},
                {"id": "c", "artist": "Else", "playCount": 99, "coverArt": "mf-else"}]}}})
        http.get(f"{NAVI}/rest/search3").mock(side_effect=search)
        art = http.get(f"{NAVI}/rest/getCoverArt.view").mock(return_value=image())

        async def go():
            async with collectors.new_client() as client:
                return await collectors.navi_artist_cover(client, CFG, tmp_path, "Band", "ar-band")
        assert run(go()) == "img/ar-band.jpg"
    assert art.calls[0].request.url.params["id"] == "mf-top"


def test_every_listed_artist_gets_a_cover_and_nothing_found_means_none(tmp_path):
    names = [("S%d" % i, "Artist %d" % i) for i in range(5)]
    with mock() as http:
        lb(http, listens(*names))
        http.get(host="coverartarchive.org").mock(return_value=httpx.Response(404))
        http.get(f"{NAVI}/rest/search3").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "searchResult3": {"artist": [{"id": "ar-1", "name": "Artist 3"}]}}}))
        http.get(f"{NAVI}/rest/getArtistInfo2.view").mock(return_value=httpx.Response(500))
        http.get(f"{NAVI}/rest/getArtist.view").mock(return_value=httpx.Response(200, json={"subsonic-response": {
            "status": "ok", "artist": {"album": [{"id": "al-1", "coverArt": "al-1"}]}}}))
        http.get(f"{NAVI}/rest/getCoverArt.view").mock(return_value=image())
        data = run(_music(tmp_path))
    by_name = {a["name"]: a["cover"] for a in data["top_artists"]}
    assert by_name["Artist 3"] and [n for n, c in by_name.items() if not c] == ["Artist 0", "Artist 1", "Artist 2", "Artist 4"]
    assert all(t["cover"] is None for t in data["top_tracks"])  # no match in Navidrome, nothing from the archive


# --- audiobooks and podcasts ---

def test_every_listed_book_gets_a_cover(tmp_path):
    import datetime as dt

    sessions = [{"mediaType": "book", "libraryItemId": f"li-book-000{i}", "startedAt": time.time() * 1000, "timeListening": 7200 - i * 100,
                 "displayTitle": f"Book {i}", "displayAuthor": "Au"} for i in range(5)]
    with mock() as http:
        http.get(f"{ABS}/api/me/listening-sessions").mock(return_value=httpx.Response(200, json={"total": 5, "sessions": sessions}))
        http.get(f"{ABS}/api/me").mock(return_value=httpx.Response(200, json={"mediaProgress": []}))
        route = http.get(host="abs.example.org", path__regex=r"/api/items/li-book-000\d/cover").mock(return_value=image())

        async def go():
            async with collectors.new_client() as client:
                now = dt.datetime.now(TZ)
                return await collectors.collect_audiobookshelf(client, CFG, now - dt.timedelta(days=2), now + dt.timedelta(days=2), tmp_path, TZ)
        data = run(go())
    assert len(data["books"]) == 5 and all(b["cover"] for b in data["books"]) and route.call_count == 5
    assert all(set(b) == {"title", "author", "hours", "finished", "cover"} for b in data["books"])


def test_every_listed_podcast_gets_a_cover_including_large_feeds(tmp_path):
    import datetime as dt

    NC = "https://cloud.example.org"
    cfg = Config(**{**CFG.__dict__, "nextcloud_url": NC, "nextcloud_user": "anna", "nextcloud_password": "x"})
    feeds = ["https://pod.example.org/a.xml", "https://pod.example.org/b.xml", "https://pod.example.org/c.xml"]
    now = dt.datetime.now(dt.timezone.utc)
    actions = [{"action": "play", "podcast": f, "episode": "e", "started": 0, "position": 1000 - i, "timestamp": now.isoformat()}
               for i, f in enumerate(feeds)]
    small = lambda n: (b'<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>P%d</title>'  # noqa: E731
                       b'<itunes:image href="https://pod.example.org/%d.jpg"/></channel></rss>') % (n, n)
    big = (b'<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Big</title>'
           b'<image><url>https://pod.example.org/2.jpg</url></image><item><title>E</title><description>'
           + b"x" * (collectors.MAX_FEED_BYTES + 10) + b"</description></item></channel></rss>")
    with mock() as http:
        http.get(f"{NC}/index.php/apps/gpoddersync/episode_action").mock(return_value=httpx.Response(200, json={"actions": actions}))
        http.get(feeds[0]).mock(return_value=httpx.Response(200, content=small(0)))
        http.get(feeds[1]).mock(return_value=httpx.Response(200, content=small(1)))
        http.get(feeds[2]).mock(return_value=httpx.Response(200, content=big))
        for n in range(3):
            http.get(f"https://pod.example.org/{n}.jpg").mock(return_value=image())

        async def go():
            async with collectors.new_client() as client:
                return await collectors.collect_podcasts(client, cfg, now - dt.timedelta(days=2), now + dt.timedelta(days=2), tmp_path)
        data = run(go())
    assert [p["title"] for p in data["podcasts"]] == ["P0", "P1", "Big"]
    assert all(p["cover"] for p in data["podcasts"]) and all(set(p) == {"title", "hours", "episodes", "cover"} for p in data["podcasts"])


# --- cap and time limits ---

def test_cover_jobs_over_the_cap_are_skipped():
    async def go():
        covers = Covers(max_jobs=3)
        items = [{} for _ in range(5)]

        async def lookup():
            return "img/x.jpg"
        await covers.fill([(i, "cover", lookup) for i in items])
        return [i["cover"] for i in items]
    assert run(go()) == ["img/x.jpg"] * 3 + [None, None]


def test_cover_lookups_run_in_parallel_but_with_a_small_limit():
    state = {"now": 0, "max": 0}

    async def go():
        covers = Covers(concurrency=4)
        items = [{} for _ in range(12)]

        async def lookup():
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1
            return "img/x.jpg"
        await covers.fill([(i, "cover", lookup) for i in items])
        return items
    assert all(i["cover"] for i in run(go())) and state["max"] == 4


def test_a_slow_lookup_times_out_and_a_failing_one_does_not_break_the_others():
    async def go():
        covers = Covers(item_seconds=0.05)
        items = [{}, {}, {}]

        async def slow():
            await asyncio.sleep(5)
            return "img/slow.jpg"

        async def broken():
            raise RuntimeError("boom")

        async def fine():
            return "img/fine.jpg"
        started = time.monotonic()
        await covers.fill([(items[0], "cover", slow), (items[1], "cover", broken), (items[2], "cover", fine)])
        return items, time.monotonic() - started
    items, took = run(go())
    assert [i["cover"] for i in items] == [None, None, "img/fine.jpg"] and took < 1


def test_the_whole_time_budget_stops_further_lookups():
    async def go():
        covers = Covers(concurrency=1, seconds=0.15, item_seconds=1)
        items = [{} for _ in range(10)]

        async def lookup():
            await asyncio.sleep(0.1)
            return "img/x.jpg"
        started = time.monotonic()
        await covers.fill([(i, "cover", lookup) for i in items])
        return items, time.monotonic() - started
    items, took = run(go())
    done = [i["cover"] for i in items]
    assert done[0] == "img/x.jpg" and done[-1] is None and took < 1


# --- the story ---

EMPTY_PH = re.compile(r'<(?:div|span) class="ph[^"]*"[^>]*>\s*</(?:div|span)>')


def ph_tiles(page):
    return re.findall(r'<div class="ph[^"]*"[^>]*><strong>([^<]*)</strong></div>', page)


def test_initials():
    assert story.initials("Family Guy") == "FG"
    assert story.initials("The Gentlemen") == "GE" and story.initials("Rock of Ages") == "RO"
    assert story.initials("MobLand") == "MO" and story.initials("#DIY") == "DI" and story.initials("01099") == "01"
    assert story.initials("Hör mal, wer da hämmert") == "HM" and story.initials("") == "•" and story.initials(None) == "•"
    assert story.initials("<script>") == "SC"


def failing_covers(http):
    http.get(host="jf.example.org", path__regex=r"/Items/[0-9a-f]{32}/Images/Primary").mock(return_value=httpx.Response(404))
    http.get(f"{JF}/Items").mock(return_value=httpx.Response(200, json={"Items": []}))
    http.post(re.compile(rf"{re.escape(JF)}/Items/RemoteSearch/.*")).mock(return_value=httpx.Response(200, json=[]))
    http.get(host="coverartarchive.org").mock(return_value=httpx.Response(404))
    http.get(host="music.example.org", path="/rest/getCoverArt.view").mock(return_value=httpx.Response(404))
    http.get(host="abs.example.org", path="/api/items/li-book-0001/cover").mock(return_value=httpx.Response(404))
    http.get("https://pod.example.org/cover.jpg").mock(return_value=httpx.Response(404))


def test_full_build_without_any_cover_still_works_and_shows_placeholders(people):
    with mock() as http:
        install(http)
        failing_covers(http)
        data = build_for()
    assert data["errors"] == {}
    assert data["jellyfin"]["series"][0]["poster"] is None and data["music"]["top_tracks"][0]["cover"] is None
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert not EMPTY_PH.search(page) and "ph" in page
    tiles = ph_tiles(page)
    assert tiles and all(t.strip() for t in tiles) and "BF" in tiles
    assert 'class="ph' in page and not re.findall(r'(?:src|href)="https?://', page)


def test_full_build_with_covers_uses_the_images_and_thumbnails(people, services):
    data = build_for()
    page = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert data["jellyfin"]["series"][0]["poster"] and data["audiobooks"]["books"][0]["cover"]
    assert data["podcasts"]["podcasts"][0]["cover"] and all(a["cover"] for a in data["music"]["top_artists"])
    assert not EMPTY_PH.search(page)


def snapshot_data(**kw):
    series = [{"name": f"Show {i}", "hours": 9 - i, "episodes": 5, "seasons": [1], "poster": None} for i in range(5)]
    base = {"month": MONTH, "seed": "7", "generated": "x", "errors": {}, "soundtrack": [],
            "totals": {"series_hours": 30, "movie_hours": 8, "music_hours": 10, "audiobook_hours": 4, "podcast_hours": 2, "media_hours": 54},
            "jellyfin": {"series": series, "series_count": 5, "episodes": 25, "series_hours": 30, "movie_hours": 8, "movies_count": 2,
                         "movies": [{"name": "Rock of Ages", "year": 2012, "hours": 2, "poster": None},
                                    {"name": "Der wilde Roboter", "year": 2024, "hours": 2, "poster": "img/r.jpg"}],
                         "days": {}, "best_day": None, "night_owl": None, "active_days": 3, "finished_series": []},
            "music": {"plays": 90, "minutes": 300, "hours": 5, "artists_count": 5, "tracks_count": 5, "active_days": 3, "days": [],
                      "top_artists": [{"name": f"Artist {i}", "plays": 9, "cover": None} for i in range(5)],
                      "top_tracks": [{"name": f"Track {i}", "artist": "A", "plays": 3, "cover": None} for i in range(5)],
                      "top_releases": [], "new_artists": []},
            "audiobooks": {"hours": 4, "minutes": 240, "finished": 0, "titles_count": 3, "days": [],
                           "books": [{"title": f"Buch {i}", "author": "Au", "hours": 2, "finished": False, "cover": None} for i in range(3)]},
            "podcasts": {"connected": True, "hours": 2, "minutes": 120, "episodes": 4,
                         "podcasts": [{"title": f"Pod {i}", "hours": 1, "episodes": 2, "cover": None} for i in range(3)]},
            "photos": {}}
    base.update(kw)
    return base


@pytest.mark.parametrize("seed", [str(n) for n in range(12)])
def test_story_variants_never_render_an_empty_placeholder(seed):
    data = snapshot_data(seed=seed)
    data["highlights"] = story.highlights(data, [])
    data["design"] = story.choose_design(data, [])
    page = story.render(data, None, [])
    assert not EMPTY_PH.search(page)
    assert all(t.strip() for t in ph_tiles(page))
    assert re.search(r'class="ph[^"]*"', page)
    for needle in ("Show 0", "Buch 0", "Pod 0", "Artist 0"):
        assert needle in page


def test_story_with_images_uses_thumbnails_in_lists():
    data = snapshot_data()
    for s in data["jellyfin"]["series"]:
        s["poster"] = "img/s.jpg"
    for a in data["music"]["top_artists"]:
        a["cover"] = "img/a.jpg"
    for b in data["audiobooks"]["books"] + data["podcasts"]["podcasts"]:
        b["cover"] = "img/b.jpg"
    seen, ranks = set(), False
    for n in range(40):
        data["seed"] = str(n)
        data["highlights"] = story.highlights(data, [])
        data["design"] = story.choose_design(data, [])
        page = story.render(data, None, [])
        seen |= set(re.findall(r'<li><img class="th" src="(img/[a-z]\.jpg)"', page))
        ranks = ranks or bool(re.search(r'<ol class="ranks thumbs"><li><b>1</b><img class="th" src="img/a.jpg"', page))
        assert not EMPTY_PH.search(page)
    assert {"img/s.jpg", "img/b.jpg"} <= seen and ranks


def test_design_is_not_changed_by_the_covers():
    plain, with_images = snapshot_data(), snapshot_data()
    for s in with_images["jellyfin"]["series"]:
        s["poster"] = "img/s.jpg"
    for d in (plain, with_images):
        d["highlights"] = story.highlights(d, [])
    assert story.choose_design(plain, []) == story.choose_design(with_images, [])


def _clips(n):
    return [{"src": f"img/clip-{i:02d}.mp3", "title": f"Song {i}", "artist": "Band"} for i in range(n)]


def _slide_clips(page):
    return re.findall(r'<section class="slide s-(\w+)[^"]*"[^>]*?data-clip="([^"]+)"', page)


@pytest.mark.parametrize("count", [3, 20])
def test_no_song_repeats_across_slides(count):
    data = snapshot_data()
    data["soundtrack"] = _clips(count)
    data["highlights"] = story.highlights(data, [])
    data["design"] = story.choose_design(data, [])
    page = story.render(data, None, [])
    used = [src for _, src in _slide_clips(page)]
    assert used and len(used) == len(set(used))  # never the same song twice
    assert dict(_slide_clips(page)).get("music") == "img/clip-00.mp3"
    assert _slide_clips(page)[0][0] == "intro"  # the story starts with sound
    if count == 3:  # more slides than songs: the rest carries no clip, so the last song plays on
        assert page.count("<section") > len(used)


def test_a_single_clip_still_starts_the_story_with_sound():
    data = snapshot_data()
    data["soundtrack"] = _clips(1)
    data["highlights"] = story.highlights(data, [])
    data["design"] = story.choose_design(data, [])
    clips = _slide_clips(story.render(data, None, []))
    assert clips[0] == ("intro", "img/clip-00.mp3")


def test_soundtrack_collects_enough_clips_for_every_slide():
    from fh_plugin_recap import collectors
    assert collectors.CLIP_COUNT >= 18
