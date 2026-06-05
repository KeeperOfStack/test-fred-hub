"""Test Fred Hub — Minecraft server + plugin management dashboard.

Multi-server-aware: every per-server handler resolves the active record
via `_srv()` and derives paths/container via `_paths(rec)`. Endpoints that
accept an explicit server id thread it through instead of using current.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import re
import shutil
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from . import (
    resolvers, registry, compose, server_types as st,
    servers, audit, plugin_inspect, scheduler as sched_mod,
    custom_jar,
)

# --- config -----------------------------------------------------------------

DATA_DIR = Path(__file__).parent.parent / "data"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
UA = resolvers.UA


def _srv(server_id: str | None = None) -> dict:
    """Return active (or named) server record, or 404."""
    rec = servers.get(server_id)
    if not rec:
        raise HTTPException(404, "no server configured; add one in the Servers menu")
    return rec


def _paths(rec: dict) -> dict[str, Any]:
    data = Path(rec["data_dir"])
    return {
        "container": rec["container"],
        "compose": Path(rec["compose"]),
        "data": data,
        "plugins": data / "plugins",
        "update": data / "plugins" / "update",
        "port": rec.get("port", 25565),
    }


def _cache_path(server_id: str) -> Path:
    return DATA_DIR / f"cache-{server_id}.json"


def _staged_path(server_id: str) -> Path:
    """Memory of what was staged into /plugins/update and at which version.

    Keyed by installed filename. Each entry: {staged_version, staged_at, source, staged_filename}.
    Used to skip re-staging plugins whose update is already pending.
    """
    return DATA_DIR / f"staged-{server_id}.json"


def _user_premium_path(server_id: str) -> Path:
    """User-curated premium plugin entries (added via search → 'Add to Catalog').

    Schema: { "<spigot_id>": {display, spigot_id, url, note, added_at, icon} }
    Merged into the built-in PREMIUM_PLUGINS list when /api/registry is served.
    """
    return DATA_DIR / f"premium-user-{server_id}.json"


def _catalog_path(server_id: str) -> Path:
    """Per-server installable catalog. Stored as JSON so users can add/remove
    entries from the UI without code changes.

    Schema: { "<key>": {display, sources: [[source, ref], ...]} }

    First-read seeding:
      - server_id == "test-fred" → seeded from REGISTRY (preserve original behavior)
      - any other server         → seeded from DEFAULT_CATALOG_SEED
    """
    return DATA_DIR / f"catalog-{server_id}.json"


def _get_server_catalog(server_id: str) -> dict[str, dict]:
    """Return the effective catalog for a server. Lazy-creates the file on
    first call. Caller mutates the returned dict + calls _save_server_catalog
    to persist.
    """
    path = _catalog_path(server_id)
    if path.exists():
        return _load_json(path, {})
    # Seed on first read.
    if server_id == "test-fred":
        seed = {k: {"display": v["display"], "sources": [list(s) for s in v["sources"]]}
                for k, v in registry.REGISTRY.items()}
    else:
        seed = {k: {"display": v["display"], "sources": [list(s) for s in v["sources"]]}
                for k, v in registry.DEFAULT_CATALOG_SEED.items()}
    _save_json(path, seed)
    return seed


def _save_server_catalog(server_id: str, catalog: dict[str, dict]) -> None:
    # Persist tuples-as-lists so JSON round-trips cleanly.
    serializable = {k: {"display": v["display"],
                        "sources": [list(s) for s in v["sources"]]}
                    for k, v in catalog.items()}
    _save_json(_catalog_path(server_id), serializable)


def _catalog_find(catalog: dict[str, dict], name: str) -> tuple[str, dict] | None:
    """Substring-match a plugin.yml name against the server's catalog keys.
    Replaces the global registry.find() helper for per-server catalogs.
    """
    if not name:
        return None
    key = registry.normalize(name)
    if key in catalog:
        return key, catalog[key]
    for k, v in catalog.items():
        if k == key or k in key or key in k:
            return k, v
    return None


def _seed_catalog_with_installed(server_id: str, data_dir: Path) -> None:
    """For a newly-tracked or created server: ensure the catalog file exists
    (lazy-creates with DEFAULT_CATALOG_SEED if missing), then walk the on-disk
    plugins/ folder and add any installed plugins that aren't already in the
    catalog. Installed-but-not-in-catalog entries get a placeholder with no
    sources — the user can edit them from the Catalog tab to wire up auto-update.
    """
    catalog = _get_server_catalog(server_id)
    plugins_dir = data_dir / "plugins"
    if not plugins_dir.exists():
        return
    added = False
    for jar in plugins_dir.glob("*.jar"):
        info = _parse_plugin_jar(jar)
        name = info.get("name") or ""
        if not name:
            continue
        key = registry.normalize(name)
        # Skip if a key in the catalog already matches (exact OR substring).
        if _catalog_find(catalog, name):
            continue
        catalog[key] = {
            "display": name,
            "sources": [],  # placeholder — user can add sources via the catalog UI
        }
        added = True
    if added:
        _save_server_catalog(server_id, catalog)


# In-memory cache for Spiget premium-plugin version lookups. Keyed by spigot_id.
# Each entry: (timestamp, payload). Refreshed every 1 hour — Spiget itself caches
# upstream, so this just stops us hammering them on every UI poll.
_SPIGET_CACHE: dict[str, tuple[float, dict]] = {}
_SPIGET_TTL = 3600.0


async def _spiget_latest_version(client: "httpx.AsyncClient", spigot_id: str) -> dict | None:
    """Fetch latest-version metadata for a Spigot resource via Spiget's public
    API. Works for both free AND premium resources (no auth needed for metadata).

    Returns ``{version, release_date_utc, plugin_name, premium, price}`` or
    ``None`` on lookup failure.
    """
    sid = str(spigot_id).strip()
    if not sid.isdigit():
        return None
    now = time.time()
    cached = _SPIGET_CACHE.get(sid)
    if cached and (now - cached[0]) < _SPIGET_TTL:
        return cached[1]
    try:
        res_r, ver_r = await asyncio.gather(
            client.get(f"https://api.spiget.org/v2/resources/{sid}", timeout=8),
            client.get(f"https://api.spiget.org/v2/resources/{sid}/versions/latest", timeout=8),
        )
        if res_r.status_code != 200 or ver_r.status_code != 200:
            return None
        res = res_r.json()
        ver = ver_r.json()
        payload = {
            "version": str(ver.get("name") or "").strip() or None,
            "release_date_utc": int(ver.get("releaseDate") or 0) or None,
            "plugin_name": (res.get("name") or "").strip() or None,
            "tag": (res.get("tag") or "").strip() or None,
            "premium": bool(res.get("premium")),
            "price": res.get("price"),
            "currency": res.get("currency"),
            "downloads": res.get("downloads"),
        }
        _SPIGET_CACHE[sid] = (now, payload)
        return payload
    except Exception:
        return None


app = FastAPI(title="Test Fred Hub")

# CORS for the browser extension: it runs on spigotmc.org and needs to POST
# jars to /api/plugins/upload. Only allow Spigot origins + localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.spigotmc.org",
        "https://spigotmc.org",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
    ],
    allow_origin_regex=r"chrome-extension://.*|moz-extension://.*",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _audit_middleware(request, call_next):
    """Log every API request to logs/api.jsonl with timing + status."""
    import time as _t
    start = _t.perf_counter()
    status = 500
    error: str | None = None
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        audit.exception("middleware_error", e, path=str(request.url.path))
        raise
    finally:
        try:
            latency_ms = (_t.perf_counter() - start) * 1000.0
            path = str(request.url.path)
            # Skip static files and the log-tail endpoint itself to keep the log readable
            if not path.startswith("/static") and not path.startswith("/api/logs/"):
                audit.api_request(
                    method=request.method,
                    path=path,
                    query=str(request.url.query or ""),
                    status=status,
                    latency_ms=latency_ms,
                    error=error,
                    client=(request.client.host if request.client else ""),
                )
        except Exception:
            pass  # logging must never break the response


# --- helpers ----------------------------------------------------------------

def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _sh(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _docker_inspect(container: str) -> dict:
    rc, out, _ = _sh("docker", "inspect", container)
    if rc != 0:
        return {}
    try:
        return json.loads(out)[0]
    except Exception:
        return {}


_VERSION_HISTORY_RE = re.compile(r"([\d.]+)-(\d+)(?:-[a-f0-9]+)?\s*\(MC:\s*([\d.]+)\)")


def _read_version_history(data_dir: Path) -> tuple[str | None, str | None]:
    """Parse /data/version_history.json — written by Paper on each boot.

    Format example: {"currentVersion":"1.21.10-129-3e25649 (MC: 1.21.10)"}
    Returns (mc_version, build) — ground truth for what's actually running.
    """
    vh = data_dir / "version_history.json"
    if not vh.exists():
        return None, None
    try:
        data = json.loads(vh.read_text())
    except Exception:
        return None, None
    cur = data.get("currentVersion") or ""
    m = _VERSION_HISTORY_RE.search(cur)
    if m:
        return m.group(3), m.group(2)  # MC version, build
    return None, None


def _server_version_build(rec: dict, paths: dict) -> tuple[str | None, str | None]:
    """Return (mc_version, current_build) for the running server.

    Priority:
      1. /data/version_history.json — written by Paper itself, ground truth
      2. paper-*.jar filename in /data — fallback if Paper hasn't booted yet
      3. compose env values — last resort (may be stale if container hasn't recreated)
    """
    # 1) version_history.json — what's actually running
    v, b = _read_version_history(paths["data"])
    if v and b:
        return v, b

    # 2) jar filename
    for jar in paths["data"].glob("paper-*.jar"):
        m = re.match(r"paper-([\d.]+)-(\d+)\.jar", jar.name)
        if m:
            return m.group(1), m.group(2)

    # 3) compose env — only if disk has nothing
    env = compose.get_env(paths["compose"])
    current = st.current_for_type(env.get("TYPE", "PAPER"), env)
    return current.get("version"), (str(current["build"]) if current.get("build") else None)


def _server_version_configured(rec: dict, paths: dict) -> tuple[str | None, str | None]:
    """What the compose file says the server SHOULD be on. Used for diffing."""
    env = compose.get_env(paths["compose"])
    current = st.current_for_type(env.get("TYPE", "PAPER"), env)
    return current.get("version"), (str(current["build"]) if current.get("build") else None)


# Cache parsed plugin metadata so we don't reread + re-hash multi-MB jars
# on every /api/registry or /api/plugins call. Key by (path, mtime, size) so
# any change to the file invalidates the entry. Bounded by jar count.
_PLUGIN_JAR_CACHE: dict[tuple[str, float, int], dict] = {}


def _parse_plugin_jar(jar_path: Path) -> dict:
    try:
        st = jar_path.stat()
    except Exception:
        st = None
    if st is not None:
        cache_key = (str(jar_path), st.st_mtime, st.st_size)
        cached = _PLUGIN_JAR_CACHE.get(cache_key)
        if cached is not None:
            return cached
    info: dict = {
        "file": jar_path.name,
        "size": st.st_size if st else 0,
        "mtime": st.st_mtime if st else 0,
        "name": None, "version": None, "main": None, "api_version": None,
        "authors": [], "description": None, "website": None,
        "sha1": None, "sha512": None,
    }
    try:
        raw = jar_path.read_bytes()
    except Exception:
        return info
    info["sha1"] = hashlib.sha1(raw).hexdigest()
    info["sha512"] = hashlib.sha512(raw).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for candidate in ("paper-plugin.yml", "plugin.yml"):
                if candidate in z.namelist():
                    data = yaml.safe_load(z.read(candidate)) or {}
                    info["name"] = data.get("name") or info["name"]
                    info["version"] = str(data.get("version") or "")
                    info["main"] = data.get("main")
                    info["api_version"] = data.get("api-version")
                    authors = data.get("authors") or (
                        [data.get("author")] if data.get("author") else []
                    )
                    info["authors"] = [a for a in authors if a]
                    info["description"] = data.get("description")
                    info["website"] = data.get("website")
                    break
    except Exception:
        pass

    # If we still don't have a version, try the more thorough inspector that
    # also handles bungee.yml, velocity-plugin.json, and MANIFEST.MF
    # Implementation-Version. Cached by (path, mtime, size) so this is free
    # on repeat scans of unchanged jars.
    if not info["version"] or not info["name"]:
        deep = plugin_inspect.inspect_jar(jar_path)
        if deep["ok"]:
            if not info["version"] and deep["version"]:
                info["version"] = deep["version"]
            if not info["name"] and deep["plugin_name"]:
                info["name"] = deep["plugin_name"]
            info["version_source"] = deep["source"]
    if st is not None:
        _PLUGIN_JAR_CACHE[(str(jar_path), st.st_mtime, st.st_size)] = info
    return info


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
    )


async def _resolve_github_tag_url(url: str, client: httpx.AsyncClient) -> str | None:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/releases/(?:tag|expanded_assets)/([^/?#]+)", url)
    if not m:
        return None
    owner, repo, tag = m.group(1), m.group(2), m.group(3)
    r = await client.get(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}")
    if r.status_code != 200:
        r = await client.get(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
        if r.status_code != 200:
            return None
    data = r.json()
    for asset in data.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".jar") and not name.endswith("-sources.jar") and not name.endswith("-javadoc.jar"):
            return asset.get("browser_download_url")
    return None


async def _download_to(url: str, dest: Path, client: httpx.AsyncClient) -> int:
    pivoted = await _resolve_github_tag_url(url, client)
    if pivoted:
        url = pivoted
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    total = 0
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" in ctype.lower():
            pivoted2 = await _resolve_github_tag_url(str(r.url), client)
            if pivoted2:
                async with client.stream("GET", pivoted2) as r2:
                    r2.raise_for_status()
                    with open(tmp, "wb") as f:
                        async for chunk in r2.aiter_bytes(64 * 1024):
                            f.write(chunk)
                            total += len(chunk)
            else:
                raise HTTPException(502, f"refusing to save HTML page as jar (url={url[:120]})")
        else:
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
    if total < 1024:
        tmp.unlink(missing_ok=True)
        raise HTTPException(502, f"download too small ({total}B); upstream likely 4xx")
    tmp.replace(dest)
    return total


def _is_jar(path: Path) -> bool:
    """Validate that ``path`` is a real plugin JAR.

    Old check only scanned the first 50 zip entries for ``META-INF/`` — which
    rejected many legitimate plugin jars whose archives list ``categories/``
    or locales before the manifest, AND rejected several older Spigot plugins
    (ChestSort, KeepChunks, GravesX, ...) whose authors strip ``META-INF/``
    from their jars entirely.

    A plugin jar is anything that's a valid zip and contains EITHER:
      - any ``META-INF/`` entry (the classic JAR signature), OR
      - a ``plugin.yml`` / ``paper-plugin.yml`` / ``bungee.yml`` / ``velocity-plugin.json``
        / ``fabric.mod.json`` / ``mods.toml`` (loader manifest), OR
      - at least one ``.class`` file (bytecode payload — final safety net)
    Anything else is HTML, JSON error pages, etc. and gets rejected.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if not names:
                return False
            manifests = {
                "plugin.yml", "paper-plugin.yml", "bungee.yml",
                "velocity-plugin.json", "fabric.mod.json",
            }
            has_meta = False
            has_manifest = False
            has_class = False
            for n in names:
                if n.startswith("META-INF/"):
                    has_meta = True
                if n in manifests or n.endswith("/mods.toml") or n == "mods.toml":
                    has_manifest = True
                if n.endswith(".class"):
                    has_class = True
                if has_meta or has_manifest or has_class:
                    return True
            return False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Multi-server registry API
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/servers")
async def servers_list() -> dict:
    return {"servers": servers.list_all(), "current": servers.get_current_id()}


@app.get("/api/servers/{sid}")
async def servers_get_one(sid: str) -> dict:
    rec = servers.get(sid)
    if not rec:
        raise HTTPException(404, f"unknown server: {sid}")
    return rec


@app.post("/api/servers/select")
async def servers_select(payload: dict) -> dict:
    sid = (payload or {}).get("id")
    if not sid:
        raise HTTPException(400, "id required")
    try:
        servers.set_current(sid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "current": servers.get(sid)}


@app.post("/api/servers/track")
async def servers_track(payload: dict) -> dict:
    """Register an existing container/compose as a new tracked server.

    If ``compose`` is omitted, scaffolds a fresh compose file at
    ``~/docker-composes/<sid>.yaml`` using the same fields accepted by
    ``/api/servers/create`` — useful when the user wants to register a
    container that's already running but doesn't have a hub-managed
    compose file yet. (Or wants to author the full config from this dialog
    rather than the Server tab afterwards.)
    """
    name = (payload.get("name") or "").strip()
    container = (payload.get("container") or "").strip()
    compose_path = (payload.get("compose") or "").strip()
    data_dir = (payload.get("data_dir") or "").strip()
    port = int(payload.get("port") or 25565)
    if not (name and container and data_dir):
        raise HTTPException(400, "name, container, data_dir are required")
    sid = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-") or "server"
    dp = Path(data_dir).expanduser()
    if compose_path:
        cp = Path(compose_path).expanduser()
        if not cp.exists():
            raise HTTPException(400, f"compose file does not exist: {cp}")
        if not dp.exists():
            raise HTTPException(400, f"data_dir does not exist: {dp}")
    else:
        # No compose provided — scaffold one using the create-style fields.
        cp = Path("~/docker-composes").expanduser() / f"{sid}.yaml"
        if cp.exists():
            raise HTTPException(409, f"compose file already exists at {cp}; "
                                "pass it as 'compose' to track it instead")
        # Surface difficulty/max_players via extra_env, same convenience as create.
        extra_env = dict(payload.get("extra_env") or {})
        if payload.get("difficulty"):
            extra_env["DIFFICULTY"] = str(payload["difficulty"]).strip()
        if payload.get("max_players"):
            extra_env["MAX_PLAYERS"] = str(payload["max_players"]).strip()
        try:
            servers.scaffold_compose(
                compose_path=cp,
                container_name=container,
                data_dir=dp,
                host_port=port,
                server_type=(payload.get("type") or "PAPER").upper(),
                version=(payload.get("version") or "LATEST").strip(),
                memory=(payload.get("memory") or "2G").strip(),
                motd=(payload.get("motd") or "A Minecraft server managed by Test Fred Hub").strip(),
                icon=(payload.get("icon") or None) or None,
                image_tag=_java_recommendation(
                    (payload.get("type") or "PAPER"),
                    (payload.get("version") or "LATEST"),
                )["tag"],
                extra_env=extra_env,
            )
        except FileExistsError as e:
            raise HTTPException(409, str(e))
    try:
        rec = servers.add({
            "id": sid, "display": name, "container": container,
            "compose": str(cp), "data_dir": str(dp), "port": port,
        })
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Seed the per-server catalog: default seed + any plugins already installed
    # on disk. Plugins already on disk get a placeholder catalog entry (no
    # sources) — the user can flesh out sources later from the Catalog tab.
    _seed_catalog_with_installed(rec["id"], dp)
    return rec


@app.post("/api/servers/create")
async def servers_create(payload: dict) -> dict:
    """Scaffold a brand-new compose file + register. Does not start container.

    Accepts the full set of env fields exposed by the Server config tab:
    type, version, memory, motd, icon, difficulty, max_players, image_tag,
    plus an ``extra_env`` dict for type-specific keys (BUILD, FORGE_VERSION,
    NEOFORGE_VERSION, ARCLIGHT_TYPE, etc.).
    """
    name = (payload.get("name") or "").strip()
    data_dir = (payload.get("data_dir") or "").strip()
    port = int(payload.get("port") or 25565)
    stype = (payload.get("type") or "PAPER").upper()
    version = (payload.get("version") or "LATEST").strip()
    memory = (payload.get("memory") or "2G").strip()
    motd = (payload.get("motd") or "A Minecraft server managed by Test Fred Hub").strip()
    icon = (payload.get("icon") or "").strip() or None
    image_tag = _java_recommendation(stype, version)["tag"]
    extra_env = dict(payload.get("extra_env") or {})
    # Convenience: surface difficulty/max_players as top-level form fields
    # but feed them through extra_env so scaffold_compose handles them uniformly.
    if payload.get("difficulty"):
        extra_env["DIFFICULTY"] = str(payload["difficulty"]).strip()
    if payload.get("max_players"):
        extra_env["MAX_PLAYERS"] = str(payload["max_players"]).strip()
    if not name or not data_dir:
        raise HTTPException(400, "name and data_dir are required")
    sid = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
    if not sid:
        raise HTTPException(400, "name must contain alphanumerics")
    # Allow the user to override the compose path; default to ~/docker-composes/<sid>.yaml.
    compose_override = (payload.get("compose") or "").strip()
    compose_path = (Path(compose_override).expanduser()
                    if compose_override
                    else Path("~/docker-composes").expanduser() / f"{sid}.yaml")
    dp = Path(data_dir).expanduser()
    if compose_path.exists():
        raise HTTPException(409, f"compose file already exists: {compose_path}")
    try:
        servers.scaffold_compose(
            compose_path=compose_path, container_name=sid,
            data_dir=dp, host_port=port, server_type=stype,
            version=version, memory=memory, motd=motd, icon=icon,
            image_tag=image_tag, extra_env=extra_env,
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    try:
        rec = servers.add({
            "id": sid, "display": name, "container": sid,
            "compose": str(compose_path), "data_dir": str(dp), "port": port,
        })
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Lazy-create the catalog file with DEFAULT_CATALOG_SEED. Fresh server has
    # no installed plugins so just touch the file.
    _get_server_catalog(rec["id"])
    return rec


@app.post("/api/servers/{sid}/start")
async def servers_start_tracked(sid: str) -> dict:
    rec = _srv(sid)
    rc, out = await compose.compose_up(Path(rec["compose"]))
    if rc != 0:
        raise HTTPException(500, out[-2000:])
    return {"ok": True, "output": out.strip()[-2000:]}


@app.delete("/api/servers/{sid}")
async def servers_remove(sid: str, payload: dict | None = None) -> dict:
    rec = servers.get(sid)
    if not rec:
        raise HTTPException(404, f"unknown server: {sid}")
    delete_files = bool((payload or {}).get("delete_files"))
    removed_files: list[str] = []
    if delete_files:
        # stop container, remove compose file. NEVER touch data_dir.
        _sh("docker", "stop", rec["container"], timeout=30)
        cp = Path(rec["compose"])
        if cp.exists():
            try:
                cp.unlink()
                removed_files.append(str(cp))
            except Exception:
                pass
    try:
        result = servers.remove(sid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {
        "ok": True, "removed_tracking": sid,
        "removed_files": removed_files,
        "kept_data_dir": rec["data_dir"],
        **result,
    }


# --- routes: server ---------------------------------------------------------

@app.get("/api/server")
async def server_status() -> dict:
    rec = _srv()
    paths = _paths(rec)
    inspect = _docker_inspect(rec["container"])
    state = inspect.get("State", {}) if inspect else {}
    version, build = _server_version_build(rec, paths)

    motd = None
    try:
        sp = paths["data"] / "server.properties"
        if sp.exists():
            for line in sp.read_text().splitlines():
                if line.startswith("motd="):
                    motd = line.split("=", 1)[1]
                    break
    except Exception:
        pass

    port = paths["port"]
    players: dict = {"online": None}
    try:
        from mcstatus import JavaServer
        srv = JavaServer.lookup(f"127.0.0.1:{port}", timeout=2)
        s = await asyncio.to_thread(srv.status)
        players = {
            "online": s.players.online, "max": s.players.max,
            "names": [p.name for p in (s.players.sample or [])],
            "latency_ms": round(s.latency, 1),
            "version": s.version.name,
        }
    except Exception as e:
        players = {"online": None, "error": str(e)[:120]}

    update_dir = paths["update"]
    pending_updates = sorted(p.name for p in update_dir.glob("*.jar")) if update_dir.exists() else []

    # Fresh installs: jars in plugins/ that have a staged-memory entry
    # ``staged_at`` newer than the container's StartedAt. These are pending in
    # the sense that the running JVM hasn't loaded them yet — a restart will.
    started_at_str = state.get("StartedAt") or ""
    started_ts = 0.0
    try:
        from datetime import datetime
        # docker uses "2026-06-05T01:40:34.445311441Z"; strip the nanoseconds tail
        s = started_at_str.replace("Z", "+00:00")
        if "." in s:
            head, _, tz = s.partition(".")
            frac, _, tail = tz.partition("+")
            s = f"{head}.{frac[:6]}+{tail}" if tail else f"{head}.{frac[:6]}"
        started_ts = datetime.fromisoformat(s).timestamp()
    except Exception:
        started_ts = 0.0
    staged_mem_now = _load_json(_staged_path(rec["id"]), {})
    pending_installs: list[str] = []
    if started_ts > 0:
        for fname, info in (staged_mem_now or {}).items():
            try:
                staged_at_v = float(info.get("staged_at") or 0)
            except Exception:
                staged_at_v = 0.0
            if staged_at_v <= started_ts:
                continue
            # Only count it as a fresh install if it lives in plugins/ (not update/)
            if (paths["plugins"] / fname).exists() and fname not in pending_updates:
                pending_installs.append(fname)
    pending_installs.sort()

    return {
        "server_id": rec["id"],
        "container": rec["container"],
        "running": state.get("Running", False),
        "started_at": state.get("StartedAt"),
        "health": state.get("Health", {}).get("Status"),
        "version": version, "build": build,
        "motd": motd, "players": players, "port": port,
        "pending_updates": pending_updates,
        "pending_installs": pending_installs,
        "pending_deletions": sorted((_load_json(_deletions_path(rec["id"]), {}) or {}).keys()),
    }


@app.post("/api/server/restart")
async def server_restart() -> dict:
    rec = _srv()
    rc, out, err = _sh("docker", "restart", rec["container"], timeout=60)
    if rc != 0:
        raise HTTPException(500, err.strip() or out.strip())
    return {"ok": True, "output": out.strip()}


# ─────────────────────────────────────────────────────────────
# Scheduled restart manager
# ─────────────────────────────────────────────────────────────

async def _get_player_count(server_id: str) -> int | None:
    """Return current online player count, or None if the server is unreachable.

    None means "don't trigger no_players yet" — used by the scheduler so we
    don't fire a restart on a server that's still booting (0 players because
    nobody can connect yet).
    """
    try:
        rec = servers.get(server_id)
    except Exception:
        return None
    if not rec:
        return None
    paths = _paths(rec)
    port = paths.get("port") or 25565
    try:
        from mcstatus import JavaServer
        srv = JavaServer.lookup(f"127.0.0.1:{port}", timeout=2)
        s = await asyncio.to_thread(srv.status)
        return s.players.online
    except Exception:
        return None


async def _recurring_stage_all_plugin_updates(server_id: str, paths: dict) -> dict:
    """Pre-flight for recurring restarts when include_plugin_updates is set.

    Walks every plugin jar, asks each resolver for a newer version, and stages
    everything that has an update into plugins/update/ so the upcoming restart
    actually applies them. Returns a small summary dict for the audit log.

    Reuses the same logic as POST /api/plugins/check + /api/plugins/stage-all
    but without going through HTTP. Errors per-plugin are caught — we never
    block the restart over one bad lookup.
    """
    rec = servers.get(server_id)
    if not rec:
        return {"checked": 0, "staged": 0, "failed": 0, "reason": "server gone"}
    version, _ = _server_version_build(rec, paths)
    if not version:
        return {"checked": 0, "staged": 0, "failed": 0, "reason": "no server jar"}
    staged_mem = _load_json(_staged_path(server_id), {})
    cat_data = _get_server_catalog(server_id)
    update_dir = paths["update"]
    results = []
    async with _client() as c:
        jars = sorted(paths["plugins"].glob("*.jar"))
        infos = [_parse_plugin_jar(j) for j in jars]
        tasks = [_resolve_for_plugin(c, info, version, catalog=cat_data) for info in infos]
        for fut in asyncio.as_completed(tasks):
            entry = await fut
            _apply_staged_memory(entry, staged_mem, update_dir)
            results.append(entry)
    _save_json(_cache_path(server_id),
               {"checked_at": time.time(), "mc_version": version, "results": results})
    # Stage every result that has an update available and isn't already staged.
    # Inline (server-id aware) version of POST /api/plugins/{file}/stage-update
    # — the HTTP handler reads server-id from active session and we're firing
    # from a background scheduler tick with no session context.
    staged_now, failed = [], []
    paths["update"].mkdir(parents=True, exist_ok=True)
    async with _client() as c:
        for r in results:
            if not r.get("update_available"):
                continue
            if r["file"] in staged_mem:
                continue  # already staged from a previous run
            if not r.get("download_url"):
                failed.append({"file": r["file"], "error": "no download URL"})
                continue
            new_name = r.get("filename") or r["file"]
            new_name = re.sub(r"[^A-Za-z0-9._-]+", "_", new_name)
            if not new_name.endswith(".jar"):
                new_name += ".jar"
            dest = paths["update"] / new_name
            try:
                await _download_to(r["download_url"], dest, c)
                if not _is_jar(dest):
                    dest.unlink(missing_ok=True)
                    failed.append({"file": r["file"], "error": "downloaded file is not a valid jar"})
                    continue
                staged_mem[r["file"]] = {
                    "staged_version": r.get("latest_version"),
                    "staged_filename": new_name,
                    "source": r.get("source"),
                    "staged_at": time.time(),
                }
                staged_now.append(r["file"])
            except Exception as e:  # noqa: BLE001
                failed.append({"file": r["file"], "error": str(e)[:200]})
    _save_json(_staged_path(server_id), staged_mem)
    audit.event(
        "recurring_plugin_stage_done", server=server_id,
        checked=len(results), staged=len(staged_now), failed=len(failed),
    )
    return {"checked": len(results), "staged": len(staged_now),
            "failed": len(failed), "files": staged_now}


async def _fire_scheduled_restart(server_id: str, intent) -> dict:
    """Scheduler callback — handles BOTH RestartIntent and RecurringSchedule.

    Behavior matrix:
      - scope=plugins                       → docker restart
      - scope=server                        → compose recreate (+ auto-bump image)
      - scope=server + include_server_updates → also check for newer Paper
        build, bump VERSION/PAPER_BUILD env, THEN recreate

    Errors raise so the scheduler marks the intent/recurring as errored.
    """
    rec = servers.get(server_id)
    if not rec:
        raise RuntimeError(f"server {server_id} no longer registered")
    paths = _paths(rec)
    is_recurring = isinstance(intent, sched_mod.RecurringSchedule)
    audit.event(
        "scheduled_restart_firing", server=server_id,
        trigger=getattr(intent, "trigger", "recurring"),
        scope=intent.scope, fire_kind="recurring" if is_recurring else "one-shot",
        note=intent.note,
        include_server_updates=getattr(intent, "include_server_updates", False),
    )
    # Recurring with include_plugin_updates → check + stage all plugin
    # updates before the restart fires, so the restart actually applies them.
    plugin_stage_summary = None
    if is_recurring and getattr(intent, "include_plugin_updates", False):
        try:
            plugin_stage_summary = await _recurring_stage_all_plugin_updates(server_id, paths)
        except Exception as e:  # noqa: BLE001
            audit.event("recurring_plugin_stage_failed", server=server_id, error=str(e)[:200])
    # Recurring with include_server_updates and scope=server → first check
    # whether a newer Paper build exists; if so, bump compose env.
    bumped_to = None
    if is_recurring and intent.include_server_updates and intent.scope == "server":
        try:
            bumped_to = await _maybe_bump_server_to_latest(rec, paths)
        except Exception as e:  # noqa: BLE001
            # Don't block the restart — just log and continue. Better to do a
            # plain recreate than skip the whole cycle because Paper's API hiccupped.
            audit.event("recurring_update_check_failed", server=server_id, error=str(e)[:200])
    if intent.scope == "server":
        _auto_bump_image_if_needed(paths)
        # Refresh custom-routed loader jar (Arclight, etc.) if applicable.
        try:
            await _ensure_loader_jar(paths)
        except Exception as e:  # noqa: BLE001
            audit.event("scheduled_loader_refresh_failed",
                        server=server_id, error=str(e)[:200])
        deleted = _apply_pending_deletions(server_id)
        rc, output = await compose.compose_recreate(paths["compose"])
        if rc != 0:
            raise RuntimeError(f"compose recreate failed: {output[-500:]}")
        return {"mode": "recreate", "output": output[-1000:],
                "bumped_to": bumped_to, "deleted": deleted}
    # Plugins-only: docker restart cycles the JVM, so we delete the jars
    # in the moment between stop and start. Safer than yanking them while loaded.
    rc1, out1, err1 = _sh("docker", "stop", rec["container"], timeout=120)
    if rc1 != 0:
        raise RuntimeError((err1 or out1).strip()[-500:])
    deleted = _apply_pending_deletions(server_id)
    rc2, out2, err2 = _sh("docker", "start", rec["container"], timeout=60)
    if rc2 != 0:
        raise RuntimeError((err2 or out2).strip()[-500:])
    return {"mode": "restart", "output": out2.strip()[-500:], "deleted": deleted}


async def _maybe_bump_server_to_latest(rec: dict, paths: dict) -> dict | None:
    """Check Paper (or whichever server type) for the latest build vs what's
    currently in the compose env. If newer, update VERSION + build env keys
    and return what we bumped to. If already current, return None.
    """
    env = compose.get_env(paths["compose"])
    type_key = (env.get("TYPE") or "PAPER").upper()
    latest = await st.get_latest(type_key)
    if not latest:
        return None
    cur_v = env.get("VERSION") or ""
    build_env_map = {
        "PAPER": "PAPER_BUILD", "FOLIA": "FOLIA_BUILD", "PURPUR": "PURPUR_BUILD",
        "LEAF": "LEAF_BUILD", "PUFFERFISH": "PUFFERFISH_BUILD",
    }
    bkey = build_env_map.get(type_key)
    cur_b = env.get(bkey) if bkey else None
    lv, lb = str(latest.get("version") or ""), str(latest.get("build") or "")
    if cur_v == lv and (not bkey or cur_b == lb):
        return None  # already current
    changes: dict[str, str] = {}
    if lv:
        changes["VERSION"] = lv
    if bkey and lb:
        changes[bkey] = lb
    if not changes:
        return None
    compose.update_env(paths["compose"], changes)
    audit.event("recurring_server_bumped", server=rec["id"],
                from_version=cur_v, from_build=cur_b,
                to_version=lv, to_build=lb)
    return {"version": lv, "build": lb}


SCHEDULER = sched_mod.RestartScheduler(
    data_dir=DATA_DIR,
    on_fire=_fire_scheduled_restart,
    get_player_count=_get_player_count,
    poll_interval=15.0,
)


@app.on_event("startup")
async def _start_scheduler() -> None:
    await SCHEDULER.start()


@app.on_event("shutdown")
async def _stop_scheduler() -> None:
    await SCHEDULER.stop()


def _coerce_intent(payload: dict) -> sched_mod.RestartIntent:
    """Translate the JSON the frontend sends into a RestartIntent.

    Frontend sends:
      {trigger, scope, scheduled_utc?, note?}
    Where trigger ∈ {now, at_time, no_players, none}
    """
    trigger = (payload.get("trigger") or "none").strip()
    if trigger not in {"now", "at_time", "no_players", "none"}:
        raise HTTPException(400, f"bad trigger: {trigger!r}")
    scope = (payload.get("scope") or "plugins").strip()
    if scope not in {"plugins", "server"}:
        raise HTTPException(400, f"bad scope: {scope!r}")
    scheduled_utc = payload.get("scheduled_utc")
    if trigger == "at_time":
        if scheduled_utc is None:
            raise HTTPException(400, "at_time trigger requires scheduled_utc")
        try:
            scheduled_utc = float(scheduled_utc)
        except (TypeError, ValueError):
            raise HTTPException(400, "scheduled_utc must be a number (epoch seconds)")
        if scheduled_utc < time.time() - 60:
            raise HTTPException(400, "scheduled_utc is in the past")
    else:
        scheduled_utc = None
    # Player gate: integer 0..1000, default 0. "now" trigger ignores the gate.
    raw_mp = payload.get("max_players", 0)
    try:
        max_players = max(0, min(1000, int(raw_mp)))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_players must be an integer")
    return sched_mod.RestartIntent(
        trigger=trigger, scope=scope,
        scheduled_utc=scheduled_utc,
        max_players=max_players,
        note=str(payload.get("note") or "")[:200],
    )


@app.get("/api/restart/schedule")
async def get_schedule() -> dict:
    rec = _srv()
    intent = SCHEDULER.get(rec["id"])
    return {"intent": sched_mod.intent_to_dict(intent)}


@app.post("/api/restart/schedule")
async def set_schedule(payload: dict) -> dict:
    """Set or replace the current scheduled-restart intent.

    "now" trigger triggers immediately by side-stepping the scheduler poll
    and calling the fire callback inline — gives sub-second UX feedback.
    """
    rec = _srv()
    intent = _coerce_intent(payload)
    # Special case: trigger == "none" with an existing pending intent should
    # cancel that intent (matches frontend "Stage only, no restart")
    if intent.trigger == "none":
        SCHEDULER.cancel(rec["id"])
        audit.event("scheduled_restart_cancelled", server=rec["id"])
        return {"ok": True, "intent": None}
    intent = SCHEDULER.set_intent(rec["id"], intent)
    audit.event("scheduled_restart_set", server=rec["id"],
                trigger=intent.trigger, scope=intent.scope,
                scheduled_utc=intent.scheduled_utc, note=intent.note)
    if intent.trigger == "now":
        # Fire synchronously so the user gets immediate feedback.
        try:
            await _fire_scheduled_restart(rec["id"], intent)
            intent.status = "done"
            intent.fired_at = time.time()
        except Exception as e:  # noqa: BLE001
            intent.status = "error"
            intent.error = str(e)[:500]
        SCHEDULER._save(rec["id"])  # noqa: SLF001 — internal but safe
    return {"ok": True, "intent": sched_mod.intent_to_dict(intent)}


@app.delete("/api/restart/schedule")
async def cancel_schedule() -> dict:
    rec = _srv()
    SCHEDULER.cancel(rec["id"])
    audit.event("scheduled_restart_cancelled", server=rec["id"])
    return {"ok": True}


# ── recurring schedule endpoints ─────────────────────────────────────────

def _coerce_recurring(payload: dict) -> sched_mod.RecurringSchedule:
    cadence = (payload.get("cadence") or "").strip()
    if cadence not in {"daily", "weekly", "monthly"}:
        raise HTTPException(400, f"bad cadence: {cadence!r}")
    local_time = (payload.get("local_time") or "").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", local_time):
        raise HTTPException(400, "local_time must be HH:MM (24h)")
    hh, mm = (int(x) for x in local_time.split(":"))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise HTTPException(400, "local_time out of range")
    tz = (payload.get("tz") or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        raise HTTPException(400, f"unknown timezone: {tz!r}")
    scope = (payload.get("scope") or "plugins").strip()
    if scope not in {"plugins", "server"}:
        raise HTTPException(400, f"bad scope: {scope!r}")
    weekday = payload.get("weekday")
    if cadence == "weekly":
        if weekday is None:
            raise HTTPException(400, "weekly cadence requires weekday (0=Mon..6=Sun)")
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            raise HTTPException(400, "weekday must be 0..6")
        if not (0 <= weekday <= 6):
            raise HTTPException(400, "weekday must be 0..6")
    else:
        weekday = None
    dom = payload.get("day_of_month")
    if cadence == "monthly":
        if dom is None:
            raise HTTPException(400, "monthly cadence requires day_of_month (1..31)")
        try:
            dom = int(dom)
        except (TypeError, ValueError):
            raise HTTPException(400, "day_of_month must be 1..31")
        if not (1 <= dom <= 31):
            raise HTTPException(400, "day_of_month must be 1..31")
    else:
        dom = None
    raw_mp = payload.get("max_players", 0)
    try:
        max_players = max(0, min(1000, int(raw_mp)))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_players must be an integer")
    return sched_mod.RecurringSchedule(
        cadence=cadence,
        local_time=f"{hh:02d}:{mm:02d}",
        tz=tz,
        weekday=weekday,
        day_of_month=dom,
        scope=scope,
        include_server_updates=bool(payload.get("include_server_updates")),
        include_plugin_updates=bool(payload.get("include_plugin_updates")),
        max_players=max_players,
        enabled=bool(payload.get("enabled", True)),
        note=str(payload.get("note") or "")[:200],
    )


@app.get("/api/restart/recurring")
async def get_recurring() -> dict:
    rec = _srv()
    sched = SCHEDULER.get_recurring(rec["id"])
    return {"recurring": sched_mod.recurring_to_dict(sched)}


@app.post("/api/restart/recurring")
async def set_recurring(payload: dict) -> dict:
    rec = _srv()
    sched = _coerce_recurring(payload)
    sched = SCHEDULER.set_recurring(rec["id"], sched)
    audit.event("recurring_set", server=rec["id"], cadence=sched.cadence,
                local_time=sched.local_time, tz=sched.tz, scope=sched.scope,
                include_server_updates=sched.include_server_updates,
                next_fire_utc=sched.next_fire_utc)
    return {"ok": True, "recurring": sched_mod.recurring_to_dict(sched)}


@app.delete("/api/restart/recurring")
async def delete_recurring() -> dict:
    rec = _srv()
    SCHEDULER.cancel_recurring(rec["id"])
    audit.event("recurring_cancelled", server=rec["id"])
    return {"ok": True}


@app.post("/api/server/start")
async def server_start() -> dict:
    rec = _srv()
    paths = _paths(rec)
    rc, out, err = _sh("docker", "start", rec["container"], timeout=30)
    if rc != 0:
        rc2, out2 = await compose.compose_up(paths["compose"])
        if rc2 != 0:
            raise HTTPException(500, (err + out2).strip())
        return {"ok": True, "via": "compose-up", "output": out2.strip()}
    return {"ok": True, "via": "docker-start", "output": out.strip()}


@app.post("/api/server/stop")
async def server_stop() -> dict:
    rec = _srv()
    rc, out, err = _sh("docker", "stop", rec["container"], timeout=60)
    if rc != 0:
        raise HTTPException(500, err.strip() or out.strip())
    return {"ok": True, "output": out.strip()}


@app.post("/api/server/recreate")
async def server_recreate() -> dict:
    rec = _srv()
    paths = _paths(rec)

    # If this server uses a custom-routed loader (Arclight, etc.) make sure
    # the jar is on disk + compose's CUSTOM_SERVER points at it BEFORE we
    # touch the image tag. This also normalizes compose env (TYPE=CUSTOM etc.)
    # so the Java-tag check below sees the final shape.
    loader_status = await _ensure_loader_jar(paths)

    # Before recreating, make sure the docker image matches the MC version the
    # compose file is set to. Without this, "Recreate Container to Apply Staged"
    # on the Updates page recreates with a stale Java tag and the container
    # fails to boot (e.g. MC 26.1.2 on itzg java21 → UnsupportedClassVersionError).
    bump = _auto_bump_image_details(paths)

    rc, output = await compose.compose_recreate(paths["compose"])
    if rc != 0:
        raise HTTPException(500, output[-1000:])
    return {
        "ok": True,
        "image_bumped": bool(bump),
        "image_bump": bump,  # full {old_tag, new_tag, reason, …} or None
        "image": (bump["new_image"] if bump else compose.get_image(paths["compose"])),
        "loader": loader_status,
        "output": output.strip()[-2000:],
    }


def _auto_bump_image_if_needed(paths: dict) -> str | None:
    """Compare the compose-configured MC version vs. the docker image's Java tag.
    If they're mismatched AND the user is on the canonical itzg base image,
    set the right tag and return the new image string. Otherwise return None.

    Pure side-effect on the compose file. Caller still needs to recreate.
    """
    bump = _auto_bump_image_details(paths)
    return bump["new_image"] if bump else None


def _auto_bump_image_details(paths: dict) -> dict | None:
    """Like _auto_bump_image_if_needed but returns full diff info:
    {old_tag, new_tag, new_image, reason}. Used by the Updates page so we
    can tell the user *why* the recreate is going to change Java versions.
    Returns None when no bump is needed or the image is custom.
    """
    env = compose.get_env(paths["compose"])
    type_key = (env.get("TYPE") or "PAPER").upper()
    cfg = st.current_for_type(type_key, env)
    target_mc = cfg.get("version") or env.get("VERSION")
    if not target_mc:
        return None
    current_image = compose.get_image(paths["compose"]) or ""
    cur_base, _, cur_tag = current_image.partition(":")
    if (cur_base or ITZG_IMAGE_BASE) != ITZG_IMAGE_BASE:
        return None  # custom image, leave alone
    rec = _java_recommendation(type_key, target_mc)
    wanted_tag = rec["tag"]
    if cur_tag == wanted_tag:
        return None
    new_image = f"{ITZG_IMAGE_BASE}:{wanted_tag}"
    compose.set_image(paths["compose"], new_image)
    return {
        "old_tag": cur_tag or "(unset)",
        "new_tag": wanted_tag,
        "new_image": new_image,
        "reason": rec["reason"],
        "type_key": type_key,
        "mc_version": target_mc,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Custom-routed loaders (Arclight, eventually Mohist/Pufferfish/SpongeVanilla)
# ─────────────────────────────────────────────────────────────────────────────

async def _ensure_loader_jar(paths: dict) -> dict | None:
    """If this server uses a custom-routed loader, make sure the jar is on
    disk and the compose's CUSTOM_SERVER points at it. Returns a dict
    describing what happened, or None if not applicable.

    Idempotent — safe to call before every recreate. Detects loader from
    either HUB_LOADER_TYPE (already routed) OR the original TYPE= field
    (first-time install, e.g. freshly-scaffolded server). For the original
    TYPE path we also pick up HUB_LOADER_CHANNEL/SUBTYPE if the caller
    pre-staged them via extra_env at create time.
    """
    env = compose.get_env(paths["compose"])
    loader = (env.get("HUB_LOADER_TYPE") or "").upper()
    # First-time install: TYPE is still ARCLIGHT (or similar) but no HUB markers yet.
    if not loader:
        raw_type = (env.get("TYPE") or "").upper()
        if raw_type in custom_jar.CUSTOM_ROUTED_TYPES:
            loader = raw_type
    if loader not in custom_jar.CUSTOM_ROUTED_TYPES:
        return None

    jar_name = env.get("HUB_LOADER_JAR") or ""
    data_dir = paths["data"]
    jar_path = data_dir / jar_name if jar_name else None

    # Already on disk + compose pointing at it → nothing to do
    if (jar_path and jar_path.exists()
            and env.get("CUSTOM_SERVER") == f"/data/{jar_name}"
            and env.get("TYPE", "").upper() == "CUSTOM"):
        return {"ok": True, "skipped": True, "jar": jar_name}

    # Need to (re)resolve a build and download it
    mc_version = env.get("VERSION") or ""
    # Subtype precedence: explicit HUB marker > legacy ARCLIGHT_TYPE field > forge default
    subtype = (env.get("HUB_LOADER_SUBTYPE")
               or env.get("ARCLIGHT_TYPE") or "forge").lower()
    channel = (env.get("HUB_LOADER_CHANNEL") or "snapshot").lower()
    tag_hint = env.get("HUB_LOADER_TAG") or "latest"

    if not mc_version:
        raise HTTPException(400, f"{loader} server has no VERSION set — cannot pick a jar")

    build = await custom_jar.resolve_build(
        type_key=loader, mc_version=mc_version,
        build_hint=tag_hint, subtype=subtype, channel=channel,
    )
    if not build:
        raise HTTPException(502, (
            f"could not resolve a {loader} jar for MC {mc_version}/{subtype} "
            f"(hint={tag_hint!r}). Try a different version or upload a jar manually."
        ))

    downloaded = await custom_jar.download_jar(build, data_dir)
    # Write hub markers + flip TYPE→CUSTOM + point CUSTOM_SERVER at the jar
    compose.update_env(paths["compose"], {
        "TYPE": "CUSTOM",
        "CUSTOM_SERVER": f"/data/{downloaded.name}",
        "HUB_LOADER_TYPE": loader,
        "HUB_LOADER_TAG": build.tag,
        "HUB_LOADER_JAR": downloaded.name,
        "HUB_LOADER_SUBTYPE": build.subtype,
        "HUB_LOADER_CHANNEL": build.channel,
        "HUB_LOADER_SOURCE": build.source,
    })
    audit.event("loader_jar_installed", loader=loader, mc_version=mc_version,
                subtype=subtype, channel=channel, tag=build.tag,
                jar=downloaded.name)
    return {
        "ok": True, "downloaded": True,
        "jar": downloaded.name, "tag": build.tag,
        "channel": build.channel, "subtype": build.subtype,
        "published_at": build.published_at,
    }


@app.get("/api/server/arclight/versions")
async def arclight_versions() -> dict:
    """List MC versions Arclight publishes builds for, plus available subtypes."""
    versions = await custom_jar.arclight_list_versions()
    out: list[dict] = []
    for v in versions:
        try:
            subs = await custom_jar.arclight_list_subtypes(v)
        except Exception:
            subs = []
        out.append({"version": v, "subtypes": subs})
    return {"versions": out}


@app.get("/api/server/arclight/builds")
async def arclight_builds(version: str, subtype: str = "forge",
                          channel: str = "any") -> dict:
    """List Arclight builds for (version, subtype). Newest first.

    channel: "stable" | "snapshot" | "any" (default).
    """
    include_stable = channel in ("stable", "any")
    include_snapshots = channel in ("snapshot", "any")
    builds = await custom_jar.arclight_list_builds(
        version, subtype,
        include_snapshots=include_snapshots,
        include_stable=include_stable,
    )
    return {
        "version": version, "subtype": subtype, "channel": channel,
        "builds": [
            {
                "tag": b.tag, "asset_name": b.asset_name,
                "download_url": b.download_url, "mc_version": b.mc_version,
                "subtype": b.subtype, "channel": b.channel,
                "source": b.source, "published_at": b.published_at,
                "size_bytes": b.size_bytes, "note": b.note,
            }
            for b in builds
        ],
    }


@app.post("/api/server/arclight/install")
async def arclight_install(payload: dict | None = None) -> dict:
    """Set the active server's Arclight build (or refresh to latest) and
    optionally recreate. Body: {version, subtype, channel?, tag?, recreate?}.

    On success the compose file ends up with:
      TYPE=CUSTOM, CUSTOM_SERVER=/data/<jar>, HUB_LOADER_TYPE=ARCLIGHT,
      HUB_LOADER_TAG/JAR/SUBTYPE/CHANNEL/SOURCE markers.
    """
    rec = _srv()
    paths = _paths(rec)
    payload = payload or {}
    mc_version = (payload.get("version") or "").strip()
    subtype = (payload.get("subtype") or "forge").strip().lower()
    channel = (payload.get("channel") or "snapshot").strip().lower()
    tag_hint = (payload.get("tag") or "latest").strip()
    do_recreate = bool(payload.get("recreate"))
    if not mc_version:
        raise HTTPException(400, "version is required")

    # Resolve before touching compose so we fail cleanly if the build is gone.
    build = await custom_jar.resolve_build(
        "ARCLIGHT", mc_version, build_hint=tag_hint,
        subtype=subtype, channel=channel,
    )
    if not build:
        raise HTTPException(404, (
            f"no Arclight build for MC {mc_version}/{subtype} matching "
            f"channel={channel!r} hint={tag_hint!r}"
        ))

    downloaded = await custom_jar.download_jar(build, paths["data"])
    compose.update_env(paths["compose"], {
        "TYPE": "CUSTOM",
        "VERSION": mc_version,
        "CUSTOM_SERVER": f"/data/{downloaded.name}",
        "HUB_LOADER_TYPE": "ARCLIGHT",
        "HUB_LOADER_TAG": build.tag,
        "HUB_LOADER_JAR": downloaded.name,
        "HUB_LOADER_SUBTYPE": build.subtype,
        "HUB_LOADER_CHANNEL": build.channel,
        "HUB_LOADER_SOURCE": build.source,
    })
    # Pin Java for Arclight (1.21.x → java21)
    img_rec = _java_recommendation("ARCLIGHT", mc_version)
    compose.set_image(paths["compose"], f"{ITZG_IMAGE_BASE}:{img_rec['tag']}")
    audit.event("arclight_install", mc_version=mc_version, subtype=subtype,
                channel=channel, tag=build.tag, jar=downloaded.name)

    result: dict = {
        "ok": True, "jar": downloaded.name, "tag": build.tag,
        "subtype": build.subtype, "channel": build.channel,
        "image_tag": img_rec["tag"], "recreated": False,
    }
    if do_recreate:
        rc, output = await compose.compose_recreate(paths["compose"])
        result["recreated"] = rc == 0
        result["output"] = output[-2000:]
        if rc != 0:
            raise HTTPException(500, output[-1000:])
    return result


@app.get("/api/server/arclight/status")
async def arclight_status() -> dict:
    """Report what Arclight build is currently installed on the active server."""
    rec = _srv()
    paths = _paths(rec)
    env = compose.get_env(paths["compose"])
    if (env.get("HUB_LOADER_TYPE") or "").upper() != "ARCLIGHT":
        return {"installed": False}
    jar_name = env.get("HUB_LOADER_JAR") or ""
    jar_path = paths["data"] / jar_name if jar_name else None
    return {
        "installed": True,
        "mc_version": env.get("VERSION"),
        "subtype": env.get("HUB_LOADER_SUBTYPE"),
        "channel": env.get("HUB_LOADER_CHANNEL"),
        "tag": env.get("HUB_LOADER_TAG"),
        "jar": jar_name,
        "jar_on_disk": bool(jar_path and jar_path.exists()),
        "jar_size_bytes": (jar_path.stat().st_size if jar_path and jar_path.exists() else 0),
    }


# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/server/logs")
async def server_logs(tail: int = 200) -> dict:
    rec = _srv()
    tail = max(1, min(int(tail), 5000))
    rc, out, err = _sh("docker", "logs", "--tail", str(tail), rec["container"], timeout=10)
    if rc != 0:
        raise HTTPException(500, err.strip() or out.strip())
    return {"ok": True, "lines": (out + err).splitlines()}


@app.get("/api/server/logs/stream")
async def server_logs_stream():
    from fastapi.responses import StreamingResponse
    rec = _srv()
    container = rec["container"]

    async def event_gen():
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "-f", "--tail", "120", container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                yield f"data: {text}\n\n"
        finally:
            try:
                proc.terminate()
                await proc.wait()
            except Exception:
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/api/server/host")
async def server_host() -> dict:
    rec = _srv()
    port = _paths(rec)["port"]
    lan = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        lan = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return {"lan_address": f"{lan}:{port}" if lan else None,
            "lan_ip": lan, "port": port}


# ─────────────────────────────────────────────────────────────


# Docker image tags from itzg/minecraft-server (from
# docker-minecraft-server_readthedocs_io/versions/java.md).
# `latest`/`stable` shift to track Mojang's required Java for newest MC.
ITZG_IMAGE_TAGS = [
    {"tag": "latest",         "java": "25", "label": "latest (Java 25 — auto-tracks newest MC)", "recommended": True},
    {"tag": "stable",         "java": "25", "label": "stable (Java 25 — pinned release of latest)"},
    {"tag": "java25",         "java": "25", "label": "java25 (Ubuntu, Hotspot)"},
    {"tag": "java25-jdk",     "java": "25", "label": "java25-jdk (Ubuntu, Hotspot+JDK)"},
    {"tag": "java25-alpine",  "java": "25", "label": "java25-alpine (Alpine)"},
    {"tag": "java21",         "java": "21", "label": "java21 (Ubuntu, Hotspot) — MC 1.20.5–1.21.x"},
    {"tag": "java21-jdk",     "java": "21", "label": "java21-jdk (Ubuntu, Hotspot+JDK)"},
    {"tag": "java21-alpine",  "java": "21", "label": "java21-alpine (Alpine)"},
    {"tag": "java17",         "java": "17", "label": "java17 — MC 1.17–1.20.4"},
    {"tag": "java16",         "java": "16", "label": "java16 — MC 1.16 only"},
    {"tag": "java11",         "java": "11", "label": "java11 — old MC"},
    {"tag": "java8",          "java": "8",  "label": "java8 — legacy MC (1.8–1.16)"},
]
ITZG_IMAGE_BASE = "itzg/minecraft-server"


def _java_tag_for_mc(mc_version: str | None) -> str:
    """Backwards-compat wrapper — returns just the tag string.
    New code should use _java_recommendation() which also returns the reason.
    """
    return _java_recommendation(None, mc_version)["tag"]


# Server types where itzg's `latest` tag is unsafe because the loader's
# bundled Mixin/ASM lags Mojang's Java bumps. Arclight 1.21.1 fails on
# Java 25 (class file major 69) because its Mixin lib only handles up to 21.
# These types must always be pinned to a specific java<N> tag.
_PIN_JAVA_TYPES = {
    "ARCLIGHT", "MOHIST", "MAGMA", "MAGMA_MAINTAINED", "BANNER", "YOUER",
    "KETTING", "CRUCIBLE", "FORGE", "NEOFORGE", "FABRIC", "QUILT",
    "SPONGEVANILLA",
}


def _java_recommendation(type_key: str | None, mc_version: str | None) -> dict:
    """Pick the right itzg image tag for a given type + MC version, and
    explain why. Returns: {tag, java, reason, type_key, mc_version}.

    Policy: we always pin a specific java<N> tag (never `latest` or `stable`)
    because itzg's `latest` shifts when Mojang bumps Java, and that silently
    breaks hybrid loaders (Arclight, Mohist) whose Mixin libs lag the JVM.
    Specific tags also make recreates deterministic.
    """
    tk = (type_key or "").upper()
    mv = (mc_version or "").strip()
    # Resolve LATEST sentinel — caller should pass a concrete version, but
    # if we get LATEST we punt to the broadest modern tag.
    if not mv or mv.upper() == "LATEST":
        return {
            "tag": "java21", "java": "21",
            "reason": "MC version is LATEST (unresolved) — defaulting to Java 21, the runtime for 1.20.5–1.21.x.",
            "type_key": tk, "mc_version": mv,
        }
    parts = [int(x) for x in re.findall(r"\d+", mv)[:3]]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]

    if major >= 26:
        # Mojang's post-1.21.x scheme. Real MC 26.x ships on Java 25.
        if tk in _PIN_JAVA_TYPES:
            return {
                "tag": "java25", "java": "25",
                "reason": (f"MC {mv} requires Java 25. {tk.title()} is a hybrid/mod-loader "
                           f"so we pin :java25 instead of :latest (itzg's :latest can shift "
                           f"when Mojang bumps Java, breaking the loader's Mixin runtime)."),
                "type_key": tk, "mc_version": mv,
            }
        return {
            "tag": "java25", "java": "25",
            "reason": f"MC {mv} requires Java 25 (Mojang's new versioning scheme).",
            "type_key": tk, "mc_version": mv,
        }

    if major == 1:
        if minor > 20 or (minor == 20 and patch >= 5):
            return {
                "tag": "java21", "java": "21",
                "reason": (f"MC {mv} requires Java 21 (Mojang bumped at 1.20.5). "
                           f"Pinned :java21 so the runtime stays stable across image updates."),
                "type_key": tk, "mc_version": mv,
            }
        if minor >= 17:
            return {
                "tag": "java17", "java": "17",
                "reason": f"MC {mv} requires Java 17 (1.17 bumped from 16; 1.20.4 is the last 17-required release).",
                "type_key": tk, "mc_version": mv,
            }
        if minor == 16:
            return {
                "tag": "java16", "java": "16",
                "reason": f"MC {mv} runs on Java 16.",
                "type_key": tk, "mc_version": mv,
            }
    return {
        "tag": "java8", "java": "8",
        "reason": f"MC {mv} is legacy — uses Java 8.",
        "type_key": tk, "mc_version": mv,
    }


@app.get("/api/java-runtime")
async def java_runtime(type_key: str = "PAPER", version: str = "") -> dict:
    """Type+version-aware Java runtime recommendation for the UI.

    The UI calls this whenever Type or MC Version changes so it can show
    the user *which* Java tag will be used and *why*. The same logic is
    invoked server-side on recreate to actually bump the image tag.
    """
    rec = _java_recommendation(type_key, version)
    # Find the matching itzg image tag entry so the UI can show a nice label.
    matched = next((t for t in ITZG_IMAGE_TAGS if t["tag"] == rec["tag"]), None)
    if matched:
        rec["label"] = matched["label"]
    return rec


@app.get("/api/server/config")
async def server_config() -> dict:
    rec = _srv()
    paths = _paths(rec)
    env = compose.get_env(paths["compose"])
    raw_type = (env.get("TYPE") or "PAPER").upper()
    # If this server is a custom-routed loader (Arclight, etc.) the actual
    # TYPE= is CUSTOM but the logical type the user picked lives in HUB_LOADER_TYPE.
    hub_loader = (env.get("HUB_LOADER_TYPE") or "").upper()
    type_key = hub_loader if hub_loader in custom_jar.CUSTOM_ROUTED_TYPES else raw_type
    image = compose.get_image(paths["compose"]) or f"{ITZG_IMAGE_BASE}:latest"
    image_base, _, image_tag = image.partition(":")
    return {
        "compose_path": str(paths["compose"]),
        "supported_types": [
            {
                "key": t.key, "display": t.display, "family": t.family,
                "description": t.description,
                "extra_fields": [
                    {"key": k, "label": lab, "hint": hint, "kind": kind}
                    for k, lab, hint, kind in t.extra_fields
                ],
                "auto_latest": bool(t.auto_latest),
                "notes": t.notes,
            } for t in st.SERVER_TYPES
        ],
        "current_type": {
            "key": type_key,
            "display": st.get_type(type_key).display,
            "family": st.get_type(type_key).family,
        },
        "image": image,
        "image_base": image_base or ITZG_IMAGE_BASE,
        "image_tag": image_tag or "latest",
        "image_tags": ITZG_IMAGE_TAGS,
        "java_recommended": _java_recommendation(type_key, env.get("VERSION")),
        "editable": sorted(compose.EDITABLE_ENV),
        "env": env,
    }


@app.post("/api/server/config")
async def update_server_config(payload: dict) -> dict:
    rec = _srv()
    paths = _paths(rec)
    changes = payload.get("changes") or {}
    image = payload.get("image")
    recreate = bool(payload.get("recreate"))
    if not isinstance(changes, dict):
        raise HTTPException(400, "changes must be a dict")
    if not changes and not image:
        raise HTTPException(400, "no changes provided")

    current_env = compose.get_env(paths["compose"])
    new_type = changes.get("TYPE")
    cur_type = current_env.get("TYPE", "PAPER")
    if new_type and new_type.upper() != cur_type.upper():
        if not payload.get("force"):
            raise HTTPException(409, (
                f"changing TYPE from {cur_type} to {new_type} will likely "
                f"break the existing world / plugins. Re-send with "
                f'{{"force": true}} to confirm.'
            ))

    new_env = current_env
    if changes:
        try:
            new_env = compose.update_env(paths["compose"], changes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except FileNotFoundError as e:
            raise HTTPException(500, str(e))

    new_image = None
    if image:
        try:
            new_image = compose.set_image(paths["compose"], image)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except FileNotFoundError as e:
            raise HTTPException(500, str(e))

    result: dict = {"ok": True, "env": new_env, "image": new_image, "recreated": False}
    if recreate:
        # Refresh the custom-routed loader jar (Arclight, etc.) if applicable.
        loader_status = await _ensure_loader_jar(paths)
        if loader_status:
            result["loader"] = loader_status
        # If the user edited VERSION/TYPE but didn't touch the image picker,
        # auto-bump the image tag so we don't recreate into a broken Java mismatch.
        if not new_image:
            bump = _auto_bump_image_details(paths)
            if bump:
                result["image"] = bump["new_image"]
                result["image_bumped"] = True
                result["image_bump"] = bump
        rc, output = await compose.compose_recreate(paths["compose"])
        result["recreated"] = (rc == 0)
        result["output"] = output[-2000:]
        if rc != 0:
            raise HTTPException(500, output[-1000:])
    return result


@app.get("/api/server/latest")
async def server_latest() -> dict:
    rec = _srv()
    paths = _paths(rec)
    env = compose.get_env(paths["compose"])
    raw_type = (env.get("TYPE") or "PAPER").upper()
    hub_loader = (env.get("HUB_LOADER_TYPE") or "").upper()
    # Display + auto-latest dispatch uses the logical loader for custom-routed types
    type_key = hub_loader if hub_loader in custom_jar.CUSTOM_ROUTED_TYPES else raw_type
    t = st.get_type(type_key)

    # What's actually running, read from disk (version_history.json / jar)
    running_v, running_b = _server_version_build(rec, paths)
    # What the compose file says (what would be applied on next recreate)
    configured_v, configured_b = _server_version_configured(rec, paths)

    running = {"version": running_v, "build": running_b}
    configured = {"version": configured_v, "build": configured_b}

    # "Pending restart" = compose was edited but container hasn't recreated yet
    pending_restart = (
        running_v is not None and configured_v is not None
        and (running_v != configured_v or str(running_b or "") != str(configured_b or ""))
    )

    # For custom-routed loaders (Arclight) we pick the latest using the
    # server's actual configured MC version + subtype + channel so we don't
    # accidentally recommend a 1.21.1 build for a 1.20.4 server.
    if type_key in custom_jar.CUSTOM_ROUTED_TYPES and type_key == "ARCLIGHT":
        mc_v = configured_v or running_v or env.get("VERSION") or "1.21.1"
        sub = (env.get("HUB_LOADER_SUBTYPE") or "forge").lower()
        chan = (env.get("HUB_LOADER_CHANNEL") or "snapshot").lower()
        build = await custom_jar.arclight_latest(mc_v, sub, chan)
        latest = ({"version": build.mc_version, "build": build.tag}
                  if build else None)
    else:
        latest = await st.get_latest(type_key) if t.auto_latest else None
    update_available = False
    if latest:
        # Compare LATEST against what is actually RUNNING (not the compose file).
        # This was the bug: after a failed recreate, compose said "26.1.2/68" but
        # the container was still on "1.21.10/129", and we declared "up to date".
        same_version = (latest.get("version") and latest["version"] == running_v)
        same_build = (str(latest.get("build") or "") == str(running_b or ""))
        update_available = not (same_version and same_build)

    # Pending Java/image change — does the configured MC version want a
    # different itzg Java tag than what's pinned in the compose image right now?
    pending_java = None
    current_image = compose.get_image(paths["compose"]) or ""
    cur_base, _, cur_tag = current_image.partition(":")
    if (cur_base or ITZG_IMAGE_BASE) == ITZG_IMAGE_BASE:
        rec_java = _java_recommendation(type_key, configured_v or running_v)
        if cur_tag and cur_tag != rec_java["tag"]:
            pending_java = {
                "old_tag": cur_tag, "new_tag": rec_java["tag"],
                "reason": rec_java["reason"], "java": rec_java["java"],
            }

    return {
        "type": {"key": t.key, "display": t.display, "family": t.family},
        "auto_latest_supported": bool(t.auto_latest),
        "current": running,         # backward compat — but now means RUNNING
        "running": running,
        "configured": configured,
        "pending_restart": pending_restart,
        "pending_java_change": pending_java,
        "latest": latest,
        "update_available": update_available,
        "notes": t.notes,
    }


@app.get("/api/server/paper/versions")
async def paper_versions(type_key: str | None = None) -> dict:
    if type_key is None:
        rec = _srv()
        paths = _paths(rec)
        env = compose.get_env(paths["compose"])
        type_key = (env.get("TYPE") or "PAPER").upper()
    else:
        type_key = type_key.upper()

    async def _fill(project: str) -> list[str]:
        async with _client() as c:
            r = await c.get(f"https://fill.papermc.io/v3/projects/{project}")
            if r.status_code != 200:
                return []
            raw = r.json()
            flat: list[str] = []
            vs = raw.get("versions") or {}
            if isinstance(vs, dict):
                for _major, subs in vs.items():
                    if isinstance(subs, list):
                        flat.extend(subs)
            elif isinstance(vs, list):
                flat = list(vs)
            return flat

    flat: list[str] = []
    if type_key in ("PAPER", "PUFFERFISH"):
        flat = await _fill("paper")
    elif type_key == "FOLIA":
        flat = await _fill("folia")
    elif type_key == "PURPUR":
        async with _client() as c:
            r = await c.get("https://api.purpurmc.org/v2/purpur")
            if r.status_code == 200:
                flat = r.json().get("versions") or []
    elif type_key == "VANILLA":
        async with _client() as c:
            r = await c.get("https://launchermeta.mojang.com/mc/game/version_manifest.json")
            if r.status_code == 200:
                d = r.json()
                flat = [v["id"] for v in d.get("versions", []) if v.get("type") == "release"]
    elif type_key == "FABRIC":
        async with _client() as c:
            r = await c.get("https://meta.fabricmc.net/v2/versions/game")
            if r.status_code == 200:
                flat = [g["version"] for g in r.json() if g.get("stable")]
    elif type_key == "FORGE":
        async with _client() as c:
            r = await c.get("https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml")
            if r.status_code == 200:
                versions = re.findall(r"<version>([^<]+)</version>", r.text)
                seen: list[str] = []
                for v in versions:
                    mc = v.split("-", 1)[0]
                    if mc and mc not in seen:
                        seen.append(mc)
                flat = seen
    elif type_key == "NEOFORGE":
        async with _client() as c:
            r = await c.get("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
            if r.status_code == 200:
                versions = r.json().get("versions") or []
                seen: list[str] = []
                for v in versions:
                    nums = re.findall(r"\d+", v)
                    if len(nums) < 2:
                        continue
                    major = int(nums[0])
                    if major >= 26:
                        mc = f"{nums[0]}.{nums[1]}"
                    else:
                        mc = f"1.{nums[0]}.{nums[1]}"
                    if mc not in seen:
                        seen.append(mc)
                flat = seen
    elif type_key == "ARCLIGHT":
        # Source: arclight.izzel.io CDN — same data the official site uses,
        # MC-version-indexed (the GitHub releases path used to live here is
        # stale; only the broken 1.0.1 stable lives there for 1.21.1).
        flat = await custom_jar.arclight_list_versions()
    else:
        flat = []

    stable = [v for v in flat if not re.search(r"(pre|rc|snapshot|beta|alpha)", v, re.I)]

    def _key(v: str) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", v))[:4]

    stable.sort(key=_key, reverse=True)
    return {"type": type_key, "versions": stable, "all_versions": flat}


@app.get("/api/server/paper")
async def paper_builds(type_key: str | None = None, version: str | None = None) -> dict:
    rec = _srv()
    paths = _paths(rec)
    env = compose.get_env(paths["compose"])
    if type_key is None:
        type_key = (env.get("TYPE") or "PAPER").upper()
    else:
        type_key = type_key.upper()
    current_version, current_build = _server_version_build(rec, paths)
    target_version = version or current_version
    if not target_version:
        raise HTTPException(400, "no version specified and no current version")

    async def _fill_builds(project: str) -> list[dict]:
        async with _client() as c:
            r = await c.get(f"https://fill.papermc.io/v3/projects/{project}/versions/{target_version}/builds")
            return r.json() if r.status_code == 200 else []

    builds: list[dict] = []
    if type_key in ("PAPER", "PUFFERFISH"):
        builds = await _fill_builds("paper")
    elif type_key == "FOLIA":
        builds = await _fill_builds("folia")
    elif type_key == "PURPUR":
        async with _client() as c:
            r = await c.get(f"https://api.purpurmc.org/v2/purpur/{target_version}")
            if r.status_code == 200:
                d = r.json()
                all_b = (d.get("builds") or {}).get("all") or []
                latest_b = (d.get("builds") or {}).get("latest")
                builds = [{"id": b, "channel": "STABLE",
                           "is_latest": str(b) == str(latest_b)}
                          for b in reversed(all_b)]
    elif type_key == "FORGE":
        async with _client() as c:
            r = await c.get("https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml")
            if r.status_code == 200:
                versions = re.findall(r"<version>([^<]+)</version>", r.text)
                prefix = f"{target_version}-"
                filt = [v[len(prefix):] for v in versions if v.startswith(prefix)]
                filt.sort(key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v))[:4],
                          reverse=True)
                builds = [{"id": v, "channel": "STABLE"} for v in filt[:50]]
    elif type_key == "NEOFORGE":
        async with _client() as c:
            r = await c.get("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
            if r.status_code == 200:
                versions = r.json().get("versions") or []
        nums = re.findall(r"\d+", target_version)
        prefix = None
        if target_version.startswith("1.") and len(nums) >= 2:
            prefix = f"{nums[1]}." + (f"{nums[2]}." if len(nums) >= 3 else "")
        elif len(nums) >= 2:
            prefix = f"{nums[0]}.{nums[1]}."
        if prefix:
            filt = [v for v in versions if v.startswith(prefix)]
            filt.sort(key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v))[:4],
                      reverse=True)
            builds = [{"id": v,
                       "channel": "BETA" if "beta" in v.lower() else "STABLE"}
                      for v in filt[:50]]
    elif type_key == "FABRIC":
        async with _client() as c:
            r = await c.get("https://meta.fabricmc.net/v2/versions/loader")
            if r.status_code == 200:
                loaders = r.json()
                builds = [{"id": v["version"],
                           "channel": "STABLE" if v.get("stable") else "BETA"}
                          for v in loaders[:50]]
    elif type_key == "ARCLIGHT":
        # Source: arclight.izzel.io CDN. Combine stable + snapshot channels;
        # snapshot first since that's the actively-maintained channel for
        # 1.21.x (stable 1.0.1 is the known-broken release).
        sub = "forge"  # default if unspecified — generic endpoint, server tab uses arclight-specific endpoint
        cdn_builds = await custom_jar.arclight_list_builds(
            target_version, sub, include_snapshots=True, include_stable=True,
        )
        builds = [
            {
                "id": b.tag,
                "channel": ("SNAPSHOT" if b.channel == "snapshot" else "STABLE"),
                "is_latest": (i == 0),
                "published_at": b.published_at,
            }
            for i, b in enumerate(cdn_builds[:50])
        ]

    latest = builds[0] if builds else None
    return {
        "type": type_key,
        "version": target_version,
        "current_build": current_build,
        "latest": latest,
        "update_available": bool(latest and str(latest.get("id")) != str(current_build)),
        "builds": builds[:50],
    }


@app.post("/api/server/update")
async def server_update(payload: dict | None = None) -> dict:
    rec = _srv()
    paths = _paths(rec)
    payload = payload or {}
    env = compose.get_env(paths["compose"])
    type_key = (env.get("TYPE") or "PAPER").upper()
    target_version = payload.get("version")
    target_build = payload.get("build")
    target_loader = payload.get("loader_version")
    do_recreate = payload.get("recreate", False)

    current = st.current_for_type(type_key, env)
    version = target_version or current.get("version")
    if not version and type_key != "CUSTOM":
        raise HTTPException(400, "no version specified and current unknown")

    if not target_build and not target_loader and st.get_type(type_key).auto_latest:
        latest = await st.get_latest(type_key)
        if latest:
            version = latest.get("version") or version
            target_build = latest.get("build")

    changes: dict[str, str] = {}
    if version:
        changes["VERSION"] = str(version)

    build_env_map = {
        "PAPER": "PAPER_BUILD", "FOLIA": "FOLIA_BUILD", "PURPUR": "PURPUR_BUILD",
        "LEAF": "LEAF_BUILD", "PUFFERFISH": "PUFFERFISH_BUILD",
        "MOHIST": "MOHIST_BUILD", "YOUER": "MOHIST_BUILD", "BANNER": "MOHIST_BUILD",
        "LIMBO": "LIMBO_BUILD", "CANYON": "CANYON_BUILD",
    }
    if target_build is not None:
        bkey = build_env_map.get(type_key)
        if bkey:
            changes[bkey] = str(target_build)

    loader_env_map = {
        "FORGE": "FORGE_VERSION", "NEOFORGE": "NEOFORGE_VERSION",
        "FABRIC": "FABRIC_LOADER_VERSION", "QUILT": "QUILT_LOADER_VERSION",
    }
    if target_loader is not None:
        lkey = loader_env_map.get(type_key)
        if lkey:
            changes[lkey] = str(target_loader)

    if not changes:
        return {"ok": True, "skipped": True, "reason": "nothing to change"}

    if all(env.get(k) == v for k, v in changes.items()):
        return {"ok": True, "skipped": True, "reason": "already on requested version/build",
                "current": current}

    new_env = compose.update_env(paths["compose"], changes)

    # Auto-bump the docker image's Java tag if the target MC version needs it.
    # Default ON. Opt out with {"auto_image": false}.
    auto_image = None
    image_bump_details = None
    if payload.get("auto_image", True):
        image_bump_details = _auto_bump_image_details(paths)
        auto_image = image_bump_details["new_image"] if image_bump_details else None
    image_bumped = bool(auto_image)

    result = {
        "ok": True,
        "from": current,
        "to": {
            "version": new_env.get("VERSION"),
            "build": new_env.get(build_env_map.get(type_key, "")),
        },
        "changes": changes,
        "env": new_env,
        "image": auto_image,
        "image_bumped": image_bumped,
        "image_bump": image_bump_details,
        "recreated": False,
        "note": "Recreate container to apply (Server tab → Recreate, or set recreate=true).",
    }
    if do_recreate:
        rc, output = await compose.compose_recreate(paths["compose"])
        result["recreated"] = rc == 0
        result["output"] = output[-2000:]
        if rc != 0:
            raise HTTPException(500, output[-1000:])
    return result


# --- routes: plugins --------------------------------------------------------

@app.get("/api/plugins")
async def list_plugins() -> dict:
    rec = _srv()
    paths = _paths(rec)
    plugins_dir = paths["plugins"]
    update_dir = paths["update"]
    plugins = []
    if plugins_dir.exists():
        for jar in sorted(plugins_dir.glob("*.jar")):
            plugins.append(_parse_plugin_jar(jar))
    pending = sorted(j.name for j in update_dir.glob("*.jar")) if update_dir.exists() else []
    return {"plugins": plugins, "dir": str(plugins_dir), "pending_updates": pending}


@app.get("/api/registry")
async def installable_registry(fast: bool = False) -> dict:
    """Return the per-server plugin catalog + premium-plugin list.

    Set ``fast=true`` to skip live Spiget version lookups (use cached values
    only). The UI passes fast=true on every refresh that follows a user
    interaction; the slow path is reserved for the explicit Refresh button.
    """
    rec = _srv()
    paths = _paths(rec)
    plugins_dir = paths["plugins"]
    installed_names = set()
    # Build a (lowercased plugin-name → installed_info) map so we can match
    # premium entries by display name when filenames don't carry a spigot_id.
    installed_by_name: dict[str, dict] = {}
    if plugins_dir.exists():
        for jar in plugins_dir.glob("*.jar"):
            info = _parse_plugin_jar(jar)
            if info["name"]:
                installed_names.add(registry.normalize(info["name"]))
                installed_by_name[info["name"].strip().lower()] = info
    items = []
    catalog = _get_server_catalog(rec["id"])
    for k, v in catalog.items():
        items.append({
            "key": k,
            "display": v["display"],
            "sources": [{"source": s[0], "ref": s[1]} for s in v["sources"]],
            "installed": k in installed_names,
        })

    # Merge built-in + user-added premium entries. Built-in wins on duplicate
    # spigot_id so the user can't shadow our curated alternatives list.
    user_premium = _load_json(_user_premium_path(rec["id"]), {})
    builtin_ids = {p["spigot_id"] for p in registry.PREMIUM_PLUGINS}
    merged: list[dict] = list(registry.PREMIUM_PLUGINS)
    for sid, entry in user_premium.items():
        if sid in builtin_ids:
            continue
        merged.append({
            "display": entry.get("display") or f"Spigot #{sid}",
            "spigot_id": sid,
            "url": entry.get("url") or f"https://www.spigotmc.org/resources/{sid}/",
            "note": entry.get("note") or "User-added premium plugin.",
            "icon": entry.get("icon"),
            "alternatives": [],
            "user_added": True,
        })

    # Attach live latest_version + installed_version for each premium entry.
    # Spiget cache keeps this cheap; failures degrade gracefully to None.
    # In fast mode we skip live lookups entirely — only cached values are
    # used, and a cache miss returns None. The user can force a slow refresh
    # via the Refresh button (which omits fast=true).
    staged_mem = _load_json(_staged_path(rec["id"]), {})

    async def _lookup_one(client, p):
        sid = str(p.get("spigot_id") or "")
        if not sid:
            return None
        if fast:
            cached = _SPIGET_CACHE.get(sid)
            return cached[1] if cached else None
        return await _spiget_latest_version(client, sid)

    async with _client() as c:
        # Parallel fan-out — each merged entry's Spiget call runs concurrently.
        latests = await asyncio.gather(*[_lookup_one(c, p) for p in merged])
        for p, latest in zip(merged, latests):
            p["latest"] = latest  # {version, release_date_utc, ...} or None
            # Match installed jar by:
            #   1) staged-memory entry whose source mentions this spigot_id
            #   2) display-name prefix match (e.g. "[Official] mcMMO …" ↔ "mcMMO")
            sid = str(p.get("spigot_id") or "")
            installed_info = None
            for fn, meta in staged_mem.items():
                if str(meta.get("source", "")).endswith(f"-{sid}"):
                    installed_info = installed_by_name.get(fn.lower().replace(".jar", ""), None)
                    if not installed_info:
                        # Look up by parsing the jar so we get the real plugin.yml version.
                        jar = plugins_dir / fn
                        if jar.exists():
                            installed_info = _parse_plugin_jar(jar)
                    break
            if not installed_info:
                # Try matching by display name (case-insensitive substring).
                disp = (p.get("display") or "").lower()
                for nm, info in installed_by_name.items():
                    if nm and (nm in disp or disp.startswith(nm)):
                        installed_info = info
                        break
            p["installed"] = bool(installed_info)
            p["installed_version"] = (installed_info or {}).get("version") or None
            p["installed_file"] = (installed_info or {}).get("file") or None
            # Update status: only flag when both versions are known AND differ.
            lv = (latest or {}).get("version")
            iv = p["installed_version"]
            if lv and iv and str(lv).strip() != str(iv).strip():
                p["update_available"] = True
            else:
                p["update_available"] = False

    return {"items": items, "premium": merged}


@app.post("/api/catalog")
async def add_catalog_entry(payload: dict) -> dict:
    """Add or update a catalog entry for the current server.

    Required: ``key`` (lowercase plugin.yml-style name) and ``display``.
    Optional: ``sources`` (list of {source, ref} dicts). Sources for known
    providers: modrinth (slug), hangar (Owner/Project), spiget (numeric ID),
    geyser (geyser|floodgate), github (owner/repo[/glob]).

    If ``key`` already exists, the entry is replaced (this is also the
    "edit" path — frontend passes the full new state).
    """
    rec = _srv()
    key = registry.normalize(payload.get("key") or "")
    if not key:
        raise HTTPException(400, "key is required")
    display = (payload.get("display") or "").strip()
    if not display:
        raise HTTPException(400, "display is required")
    raw_sources = payload.get("sources") or []
    if not isinstance(raw_sources, list):
        raise HTTPException(400, "sources must be a list")
    sources: list[list[str]] = []
    valid_sources = {"modrinth", "hangar", "spiget", "geyser", "github"}
    for s in raw_sources:
        if not isinstance(s, dict):
            raise HTTPException(400, "each source must be {source, ref}")
        src = (s.get("source") or "").strip().lower()
        ref = (s.get("ref") or "").strip()
        if src not in valid_sources:
            raise HTTPException(400, f"unknown source {src!r}; allowed: {sorted(valid_sources)}")
        if not ref:
            raise HTTPException(400, f"empty ref for source {src!r}")
        sources.append([src, ref])
    catalog = _get_server_catalog(rec["id"])
    is_update = key in catalog
    catalog[key] = {"display": display, "sources": sources}
    _save_server_catalog(rec["id"], catalog)
    audit.event("catalog_entry_saved", key=key, display=display,
                source_count=len(sources), is_update=is_update)
    return {"ok": True, "key": key, "is_update": is_update}


@app.delete("/api/catalog/{key}")
async def remove_catalog_entry(key: str) -> dict:
    """Remove a catalog entry from the current server's catalog. Does NOT
    touch any installed jar; only removes the auto-install/update wiring.
    """
    rec = _srv()
    catalog = _get_server_catalog(rec["id"])
    key = registry.normalize(key)
    if key not in catalog:
        raise HTTPException(404, f"no catalog entry with key {key!r}")
    catalog.pop(key)
    _save_server_catalog(rec["id"], catalog)
    audit.event("catalog_entry_removed", key=key)
    return {"ok": True}


@app.post("/api/catalog/reset-defaults")
async def reset_catalog_defaults() -> dict:
    """Reset the current server's catalog to DEFAULT_CATALOG_SEED (or
    REGISTRY for test-fred). Wipes user edits — use with confirmation.
    """
    rec = _srv()
    path = _catalog_path(rec["id"])
    if path.exists():
        path.unlink()
    catalog = _get_server_catalog(rec["id"])  # re-seeds
    audit.event("catalog_reset", entries=len(catalog))
    return {"ok": True, "entries": len(catalog)}


@app.post("/api/premium")
async def add_premium_plugin(payload: dict) -> dict:
    """Add a user-curated premium plugin to the catalog.

    Required: ``spigot_id`` (integer or numeric string). Optional: ``display``,
    ``url``, ``note``. If display/url are omitted we fetch them from Spiget.
    """
    rec = _srv()
    sid = str(payload.get("spigot_id") or "").strip()
    if not sid.isdigit():
        raise HTTPException(400, "spigot_id must be a numeric Spigot resource ID")
    # Don't duplicate a built-in.
    if any(p["spigot_id"] == sid for p in registry.PREMIUM_PLUGINS):
        raise HTTPException(409, f"Spigot ID {sid} is already in the built-in premium catalog")
    path = _user_premium_path(rec["id"])
    mem = _load_json(path, {})
    if sid in mem:
        raise HTTPException(409, f"Spigot ID {sid} is already in the catalog")
    # Resolve display + tag from Spiget if not provided.
    display = (payload.get("display") or "").strip()
    note = (payload.get("note") or "").strip()
    url = (payload.get("url") or "").strip() or f"https://www.spigotmc.org/resources/{sid}/"
    icon = payload.get("icon")
    async with _client() as c:
        latest = await _spiget_latest_version(c, sid)
    if not display and latest:
        display = latest.get("plugin_name") or f"Spigot #{sid}"
    if not display:
        display = f"Spigot #{sid}"
    if not note and latest and latest.get("tag"):
        note = latest["tag"]
    mem[sid] = {
        "display": display, "spigot_id": sid, "url": url, "note": note,
        "icon": icon, "added_at": time.time(),
        "premium_confirmed": bool((latest or {}).get("premium")),
    }
    _save_json(path, mem)
    audit.event("premium_plugin_added", spigot_id=sid, display=display,
                premium_confirmed=mem[sid]["premium_confirmed"])
    return {"ok": True, "spigot_id": sid, "display": display,
            "premium_confirmed": mem[sid]["premium_confirmed"]}


@app.delete("/api/premium/{spigot_id}")
async def remove_premium_plugin(spigot_id: str) -> dict:
    """Remove a user-added premium plugin from the catalog. Built-in entries
    are protected — only user-added entries can be removed."""
    rec = _srv()
    if any(p["spigot_id"] == spigot_id for p in registry.PREMIUM_PLUGINS):
        raise HTTPException(403, "built-in premium entries cannot be removed")
    path = _user_premium_path(rec["id"])
    mem = _load_json(path, {})
    if spigot_id not in mem:
        raise HTTPException(404, f"no user-added premium plugin with Spigot ID {spigot_id}")
    mem.pop(spigot_id)
    _save_json(path, mem)
    audit.event("premium_plugin_removed", spigot_id=spigot_id)
    return {"ok": True}


async def _resolve_for_plugin(
    client: httpx.AsyncClient, info: dict, mc_version: str,
    catalog: dict[str, dict] | None = None,
) -> dict:
    entry = {
        "file": info["file"], "name": info["name"],
        "current_version": info["version"],
        "source": None, "matched_via": None, "latest_version": None,
        "download_url": None, "project_url": None, "icon": None,
        "description": None, "update_available": False, "error": None,
        "registry_key": None,
    }
    try:
        reg_hit = _catalog_find(catalog, info["name"] or info["file"]) if catalog else registry.find(info["name"] or info["file"])
        if reg_hit:
            key, entry_def = reg_hit
            entry["registry_key"] = key
            sources = entry_def["sources"]
            # Catalog stores sources as [source, ref] lists; REGISTRY as tuples.
            for source, ref in (tuple(s) for s in sources):
                resolved = await resolvers.resolve_source(client, source, ref, mc_version)
                if resolved:
                    _apply_resolved(entry, resolved)
                    return _finalize(entry)

        if info.get("sha512"):
            r = await resolvers.resolve_modrinth_by_hash(client, info["sha512"], mc_version)
            if r:
                _apply_resolved(entry, r)
                return _finalize(entry)

        if info.get("name"):
            r = await resolvers.resolve_modrinth_search(client, info["name"], mc_version)
            if r:
                _apply_resolved(entry, r)
                return _finalize(entry)
    except Exception as e:
        entry["error"] = f"{type(e).__name__}: {e}"[:200]
    return _finalize(entry)


def _apply_resolved(entry: dict, r: resolvers.ResolvedVersion) -> None:
    entry["source"] = r.source
    entry["matched_via"] = r.matched_via
    entry["latest_version"] = r.version
    entry["download_url"] = r.download_url
    entry["filename"] = r.filename
    entry["project_url"] = r.project_url
    entry["icon"] = r.icon
    entry["description"] = r.description


def _finalize(entry: dict) -> dict:
    if entry["latest_version"] and entry["current_version"]:
        entry["update_available"] = resolvers.version_newer(
            entry["latest_version"], entry["current_version"]
        )
    return entry


def _apply_staged_memory(entry: dict, staged: dict, update_dir: Path) -> dict:
    """Cross-reference the staged-update memory with the current scan.

    Two states for each remembered plugin:

    1. STAGED — the jar is sitting in /plugins/update/ waiting on a restart.
       Show as pending_restart, suppress update_available if staged ≥ latest.

    2. APPLIED — the staged jar is gone from /plugins/update/. Paper consumed
       it on boot and the new jar is now in /plugins/. The new jar's parsed
       plugin.yml may still report a generic version like "5.9.2-SNAPSHOT"
       (especially for Hangar plugins) so we override `current_version` with
       what we actually installed. Compare upstream-latest against THAT, not
       against the lying plugin.yml field.

    Mutates entry in place and returns it.
    """
    entry["pending_restart"] = False
    entry["staged_version"] = None
    entry["applied_version"] = None
    rec = staged.get(entry["file"])
    if not rec:
        return entry

    staged_jar_name = rec.get("staged_filename", "")
    staged_jar = update_dir / staged_jar_name if staged_jar_name else None
    remembered_version = rec.get("staged_version")

    if staged_jar and staged_jar.exists():
        # State 1: still staged, waiting on restart
        entry["staged_version"] = remembered_version
        entry["pending_restart"] = True
        if entry.get("latest_version") and remembered_version:
            if not resolvers.version_newer(entry["latest_version"], remembered_version):
                entry["update_available"] = False
    else:
        # State 2: applied. Use our recorded version, not the parsed plugin.yml.
        entry["applied_version"] = remembered_version
        if remembered_version:
            # Override the displayed installed-version so the UI tells the truth
            entry["current_version"] = remembered_version
            # Recompute update_available against the real installed version
            if entry.get("latest_version"):
                entry["update_available"] = resolvers.version_newer(
                    entry["latest_version"], remembered_version
                )
            else:
                entry["update_available"] = False
    return entry


@app.post("/api/plugins/check")
async def check_all_updates() -> dict:
    rec = _srv()
    paths = _paths(rec)
    version, _ = _server_version_build(rec, paths)
    if not version:
        raise HTTPException(500, "no paper jar found")
    staged = _load_json(_staged_path(rec["id"]), {})
    cat_data = _get_server_catalog(rec["id"])
    update_dir = paths["update"]
    results = []
    async with _client() as c:
        jars = sorted(paths["plugins"].glob("*.jar"))
        infos = [_parse_plugin_jar(j) for j in jars]
        tasks = [_resolve_for_plugin(c, info, version, catalog=cat_data) for info in infos]
        for fut in asyncio.as_completed(tasks):
            entry = await fut
            _apply_staged_memory(entry, staged, update_dir)
            results.append(entry)
    results.sort(key=lambda r: r["file"])
    _save_json(_cache_path(rec["id"]),
               {"checked_at": time.time(), "mc_version": version, "results": results})
    return {"checked_at": time.time(), "mc_version": version, "results": results}


@app.post("/api/plugins/{filename}/stage-update")
async def stage_update(filename: str) -> dict:
    rec = _srv()
    paths = _paths(rec)
    cache = _load_json(_cache_path(rec["id"]), {"results": []})
    target = next((r for r in cache.get("results", []) if r["file"] == filename), None)
    if not target:
        raise HTTPException(404, "plugin not found in cache; run check first")
    if not target.get("download_url"):
        raise HTTPException(400, "no download URL known")
    # Filename of the new jar — prefer what the resolver gave us, fall back to old name
    new_name = target.get("filename") or filename
    new_name = re.sub(r"[^A-Za-z0-9._-]+", "_", new_name)
    if not new_name.endswith(".jar"):
        new_name += ".jar"
    dest = paths["update"] / new_name
    async with _client() as c:
        size = await _download_to(target["download_url"], dest, c)
    if not _is_jar(dest):
        dest.unlink(missing_ok=True)
        raise HTTPException(502, "downloaded file is not a valid jar")
    # Record what we staged so future `check` runs don't re-flag this
    staged = _load_json(_staged_path(rec["id"]), {})
    staged[filename] = {
        "staged_version": target.get("latest_version"),
        "staged_filename": new_name,
        "source": target.get("source"),
        "staged_at": time.time(),
    }
    _save_json(_staged_path(rec["id"]), staged)
    return {"ok": True, "staged": dest.name, "size": size,
            "staged_version": target.get("latest_version"),
            "note": "restart server to apply (Paper update folder)"}


@app.post("/api/plugins/stage-all")
async def stage_all_updates() -> dict:
    rec = _srv()
    cache = _load_json(_cache_path(rec["id"]), {"results": []})
    staged, skipped, failed = [], [], []
    for r in cache.get("results", []):
        if not r.get("update_available"):
            skipped.append(r["file"])
            continue
        try:
            res = await stage_update(r["file"])
            staged.append(res)
        except HTTPException as e:
            failed.append({"file": r["file"], "error": str(e.detail)})
        except Exception as e:
            failed.append({"file": r["file"], "error": f"{type(e).__name__}: {e}"[:200]})
    return {"staged": staged, "skipped": skipped, "failed": failed,
            "note": "restart server to apply all"}


@app.post("/api/plugins/install/{key}")
async def install_from_registry(key: str, immediate: bool = False) -> dict:
    """Install a catalog plugin.

    Default: stages the jar in /plugins/update/ and records it in staged-memory
    so the UI knows it's pending. Set ``immediate=true`` to drop it directly
    into /plugins/ (only safe when the server is offline).
    """
    rec = _srv()
    paths = _paths(rec)
    catalog = _get_server_catalog(rec["id"])
    entry_def = catalog.get(key)
    if not entry_def:
        raise HTTPException(404, f"unknown registry key: {key}")
    version, _ = _server_version_build(rec, paths)
    if not version:
        raise HTTPException(500, "no paper jar found")

    # Build the source list to try, then append automatic fallbacks so the
    # install path stays airtight even when an upstream source rots. The
    # display name is searched on Modrinth/Hangar/Spiget so a Spigot resource
    # that flipped to "external link" doesn't dead-end the user.
    sources: list[tuple[str, str]] = [
        tuple(s) for s in (entry_def.get("sources") or [])
    ]
    display = entry_def.get("display") or key
    # Don't duplicate refs already in the curated list.
    seen_refs = {(s, r) for s, r in sources}
    for src, ref in [
        ("modrinth", key),
        ("modrinth", display.lower().replace(" ", "-")),
        ("modrinth", _search_modrinth_),  # sentinel — search by display name
        ("hangar", _search_hangar_),
    ]:
        if isinstance(ref, str) and (src, ref) not in seen_refs:
            sources.append((src, ref))
            seen_refs.add((src, ref))
        elif callable(ref):
            sources.append((src, ref))

    last_errors: list[str] = []
    async with _client() as c:
        for source, ref in sources:
            try:
                # Sentinels are functions that perform a name-based search
                if callable(ref):
                    resolved = await ref(c, display, version)
                else:
                    resolved = await resolvers.resolve_source(c, source, ref, version)
            except Exception as e:
                last_errors.append(f"{source}:{ref!r}: {type(e).__name__}")
                continue
            if not resolved:
                last_errors.append(f"{source}:{ref}: no match")
                continue
            filename = resolved.filename or f"{entry_def['display']}.jar"
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
            # New installs go straight to plugins/ — they only start interacting
            # with the server on restart, so there's nothing to disrupt.
            # Updates (file already in plugins/) MUST stage to update/ so itzg's
            # update mechanism can swap them in cleanly. `immediate=true` overrides
            # and forces a direct write to plugins/ even when it's an update.
            plugins_target = paths["plugins"] / filename
            is_update = plugins_target.exists()
            if is_update and not immediate:
                paths["update"].mkdir(parents=True, exist_ok=True)
                dest = paths["update"] / filename
                staged = True
            else:
                paths["plugins"].mkdir(parents=True, exist_ok=True)
                dest = plugins_target
                staged = False
            try:
                size = await _download_to(resolved.download_url, dest, c)
            except HTTPException as e:
                last_errors.append(f"{source}:{ref}: dl {e.detail}")
                continue
            except Exception as e:
                last_errors.append(f"{source}:{ref}: dl {type(e).__name__}")
                continue
            if not _is_jar(dest):
                dest.unlink(missing_ok=True)
                last_errors.append(f"{source}:{ref}: not a jar")
                continue
            # Record staged-memory for BOTH paths so the UI's "pending" /
            # "recently added" lists pick up new installs too. The staged-memory
            # is purely a UI hint — it doesn't change where the jar lives.
            _record_staged(rec["id"], filename, resolved.version, f"catalog:{key}")
            audit.event("plugin_install_ok", file=filename, source=resolved.source,
                        version=resolved.version, staged=staged, is_update=is_update,
                        key=key, matched_via=resolved.matched_via)
            return {"ok": True, "key": key, "source": resolved.source,
                    "version": resolved.version, "file": filename, "size": size,
                    "staged": staged, "is_update": is_update,
                    "matched_via": resolved.matched_via,
                    "note": ("update staged in plugins/update/ — restart to apply"
                             if staged else "installed to plugins/ — loads on next restart")}
    audit.event("plugin_install_fail", key=key, errors=last_errors[:10])
    raise HTTPException(502, f"all sources failed for {key}: " + "; ".join(last_errors[:4]))


async def _search_modrinth_(client: httpx.AsyncClient, display: str, mc_version: str):
    """Last-resort fallback: search Modrinth for the plugin by display name."""
    return await resolvers.resolve_modrinth_search(client, display, mc_version)


async def _search_hangar_(client: httpx.AsyncClient, display: str, mc_version: str):
    """Last-resort fallback: search Hangar for the plugin by display name."""
    return await resolvers.resolve_hangar(client, display, mc_version)


def _record_staged(server_id: str, filename: str, version: str | None, source: str) -> None:
    """Write a staged-memory entry. Used by every install/update path so the
    UI can show "pending — restart to apply" and not re-flag the plugin on the
    next check cycle. Keyed by filename — overwrites prior staging for the
    same file."""
    try:
        staged_path = _staged_path(server_id)
        mem = _load_json(staged_path, {})
        mem[filename] = {
            "staged_version": version,
            "staged_filename": filename,
            "source": source,
            "staged_at": time.time(),
        }
        _save_json(staged_path, mem)
    except Exception:
        pass  # staging memory is best-effort; the jar itself is what counts


@app.post("/api/plugins/install-all-missing")
async def install_all_missing() -> dict:
    rec = _srv()
    paths = _paths(rec)
    installed = set()
    if paths["plugins"].exists():
        for jar in paths["plugins"].glob("*.jar"):
            info = _parse_plugin_jar(jar)
            if info["name"]:
                installed.add(registry.normalize(info["name"]))
    results = {"installed": [], "skipped": [], "failed": []}
    catalog = _get_server_catalog(rec["id"])
    for key in catalog:
        if key in installed:
            results["skipped"].append(key)
            continue
        try:
            res = await install_from_registry(key)
            results["installed"].append(res)
        except HTTPException as e:
            results["failed"].append({"key": key, "error": str(e.detail)})
        except Exception as e:
            results["failed"].append({"key": key, "error": f"{type(e).__name__}: {e}"[:200]})
    return results


@app.post("/api/plugins/upload")
async def upload_plugin(file: UploadFile = File(...), immediate: bool = False,
                        spigot_id: str | None = None) -> dict:
    """Manual jar upload from the user's browser.

    DEFAULT: updates (file already in /plugins/) stage to /plugins/update/ so
    itzg's update mechanism can swap them in cleanly on next restart. Fresh
    installs (no existing file) go straight to /plugins/ — they don't touch
    the running JVM until restart anyway, and putting new jars in /update/
    leaves them dangling because itzg only "applies" updates against
    pre-existing jars of the same name.

    Pass ``immediate=true`` to bypass staging for updates too — only safe
    when the server is offline.
    """
    rec = _srv()
    paths = _paths(rec)
    if not file.filename or not file.filename.lower().endswith(".jar"):
        raise HTTPException(400, "must be a .jar file")

    paths["plugins"].mkdir(parents=True, exist_ok=True)
    safe_name = os.path.basename(file.filename)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", safe_name)

    plugins_target = paths["plugins"] / safe_name
    is_update = plugins_target.exists()
    if is_update and not immediate:
        paths["update"].mkdir(parents=True, exist_ok=True)
        dest = paths["update"] / safe_name
        should_stage = True
    else:
        dest = plugins_target
        should_stage = False

    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Validate the bytes are actually a jar (zip with META-INF/), not an HTML
    # error page or a renamed exe. _is_jar peeks zip entries.
    if not _is_jar(tmp):
        tmp.unlink(missing_ok=True)
        audit.event("plugin_upload_rejected", file=safe_name,
                    reason="not_a_valid_jar", spigot_id=spigot_id)
        raise HTTPException(400, "uploaded file is not a valid .jar (zip with META-INF/)")
    size = tmp.stat().st_size
    tmp.replace(dest)

    # Record in staged-memory whether premium hand-off or not — so the UI's
    # "pending updates" list reflects all staged jars uniformly.
    if should_stage:
        source = f"manual-spigot-{spigot_id}" if spigot_id else "manual-upload"
        _record_staged(rec["id"], safe_name, None, source)

    audit.event("plugin_upload_ok", file=safe_name, size=size,
                staged=should_stage, is_update=is_update, spigot_id=spigot_id)
    return {"ok": True, "file": dest.name, "size": size,
            "staged": should_stage, "is_update": is_update,
            "note": "restart server to apply" if should_stage else "ready on next plugin reload"}


@app.delete("/api/plugins/{filename}")
async def delete_plugin(filename: str, immediate: bool = False) -> dict:
    """Delete a plugin.

    DEFAULT: staged deletion — records intent so the next scheduled restart
    removes the jar after the server shuts down. The plugin keeps running
    until then, so we don't yank a loaded jar out from under the JVM.

    Pass ``immediate=true`` to delete the file right now. Only safe if the
    server is already offline or if you accept that the plugin will probably
    misbehave or crash the server when it tries to access its missing jar.
    """
    rec = _srv()
    paths = _paths(rec)
    path = paths["plugins"] / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "no such plugin")
    if immediate:
        path.unlink()
        audit.event("plugin_deleted", file=filename, mode="immediate")
        return {"ok": True, "deleted": filename, "mode": "immediate"}
    # Stage the deletion. The actual removal happens in _apply_pending_deletions
    # right before each restart fires (called from _fire_scheduled_restart).
    deletions_path = _deletions_path(rec["id"])
    pending = _load_json(deletions_path, {})
    pending[filename] = {"staged_at": time.time()}
    _save_json(deletions_path, pending)
    audit.event("plugin_delete_staged", file=filename)
    return {"ok": True, "deleted": filename, "mode": "staged",
            "note": "deletion will apply on next restart"}


@app.delete("/api/plugins/{filename}/staged-delete")
async def cancel_staged_delete(filename: str) -> dict:
    """Cancel a pending staged deletion."""
    rec = _srv()
    deletions_path = _deletions_path(rec["id"])
    pending = _load_json(deletions_path, {})
    if filename not in pending:
        raise HTTPException(404, "no staged deletion for that plugin")
    pending.pop(filename)
    _save_json(deletions_path, pending)
    audit.event("plugin_delete_staged_cancelled", file=filename)
    return {"ok": True}


def _deletions_path(server_id: str) -> Path:
    return DATA_DIR / f"staged-deletions-{server_id}.json"


def _apply_pending_deletions(server_id: str) -> list[str]:
    """Remove every jar in the staged-deletions list. Returns the list of
    successfully deleted filenames. Called by _fire_scheduled_restart so the
    deletion happens AFTER the container is stopped/restarted (right before
    docker brings it back up, the jar is gone).
    """
    rec = servers.get(server_id)
    if not rec:
        return []
    paths = _paths(rec)
    deletions_path = _deletions_path(server_id)
    pending = _load_json(deletions_path, {})
    if not pending:
        return []
    removed: list[str] = []
    for filename in list(pending.keys()):
        path = paths["plugins"] / filename
        try:
            if path.exists():
                path.unlink()
                removed.append(filename)
            pending.pop(filename, None)
        except Exception as e:  # noqa: BLE001
            audit.event("plugin_delete_failed", file=filename, error=str(e)[:200])
    _save_json(deletions_path, pending)
    if removed:
        audit.event("plugin_deletions_applied", files=removed, count=len(removed))
    return removed


@app.delete("/api/plugins/{filename}/staged-install")
async def cancel_staged_install(filename: str) -> dict:
    """Cancel a pending staged install/update — remove the jar (from
    plugins/update/ if staged as an update, or from plugins/ if a fresh
    install) and drop its staged-memory entry. Used when the user clicks
    Cancel in the post-staging picker.
    """
    rec = _srv()
    paths = _paths(rec)
    update_target = paths["update"] / filename
    plugins_target = paths["plugins"] / filename
    # Prefer the staged-update dir; fall back to plugins/ for fresh installs.
    # Never touch a jar in plugins/ unless we have a matching staged-memory
    # entry — otherwise we could nuke a long-installed plugin the user just
    # happens to be re-resolving in the UI.
    staged_path = _staged_path(rec["id"])
    mem = _load_json(staged_path, {})
    had_mem = filename in mem
    removed = False
    if update_target.exists():
        try:
            update_target.unlink()
            removed = True
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"failed to remove {filename}: {e}") from e
    elif plugins_target.exists() and had_mem:
        # Fresh install lives directly in plugins/. Only remove if staged-memory
        # confirms WE put it there recently — never remove arbitrary plugins.
        try:
            plugins_target.unlink()
            removed = True
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"failed to remove {filename}: {e}") from e
    # Drop staged-memory entry even if the file was already gone.
    if had_mem:
        mem.pop(filename, None)
        _save_json(staged_path, mem)
    if not removed and not had_mem:
        raise HTTPException(404, "no staged install for that plugin")
    audit.event("plugin_staged_install_cancelled", file=filename, removed=removed)
    return {"ok": True, "removed": removed}


