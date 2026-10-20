# Tandoor Recipes for Familienhub

Adds your own [Tandoor Recipes](https://tandoor.dev) server as a recipe source of [Familienhub](https://github.com/Brottuetchen/familienhub). In the app it appears next to Mealie and Cookidoo as **Recipe from Tandoor**:

- **search** your Tandoor recipes (at least 3 letters) with thumbnails and cooking time,
- **quick view**: picture, servings, working plus waiting time, ingredients and steps, link to the recipe in Tandoor,
- **plan** a recipe on a day of the meal plan,
- **ingredients to the shopping list**.

Requires Familienhub **1.0.0-beta.3** or newer. On an older hub the plugin refuses to install; if it is added to an older hub in another way (custom image), it reports itself as not configured with the explanation and does nothing.

## Setup

1. In Tandoor open **Settings → API** and create a token (a name such as `Familienhub`; **read** scope is enough, the plugin never writes to Tandoor).
2. In Familienhub: **Administration → Store → Tandoor Recipes → Install** (or paste the repository link under *Plugins & access → Install plugin*), fill in the settings and use **Test connection**.

| Setting | Required | Meaning |
|---|---|---|
| `TANDOOR_URL` | yes | Address of Tandoor as the **hub** reaches it, e.g. `http://tandoor.lan:8080` (no `/api`; a trailing `/api` is removed). |
| `TANDOOR_TOKEN` | yes | The API token. Stored encrypted, never shown again. |

**Test connection** asks Tandoor for one recipe (`GET /api/recipe/?page_size=1`) with the values typed in, before they are saved, and answers for example "Connected, 42 recipes". It tells apart: not reachable, token rejected (401/403), no Tandoor API at that address, other errors.

## Hosts

The manifest lists **no fixed hosts** (`"hosts": []`) because the only host the plugin contacts is the Tandoor address *you* enter. It never contacts anything else: no telemetry, no third party. Image addresses that Tandoor returns are reduced to their path and requested from the configured address only.

## What it reads and writes

Reads (with the token as `Authorization: Bearer …`):

| Tandoor endpoint | Used for |
|---|---|
| `GET /api/recipe/?query=…&page_size=…` | search (id, name, image, working_time, waiting_time) |
| `GET /api/recipe/{id}/` | quick view, planning and shopping (steps, ingredients, servings) |
| `GET <image path>` (usually `/media/recipes/…`) | recipe pictures, through the hub |

It writes nothing to Tandoor. Planning is stored by the hub (`meal_plan`), like for Mealie. Credentials are the shared token of the household: Tandoor recipes are read by every family member who can use the meal planner.

### How recipes are mapped

- `minutes` = working time + waiting time (Tandoor's minutes).
- Ingredients: one line per ingredient of every step in step order, e.g. `200 ml cream`, `1.5 kg potatoes (waxy)`, `2 garlic cloves` (plural names are used when Tandoor has them and the amount is not 1), `salt (to taste)` for ingredients without an amount. Ingredient group headers are skipped.
- Steps: the instruction text of each step in order, whitespace collapsed; `{{ scale(40) }}` placeholders become the number. Recipes embedded as a step of another recipe are expanded (two levels).
- Link: `<TANDOOR_URL>/view/recipe/<id>` (the web UI).

### Images

Tandoor image addresses may be internal. The plugin serves them from its own authenticated hub route `/api/plugins/tandoor/image/<recipe id>` using the hub's `proxy_image` (type check by content, 5 MB limit, no redirects, caching headers). It is **not an open proxy**: only numeric recipe ids that the plugin returned in a search or recipe call in the last 7 days are served, and the URL is always the configured Tandoor address plus the image path Tandoor reported for that recipe. The token is only ever sent to that address.

### Shopping list

`recipe_to_shopping` is deliberately **not implemented**: the hub puts the ingredient lines on its own shopping list and merges equal ones, like for Mealie recipes. Tandoor's own shopping list is not touched (writing there would need a write-scope token and would split the family's list in two places).

### Failing softly

Every call has an 8 s timeout and redirects are not followed. A Tandoor that is down, slow or answering nonsense gives the app "not reachable" for that search or recipe (the hub turns the plugin's error into that status; no stack trace, no address, no token is logged or shown). Search results are cached for 60 s and recipes for 5 minutes in memory.

## Limitations

- Searching uses Tandoor's name search (`query`); it does not search ingredients or keywords.
- The image whitelist and caches are in memory. After a hub restart a planned recipe's thumbnail is empty until the recipe is searched or opened again.
- One Tandoor space: the space of the token's user is used. There is no space setting.
- Instructions are plain text (Tandoor's Markdown is not rendered; other template tags than `scale()` are left as they are).
- Not verified against a live Tandoor server, see below.

## Tested and untested

Tests (`pytest`, 56 cases) mock Tandoor with `respx` using the response shapes of Tandoor's API (paginated `count/results` list, recipe with `steps[].ingredients[]` with `food`, `unit`, `amount`, `note`, `is_header`, `no_amount`). They exercise the plugin together with the hub's own `PluginRecipes` adapter and `proxy_image`. **Not tested against a real Tandoor server or a running hub** (no such instance was available): the exact fields of your Tandoor version, whether `/media/` is public or needs the token on your setup, the installer round trip, and the app screens (those were rendered with a fake backend).

## Development

```
DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest
```

MIT license.
