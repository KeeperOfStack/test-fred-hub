# Test Fred Hub — Human Developer Guide

A self-hosted dashboard for managing modded/vanilla Minecraft servers running in
`itzg/minecraft-server` Docker containers.  Plugin updates, scheduled restarts,
multi-source plugin search, per-server catalogs, and transparent support for
hybrid loaders (Arclight, Forge, Fabric, Paper, Purpur, Velocity, etc.).

This document tells **you** how to stand up the project from a bare machine,
how the pieces fit together, and what to avoid.

---

## 1. What you're building

A FastAPI backend + vanilla-JS frontend served as static files.  No build step,
no bundler, no framework — just `index.html` + `app.js` + `style.css`.

```
┌────────────────────────────────────────────────────────────────────┐
│ Browser (Minecraft-launcher themed UI)                             │
│   index.html ──► app.js ──► style.css                              │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ HTTP / SSE
┌──────────────────────────────▼─────────────────────────────────────┐
│ FastAPI (app/main.py)                                              │
│   ├─ servers.py      ── tracked-server registry + compose paths    │
│   ├─ compose.py      ── docker-compose YAML read/write             │
│   ├─ server_types.py ── per-type version sources (Paper, Forge…)   │
│   ├─ custom_jar.py   ── Arclight build CDN + jar download cache    │
│   ├─ registry.py     ── built-in plugin catalog                    │
│   ├─ resolvers.py    ── Modrinth/Hangar/Spiget/Geyser lookup       │
│   ├─ plugin_inspect.py ── parse plugin.yml/paper-plugin.yml/MANIFEST│
│   ├─ scheduler.py    ── one-shot + recurring restart queue         │
│   └─ audit.py        ── append-only JSONL audit log per server     │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ docker CLI + filesystem
┌──────────────────────────────▼─────────────────────────────────────┐
│ itzg/minecraft-server containers (one per tracked server)          │
│   /media/Minecraft/<server-name>/  ← bind-mounted world data       │
│   ~/docker-composes/<server>.yaml  ← compose file (edited in-place)│
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. Prerequisites

| Thing | Version | Why |
|---|---|---|
| Linux | Any modern distro | Tested on Ubuntu 24.04+ |
| Python | 3.11+ | FastAPI + type hints |
| Docker + compose plugin | Recent | `docker compose -f … up -d` |
| `itzg/minecraft-server` image | latest | The actual server runtime |
| World data dir | writable | e.g. `/media/Minecraft/<name>/` |
| Compose dir | writable | e.g. `~/docker-composes/<name>.yaml` |

The user the hub runs as must be in the `docker` group so it can call
`docker ps`, `docker compose`, `docker cp`, etc.

---

## 3. From-scratch install

```bash
# 1. Clone
git clone https://github.com/KeeperOfStack/test-fred-hub.git
cd test-fred-hub

# 2. Python venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Run
./run.sh          # binds 0.0.0.0:8765
```

Open `http://<host>:8765` in a browser.  The first time the UI loads with no
tracked servers, click the **`+`** button in the sidebar to add one — point it
at an existing compose file path **or** create a new server from a curated
template (Paper/Purpur/Folia/Forge/Fabric/Velocity/Arclight/…).

---

## 4. Project layout

```
test-fred-hub/
├── app/                     # FastAPI backend (Python)
│   ├── main.py              # ALL HTTP routes (~2900 lines, intentionally flat)
│   ├── servers.py           # tracked-server registry (~/.hermes/test-fred-hub/servers.json)
│   ├── compose.py           # docker-compose YAML round-trip + EDITABLE_ENV allowlist
│   ├── server_types.py      # per-type version pickers + Java runtime recommendation
│   ├── custom_jar.py        # Arclight build CDN client + 10-min build-list cache
│   ├── registry.py          # built-in plugin catalog (the "test-fred" seed)
│   ├── resolvers.py         # Modrinth/Hangar/Spiget/Geyser/PaperMC search + version lookup
│   ├── plugin_inspect.py    # parse plugin.yml, paper-plugin.yml, MANIFEST.MF from .jar
│   ├── scheduler.py         # one-shot + recurring restart queue, persistent across reloads
│   └── audit.py             # append-only JSONL audit log per server
├── static/                  # Frontend (vanilla — no build step)
│   ├── index.html           # Markup + skeleton, no logic
│   ├── app.js               # ~3000 lines of vanilla JS, one big file by design
│   └── style.css            # Minecraft-launcher palette (emerald + dirt + gold)
├── data/                    # ── GITIGNORED ── runtime state (caches, catalogs, schedules)
├── logs/                    # ── GITIGNORED ── uvicorn / app logs
├── developer/               # this folder (HUMAN.md + AI.md)
├── requirements.txt
├── run.sh                   # `exec uvicorn app.main:app --host 0.0.0.0 --port 8765`
└── README.md
```

### Why one giant `main.py` and one giant `app.js`?