@app.get("/api/plugins/pending")
async def list_pending() -> dict:
    rec = _srv()
    paths = _paths(rec)
    update_dir = paths["update"]
    if not update_dir.exists():
        return {"pending": []}
    items = []
    for jar in sorted(update_dir.glob("*.jar")):
        sti = jar.stat()
        items.append({"file": jar.name, "size": sti.st_size, "mtime": sti.st_mtime})
    return {"pending": items}


# ─────────────────────────────────────────────────────────────
# Plugin search (across all upstreams) + install by source
# ─────────────────────────────────────────────────────────────

@app.get("/api/plugins/search")
async def plugin_search(q: str = "", limit: int = 8) -> dict:
    rec = _srv()
    paths = _paths(rec)
    version, _ = _server_version_build(rec, paths)
    async with _client() as c:
        hits = await resolvers.unified_search(c, q, version or "", limit=limit)
    return {"query": q, "mc_version": version, "hits": hits}


@app.post("/api/plugins/install-source")
async def install_from_source(payload: dict) -> dict:
    rec = _srv()
    paths = _paths(rec)
    source = payload.get("source")
    ref = payload.get("ref")
    display = payload.get("display") or ref
    immediate = bool(payload.get("immediate"))
    if not source or not ref:
        raise HTTPException(400, "source and ref are required")
    version, _ = _server_version_build(rec, paths)
    if not version:
        raise HTTPException(500, "no MC version configured")
    async with _client() as c:
        resolved = await resolvers.resolve_source(c, source, ref, version)
        if not resolved:
            raise HTTPException(502, f"{source}:{ref} returned no compatible build")
        filename = resolved.filename or f"{display}.jar"
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
        # Updates stage to plugins/update/; fresh installs go straight to plugins/.
        # The jar doesn't touch the running JVM either way — load happens on restart.
        plugins_target = paths["plugins"] / filename
        is_update = plugins_target.exists()
        if is_update and not immediate:
            paths["update"].mkdir(parents=True, exist_ok=True)
            dest = paths["update"] / filename
            staged = True
        else:
            paths["plugins"].mkdir(parents=True, exist_ok=True)
            dest = plugins_target
            staged = False
        size = await _download_to(resolved.download_url, dest, c)
    if not _is_jar(dest):
        dest.unlink(missing_ok=True)
        raise HTTPException(502, "downloaded file is not a valid jar")
    _record_staged(rec["id"], filename, resolved.version, f"{source}:{ref}")
    audit.event("plugin_install_source_ok", file=filename, source=resolved.source,
                version=resolved.version, staged=staged, is_update=is_update)
    return {"ok": True, "source": resolved.source, "version": resolved.version,
            "file": filename, "size": size, "staged": staged, "is_update": is_update,
            "note": ("update staged in plugins/update/ — restart to apply"
                     if staged else "installed to plugins/ — loads on next restart")}


