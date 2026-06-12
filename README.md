# Test Fred Hub

> A self-hosted dashboard for managing `itzg/minecraft-server` Docker containers — plugin updates, scheduled restarts, multi-source search, per-server catalogs, and transparent support for hybrid loaders (Paper, Purpur, Folia, Forge, NeoForge, Fabric, Quilt, Velocity, Arclight, and more).

![status](https://img.shields.io/badge/status-active-emerald) ![license](https://img.shields.io/badge/license-MIT-blue) ![python](https://img.shields.io/badge/python-3.11%2B-3776ab) ![framework](https://img.shields.io/badge/api-FastAPI-009688) ![ui](https://img.shields.io/badge/ui-vanilla%20JS-yellow)

---

## What it does

- **🧩 Plugin updates** with multi-source resolution: Modrinth → Hangar (PaperMC) → Spiget (SpigotMC) → Geyser official.  Stages updates through Paper's official `plugins/update/` side-load directory — zero clobbering of running jars.
- **📦 Server updates** across every type — Paper/Purpur/Folia (PaperMC fill API), Forge/NeoForge/Fabric/Quilt (vendor APIs), Arclight (CDN snapshots), Velocity, Limbo, Leaf, and more.
- **⏰ Scheduled restarts** — one-shot ("apply pending updates at 4 AM") and recurring ("every Sunday 06:00").  Persistent across hub restarts.
- **🌿 Per-server catalogs** — curated plugin list with sources, edit/remove/install, premium-plugin tracking.
- **🔍 Concurrent multi-upstream search** — query Modrinth, Hangar, and Spiget in parallel.  Toggle premium-only.
- **⚙ Type-aware Java runtime** — image tag picked automatically from the MC version (Java 8/17/21/25).  Hybrid loaders pinned to compatible Java.
- **📜 Live console** over SSE.
- **🧱 Multi-server** — track and switch between any number of compose files.

## Stack

- **Backend**: FastAPI (Python 3.11+).  ~5500 LOC across 9 modules.
- **Frontend**: vanilla JS + vanilla CSS.  No framework, no build step.  Minecraft-launcher palette.
- **Runtime**: `itzg/minecraft-server` Docker containers.  The hub edits each server's compose file in-place and recreates the container on demand.

## Quick start

```bash
git clone https://github.com/KeeperOfStack/test-fred-hub.git
cd test-fred-hub
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh                # serves on http://0.0.0.0:8765
```

Open the URL, click **`+`** in the sidebar to track an existing compose file or create a new server from a curated template.

## Requirements

- Linux (tested on Ubuntu 24.04+)
- Python 3.11+
- Docker + compose plugin
- The user running the hub must be in the `docker` group
- Bind-mounted world data directory (e.g. `/media/Minecraft/<server>/`)
- Compose files (e.g. `~/docker-composes/<server>.yaml`)

## Documentation

| Audience | File |
|---|---|
| Setting up + extending as a human dev | [`developer/HUMAN.md`](developer/HUMAN.md) |
| Picking the project up cold as an AI agent | [`developer/AI.md`](developer/AI.md) |

The two docs cover: architecture, file responsibilities, data model, plugin/server-type extension checklists, frontend/backend conventions, and ~16 hard-earned pitfalls.

## Project layout

```
test-fred-hub/
├── app/           # FastAPI backend (one route file, flat by design)
├── static/        # index.html + app.js + style.css (no build step)
├── developer/     # HUMAN.md + AI.md handoff docs
├── data/          # ── gitignored ── per-server caches, catalogs, schedules
├── logs/          # ── gitignored ── uvicorn / app logs
├── requirements.txt
├── run.sh
└── README.md
```

## ⚖️ Licensing & Commercial Use

This project is **dual-licensed** to ensure the code remains open-source while protecting the project's development.

### 1. Open Source Use (AGPLv3)
By default, this repository is licensed under the **GNU Affero General Public License v3.0**. You are free to fork, modify, and use this software, provided that any derivative works or cloud services utilizing this code are also open-sourced under the same AGPLv3 terms.

### 2. Commercial Licensing
If you or your company wish to use this software in a proprietary, closed-source environment (or within a system where you cannot comply with the AGPLv3 open-source requirements), you must obtain a commercial exception license.

To inquire about purchasing a commercial license, please **[open a new GitHub Issue here](../../issues/new?title=Commercial+License+Inquiry)** using the title "Commercial License Inquiry."

## Acknowledgements

- [itzg/minecraft-server](https://github.com/itzg/docker-minecraft-server) — the container image that does the heavy lifting
- [PaperMC](https://papermc.io/), [Modrinth](https://modrinth.com/), [Hangar](https://hangar.papermc.io/), [Spiget](https://spiget.org/), [Geyser](https://geysermc.org/) — plugin and server upstreams
- [Arclight](https://github.com/IzzelAliz/Arclight) — hybrid Bukkit/Forge loader
