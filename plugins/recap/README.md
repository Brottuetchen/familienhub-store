# Recap

A Familienhub plugin that builds a **monthly recap per person**, in the style of a music service's year in review: a tap-through story with big numbers, posters, photos, a calendar of your best viewing day, and a short soundtrack that crossfades from slide to slide.

Per person means: the admin enters only the addresses of the services (and one Jellyfin server key). Everybody connects **their own** accounts under *More → My connections*, and only ever sees their own recap.

**Requires a Familienhub with personal plugin settings and plugin pages** (merged into `main` after 1.0.1). Older hubs refuse the manifest.

## What a recap contains

| Source | What it reads | Needs |
|---|---|---|
| Jellyfin | Series, episodes, films, longest viewing day, night-owl moment, finished series | [Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting) plugin, a server API key, your Jellyfin user name |
| ListenBrainz | Plays, minutes, top artists and tracks, new artists, cover art | your ListenBrainz user name (token optional) |
| Audiobookshelf | Listening time, top books, finished books | your API token |
| Podcasts | AntennaPod listening through Nextcloud GPodder Sync | Nextcloud address, user name and app password |
| Immich | Photos and videos, busiest day, places, a few highlight pictures, trips | your API key |
| Navidrome | The soundtrack: ten short clips of your favourite tracks | your user name and password |

A source whose server address is empty, or for which a person entered nothing, is skipped. A source that fails while building is skipped, too, and only noted in the snapshot (`errors`); the rest of the recap is still built. Slides appear only for sources that delivered data. There is no language model: the texts are text blocks, every statement comes straight from the data. The story texts are German; translating them is a follow-up.

The look follows a design engine with a **memory**: palette, background shape, type style, layout variant and text of every slide are chosen per person and month, and exclude what the person's previous months used, so no month looks like the last one. Special slides appear when something special happened (new record, finished series, new artists, a trip, a 7-day streak).

## Covers

Every item the story shows (series, films, top tracks, top artists, books, podcasts) gets a picture, found through a fallback chain that stops at the first hit. Nothing here can fail a build: a step that errors or finds nothing just hands over to the next one.

| Item | Chain |
|---|---|
| Series, films | Jellyfin item image, then a Jellyfin library search by name (type, recursive, with the year if known), then Jellyfin's **remote search** (`POST /Items/RemoteSearch/Series` or `/Movie`, normally TMDb; the picture is downloaded from the image host, the Jellyfin key is never sent there). Needed because Maintainerr deletes watched media, so the item id from the play history may no longer exist. |
| Top tracks | Cover Art Archive (release from ListenBrainz), then the matching Navidrome song (`search3`, title and artist, `getCoverArt`). |
| Top artists | Navidrome artist (`search3`, `getArtistInfo2` image), then the cover of the artist's most played album or song in Navidrome, then the Cover Art Archive release of the month. |
| Books | Audiobookshelf cover of every listed book. |
| Podcasts | `itunes:image` or `image/url` of every listed feed (the channel head of very large feeds is read without parsing the whole XML). |

Limits: lookups run four at a time, one lookup (with all its steps) gets 40 seconds, all lookups of a build together 3 minutes and at most 80 lookups; whatever is over a limit gets the placeholder. Downloads accept only http(s) addresses, real images (checked by content, and `image/*` if the server names a type) and at most 5 MB. The Navidrome steps need the Navidrome connection of the person; without it they are skipped.

When nothing is found the story draws a **placeholder tile**: the initials of the item on a gradient made from the slide's own accent colours, pure CSS, no external resources. It never shows an empty grey box. Lists (series, books, podcasts, top artists) show small round thumbnails next to the entries. Palettes and layout variants do not depend on whether covers were found.

## Settings

Admin (server-wide), all optional:

| Setting | Meaning |
|---|---|
| `RECAP_JELLYFIN_URL` | Address of Jellyfin as the hub reaches it. |
| `RECAP_JELLYFIN_TOKEN` | Jellyfin **server** API key (Dashboard → API keys). Playback Reporting needs it. Secret. |
| `RECAP_ABS_URL` | Address of Audiobookshelf. |
| `RECAP_IMMICH_URL` | Address of Immich (a trailing `/api` is removed). |
| `RECAP_NAVIDROME_URL` | Address of Navidrome. |
| `RECAP_NEXTCLOUD_URL` | Address of Nextcloud (with the GPodder Sync app). |

