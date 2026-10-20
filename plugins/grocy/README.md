# Grocy for Familienhub

Connects the self-hosted [Grocy](https://grocy.info) to [Familienhub](https://github.com/Brottuetchen/familienhub): Grocy can be the hub's **shopping list**, **Today** shows food that is about to expire, and parents get **one push per product** before it goes off.

Requires Familienhub **1.0.0-beta.3** or newer (the plugin hooks it uses do not exist before). On an older hub the plugin shows as not set up and does nothing; the installer refuses it.

## What it does

- **Shopping list (capability `shopping`).** Choose Grocy under *More → Administration → Sources per area → Shopping*. From then on everything in the hub uses Grocy's list: the shopping screen, template lists ("apply to shopping list"), recipe ingredients (also the week's list) and voice ("add milk"). Without that choice the plugin's list is not used.
  - Reading: items of the configured Grocy list. Product entries show the product name and the amount with its unit (`2 Bottles`); free-text entries use Grocy's *note*. A checked entry is `completed`.
  - Adding: if the name matches a Grocy product (case-insensitive, active products only) the entry is a product entry, otherwise free text. A plain number as description is the amount (`2`); any other description (`500 g`) is kept in the note. An entry that is already open is not added twice; one that is already checked is unchecked instead.
  - Check/uncheck, delete, and renaming of free-text entries (so equal recipe ingredients can merge).
- **Today card "Grocy" / "Vorräte".** `Expiring soon: Yoghurt (tomorrow), Milk (2 days)`, `Past date: …`, `Missing: 3 products`, with a badge. No card when nothing is relevant or Grocy is unreachable. Text language follows the person's app language (German or English).
- **Daily hint for parents.** Every 30 minutes the hub runs a job; from `GROCY_NOTIFY_HOUR` on it looks for products due within `GROCY_DUE_DAYS` days and sends **one push to admins and partners** per product and due date (category *Plugins* in the notification settings, which each person can switch off). Already announced products are remembered in `<DATA_DIR>/grocy_state.json`, so nothing is announced twice.
- **Optional: missing products on the list.** With `GROCY_ADD_MISSING` the job calls Grocy's "add missing products to shopping list" once a day.

## Create the API key in Grocy

Grocy → the gear icon → **Manage API keys** → **Add** → copy the key into the hub. The key has the access of the Grocy user who created it, so use a user that may edit the shopping list.

## Settings

| Key | Type | Default | Meaning |
|---|---|---|---|
| `GROCY_URL` | url, required | | Address of Grocy as the hub reaches it, e.g. `http://grocy.lan:9283`, without `/api` |
| `GROCY_API_KEY` | secret, required | | The API key (stored encrypted, never shown again) |
| `GROCY_SHOPPING_LIST_ID` | number | 1 | Which Grocy shopping list the hub uses (1 is the default list) |
| `GROCY_DUE_DAYS` | number | 2 | "Expiring soon" means due within this many days (0-365) |
| `GROCY_NOTIFY` | bool | on | Send the daily hint to parents |
| `GROCY_NOTIFY_HOUR` | number 0-23 | 8 | The hint is sent at the first job run from this hour on (hub local time) |
| `GROCY_ADD_MISSING` | bool | off | Put missing products on the list once a day |

**Test connection** (install and settings screen) checks the typed, not yet saved values: `GET /api/system/info` with the key (`Connected, Grocy 4.x`) and whether the shopping list with that ID exists.

## Hosts

The manifest's `hosts` list is **empty on purpose**: Grocy runs wherever you host it, so the plugin contacts **only the address you enter in `GROCY_URL`** and nothing else (no telemetry, no other service). The hub does not enforce `hosts`; it is information for the review.

## Data it reads and writes

Reads from Grocy: system info (version), shopping lists and their entries, products, quantity units, and the stock overview (`/api/stock/volatile`: due, overdue, expired and missing products). Household-wide data only; no per-person data, so one shared API key is fine.

Writes to Grocy: shopping list entries (create, check/uncheck, rename, delete) and, with `GROCY_ADD_MISSING`, "add missing products". It never changes stock, products or recipes.

Writes in the hub: `<DATA_DIR>/grocy_state.json` (which products were announced, and the date of the last "add missing" run). Pushes go through the hub's notification service. The API key is sent only in the `GROCY-API-KEY` header, never logged.

## Limitations

- Product entries are renamed by changing the product in Grocy, not through the hub.
- Everything is read as the single household, not per person. Entries are not marked with who added them.
- Product names are matched exactly (ignoring case); there is no fuzzy matching, so "Milch" does not find "Milk".
- Amount/unit display uses the unit on the entry, or the product's purchase unit when the amount is not 1.
- Today card and lists are cached for up to 60 seconds (products and units 5 minutes); a change in Grocy can show up that much later.
- The daily hint uses `GROCY_DUE_DAYS` and Grocy's own due date; products without a due date or "never expires" are ignored. The job runs every 30 minutes, so a hub that is down at `GROCY_NOTIFY_HOUR` sends later the same day.
- At most 20 plugin pushes per hour are allowed by the hub; the plugin sends one call per language group.
- Tested against simulated Grocy answers only, see below.

## What was tested

The pytest suite (`tests/`, 62 tests) runs the plugin against **simulated** Grocy answers (respx), written from Grocy's API documentation. **It has not been run against a real Grocy server.** The shapes of `/api/stock/volatile`, `/api/objects/shopping_list` and the error status codes for missing entries (404 assumed) should be confirmed on a real instance. Run the tests:

```
DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest
```

## Install

*More → Administration → Plugins & access → Install plugin* (or from the store), enter address and API key, press *Test connection*.

License: MIT
