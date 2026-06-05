"""Source adapters for server types where itzg's auto-deploy is broken.

For Arclight (and eventually Mohist / Pufferfish / SpongeVanilla) we fetch the
jar ourselves and route it through `TYPE=CUSTOM` + `CUSTOM_SERVER=/data/<jar>`.
The UI keeps the original type label — the user picks "Arclight" and we
silently translate.

Hub-internal compose markers (NOT itzg env vars — prefixed HUB_):
  HUB_LOADER_TYPE    - logical type user picked (ARCLIGHT, ...)
  HUB_LOADER_TAG     - source build identifier (used for update detection)
  HUB_LOADER_JAR     - jar filename inside /data
  HUB_LOADER_SUBTYPE - for ARCLIGHT: forge / neoforge / fabric
  HUB_LOADER_CHANNEL - stable / snapshot (user preference for "latest")
  HUB_LOADER_SOURCE  - arclight_io / upload

Primary source for Arclight: the same CDN that arclight.izzel.io reads from —
  https://files.hypoglycemia.icu/v1/files/arclight/minecraft/<MC>/loaders/<LOADER>/versions-<channel>/

This is MC-version-indexed and has every snapshot back to 2025. Examples for
1.21.1/forge: 80+ snapshots between 2025-02 and 2026-05, plus stable 1.0.1.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

UA = "test-fred-hub/0.6 (+custom-jar)"
ARCLIGHT_CDN_BASE = "https://files.hypoglycemia.icu/v1/files/arclight/minecraft"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )


@dataclass
class JarBuild:
    """One downloadable jar candidate."""
    tag: str                   # canonical id used for update-detection (e.g. "snapshot/1.0.2-SNAPSHOT-0769551")
    asset_name: str            # filename to save on disk
    download_url: str
    mc_version: str
    subtype: str = ""          # forge/neoforge/fabric for arclight
    channel: str = ""          # "stable" or "snapshot"
    source: str = "arclight_io"
    published_at: str = ""     # ISO timestamp
    size_bytes: int = 0
    note: str = ""             # optional UI hint


# ─────────────────────────────────────────────────────────────────────────────
# Cache — keep API hits down; refresh every 10 minutes
# ─────────────────────────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 600.0


def _cached(key: str) -> Any | None:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _put_cache(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


def clear_cache() -> None:
    _CACHE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Arclight — arclight.izzel.io CDN
# ─────────────────────────────────────────────────────────────────────────────

async def _cdn_list(path: str) -> list[dict]:
    """List a CDN directory. Returns the `files` array (or [])."""
    url = f"{ARCLIGHT_CDN_BASE}/{path}".rstrip("/") + "/"
    cached = _cached(f"list:{url}")
    if cached is not None:
        return cached
    async with _client() as c:
        try:
            r = await c.get(url)
            data = (r.json() or {}).get("files", []) if r.status_code == 200 else []
        except Exception:
            data = []
    _put_cache(f"list:{url}", data)
    return data


async def arclight_list_versions() -> list[str]:
    """All MC versions Arclight publishes builds for (newest first)."""
    files = await _cdn_list("")
    versions = [f["name"] for f in files if f.get("name")]

    def _key(v: str) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", v))[:4]

    return sorted(versions, key=_key, reverse=True)


async def arclight_list_subtypes(mc_version: str) -> list[str]:
    """Available loader variants for an MC version (forge/neoforge/fabric)."""
    files = await _cdn_list(f"{mc_version}/loaders")
    return [f["name"] for f in files
            if f.get("type") == "directory" and f.get("name") in {"forge", "neoforge", "fabric"}]


async def arclight_list_builds(
    mc_version: str,
    subtype: str = "forge",
    include_snapshots: bool = True,
    include_stable: bool = True,
) -> list[JarBuild]:
    """List Arclight builds for (mc_version, subtype), newest first.

    Combines `versions-stable/` and `versions-snapshot/` directories.
    """
    sub = (subtype or "forge").lower()
    out: list[JarBuild] = []

    if include_stable:
        listing = await _cdn_list(f"{mc_version}/loaders/{sub}/versions-stable")
        for entry in listing:
            if entry.get("type") not in ("object", "file"):
                continue
            sid = entry["name"]
            jar_name = f"arclight-{sub}-{mc_version}-{sid}.jar"
            out.append(JarBuild(
                tag=f"stable/{sid}",
                asset_name=jar_name,
                download_url=f"{ARCLIGHT_CDN_BASE}/{mc_version}/loaders/{sub}/versions-stable/{sid}",
                mc_version=mc_version,
                subtype=sub,
                channel="stable",
                source="arclight_io",
                published_at=entry.get("last-modified") or "",
                size_bytes=int(entry.get("size") or 0),
                note="stable",
            ))

    if include_snapshots:
        listing = await _cdn_list(f"{mc_version}/loaders/{sub}/versions-snapshot")
        for entry in listing:
            if entry.get("type") not in ("object", "file"):
                continue
            sid = entry["name"]
            jar_name = f"arclight-{sub}-{mc_version}-{sid}.jar"
            out.append(JarBuild(
                tag=f"snapshot/{sid}",
                asset_name=jar_name,
                download_url=f"{ARCLIGHT_CDN_BASE}/{mc_version}/loaders/{sub}/versions-snapshot/{sid}",
                mc_version=mc_version,
                subtype=sub,
                channel="snapshot",
                source="arclight_io",
                published_at=entry.get("last-modified") or "",
                size_bytes=int(entry.get("size") or 0),
                note="snapshot",
            ))

    out.sort(key=lambda b: b.published_at or "", reverse=True)
    return out


async def arclight_latest(
    mc_version: str,
    subtype: str = "forge",
    channel: str = "snapshot",
) -> JarBuild | None:
    """Pick the newest Arclight jar in the requested channel.

    channel: "snapshot" (default for Arclight — stable is often broken),
             "stable", or "any" (picks newest regardless).
    """
    channel = (channel or "snapshot").lower()
    if channel == "stable":
        builds = await arclight_list_builds(mc_version, subtype, include_snapshots=False, include_stable=True)
    elif channel == "any":
        builds = await arclight_list_builds(mc_version, subtype, include_snapshots=True, include_stable=True)
    else:
        builds = await arclight_list_builds(mc_version, subtype, include_snapshots=True, include_stable=False)
    return builds[0] if builds else None


# ─────────────────────────────────────────────────────────────────────────────
# Download / file management
# ─────────────────────────────────────────────────────────────────────────────

async def download_jar(build: JarBuild, data_dir: Path) -> Path:
    """Stream-download `build.download_url` into `data_dir/asset_name`.

    Atomic: writes to `.partial`, then renames. Skips if file matches size.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = data_dir / build.asset_name

    if dest.exists() and build.size_bytes and dest.stat().st_size == build.size_bytes:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()

    async with _client() as c:
        async with c.stream("GET", build.download_url) as r:
            if r.status_code != 200:
                raise RuntimeError(f"download failed: HTTP {r.status_code} for {build.download_url}")
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)

    tmp.replace(dest)
    return dest


