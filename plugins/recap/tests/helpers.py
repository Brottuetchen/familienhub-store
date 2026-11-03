"""Fake services and data for the recap tests. Nothing here talks to a real service."""

import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import respx

JF, ABS, IMMICH, NAVI, NC, FEED = ("https://jf.example.org", "https://abs.example.org", "https://photos.example.org",
                                   "https://music.example.org", "https://cloud.example.org", "https://pod.example.org")
LB = "https://api.listenbrainz.org"
ANNA_ID, BEN_ID = "a" * 32, "b" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 800
MP3 = b"ID3" + b"1" * 200000
SERIES_ID, EPISODE_ID, MOVIE_ID = "5e51e5" + "0" * 26, "e91500" + "0" * 26, "0a01e0" + "0" * 26
TZ = ZoneInfo("Europe/Berlin")

GLOBAL_ENV = {"RECAP_JELLYFIN_URL": JF, "RECAP_JELLYFIN_TOKEN": "server-token-xyz", "RECAP_ABS_URL": ABS,
              "RECAP_IMMICH_URL": IMMICH, "RECAP_NAVIDROME_URL": NAVI, "RECAP_NEXTCLOUD_URL": NC}
ANNA = {"RECAP_JELLYFIN_USER": "Anna", "RECAP_LISTENBRAINZ_USER": "anna_lb", "RECAP_LISTENBRAINZ_TOKEN": "lb-secret-1",
        "RECAP_ABS_TOKEN": "abs-secret-1", "RECAP_IMMICH_KEY": "immich-secret-1", "RECAP_NAVIDROME_USER": "anna",
        "RECAP_NAVIDROME_PASSWORD": "navi-secret-1", "RECAP_NEXTCLOUD_USER": "anna",
        "RECAP_NEXTCLOUD_APP_PASSWORD": "nc-secret-1"}
SECRETS = ("server-token-xyz", "lb-secret-1", "abs-secret-1", "immich-secret-1", "navi-secret-1", "nc-secret-1")


def ts(year, month, day, hour=12):
    return dt.datetime(year, month, day, hour, tzinfo=TZ).timestamp()


def listen(n, track, artist, day):
    return {"listened_at": int(ts(2026, 10, day, 10 + n % 8)), "track_metadata": {
        "track_name": track, "artist_name": artist, "release_name": "Album " + artist,
        "mbid_mapping": {"caa_release_mbid": "11111111-2222-3333-4444-55555555555" + str(len(artist) % 10)},
        "additional_info": {"duration_ms": 200000}}}


