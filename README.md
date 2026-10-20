# Familienhub Store

The reviewed catalogue of plugins and themes for [Familienhub](https://github.com/Brottuetchen/familienhub). Admins browse it in the app under **Administration → Store** and install an entry with one click.

| | |
|---|---|
| **What is listed** | Only entries the maintainers reviewed. Each one is pinned to an exact commit (`sha`), and the hub installs only that commit. |
| **Updates** | A new version is a pull request that changes the pinned `sha` and `version`. The hub then shows "Update available"; nothing updates automatically. |
| **Own sources** | Admins can add their own GitHub repositories with an `index.json` in the same format. The hub marks those entries "Not reviewed" and warns before installing. |
| **Signatures** | None. Trust comes from the review plus the pinned commit. |

Format, security model and API: [docs/store.md](https://github.com/Brottuetchen/familienhub/blob/main/docs/store.md) in the main repository.

## Layout

```
index.json                    the catalogue (format version 1)
screenshots/<entry-id>/1.png  screenshots, referenced by relative path from index.json
plugins/<id>/                 official plugins maintained in this repository
tools/validate.py             the check CI runs on every pull request
```

CI runs `tools/validate.py` on every pull request, and the tests of every plugin under `plugins/` against the current Familienhub `main`.

## Currently listed

All themes from the [theme gallery](https://github.com/Brottuetchen/familienhub-themes), each with a light and a dark screenshot, and seven official plugins. The plugins need Familienhub 1.0.0-beta.3 or newer. Each asks for its addresses and credentials during installation and has a connection test.

| Plugin | What it does |
|---|---|
| [Immich](plugins/immich) | "On this day" photos from your Immich on the Today screen. |
| [WebUntis](plugins/webuntis) | Children's timetable from WebUntis, with a push for cancellations and substitutions. |
| [Screen time](plugins/screentime) | Internet time as a reward: unlocks a device on a Fritz!Box, AdGuard Home or Pi-hole and locks it again. |
| [DWD](plugins/dwd) | Official weather warnings and the pollen forecast from the Deutscher Wetterdienst, with push for warnings. |
| [Departures](plugins/departures) | Next departures at your stop, including delays and walking time. |
| [Grocy](plugins/grocy) | Grocy as the shopping list, expiring stock on Today, daily hints for parents. |
| [Tandoor](plugins/tandoor) | Tandoor as a recipe source: search, plan, ingredients to the shopping list. |

Each plugin README says what was tested against the real service and what only against simulated answers.

## Submitting a plugin or theme

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: publish your plugin or theme in a public GitHub repository, open a pull request here that adds one entry to `index.json` plus its screenshots, and wait for review.

## License

The catalogue (index and screenshots) is [CC0 1.0](LICENSE). Every listed plugin or theme keeps its own license in its own repository.