Both files grew organically and stayed flat on purpose.  Search-with-grep and
"jump to function" beat any folder hierarchy at this size for a single-author
project.  If `main.py` ever crosses ~4000 lines, split it by **resource**
(plugins, servers, scheduler, search) — never by HTTP verb.

---

## 5. Data model

### Where state lives

| Path | Owner | What |
|---|---|---|
| `~/.hermes/test-fred-hub/servers.json` | `servers.py` | Tracked servers (id, display, compose path) — chmod 600 |
| `data/catalog-<sid>.json` | `main.py` | Per-server plugin catalog (auto-seeded from `registry.py` on first read for `test-fred`, from a smaller default elsewhere) |
| `data/cache-<sid>.json` | `main.py` | Last "Check for Updates" results — read-through cache |
| `data/staged-<sid>.json` | `main.py` | Pending plugin updates (filename → new jar URL + checksum) |
| `data/staged-deletions-<sid>.json` | `main.py` | Plugin files marked for delete-on-restart |
| `data/premium-user-<sid>.json` | `main.py` | User-added premium (Spiget) catalog entries |
| `data/scheduled-restart-<sid>.json` | `scheduler.py` | One-shot + recurring restart schedule |
| `data/mappings.json` | resolvers | Filename → upstream project_id (sha-512 cache hits) |
| `data/audit-<sid>.jsonl` | `audit.py` | Append-only audit log |

**None of `data/` should be committed.**  Catalogs reseed from
`app/registry.py` (which is in-repo) on first read for a new server.

### Compose file conventions

Each tracked server points at one compose file (e.g.
`~/docker-composes/king01.yaml`).  The hub edits it in place — but only
modifies environment variables listed in `compose.EDITABLE_ENV`.  Extra
variables you add by hand are preserved verbatim.

Hub-specific markers (start with `HUB_LOADER_*`) live in the env block and
let the backend remember the *logical* server type even when `TYPE=CUSTOM`
is required under the hood (Arclight uses this — see §7).

---

## 6. The plugin update flow

1. **Check** — `POST /api/plugins/check` walks every `.jar` in `plugins/`,
   parses plugin metadata with `plugin_inspect.py`, then asks each upstream
   resolver (Modrinth → Hangar → Spiget → Geyser) "do you have this?".
   First match wins; ties broken by the order in `resolvers.RESOLVERS`.
