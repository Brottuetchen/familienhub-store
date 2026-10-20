# Submitting an entry

1. **Publish** your plugin or theme in a public GitHub repository. Pick the exact commit you want to ship.
2. **Add one entry** to `index.json` (format: [docs/store.md](https://github.com/Brottuetchen/familienhub/blob/main/docs/store.md#index-format)):
   - `type` (`theme` or `plugin`), `id`, `name`, `summary` (de and en if you can), `author`, `repo`, `path` (folder inside your repository), `sha` (full 40-character commit), `version`.
   - For a plugin, `id` must equal the `id` in your `familienhub-plugin.json`.
3. **Add screenshots** under `screenshots/<id>/1.png`, `2.png`, … (PNG, JPEG or WebP, at most 2 MB each, at most 8). For themes, a light and a dark screenshot of the Today screen; the theme gallery has a [generator](https://github.com/Brottuetchen/familienhub-themes/tree/main/tools).
4. **Open a pull request.** CI checks the index with the hub's own parser and checks that every screenshot exists.

## What reviewers check

- The entry passes CI.
- The `sha` exists **on a branch of the listed repository**, not only in a fork: GitHub serves fork commits under the parent repository's URL.
- Themes: `theme.json` at that commit is valid and looks like the screenshots.
- Plugins:
  - The code at that commit does what the description says.
  - It declares the hosts it talks to. A plugin that only talks to an address the admin enters may leave `hosts` empty and says so in its description.
  - It has no obfuscated code, no telemetry without consent, and no unexpected network access.
  - Plugins run inside the hub process with full access, so this review is the main protection.
- Updates: the diff between the old and the new `sha`.

## Updating an entry

Open a pull request that changes `sha`, `version` and, if needed, the screenshots. The hub shows "Update available" to admins who installed the entry.

## Official plugins in this repository

The plugins under `plugins/<id>/` are maintained here. Their index entries use `repo` = this repository and `path` = `plugins/<id>`. `plugins/<id>/store-entry.json` is the draft of the entry (with `sha` `TBD`, checked by the plugin's own tests); the pinned `sha` lives only in `index.json`. A change to such a plugin is a pull request that changes the plugin and, after merge, a second one that moves its `sha` in `index.json` to the merged commit.