Personal (*More → My connections*), all optional; the form has a *Test connection* button that checks each filled source and answers with one short line per source:

| Setting | Meaning |
|---|---|
| `RECAP_JELLYFIN_USER` | Your Jellyfin user name. Only plays of this user are used (the plugin looks up the user id by name). |
| `RECAP_LISTENBRAINZ_USER`, `RECAP_LISTENBRAINZ_TOKEN` | Your ListenBrainz account; the token is optional and a secret. |
| `RECAP_ABS_TOKEN` | Your Audiobookshelf API token. Secret. |
| `RECAP_IMMICH_KEY` | Your Immich API key. Secret. |
| `RECAP_NAVIDROME_USER`, `RECAP_NAVIDROME_PASSWORD` | Your Navidrome login (used with Subsonic token authentication, the password is never sent). Secret. |
| `RECAP_NEXTCLOUD_USER`, `RECAP_NEXTCLOUD_APP_PASSWORD` | Your Nextcloud user name and an **app password**. Secret. |
| `RECAP_RA_USERNAME`, `RECAP_RA_API_KEY` | Your RetroAchievements user name and Web API key (retroachievements.org → Settings → Keys). The key is a secret. Empty (either one) = no achievements slide. |
| `RECAP_STEAM_ID`, `RECAP_STEAM_API_KEY` | Your SteamID64 (17 digits) and a Steam Web API key (steamcommunity.com/dev/apikey). The key is a secret. The profile's game details must be public. Empty (either one) = no Steam. |

