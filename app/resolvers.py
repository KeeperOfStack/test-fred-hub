"""Plugin source resolvers.

Each resolver is responsible for one upstream (Modrinth, Hangar, Geyser,
Spiget). Given a registry entry + the current MC version, it returns a
ResolvedVersion or None.

Registry entries declare an ordered list of sources to try, so a plugin can
fall back from "Spigot external link" → Modrinth → Hangar without code
duplication on the call site.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any

import httpx

UA = "test-fred-hub/0.2 (+https://localhost)"

MODRINTH_API = "https://api.modrinth.com/v2"
HANGAR_API = "https://hangar.papermc.io/api/v1"
GEYSER_API = "https://download.geysermc.org/v2"
SPIGET_API = "https://api.spiget.org/v2"
GITHUB_API = "https://api.github.com"

PAPER_LOADERS = ["paper", "bukkit", "spigot", "purpur", "folia"]


@dataclass
class ResolvedVersion:
    source: str                # "modrinth" | "hangar" | "geyser" | "spiget"
    project_key: str           # slug / id / resource id
    version: str               # human-readable version string
    download_url: str
    filename: str | None = None
    project_url: str | None = None
    icon: str | None = None
    description: str | None = None
    matched_via: str | None = None    # "hash" | "registry" | "name-search"

    def dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Modrinth
# ─────────────────────────────────────────────────────────────────────────────

async def modrinth_by_hash(client: httpx.AsyncClient, sha512: str) -> dict | None:
    r = await client.get(
        f"{MODRINTH_API}/version_file/{sha512}",
        params={"algorithm": "sha512"},
    )
    return r.json() if r.status_code == 200 else None


async def modrinth_project(client: httpx.AsyncClient, slug_or_id: str) -> dict | None:
    r = await client.get(f"{MODRINTH_API}/project/{slug_or_id}")
    return r.json() if r.status_code == 200 else None


async def modrinth_latest_for_mc(
    client: httpx.AsyncClient,
    slug_or_id: str,
    mc_version: str,
    allow_any_loader: bool = False,
) -> dict | None:
    params: dict[str, Any] = {
        "loaders": json.dumps(PAPER_LOADERS),
        "game_versions": json.dumps([mc_version]),
    }
    r = await client.get(
        f"{MODRINTH_API}/project/{slug_or_id}/version", params=params
    )
    if r.status_code != 200:
        return None
    versions = r.json()
    if versions:
        return versions[0]
    if not allow_any_loader:
        return None
    # last-ditch: drop loader filter
    r = await client.get(
        f"{MODRINTH_API}/project/{slug_or_id}/version",
        params={"game_versions": json.dumps([mc_version])},
    )
    if r.status_code == 200:
        v = r.json()
        return v[0] if v else None
    return None


def _modrinth_to_resolved(
    proj: dict | None, ver: dict, matched_via: str
) -> ResolvedVersion | None:
    files = ver.get("files") or []
    primary = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not primary:
        return None
    slug = (proj or {}).get("slug") or ver.get("project_id")
    return ResolvedVersion(
        source="modrinth",
        project_key=slug or "",
        version=ver.get("version_number") or ver.get("name") or "?",
        download_url=primary["url"],
        filename=primary.get("filename"),
        project_url=f"https://modrinth.com/plugin/{slug}" if slug else None,
        icon=(proj or {}).get("icon_url"),
        description=(proj or {}).get("description"),
        matched_via=matched_via,
    )


async def resolve_modrinth(
    client: httpx.AsyncClient, slug: str, mc_version: str
) -> ResolvedVersion | None:
    ver = await modrinth_latest_for_mc(client, slug, mc_version, allow_any_loader=True)
    if not ver:
        return None
    proj = await modrinth_project(client, slug)
    return _modrinth_to_resolved(proj, ver, "registry")


async def resolve_modrinth_by_hash(
    client: httpx.AsyncClient, sha512: str, mc_version: str
) -> ResolvedVersion | None:
    hit = await modrinth_by_hash(client, sha512)
    if not hit:
        return None
    proj_id = hit.get("project_id")
    proj = await modrinth_project(client, proj_id) if proj_id else None
    latest = (
        await modrinth_latest_for_mc(client, proj_id, mc_version, allow_any_loader=True)
        if proj_id else None
    ) or hit
    return _modrinth_to_resolved(proj, latest, "sha512")


async def resolve_modrinth_search(
    client: httpx.AsyncClient, name: str, mc_version: str
) -> ResolvedVersion | None:
    facets = [["project_type:plugin"], [f"versions:{mc_version}"]]
    r = await client.get(
        f"{MODRINTH_API}/search",
        params={"query": name, "facets": json.dumps(facets), "limit": 5},
    )
    if r.status_code != 200:
        return None
    hits = r.json().get("hits", [])
    if not hits:
        return None
    lname = name.lower()
    hit = next(
        (h for h in hits
         if h.get("title", "").lower() == lname or h.get("slug", "").lower() == lname),
        hits[0],
    )
    proj_id = hit.get("project_id") or hit.get("slug")
    if not proj_id:
        return None
    return await resolve_modrinth(client, proj_id, mc_version)


# ─────────────────────────────────────────────────────────────────────────────
# Hangar
# ─────────────────────────────────────────────────────────────────────────────

def _hangar_paper_matches(version: dict, mc_version: str) -> bool:
    plat = (version.get("platformDependencies") or {}).get("PAPER") or []
    if mc_version in plat:
        return True
    # accept "1.21.x" style entries
    base = ".".join(mc_version.split(".")[:2])
    return any(p == f"{base}.x" or p.startswith(base) for p in plat)


async def resolve_hangar(
    client: httpx.AsyncClient, slug: str, mc_version: str
) -> ResolvedVersion | None:
    """slug is "Owner/Project" or just "Project" (we'll look up the owner)."""
    if "/" in slug:
        owner, project = slug.split("/", 1)
    else:
        # name search to find owner
        r = await client.get(f"{HANGAR_API}/projects", params={"query": slug, "limit": 5})
        if r.status_code != 200:
            return None
        results = r.json().get("result", [])
        lname = slug.lower()
        proj = next((p for p in results if p.get("name", "").lower() == lname),
                    results[0] if results else None)
        if not proj:
            return None
        ns = proj.get("namespace", {})
        owner, project = ns.get("owner"), ns.get("slug") or proj.get("name")
        if not (owner and project):
            return None

    r = await client.get(
        f"{HANGAR_API}/projects/{owner}/{project}/versions",
        params={"limit": 25},
    )
    if r.status_code != 200:
        return None
    chosen = None
    for v in r.json().get("result", []):
        if _hangar_paper_matches(v, mc_version):
            chosen = v
            break
    if not chosen:
        return None
    dls = (chosen.get("downloads") or {}).get("PAPER") or {}
    url = dls.get("downloadUrl") or dls.get("externalUrl")
    if not url:
        # Hangar download endpoint also works as a stable URL
        url = f"{HANGAR_API}/projects/{owner}/{project}/versions/{chosen.get('name')}/PAPER/download"

    # try to get project metadata for icon/description
    rp = await client.get(f"{HANGAR_API}/projects/{owner}/{project}")
    proj_data = rp.json() if rp.status_code == 200 else {}
    return ResolvedVersion(
        source="hangar",
        project_key=f"{owner}/{project}",
        version=chosen.get("name", "?"),
        download_url=url,
        filename=(dls.get("fileInfo") or {}).get("name"),
        project_url=f"https://hangar.papermc.io/{owner}/{project}",
        icon=proj_data.get("avatarUrl"),
        description=proj_data.get("description"),
        matched_via="registry",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Geyser
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_geyser(
    client: httpx.AsyncClient, project: str, mc_version: str
) -> ResolvedVersion | None:
    """project is 'geyser' or 'floodgate'. mc_version unused — Geyser supports all."""
    r = await client.get(f"{GEYSER_API}/projects/{project}/versions/latest/builds/latest")
    if r.status_code != 200:
        return None
    data = r.json()
    downloads = data.get("downloads") or {}
    # prefer 'spigot' artifact (works on Paper), fall back to first
    artifact = downloads.get("spigot") or next(iter(downloads.values()), None)
    if not artifact:
        return None
    build = data.get("build")
    version = data.get("version")
    url = f"{GEYSER_API}/projects/{project}/versions/{version}/builds/{build}/downloads/{'spigot' if 'spigot' in downloads else next(iter(downloads))}"
    return ResolvedVersion(
        source="geyser",
        project_key=project,
        version=f"{version} build {build}",
        download_url=url,
        filename=artifact.get("name"),
        project_url=f"https://geysermc.org/download?project={project}",
        icon="https://geysermc.org/img/logos/geyser.png" if project == "geyser" else None,
        description=f"GeyserMC {project} (Bedrock ↔ Java bridge)",
        matched_via="registry",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Spiget (SpigotMC mirror API)
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_spiget(
    client: httpx.AsyncClient, resource_id: int | str, mc_version: str
) -> ResolvedVersion | None:
    rid = str(resource_id)
    r = await client.get(f"{SPIGET_API}/resources/{rid}")
    if r.status_code != 200:
        return None
    data = r.json()
    fileinfo = data.get("file") or {}
    # latest version string
    rv = await client.get(f"{SPIGET_API}/resources/{rid}/versions/latest")
    ver_name = rv.json().get("name", "?") if rv.status_code == 200 else "?"

    if data.get("premium"):
        return None  # can't download premium without login

    if fileinfo.get("type") == "external":
        ext = fileinfo.get("externalUrl") or ""
        # only honor external links that look like direct jar downloads
        if ext.endswith(".jar") or "/releases/download/" in ext or "/artifact/" in ext:
            url = ext
            filename = ext.rsplit("/", 1)[-1] if "/" in ext else None
        else:
            return None  # patreon/hangar HTML/etc — can't use
    else:
        # The Spiget direct-download endpoint returns the actual file ONLY for
        # non-premium, non-external resources. For premium it returns a JSON
        # error or HTML; the caller's content-type check catches that.
        url = f"https://api.spiget.org/v2/resources/{rid}/download"
        filename = f"{data.get('name', 'plugin')}.jar"

    return ResolvedVersion(
        source="spiget",
        project_key=rid,
        version=ver_name,
        download_url=url,
        filename=filename,
        project_url=f"https://www.spigotmc.org/resources/{rid}/",
        icon=(data.get("icon") or {}).get("url") or None,
        description=(data.get("tag") or "").strip() or None,
        matched_via="registry",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_source(
    client: httpx.AsyncClient, source: str, ref: str, mc_version: str
) -> ResolvedVersion | None:
    if source == "modrinth":
        return await resolve_modrinth(client, ref, mc_version)
    if source == "hangar":
        return await resolve_hangar(client, ref, mc_version)
    if source == "geyser":
        return await resolve_geyser(client, ref, mc_version)
    if source == "spiget":
        return await resolve_spiget(client, ref, mc_version)
    if source == "github":
        return await resolve_github(client, ref, mc_version)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Releases
# ─────────────────────────────────────────────────────────────────────────────

import fnmatch


async def resolve_github(
    client: httpx.AsyncClient, ref: str, mc_version: str
) -> ResolvedVersion | None:
    """ref = 'owner/repo' or 'owner/repo/<glob>' (e.g. 'mcMMO-Dev/mcMMO/*.jar').

    Picks the first asset on the latest non-prerelease that matches the glob
    (defaults to '*.jar'). MC version is ignored — GitHub releases don't carry
    that metadata; the user picked this source intentionally.
    """
    parts = ref.split("/", 2)
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    glob = parts[2] if len(parts) >= 3 else "*.jar"

    r = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/releases?per_page=10")
    if r.status_code != 200:
        return None
    for rel in r.json():
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for asset in rel.get("assets") or []:
            name = asset.get("name", "")
            if fnmatch.fnmatch(name.lower(), glob.lower()):
                return ResolvedVersion(
                    source="github",
                    project_key=f"{owner}/{repo}",
                    version=rel.get("tag_name") or rel.get("name") or "?",
                    download_url=asset.get("browser_download_url"),
                    filename=name,
                    project_url=rel.get("html_url") or f"https://github.com/{owner}/{repo}",
                    icon=f"https://github.com/{owner}.png?size=80",
                    description=(rel.get("name") or "")[:200] or None,
                    matched_via="registry",
                )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Unified search across all upstreams
# ─────────────────────────────────────────────────────────────────────────────

async def unified_search(
    client: httpx.AsyncClient, query: str, mc_version: str, limit: int = 8
) -> list[dict]:
    """Concurrent search of Modrinth + Hangar + Spiget. Returns normalized hits."""
    if not query.strip():
        return []
    q = query.strip()

    async def _modrinth() -> list[dict]:
        facets = [["project_type:plugin"]]
        if mc_version:
            facets.append([f"versions:{mc_version}"])
        r = await client.get(
            f"{MODRINTH_API}/search",
            params={"query": q, "facets": json.dumps(facets), "limit": limit},
        )
        if r.status_code != 200:
            return []
        out = []
        for h in r.json().get("hits", [])[:limit]:
            out.append({
                "source": "modrinth",
                "ref": h.get("slug") or h.get("project_id"),
                "title": h.get("title"),
                "summary": (h.get("description") or "")[:200],
                "icon": h.get("icon_url"),
                "downloads": h.get("downloads"),
                "url": f"https://modrinth.com/plugin/{h.get('slug')}" if h.get("slug") else None,
            })
        return out

    async def _hangar() -> list[dict]:
        r = await client.get(
            f"{HANGAR_API}/projects",
            params={"query": q, "limit": limit, "platform": "PAPER"},
        )
        if r.status_code != 200:
            return []
        out = []
        for p in r.json().get("result", [])[:limit]:
            ns = p.get("namespace") or {}
            owner, slug = ns.get("owner"), ns.get("slug") or p.get("name")
            if not (owner and slug):
                continue
            out.append({
                "source": "hangar",
                "ref": f"{owner}/{slug}",
                "title": p.get("name"),
                "summary": (p.get("description") or "")[:200],
                "icon": p.get("avatarUrl"),
                "downloads": (p.get("stats") or {}).get("downloads"),
                "url": f"https://hangar.papermc.io/{owner}/{slug}",
            })
        return out

    async def _spiget() -> list[dict]:
        r = await client.get(
            f"{SPIGET_API}/search/resources/{q}",
            params={"size": limit, "fields": "id,name,tag,downloads,icon,premium,file,price"},
        )
        if r.status_code != 200:
            return []
        out = []
        for p in r.json()[:limit]:
            f = p.get("file") or {}
            is_premium = bool(p.get("premium"))
            # For non-premium, filter out external links we can't download
            if not is_premium and f.get("type") == "external":
                ext = (f.get("externalUrl") or "")
                if not (ext.endswith(".jar") or "/releases/download/" in ext):
                    continue
            out.append({
                "source": "spiget",
                "ref": str(p.get("id")),
                "title": p.get("name"),
                "summary": (p.get("tag") or "").strip()[:200],
                "icon": (p.get("icon") or {}).get("url"),
                "downloads": p.get("downloads"),
                "url": f"https://www.spigotmc.org/resources/{p.get('id')}/",
                "premium": is_premium,
                "price": p.get("price"),
            })
        return out

    results = await asyncio.gather(
        _modrinth(), _hangar(), _spiget(),
        return_exceptions=True,
    )
    hits: list[dict] = []
    for r in results:
        if isinstance(r, list):
            hits.extend(r)
    # de-dup by (lowercase title) preferring modrinth → hangar → spiget order
    seen: dict[str, dict] = {}
    for h in hits:
        key = (h.get("title") or "").lower().strip()
        if not key:
            continue
        if key in seen:
            continue
        seen[key] = h
    # sort by downloads (best-effort)
    return sorted(seen.values(),
                  key=lambda h: -(h.get("downloads") or 0))[:limit * 2]


import asyncio  # late import to keep it grouped with usage above


# ─────────────────────────────────────────────────────────────────────────────
# Version comparison (best-effort)
# ─────────────────────────────────────────────────────────────────────────────

_VER_RE = re.compile(r"\d+")


def version_newer(latest: str, current: str) -> bool:
    """Best-effort 'is `latest` newer than `current`'.

    Strategy: extract every numeric run from both strings (including build
    numbers after '+' or '-'), pad shorter with zeros, lexicographically
    compare. Handles cases like 5.9.2-SNAPSHOT+1002 > 5.9.2-SNAPSHOT.
    """
    if not latest or not current:
        return False
    if latest.strip() == current.strip():
        return False

    def nums(s: str) -> list[int]:
        return [int(x) for x in _VER_RE.findall(s)][:6]

    a, b = nums(latest), nums(current)
    while len(a) < len(b): a.append(0)
    while len(b) < len(a): b.append(0)
    if a != b:
        return a > b
    # numeric tie — SNAPSHOT/dev/beta in current but not latest → newer
    cu, lu = current.upper(), latest.upper()
    for tag in ("SNAPSHOT", "DEV", "BETA", "ALPHA", "RC"):
        if tag in cu and tag not in lu:
            return True
    return False
