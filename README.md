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
tools/validate.py             the check CI runs on every pull request
```

## Currently listed

All themes from the [theme gallery](https://github.com/Brottuetchen/familienhub-themes), each with a light and a dark screenshot. Plugins will follow as they are reviewed.

## Submitting a plugin or theme

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: publish your plugin or theme in a public GitHub repository, open a pull request here that adds one entry to `index.json` plus its screenshots, and wait for review.

## License

The catalogue (index and screenshots) is [CC0 1.0](LICENSE). Every listed plugin or theme keeps its own license in its own repository.