def list_jars_in_data_dir(data_dir: Path) -> list[dict[str, Any]]:
    p = Path(data_dir)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for jar in sorted(p.glob("*.jar")):
        try:
            st = jar.stat()
            out.append({"name": jar.name, "size_bytes": st.st_size, "mtime": st.st_mtime})
        except OSError:
            pass
    return out


def remove_jar(data_dir: Path, jar_name: str) -> bool:
    p = Path(data_dir).resolve()
    target = (p / jar_name).resolve()
    if target.suffix != ".jar":
        return False
    try:
        target.relative_to(p)
    except ValueError:
        return False
    if not target.exists():
        return False
    target.unlink()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Registry of custom-routed types
# ─────────────────────────────────────────────────────────────────────────────

CUSTOM_ROUTED_TYPES: set[str] = {"ARCLIGHT"}


async def resolve_build(
    type_key: str,
    mc_version: str,
    build_hint: str | None = None,
    subtype: str | None = None,
    channel: str | None = None,
) -> JarBuild | None:
    """Resolve a JarBuild for one of the custom-routed types.

    build_hint:
      - None / "latest" / "" → newest in requested channel
      - tag (e.g. "snapshot/1.0.2-SNAPSHOT-0769551") → exact match
      - substring of any tag or asset name → first match
    channel: "snapshot" (default for arclight), "stable", or "any"
    """
    type_key = (type_key or "").upper()
    if type_key != "ARCLIGHT":
        return None

    sub = (subtype or "forge").lower()
    ch = (channel or "snapshot").lower()

    if not build_hint or build_hint.lower() in ("latest", ""):
        return await arclight_latest(mc_version, sub, ch)

    builds = await arclight_list_builds(mc_version, sub)
    for b in builds:
        if build_hint == b.tag or build_hint == b.asset_name:
            return b
    for b in builds:
        if build_hint in b.tag or build_hint in b.asset_name:
            return b
    return None
