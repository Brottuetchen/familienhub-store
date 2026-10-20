# Departures at your stop (`departures`)

A first-party plugin for [Familienhub](https://github.com/Brottuetchen/familienhub). It puts a card on the **Today** screen with the next buses and trains from your stop, for the school run:

```
Musterstadt Schulzentrum                           12 min
Bus 12 → Schulzentrum · 7:42 (+2)       leave in 5 min
Bus 14 → Hauptbahnhof · 7:44            leave in 7 min
Bus 12 → Schulzentrum · 7:50            cancelled
```

- the badge shows the minutes until the next departure that runs,
- `(+2)` is the delay in minutes, `cancelled` marks a departure that does not run,
- with a walking time, departures you cannot reach any more are hidden and the second line says *leave in X min*,
- the card only appears inside the time window you choose (for example 06:30 to 08:30, Monday to Friday) and never when there are no departures. No card also means no request to the API.

It needs **Familienhub 1.0.0-beta.3 or newer** (Today cards and the connection test). On an older hub the plugin stays inactive and never crashes anything.

## Data source and fair use

Departures come from a public [transport.rest](https://transport.rest) instance (a free HTTP API in front of the HAFAS systems of transit operators, built on `hafas-client`). The default is `https://v6.db.transport.rest`; regional instances such as `https://v6.vbb.transport.rest` (Berlin/Brandenburg) work the same way. The timetable data belongs to the transit companies behind each instance; the API is run by volunteers.

Please be a good guest:

- The instances are free and shared. Their documentation asks for a User-Agent and states a rate limit (100 requests per minute and IP at the time of writing; check the docs of your instance). The plugin sends the User-Agent `familienhub-plugin-departures/1.0 (+https://github.com/Brottuetchen/familienhub-store)`.
- The plugin keeps answers for **45 seconds** (failures for 30 seconds), shares one request between people who open Today at the same moment, does not poll in the background and makes no request outside your time window. One household needs about one request per minute while somebody has the app open.
- If you have many hubs or need a guaranteed service, host your own instance and put its address into the setting.
- No availability is promised: public instances change, move and sometimes are down (the card then simply is not shown). Check the instance's terms before relying on it.

## Settings

| Setting | Meaning |
|---|---|
| `DEPARTURES_STOP` (required) | Stop ID on the instance, e.g. `8000105`. A stop **name** also works (it is looked up once and cached) but may pick the wrong stop of a town, so use the ID, see below. |
| `DEPARTURES_BASE_URL` | Address of the transport.rest instance, default `https://v6.db.transport.rest`. |
| `DEPARTURES_LINES` | Optional, comma separated line names as shown on the card, e.g. `Bus 12, Bus 14`. `12` and `bus12` match `Bus 12` too. Empty = all lines. |
| `DEPARTURES_DIRECTIONS` | Optional, comma separated parts of the destination name, case does not matter, e.g. `Schulzentrum`. |
| `DEPARTURES_MAX` | Number of departures on the card, 1 to 6, default 4. |
| `DEPARTURES_WALK_MINUTES` | Walking time to the stop, 0 to 90, default 0 (off). |
| `DEPARTURES_SHOW_FROM` / `DEPARTURES_SHOW_UNTIL` | Optional `HH:MM`. Show the card only in this window (both ends inclusive). Only one set: from then until midnight, or from midnight until then. *From* later than *until* is an overnight window. Times are in the hub's timezone (`TIMEZONE`, default Europe/Berlin). |
| `DEPARTURES_WEEKDAYS_ONLY` | Switch. Show the card Monday to Friday only. |

The connection test checks the typed values (before saving), looks up the stop and shows its name and the next departure, for example *Stop found: Musterstadt Schulzentrum. Next: Bus 12 → Schulzentrum · 7:42 (+2) (in 12 min).* It also tells you when your filters match nothing, or when a stop name was resolved to a stop you did not mean.

## Find your stop ID

IDs are different on different instances, so search on the instance you use:

```
curl 'https://v6.db.transport.rest/locations?query=Musterstadt%20Schulzentrum&results=5&stops=true&addresses=false&poi=false'
```

Each result has an `id` and a `name`; enter the `id` of the right stop as `DEPARTURES_STOP`. (Alternatively type the name as the stop and run the connection test: it prints the ID it found and the next best matches.) A bus stop often has one ID per direction or platform; if the card shows the wrong direction, search again and try the neighbouring entries, or use the direction filter.

## Hosts and what the hub allows

`hosts` in the manifest lists the instances this plugin is known to work with: `v6.db.transport.rest`, `v6.vbb.transport.rest`, `v6.bvg.transport.rest`. The hub shows this list before installing but **does not enforce it** (see the plugin documentation), so any other address you type into `DEPARTURES_BASE_URL` works without further approval, as long as your server can reach it. This also means a changed address sends the stop ID to that host: only use instances you trust. Redirects are not followed.

## What the plugin reads and writes

- Reads from the transport.rest instance: `GET /locations` (only when the stop is a name, and in the connection test) and `GET /stops/{id}/departures` (line, direction, planned and real-time time, delay, cancellation, platform).
- It sends only the stop ID or the search text to the instance, no personal data, no credentials. It writes nothing, stores nothing in the hub database and needs no password.
- Everyone who can open Today sees the card; the stop is a household setting.
- Logs contain error types only, never addresses.

## Limitations

- Real-time data only where the instance has it; otherwise the planned time is shown without a delay.
- The card icon is the bus icon for every kind of line: the plugin icon list of the hub has no train icon.
- Texts follow the person's app language (German or English). The texts of the connection test are English.
- If the instance takes longer than 3.5 seconds the hub shows no card on that load (the hub allows plugins 4 seconds); the answer is kept for the next load.
- Only the next 90 minutes (plus walking time) are requested.
- `v6.db.transport.rest` answered with HTTP 503 when this plugin was tested (see below); instances come and go.

## Tested and not tested

- Automated tests (`pytest`, `respx`) use answers modelled on the documented API and on real answers of `v6.vbb.transport.rest`: parsing, filters, delay and cancellation, walking time, time window, timezone, caching, error paths, settings validation, compatibility guard, manifest.
- A manual run against the live `v6.vbb.transport.rest` (name search, departures, connection test, card) worked. The default instance `v6.db.transport.rest` could not be tested: it answered 503 at the time.
- Cancelled departures were never seen live; their handling follows the `hafas-client` documentation (`cancelled: true`, `when: null`).

## Develop

```
pip install pytest respx
pip install -e .
DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest
```

MIT licensed.