2. **Stage** — `POST /api/plugins/<filename>/stage-update` downloads the new
   jar to `plugins/update/` (Paper's official side-load convention) and
   records it in `staged-<sid>.json`.
3. **Apply** — happens automatically the next time the server restarts.
   Paper sees `plugins/update/foo.jar`, atomically swaps it in over
   `plugins/foo.jar`, deletes the source.  Zero clobbering of running jars.

Premium (Spigot paid) plugins are tracked in `premium-user-<sid>.json` but
**never auto-downloaded** — Spiget can't deliver the .jar without a paid
session.  The UI just tells you when a new version released so you can buy + upload.

---

## 7. Hybrid loader handling (Arclight)

The `itzg/minecraft-server` image doesn't have first-class support for
modern Arclight snapshots.  We use `TYPE=CUSTOM` + `CUSTOM_SERVER=/data/<jar>`
under the hood but expose **Arclight** as a first-class type in the UI:

```
UI says            Backend writes to compose.yaml
─────────────────  ───────────────────────────────────────────
TYPE: Arclight  →  TYPE=CUSTOM
                   CUSTOM_SERVER=/data/arclight-forge-…-<tag>.jar
                   HUB_LOADER_TYPE=ARCLIGHT
                   HUB_LOADER_SUBTYPE=forge
                   HUB_LOADER_CHANNEL=snapshot
                   HUB_LOADER_TAG=0769551
                   HUB_LOADER_JAR=arclight-forge-…-<tag>.jar
```

When the user clicks Save & Recreate:
1. `_ensure_loader_jar()` runs in `main.py`
2. Uses `custom_jar.list_builds()` to confirm the tag still exists on
   `arclight.izzel.io` (the CDN — NOT GitHub releases, which are stale)
3. Streams the .jar into `<server-dir>/arclight-…-<tag>.jar` atomically
4. Sets the `:java21` image tag (Arclight 1.20.5+ does NOT work on Java 25
   — Mixin libs lag Mojang Java bumps)
5. `docker compose -f <path> rm -fsv && up -d` rebuilds the container

The Forge subtype, channel (stable/snapshot), and tag are picked in the
**Server tab** Arclight picker (custom save route, not the generic
`/api/server/config` route — see `saveArclightConfig` in `app.js`).

**Working Arclight builds at time of writing:**
Forge 1.21.1 snapshot `0769551` (May 9 2026) — the GitHub-released stable
1.0.1 is broken; only the CDN snapshots have the mixin fixes.

---

## 8. Java runtime selection

`server_types._java_recommendation(mc_version)` is the single source of
truth.  Mapping (subject to change as Mojang releases):

| MC version range | Java | itzg image tag |
|---|---|---|
| ≤ 1.16.5 | 8 | `:java8` or `:java8-multiarch` |
| 1.17 – 1.20.4 | 17 | `:java17` |
| 1.20.5 – 1.21.x | 21 | `:java21` |
| 1.22+ (current latest) | 25 | `:latest` |

**Never use `:latest` for hybrid loaders** (Arclight, Mohist, etc.) — pin
to the explicit `:java21` (or whatever the Mixin libs support).

---

## 9. The frontend, briefly

`static/app.js` is 3000 lines of vanilla JS.  No framework.  Conventions:

- **Functions named by tab**: `renderPluginsList()`, `renderRegistry()`,
  `renderSearchResults()`, etc.  Each tab has its own render function that
  takes no args and reads from module-level state.
- **State is module-level**: `_searchLastHits`, `_currentServerId`,
  `_pluginsCache`, etc.  No Redux, no observables, no signals.
- **API helper**: `api(path, opts)` wraps `fetch` with JSON encoding +
  toast on error.  Use it for every backend call.
- **Toasts**: `toast("msg", "ok"|"warn"|"err")` — see `#toast-container`.
- **Cache-bust**: every time you change `app.js` or `style.css`, bump the
  `?v=N` in `index.html`.  Browsers cache aggressively.
- **All cards use the same layout idiom**: 3px dark gradient panel,
  3px box-shadow, 5px colored left edge, `48px 1fr` inner grid.  See
  `.plugin`, `.reg-item`, `.search-hit` — all parallel.

---

## 10. Common dev workflows

```bash
# Quick edit-restart loop (uvicorn doesn't auto-reload static files, so a
# hard refresh after bumping ?v= is usually enough)
./run.sh

# When changing Python — Ctrl-C and restart
./run.sh

# When changing static — just refresh, but bump ?v= in index.html or
# the browser will serve the old version

# Tail the logs
tail -f logs/uvicorn.log

# Run against a real container WITHOUT touching it accidentally
# (the hub will only docker-compose-recreate when YOU click Recreate)
```

### Editing the compose file by hand

Safe.  The hub reads on demand and only writes the keys in
`compose.EDITABLE_ENV`.  Extra env, networks, volumes, depends_on, etc.
all round-trip untouched.

### Adding a new plugin source

1. Add a class to `resolvers.py` implementing `search()` + `latest_for()`
   + `download_url_for()`
2. Append it to `RESOLVERS` (order = priority)
3. Add a CSS color class `.source.<name>` in `style.css`
4. Bump `?v=` on the CSS

### Adding a new server type

1. Add to `server_types.SERVER_TYPES`
2. Implement a `latest_for()` and `extra_fields()` (the latter drives the
   per-type UI fields in the Add Server modal + Server tab)
3. Hook it into `_java_recommendation()` if Java requirements differ
4. If the type uses a CDN-supplied jar instead of itzg's built-in resolver,
   route it through the `_ensure_loader_jar` pattern like Arclight

---

## 11. Pitfalls (the things we learned the hard way)

1. **`docker compose stop` + `up -d` reuses the existing container** —
   env vars from the old run-config win.  To pick up compose-file edits
   you MUST `docker compose rm -fsv && up -d` (or use the hub's "Recreate"
   button which does exactly that).

2. **`:latest` for hybrid loaders breaks** when Mojang bumps Java
   ahead of the Mixin ecosystem.  Always pin to an explicit Java image.

3. **Arclight stable releases on GitHub are stale.**  Use
   `arclight.izzel.io/arclight/minecraft/<mc>/loaders/<loader>/versions-{stable,snapshot}/`
   instead.  GitHub releases only have broken 1.0.1.

4. **Browsers cache static files aggressively.**  Always bump `?v=N` on
   `app.js` and `style.css` after edits.

5. **`servers.json` is in `~/.hermes/test-fred-hub/`, not the repo.**  It's
   a runtime config (paths to compose files on the host), not source.

6. **The test-fred catalog gets seeded once on first read and is then
   user-owned.**  Don't auto-regenerate it — users can edit catalog
   entries from the UI and those edits must survive backend restarts.

7. **Premium plugins can never be auto-downloaded.**  Spiget API
   doesn't serve paid-resource binaries.  The UI surfaces "new version
   available" + a buy-and-upload prompt; that's the most you can do.

8. **`plugins/update/` is Paper's official side-load directory.**  Use it
   to stage updates — Paper swaps jars atomically on restart with zero
   clobbering of running JVM jar handles.

---

## 12. License

MIT — see `LICENSE`.

## 13. Acknowledgements

- [itzg/minecraft-server](https://github.com/itzg/docker-minecraft-server) — the container image that makes this whole thing possible
- [Arclight](https://github.com/IzzelAliz/Arclight) — hybrid Bukkit/Forge loader
- [PaperMC](https://papermc.io/), [Modrinth](https://modrinth.com/), [Hangar](https://hangar.papermc.io/), [Spiget](https://spiget.org/), [Geyser](https://geysermc.org/) — the plugin upstreams we resolve against
