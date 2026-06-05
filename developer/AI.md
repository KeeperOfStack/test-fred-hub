# Test Fred Hub — AI Agent Handoff

If you are an LLM agent picking this project up cold, read this top to bottom
before touching code.  It is dense on purpose.  The pitfalls section captures
mistakes that cost the original team hours.

---

## TL;DR

- **Stack**: FastAPI (Python 3.11+) + vanilla JS + vanilla CSS.  No build step.
- **Purpose**: Dashboard for managing `itzg/minecraft-server` Docker containers
  (plugin updates, server-type switching, restart scheduling, multi-source
  plugin search, hybrid loader support).
- **Surface**: One Python entrypoint (`uvicorn app.main:app`), one HTML page,
  one JS file, one CSS file.  All deliberate.
- **External services**: Modrinth, Hangar (PaperMC), Spiget (SpigotMC),
  Geyser official, PaperMC fill API, `arclight.izzel.io` CDN.  All read-only,
  unauthenticated.

---

## File-by-file responsibility (no guessing)

| File | LOC | Owns |
|---|---|---|
| `app/main.py` | ~2900 | All FastAPI routes.  Flat by design — search by route path. |
| `app/servers.py` | ~220 | Tracked-server registry. State at `~/.hermes/test-fred-hub/servers.json` (chmod 600). |
| `app/compose.py` | ~200 | docker-compose YAML read/write. `EDITABLE_ENV` is the **allowlist** — anything not in it round-trips untouched. |
| `app/server_types.py` | ~500 | Per-type metadata + `current_for_type()` + `latest_for_type()` + `_java_recommendation()`. |
| `app/custom_jar.py` | ~300 | Arclight CDN client (`arclight.izzel.io`). 10-min in-process cache for build lists. Atomic streamed downloads. |
| `app/registry.py` | ~200 | The built-in plugin catalog (the "test-fred" seed).  In-repo data, not runtime state. |
| `app/resolvers.py` | ~530 | Multi-upstream plugin resolution.  Each upstream is a class with `search()` + `latest_for()` + `download_url_for()`. |
| `app/plugin_inspect.py` | ~190 | Parse `plugin.yml`, `paper-plugin.yml`, `bungee.yml`, `velocity-plugin.json`, `MANIFEST.MF` from a .jar. |
| `app/scheduler.py` | ~360 | One-shot + recurring restart queue, persisted per-server. |
| `app/audit.py` | ~130 | Append-only JSONL audit log per server. |
| `static/index.html` | ~350 | Markup only.  No logic. |
| `static/app.js` | ~3000 | All frontend logic.  Vanilla.  Module-level state, no framework. |
| `static/style.css` | ~1300 | Minecraft-launcher palette.  All cards share the same idiom. |

There are **55 HTTP routes** (`grep '@app\.' app/main.py | wc -l`).  Pattern:
`GET` reads, `POST` mutates compose/state, `DELETE` removes.  Server-id is
either a query param (`?sid=`) or a path param (`/api/servers/{sid}`).

---

## Conceptual model

```
┌─ Tracked Server (servers.json) ────────────────┐
│  id, display, compose_path                     │
│         │                                      │
│         ▼                                      │
│  ~/docker-composes/<id>.yaml  (compose file)   │
│         │                                      │
│         ▼                                      │
│  /media/Minecraft/<id>/        (bind-mounted)  │
│      ├── plugins/               (live jars)    │
│      ├── plugins/update/        (staged for next restart) │
│      ├── world/, etc.                          │
│      └── arclight-…-<tag>.jar   (for CUSTOM types) │
│                                                │
│  data/catalog-<id>.json    (per-server catalog) │
│  data/cache-<id>.json      (last check result) │
│  data/staged-<id>.json     (pending updates)   │
│  data/scheduled-restart-<id>.json  (queue)     │
└────────────────────────────────────────────────┘
```

A "server" is a tuple of (logical id, compose file path).  The hub never
holds the world data — that's in the bind-mount.  Re-tracking the same
compose path on a fresh hub install rebinds everything.

---

## The "HUB_LOADER_*" markers (don't strip these)

When the actual `TYPE=` in the compose env can't match the logical server
type (Arclight → `TYPE=CUSTOM`), the hub stores the logical type/tag in
custom marker vars:

```yaml
environment:
  TYPE: CUSTOM
  CUSTOM_SERVER: /data/arclight-forge-1.21.1-1.0.2-SNAPSHOT-0769551.jar
  HUB_LOADER_TYPE: ARCLIGHT
  HUB_LOADER_SUBTYPE: forge
  HUB_LOADER_CHANNEL: snapshot
  HUB_LOADER_TAG: 0769551
  HUB_LOADER_JAR: arclight-forge-1.21.1-1.0.2-SNAPSHOT-0769551.jar
  HUB_LOADER_VERSION: 1.21.1
```