def install(http: respx.MockRouter, *, year=2026, month=10, fail=()):
    """Registers every service. ``fail`` names sources that answer with an error."""
    def maybe(name, response):
        return httpx.Response(500) if name in fail else response

    http.route(host="testserver").pass_through()
    # Jellyfin
    http.get(f"{JF}/Users").mock(side_effect=lambda r: maybe("jellyfin", httpx.Response(
        200, json=[{"Id": BEN_ID, "Name": "Ben"}, {"Id": ANNA_ID, "Name": "Anna"}])))
    stamp = f"{year}-{month:02d}-"
    rows = [[EPISODE_ID, "Episode", "Great Show - s01e02 - Pilot", 2700, stamp + "05 20:15:00"],
            [EPISODE_ID, "Episode", "Great Show - s01e02 - Pilot", 900, stamp + "05 23:00:00"],
            [MOVIE_ID, "Movie", "Big Film", 7200, stamp + "12 21:00:00"]]
    http.post(f"{JF}/user_usage_stats/submit_custom_query").mock(
        side_effect=lambda r: maybe("jellyfin", httpx.Response(200, json={"results": rows})))
    http.get(host="jf.example.org", path=f"/Users/{ANNA_ID}/Items").mock(return_value=httpx.Response(200, json={"Items": [
        {"Id": EPISODE_ID, "SeriesName": "Great Show", "SeriesId": SERIES_ID, "ParentIndexNumber": 1},
        {"Id": MOVIE_ID, "Name": "Big Film", "ProductionYear": 2019}]}))
    http.get(host="jf.example.org", path=f"/Users/{ANNA_ID}/Items/{SERIES_ID}").mock(
        return_value=httpx.Response(200, json={"UserData": {"Played": True}}))
    http.get(host="jf.example.org", path__regex=r"/Items/[0-9a-f]{32}/Images/Primary").mock(
        return_value=httpx.Response(200, content=JPEG))
    # ListenBrainz and cover art
    listens = [listen(i, f"Song {i % 3}", ("Band A", "Band B", "Band C")[i % 3], 1 + i % 20) for i in range(30)]
    http.get(f"{LB}/1/user/anna_lb/listens").mock(side_effect=lambda r: maybe("music", httpx.Response(
        200, json={"payload": {"listens": listens}})))
    http.get(f"{LB}/1/stats/user/anna_lb/artists").mock(return_value=httpx.Response(
        200, json={"payload": {"artists": [{"artist_name": "Band A", "listen_count": 10}]}}))
    http.get(f"{LB}/1/stats/user/anna_lb/recordings").mock(return_value=httpx.Response(
        200, json={"payload": {"recordings": []}}))
    http.get(host="coverartarchive.org").mock(return_value=httpx.Response(200, content=JPEG))
    # Audiobookshelf
    http.get(f"{ABS}/api/me/listening-sessions").mock(side_effect=lambda r: maybe("audiobooks", httpx.Response(200, json={
        "total": 1, "sessions": [{"mediaType": "book", "libraryItemId": "li-book-0001", "startedAt": ts(2026, 10, 8) * 1000,
                                  "timeListening": 7200, "displayTitle": "Long Book", "displayAuthor": "An Author"}]})))
    http.get(f"{ABS}/api/me").mock(return_value=httpx.Response(200, json={"mediaProgress": [
        {"libraryItemId": "li-book-0001", "isFinished": True, "finishedAt": ts(2026, 10, 20) * 1000}]}))
    http.get(host="abs.example.org", path="/api/items/li-book-0001/cover").mock(return_value=httpx.Response(200, content=JPEG))
    # Immich
    assets = [{"id": f"asset-{i:04d}-xx", "type": "IMAGE", "isFavorite": i < 2, "localDateTime": f"{stamp}{10 + i:02d}T10:00:00.000Z",
               "exifInfo": {"city": "Seaside", "country": "Land", "latitude": 54.0, "longitude": 10.0, "make": "Cam"}}
              for i in range(5)]
    http.post(f"{IMMICH}/api/search/metadata").mock(side_effect=lambda r: maybe("photos", httpx.Response(
        200, json={"assets": {"items": assets, "nextPage": None}})))
    http.get(host="photos.example.org", path__regex=r"/api/assets/asset-\d+-xx/thumbnail").mock(
        return_value=httpx.Response(200, content=JPEG))
    # Nextcloud GPodder Sync and a podcast feed
    http.get(f"{NC}/index.php/apps/gpoddersync/episode_action").mock(side_effect=lambda r: maybe("podcasts", httpx.Response(
        200, json={"actions": [{"action": "play", "podcast": FEED + "/feed.xml", "episode": "e1", "started": 0,
                                "position": 1800, "timestamp": f"{stamp}09T08:00:00"}]})))
    http.get(f"{FEED}/feed.xml").mock(return_value=httpx.Response(200, content=(
        b'<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Talk Show</title>'
        b'<itunes:image href="https://pod.example.org/cover.jpg"/></channel></rss>')))
    http.get(f"{FEED}/cover.jpg").mock(return_value=httpx.Response(200, content=JPEG))
    # Navidrome (Subsonic)
    songs = [{"id": f"song-{i}", "title": f"Song {i}", "artist": a, "duration": 200, "playCount": 5 - i}
             for i, a in enumerate(("Band A", "Band B", "Band C"))]
    http.get(host="music.example.org", path="/rest/search3").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "searchResult3": {"song": songs}}}))
    http.get(host="music.example.org", path="/rest/stream.view").mock(side_effect=lambda r: maybe(
        "soundtrack", httpx.Response(200, content=MP3)))
    http.get(host="music.example.org", path="/rest/ping.view").mock(return_value=httpx.Response(
        200, json={"subsonic-response": {"status": "ok"}}))
    http.get(host="api.listenbrainz.org", path="/1/validate-token").mock(return_value=httpx.Response(
        200, json={"valid": True, "user_name": "anna_lb"}))
    http.get(host="photos.example.org", path="/api/users/me").mock(return_value=httpx.Response(200, json={"id": "u"}))