@app.delete("/api/plugins/pending/{filename}")
async def cancel_pending(filename: str) -> dict:
    rec = _srv()
    paths = _paths(rec)
    path = paths["update"] / filename
    if not path.exists():
        raise HTTPException(404, "no such pending update")
    path.unlink()
    return {"ok": True, "cancelled": filename}


@app.post("/api/server/cancel-all-staged")
async def cancel_all_staged(payload: dict | None = None) -> dict:
    """Undo every staged change for the current server in one shot.

    Cancels:
      - all jars in plugins/update/ (pending plugin updates)
      - all entries in staged-deletions
      - all recent fresh-installs (jars in plugins/ added since the container
        started — undoes the install entirely)
      - the staged server version/build change (reverts compose env to match
        the currently-running container's actual env values)

    Pass {"include_installs": false} to keep recent fresh-installs.
    Pass {"include_server": false} to leave the compose server-version change alone.
    """
    payload = payload or {}
    include_installs = payload.get("include_installs", True)
    include_server = payload.get("include_server", True)

    rec = _srv()
    paths = _paths(rec)
    server_id = rec["id"]
    result: dict = {
        "cancelled_updates": [],
        "cancelled_deletions": [],
        "cancelled_installs": [],
        "reverted_server": None,
        "errors": [],
    }

    # 1) Pending updates — delete every jar in plugins/update/
    update_dir = paths["update"]
    if update_dir.exists():
        for jar in list(update_dir.glob("*.jar")):
            try:
                jar.unlink()
                result["cancelled_updates"].append(jar.name)
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"update {jar.name}: {e}")

    # 2) Pending deletions — clear the staged-deletions file
    deletions_path = _deletions_path(server_id)
    pending_del = _load_json(deletions_path, {})
    if pending_del:
        result["cancelled_deletions"] = sorted(pending_del.keys())
        _save_json(deletions_path, {})

    # 3) Fresh-installs — find every staged-memory entry with staged_at newer
    #    than container StartedAt and remove the jar from plugins/, then drop
    #    the staged-memory entry.
    if include_installs:
        # Reuse the same StartedAt-vs-staged_at comparison the status endpoint uses.
        rc, out, err = _sh("docker", "inspect", rec["container"], timeout=5)
        started_ts = 0.0
        try:
            data = json.loads(out)[0]
            sa = data.get("State", {}).get("StartedAt") or ""
            from datetime import datetime
            s = sa.replace("Z", "+00:00")
            if "." in s:
                head, _, tz = s.partition(".")
                frac, _, tail = tz.partition("+")
                s = f"{head}.{frac[:6]}+{tail}" if tail else f"{head}.{frac[:6]}"
            started_ts = datetime.fromisoformat(s).timestamp()
        except Exception:
            started_ts = 0.0
        staged_mem = _load_json(_staged_path(server_id), {})
        keep: dict = {}
        for fname, info in (staged_mem or {}).items():
            try:
                staged_at_v = float(info.get("staged_at") or 0)
            except Exception:
                staged_at_v = 0.0
            if started_ts > 0 and staged_at_v > started_ts:
                jar = paths["plugins"] / fname
                if jar.exists() and jar.is_file():
                    try:
                        jar.unlink()
                        result["cancelled_installs"].append(fname)
                    except Exception as e:  # noqa: BLE001
                        result["errors"].append(f"install {fname}: {e}")
                        keep[fname] = info
                # Drop the staged-memory regardless (jar already gone or removed)
            else:
                keep[fname] = info
        _save_json(_staged_path(server_id), keep)

    # 4) Server version/build change — revert compose env to match the
    #    container's running env. The container's env is the ground truth for
    #    "what's actually live"; the compose file is the staging buffer.
    if include_server:
        try:
            rc, out, err = _sh("docker", "inspect", rec["container"], timeout=5)
            running_env: dict[str, str] = {}
            if rc == 0:
                container_info = json.loads(out)[0]
                for kv in (container_info.get("Config", {}).get("Env") or []):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        running_env[k] = v
            file_env = compose.get_env(paths["compose"])
            # Compare ONLY the version/build-related keys
            keys_to_check = [
                "VERSION", "TYPE", "PAPER_BUILD", "FOLIA_BUILD", "PURPUR_BUILD",
                "LEAF_BUILD", "PUFFERFISH_BUILD", "MOHIST_BUILD", "LIMBO_BUILD",
                "CANYON_BUILD", "FORGE_VERSION", "NEOFORGE_VERSION",
                "FABRIC_LOADER_VERSION", "QUILT_LOADER_VERSION",
            ]
            revert: dict[str, str] = {}
            for k in keys_to_check:
                in_file = file_env.get(k)
                in_running = running_env.get(k)
                if in_file is not None and in_running is not None and in_file != in_running:
                    revert[k] = in_running
            if revert:
                compose.update_env(paths["compose"], revert)
                result["reverted_server"] = revert
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"server-revert: {e}")

    audit.event(
        "cancel_all_staged",
        updates=len(result["cancelled_updates"]),
        deletions=len(result["cancelled_deletions"]),
        installs=len(result["cancelled_installs"]),
        reverted_server=bool(result["reverted_server"]),
    )
    return result