`compose.EDITABLE_ENV` includes all six.  `server_types.current_for_type()`
reads them for `CUSTOM`-typed compose files to recover the logical type.
If you add a new "transparent" type (e.g. Mohist via CUSTOM), follow this
same pattern — do NOT add new code paths.

---

## Plugin update flow (the side-load contract)

`itzg/minecraft-server` runs Paper/Purpur/etc.  Paper supports an official
side-load directory: drop a jar at `plugins/update/foo.jar` and Paper
atomically swaps it over `plugins/foo.jar` on the next restart.  This is the
ONLY safe way to update — overwriting a loaded .jar would corrupt the JVM's
zip handle.

Flow:
1. **Check**: walk `plugins/`, parse each jar via `plugin_inspect.extract_metadata()`.
2. **Resolve**: for each plugin, try each `resolvers.RESOLVERS` entry in order
   until one returns a `LatestRelease`.  Cache resolver hits in `mappings.json`
   keyed by exact filename + sha512.
3. **Stage**: `POST /api/plugins/<filename>/stage-update` downloads the new jar
   into `plugins/update/` (via `docker cp` or direct bind-mount write —
   whichever is cleaner) and adds it to `staged-<sid>.json`.
4. **Apply**: happens implicitly on restart.  No active job — Paper does it.
5. **Verify**: on next "Check for Updates" the staged file is gone (Paper ate
   it) and the live version reflects the new tag.

Premium plugins (Spigot paid resources) get tracked in `premium-user-<sid>.json`
but can never be auto-downloaded — Spiget's API only serves binaries for free
resources.  Show "new version available" + a link, never pretend you can install.

---

## Adding a new server type — checklist

1. **`server_types.SERVER_TYPES`** — add the dict entry (id, display,
   description, supported MC versions, java_pref).
2. **`latest_for_type()`** — branch for the new type, query the appropriate
   upstream, return a `(version, source_url, notes)` tuple.
3. **`current_for_type()`** — branch reading the appropriate env vars from
   the parsed compose (probably `VERSION` + something type-specific).
4. **`extra_fields()`** — return the per-type form-field schema (kind, label,
   choices) so `app.js` renders the right Server-tab and Add-Server inputs.
5. **`_java_recommendation()`** — if this type pins a specific Java version
   regardless of MC version, branch here.
6. **`compose.EDITABLE_ENV`** — add any new env keys you write.
7. **`app.js` `renderTypeFields()`** — handler for the new `extra_fields` kinds
   if you introduced one.  Reuse existing kinds when possible.
8. **`app.js` Add Server modal `renderExtras()` + `collect()`** — mirror the
   same.
9. If the type needs a CDN-supplied jar (no itzg auto-deploy support), copy
   the Arclight pattern: `_ensure_loader_jar()`, `/api/server/<type>/{versions,builds,install,status}` routes, `HUB_LOADER_*` markers, transparent `TYPE=CUSTOM` translation.

---

## Adding a new plugin upstream — checklist

1. New class in `resolvers.py` with:
   - `name: str` (lowercase, matches CSS class `.source.<name>`)
   - `async search(query: str, mc_version: str) -> list[SearchHit]`
   - `async latest_for(ref: str, mc_version: str) -> LatestRelease | None`
   - `async download_url_for(ref: str, version: str) -> str | None`
2. Append to `RESOLVERS` (order = priority).
3. Add a CSS class `.source.<name> { background: …; color: …; }` to `style.css`.
4. Bump `?v=` on the CSS link in `index.html`.
5. Add a row to the About tab's source list (`index.html`).

---

## Frontend conventions (mandatory)

- **State is module-level vars** in `app.js`, prefixed with `_`
  (`_searchLastHits`, `_currentServerId`, `_pluginsCache`, etc.).  No store, no
  framework.
- **API helper**: `api(path, opts)` — always use it, never raw `fetch()`.
  It JSON-encodes bodies and toasts on errors.
- **Toasts**: `toast(msg, "ok"|"warn"|"err")`.  All user feedback goes through
  this.
- **Render functions** are named `render<Tab>()` and take no args.  Re-render
  is idempotent.
- **Cards** (`.plugin`, `.reg-item`, `.search-hit`) share the same visual
  idiom: dark vertical gradient, 3px border, 3px hard box-shadow, 5px colored
  left edge, `48px 1fr` icon-and-body inner grid.  Match this for new card
  types.
- **Cache-bust** by bumping `?v=N` on `app.js` and `style.css` in `index.html`
  after every change.  Always.  Browsers cache aggressively.
- **Never put JSON in HTML attributes** — escape with
  `.replace(/'/g, "&#39;")` or use `dataset` + JSON.parse on demand.

---

## Backend conventions (mandatory)

- **`async` everywhere** in `main.py`.  Even handlers that don't await.
- **HTTPException + clear message** on user-facing errors.  Don't return
  `{"error": "..."}` dicts.
- **`docker compose -f <path> …`** — always pass the explicit path, never
  rely on cwd.  Quote the path.
