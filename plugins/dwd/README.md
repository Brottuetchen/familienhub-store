# DWD weather warnings and pollen

A first-party plugin for [Familienhub](https://github.com/Brottuetchen/familienhub). It brings two things from the German Weather Service (Deutscher Wetterdienst, DWD) to your family's Today screen:

- **Official weather warnings** for your location, with a push notification when a new warning reaches the level you choose.
- **The pollen forecast** (Pollenflug-Gefahrenindex) for your region: only the pollen that matters today.

No account and no API key are needed. Works for places in Germany.

Requires Familienhub **1.0.0-beta.3** or newer. On an older hub the plugin shows as not set up with an explanation and does nothing.

## What it does

### Cards on Today

Two separate cards, so each family can hide or arrange them on its own (*Today → customise*):

| Card id | Shown when | Content |
|---|---|---|
| `plugin:dwd:warnings` | at least one warning is active or announced for your location | title "Weather warning(s)", badge with the highest level, one line per warning (DWD headline, time window such as "Tue 14:00–20:00" or "until Tue 20:00"); more than six warnings are cut with "+N more" |
| `plugin:dwd:pollen` | a pollen type is at or above your minimum level today | title "Pollen · <region>", badge with the highest level, one line per type ("Birch: high", with "tomorrow: medium" underneath), highest first |

If nothing is relevant, there is no card. If DWD or Bright Sky are unreachable, there is no card either (the plugin never shows an error on Today). Texts follow each person's app language (German or English).

### Push notifications

A job runs **every 15 minutes**. For every warning at or above `DWD_MIN_SEVERITY` that has not been announced yet, admins, partners and children get a push (guests do not). The text is in each person's language (German if none is set). The title is DWD's name of the level plus the event, for example "Unwetterwarnung: STURMBÖEN"; the body has the time window and DWD's description.

- **Every warning once.** Announced warnings are remembered in a small file, `plugin_dwd_state.json`, in the hub's data folder. Nothing is announced twice, also not after a restart. Entries disappear when the warning has expired.
- **Upgrades.** If a warning you were told about gets a higher level, you get one more push, titled "Update: …" ("Aktualisiert: …"). A warning that rises from below your minimum to above it counts as new.
- **Re-issued warnings.** DWD re-issues a changed warning under a new id. A new id with the same event code and an overlapping time window as one you were already told about is treated as the same warning (an update only if the level is higher).
- **Rate limit.** The hub allows 20 push calls per plugin and hour. If more than three new warnings appear in one run, one summary push per language is sent instead of one per warning. A push that the hub drops because of the limit is retried in the next run.
- If the state file cannot be written, the plugin keeps working but may repeat pushes after a restart (a warning is logged).

Each person can switch plugin notifications off under *More → Notification settings → Plugins*.

## Settings

All settings are optional.

| Key | Meaning | Default |
|---|---|---|
| `DWD_LATITUDE`, `DWD_LONGITUDE` | Your location in decimal degrees (e.g. `50.11` and `8.68`). Both or none. | the location of the weather plugin (`WEATHER_LATITUDE`/`WEATHER_LONGITUDE`) |
| `DWD_MIN_SEVERITY` | Lowest level that triggers a push: `minor`, `moderate`, `severe`, `extreme`. The card shows all levels. | `moderate` |
| `DWD_NOTIFY` | Switch for push notifications. | on |
| `DWD_POLLEN_REGION` | Pollen region: a partregion number or a name, see below. | the federal state of your location |
| `DWD_POLLEN_TYPES` | Comma list of pollen types to show, German or English names (`Birke, Gräser` or `birch, grasses`). | all |
| `DWD_POLLEN_MIN` | Show pollen at or above this level: `low`, `medium`, `high`. | `medium` |

**About the location.** The hub's weather plugin always has a built-in default location (Berlin). The DWD plugin does **not** take that for your home: unless you or the weather plugin set a real location, the plugin stays "not set up" and asks for `DWD_LATITUDE` and `DWD_LONGITUDE`. If you really live in Berlin, enter the coordinates here.

**Warning levels.** DWD levels map to `minor` (Wetterwarnung), `moderate` (Markante Wetterwarnung), `severe` (Unwetterwarnung) and `extreme` (Extreme Unwetterwarnung). The default `moderate` keeps frost and light wind out of your pocket.

**How to find your pollen region.** The DWD publishes the forecast for 27 regions. Without a setting, the plugin takes the federal state of your location (from Bright Sky); where a state has several regions (for example Hessen), it shows the **highest** level of all of them. For a precise forecast, enter your region in `DWD_POLLEN_REGION`, either the number or the name:

| Number | Region | Number | Region |
|---|---|---|---|
| 11 | Inseln und Marschen (Schleswig-Holstein, Hamburg) | 92 | Rhein-Main |
| 12 | Geest, Schleswig-Holstein und Hamburg | 101 | Rhein, Pfalz, Nahe und Mosel |
| 20 | Mecklenburg-Vorpommern | 102 | Mittelgebirgsbereich Rheinland-Pfalz |
| 31 | Westl. Niedersachsen/Bremen | 103 | Saarland |
| 32 | Östl. Niedersachsen | 111 | Oberrhein und unteres Neckartal |
| 41 | Rhein.-Westfäl. Tiefland | 112 | Hohenlohe/mittlerer Neckar/Oberschwaben |
| 42 | Ostwestfalen | 113 | Mittelgebirge Baden-Württemberg |
| 43 | Mittelgebirge NRW | 121 | Allgäu/Oberbayern/Bay. Wald |
| 50 | Brandenburg und Berlin | 122 | Donauniederungen |
| 61 | Tiefland Sachsen-Anhalt | 123 | Bayern nördl. der Donau, o. Bayr. Wald, o. Mainfranken |
| 62 | Harz | 124 | Mainfranken |
| 71 | Tiefland Thüringen | 81 | Tiefland Sachsen |
| 72 | Mittelgebirge Thüringen | 82 | Mittelgebirge Sachsen |
| 91 | Nordhessen und hess. Mittelgebirge | | |

(The numbers are the DWD's `partregion_id`; Mecklenburg-Vorpommern and Brandenburg/Berlin have one region each, so their numbers 20 and 50 are the region ids.) A state name such as `Hessen` or `Berlin` works too. **Test connection** in the plugin settings lists the valid regions if yours is not found.

**Pollen levels.** The DWD index (0 to 3, with in-between values) is shown as none / low / medium / high: `0` none; `0-1` and `1` low; `1-2` and `2` medium; `2-3` and `3` high. In-between values round up. Outside the season everything is 0 and there is no card.

**Test connection** checks the values you typed, before saving: coordinates and enum values are validated, the warnings service is asked for your location (the message names the place DWD found and whether warnings are active), and the pollen region is looked up in the real DWD file.

## Hosts

| Host | What for |
|---|---|
| `api.brightsky.dev` | Warnings: a free JSON interface to the official DWD alerts. Receives your coordinates (rounded to four decimals). |
| `opendata.dwd.de` | Pollen forecast file `s31fg.json`. Receives nothing about you. |

Both are contacted from the hub server (not from phones), over https, with a timeout of 6 seconds. Answers are cached in memory (warnings 5 minutes, pollen 30 minutes), so opening Today does not hit the services every time. A copy that is at most one hour old is used when a service is briefly down.

## Data

- **Reads:** the two services above; your location settings; the list of active people and their language (for push).
- **Writes:** only `plugin_dwd_state.json` in the hub's data folder (announced warning ids, levels and time windows; no personal data). Push goes through the hub's notification service.
- No account, no token, no cookies. Nothing is sent anywhere else.

## Why Bright Sky and not the DWD directly

The DWD publishes warnings as large CAP/GeoJSON files for all of Germany, which have to be matched with warning cells. Bright Sky does this for a coordinate, is free and requires no key. The price is a dependency on a third-party service (run by a volunteer, with the DWD as the data source). If Bright Sky is down, there are no warnings; pollen (straight from the DWD) is not affected.

## Limitations

- Germany only. Places outside the DWD area give "not covered".
- Warnings come from Bright Sky and are as current as it is (usually within minutes). The plugin polls every 15 minutes, so a push can be up to about 20 minutes late. It is a convenience, **not a replacement for official warning apps** such as NINA or WarnWetter when safety depends on it.
- The pollen forecast is per DWD region, not per street, and is published once a day (around 11:00).
- The pollen region for a state with several regions is the highest level of the state unless you choose a region.
- Re-issued warnings are matched by event code and time overlap (see above); in rare cases a changed warning may be announced once more, or an upgrade to a different event type may appear as a new warning.
- The first run after installation announces warnings that are already active.
- Texts of warnings (headline, description) are DWD's own, in German or English as provided by Bright Sky.

## Attribution

- Warnings and pollen data: **Quelle: Deutscher Wetterdienst** (DWD). Warnings are used under the DWD's open-data terms (GeoNutzV); the pollen index is DWD open data.
- Warning delivery: [Bright Sky](https://brightsky.dev), a free API by Jakob de Maeyer, which serves DWD data.

## Development

```sh
pip install pytest respx
PYTHONPATH=/path/to/familienhub/backend pytest
```

The tests simulate both services with `respx`. The pollen file in `tests/fixtures/s31fg.json` is a recording of the real DWD file; the alert shape follows the Bright Sky documentation. See the hub's [plugin guide](https://github.com/Brottuetchen/familienhub/blob/main/docs/plugins.md).

License: MIT.