# --- premium / spigot cookie fallbacks --------------------------------------


# --- Playwright fallback ----------------------------------------------------


# --- noVNC mini-desktop -----------------------------------------------------


# --- The orchestrated login flow --------------------------------------------

# Track the background login task so the UI can poll for completion
_login_task: dict = {"task": None, "result": None, "started_at": None}


# --- Browser extension distribution -----------------------------------------


# ─────────────────────────────────────────────────────────────────────────────
# Extension diagnostic log endpoint — extension POSTs every event here so we
# can see WTF is happening from the browser side without dev-tools access.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# DEV-ONLY: page firehose. Receives ALL console + fetch + XHR + error events
# from the extension's page-world instrumentor. Writes to date-stamped JSONL
# under logs/extension-firehose/ so I can grep/tail them from the host.
# Remove this endpoint + the page-world instrumentor when we're done.
# ─────────────────────────────────────────────────────────────────────────────


# --- logs API ----------------------------------------------------------------

@app.get("/api/logs/tail")
async def logs_tail(which: str = "api", limit: int = 100,
                    since_ts: float | None = None) -> dict:
    """Return the last N entries from the named log.

    which: "api" (HTTP requests) or "events" (cookie/download/error events).
    limit: max entries (default 100).
    since_ts: optional unix timestamp; only return entries newer than this.
    """
    if which not in ("api", "events"):
        raise HTTPException(400, "which must be 'api' or 'events'")
    return {"which": which, "entries": audit.tail(which, limit=limit, since_ts=since_ts)}


@app.delete("/api/logs")
async def logs_clear(which: str = "all") -> dict:
    if which not in ("all", "api", "events"):
        raise HTTPException(400, "which must be 'all', 'api', or 'events'")
    return audit.clear(which)


# --- static -----------------------------------------------------------------

STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _asset_version() -> str:
    try:
        latest = max(
            (p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file()),
            default=0,
        )
        return str(int(latest))
    except Exception:
        return str(int(time.time()))


from fastapi.responses import HTMLResponse as _HTMLResponse


@app.get("/", response_class=_HTMLResponse)
async def index() -> _HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    v = _asset_version()
    html = html.replace('href="/static/style.css"', f'href="/static/style.css?v={v}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={v}"')
    return _HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    })