RetroAchievements: the achievements you earned in the month (`API_GetAchievementsEarnedBetween`, month boundaries in the hub's timezone) become an *Achievements* slide right after the games: count, points, the rarest achievements as a badge grid and the games with the most achievements. Games mastered or beaten in the month (`API_GetUserAwards`) get a slide of their own. Badges and icons are downloaded at build time like all other pictures (placeholder tiles when one fails). A failing RA request only leaves the slide out. The key is sent as the API requires (query parameter), is never logged and never stored in the recap.

Steam: the library's play time (`IPlayerService/GetOwnedGames`, all devices) is cumulative like ES-DE's, so it gets its own daily snapshot (`steam/<date>.json` next to `games/`, one request per day taken by the scheduled job or a build on demand; per app: name, total, Steam Deck / Linux / Windows minutes, last played, last two weeks). A month is the total at its end minus the latest snapshot on or before its first day; an app missing from that snapshot counts from zero. Without such a snapshot (the first month) only apps last played within the month count, and only with their two-week play time and only if that two-week window lies wholly inside the month; older lifetime totals are never used. Zero, negative and more-than-the-month deltas are dropped. Steam apps join the games ranking as system *Steam* with the portrait from Steam's CDN (header picture, store search, then the placeholder as fallbacks). A Steam/PC title that ES-DE also reports (system `steam` or `windows`, same title ignoring case, punctuation, an edition suffix and a leading article) is not added twice: the larger of the two play times is kept, with ES-DE's launch count. A failing Steam request only leaves Steam out (an empty answer, e.g. a private profile, is treated as a failure and never stored). The key is sent as the API requires (query parameter), is never logged and never stored in the recap or the snapshots.

The plugin is active without any setting. A person with no source connected simply gets no recap.

## When recaps are built

The plugin runs a scheduled job every 60 minutes. A run looks at the active people in order and, for the **first** person who has at least one source and no snapshot for the previous month, starts the build in a **background task** and returns at once (the hub gives a scheduled job 120 seconds, a build downloads posters, photos and about ten audio clips and may take minutes). So:

- one person per run, one build at a time; the same month is never built twice in parallel;
- a person's month counts as built when its snapshot exists, so later runs skip it;
- a build that fails completely (nothing collected, a source failed) is tried again after 6 hours, at most 3 times;
- a month in which nothing was played, listened to or photographed gets an empty snapshot and no story;
- when the story is done the person gets **one** push ("Dein Oktober-Rückblick ist da") that opens the recap. The snapshot remembers it was sent; if the push could not be sent (rate limit), a later run retries only the push.

## Where the recaps are

*More → Recaps* (German: *Rückblicke*) lists the person's months, newest first; a tap opens the story. The page is served by the hub with its fixed content security policy: the story loads **nothing from other servers**. Posters, photos, covers and audio clips are downloaded at build time. Autoplay of the soundtrack starts with the first touch if the browser blocks it.

You can also build on demand (the app has no button for it yet; use the API with a normal login or an unrestricted access key):

| Endpoint | Meaning |
|---|---|
| `POST /api/plugins/recap/build[?month=YYYY-MM][&force=true]` | Builds your own previous month (or a finished month you name) in the background. `status` is `started`, `running`, `busy` (another build runs), `exists` (add `force=true` to rebuild; the design stays the same), or `no_sources`. |
| `GET /api/plugins/recap/months` | Your months (newest first), figures, failed sources, and months being built. |
| `GET /api/plugins/recap/markdown/{month}` | The Obsidian note of a month (front matter, totals, top lists, link to the story as an in-app route). Scoped access keys cannot reach plugin routes; use an unrestricted key. |

## Where data is stored

No database tables. Files below the hub's data directory:

```
<DATA_DIR>/plugin-data/recap/<user id>/
    2026-10.json              snapshot of the month: figures, design, errors, "notified" (also marks the month as built)
    2026-10.attempts.json     failed attempts (removed after a success)
    2026-10/index.html        the story
    2026-10/img/              posters, photos, covers (jpg, png, webp) and soundtrack clips (mp3)
```

The snapshots are also the memory of the design engine: a person's history is that person's earlier snapshots. Deleting a month's folder and snapshot lets the scheduler build it again; a person's files are not deleted automatically when the person or the plugin is removed (delete `plugin-data/recap/<user id>` if wanted). **The recap contains personal viewing, listening and photo data and photos: include the data directory in your backups and treat it as private.**

## Hosts contacted

- `api.listenbrainz.org` (listens, statistics), `coverartarchive.org` and, through its redirects, `archive.org` (cover art).
- Your own Jellyfin, Audiobookshelf, Immich, Navidrome and Nextcloud, at the addresses the admin enters.
- The **podcast feed addresses and cover images from your own AntennaPod subscriptions**, to read the podcast title and cover. Only `http(s)` is followed, feeds with a DOCTYPE or entity declaration and feeds over 3 MB are ignored.

No telemetry. Nothing else is contacted.

## Security notes

- A person can only reach their own folder. A page path is mapped by *building* the file path from validated parts (`YYYY-MM`, `index.html`, or `img/<name>.<jpg|png|webp|mp3>` with a strict file name pattern); anything else answers 404. Symlinks and anything resolving outside the person's folder are refused.
- Tokens, passwords and addresses are never logged or returned by endpoints or the connection test. Errors in the snapshot hold only an error class and an HTTP status. Because Navidrome's Subsonic token and salt are part of the request URL, the plugin raises the log level of `httpx`/`httpcore` to WARNING so that request lines do not reach the hub log.
- The Jellyfin server key is a server-wide secret (Playback Reporting needs it). The plugin uses it only to read the playback table **filtered by the person's user id** and to read that user's items; every person only gets their own plays. Keep in mind that anyone who can open the plugin's settings as admin can use the key.
- Page content is shown from the hub's origin under a strict CSP; the story contains no external resource. All text coming from services is HTML-escaped.
- Tokens given in the personal settings are stored encrypted by the hub.

## Limitations

- Written against the documented APIs of Jellyfin Playback Reporting, ListenBrainz, Audiobookshelf, Immich, Subsonic/Navidrome and Nextcloud GPodder Sync. The standalone script this plugin grew out of ran against real servers for one household; the plugin itself has been tested only against simulated answers so far.
- Playback Reporting stores local time; the plugin reads it as such.
- Months are built after the month ended (first run after midnight on the 1st). Nothing is built for the running month by the scheduler.
- The story is German only. The page title and the push title are German; the page name in the app is translated.

## Development

```
cd plugins/recap
pip install pytest respx
DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest
```

MIT licensed.

Design preview: the index page links to "Design-Vorschau" (page `preview/`, also `GET /api/plugins/recap/preview` and `/preview/story`, login required). It shows every slide in every layout variant, plus the special slides, as static phone-sized frames with a made-up sample month (`sample.py`, inline SVG placeholders, no real data). Palette, shape, type and colour offset are chosen in the header; a variant can be picked per slide and opened as the full swipeable sample story. The frames are the story's own slide HTML and page CSS (`gallery.py` cuts them out of `story.render`), with animations switched off.