- **Atomic writes** for any data/ JSON file: write to `<file>.tmp`, then
  `os.replace()`.  Half-written JSON corrupts state.
- **Don't recreate containers without explicit user consent.**  Auto-recreate
  is a footgun.  Every recreate goes through `/api/server/recreate` which is
  an explicit click.
- **Always use `_java_recommendation()`** for image tag selection.  Single
  source of truth.

---

## Pitfalls (read this — these cost time)

### Container lifecycle

1. **`docker compose stop` + `up -d` REUSES the existing container.**
   Environment changes in the compose file are ignored — the container keeps
   the env it was created with.  To pick up edits, do `rm -fsv && up -d`.
   This was the #1 bug in early development.

2. **Custom env vars (anything beyond `EDITABLE_ENV`) must round-trip
   untouched.**  Compose-file edits made outside the hub must not be lost.
   Test by adding a custom env line by hand and re-saving via the UI.

### Java + image tag

3. **Never use `:latest` for hybrid loaders.**  When Mojang ships a Java
   bump (e.g. 21 → 25), Mixin libraries lag by months.  Arclight, Mohist,
   etc. need explicit `:java21` (or whatever Mixin currently supports).
   `_java_recommendation()` handles vanilla / Paper correctly — hybrid
   loaders override.

4. **Java mapping changes over time.**  Re-verify when you touch a new MC
   version.  Mojang's required-Java table is the ground truth.

### Arclight + similar

5. **Arclight GitHub releases are stale.**  Only `arclight.izzel.io` CDN
   has working snapshots.  See `custom_jar.list_builds()`.  Tested working:
   forge 1.21.1 snapshot `0769551`.

6. **`TYPE=CUSTOM` + `CUSTOM_SERVER=` is the only path** for Arclight under
   itzg.  itzg's `TYPE=ARCLIGHT` exists but its auto-deploy is broken for
   modern versions.  Keep the transparent translation.

### Plugins

7. **`plugins/update/` is Paper's side-load convention.**  Don't overwrite
   live jars.  Paper swaps atomically on restart.

8. **Premium = read-only.**  Spiget API will not serve paid-resource binaries
   no matter what you do.  Never imply install is possible.

9. **`plugin_inspect.extract_metadata()` must handle malformed jars.**  Real
   Minecraft plugins have surprising contents (no plugin.yml, multiple
   manifests, nested archives).  Catch everything and degrade to filename
   parsing.

10. **`mappings.json` is keyed by exact filename + sha512.**  If a plugin
    auto-updates its own version string in the filename, the cache misses
    and you re-resolve.  This is fine — the resolver will hit Modrinth's
    sha-512 hash lookup and recover.

### Frontend

11. **Always bump `?v=` on static assets.**  Browsers cache `app.js` and
    `style.css` for hours.

12. **Don't put inline `style="display:flex;…"` overrides on classed
    elements.**  They beat the CSS file and break responsive layouts.
    Add a CSS class instead.

13. **The Server-tab form is a single big `.config-grid`.**  Don't fight
    it — use `data-section` + `::before` to add visual sections without
    breaking the grid.

### Operational

14. **`servers.json` is in `~/.hermes/test-fred-hub/`, not the repo.**  This
    is a feature — different machines have different paths to compose files.

15. **`data/` directory is per-install state.**  Catalogs reseed from
    `app/registry.py` (which IS in the repo) on first read of a new server-id.

16. **Audit logs append-only, never rotate in code.**  Use logrotate or `mv`
    out of band if they grow.

---

## What NOT to "improve"

These have been considered and explicitly rejected:

- **Don't split `main.py` into one file per route group.**  Search-with-grep
  beats folder hierarchy at this size.  Split only when crossing ~4000 lines,
  and split by resource (plugins/, servers/), never by HTTP verb.
- **Don't add a frontend framework.**  Vanilla JS is a feature.  3000 LOC of
  vanilla beats 6000 LOC of React + setup.
- **Don't add a build step.**  No webpack, no vite, no TS.  Direct edits.
- **Don't bundle CSS.**  One file.  Find with grep.
- **Don't add a database.**  Per-server JSON files are the right granularity
  — easy to back up, easy to inspect, easy to delete one server cleanly.
- **Don't auto-recreate containers.**  Every recreate is an explicit user
  click.

---

## When you have a question the docs don't answer

Order of operations:
1. `grep -rn "<term>"` in `app/`, then `static/`.
2. Check existing patterns — most "how do I do X" has a sibling pattern
   already shipped (e.g. "how do I add a server type?" → look at how
   Arclight does it).
3. Read the audit log: `tail -n 50 data/audit-<sid>.jsonl` for ground truth
   on what the running system actually did.
4. Check the container itself: `docker inspect <name>` for runtime env vs
   `cat ~/docker-composes/<name>.yaml` for desired-state env.  Mismatch =
   container needs `rm -fsv && up -d`.

---

## License

MIT.  Contributions welcome but stay in-idiom — vanilla JS, flat Python,
no build step.
