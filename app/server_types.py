"""Server-type metadata: per-TYPE config schema + latest-version lookups.

For each itzg/minecraft-server TYPE, this module knows:
  - Display name + family (paper-like, mod-loader, vanilla, hybrid, etc.)
  - Which env vars are relevant (so the Server tab only shows useful fields)
  - How to find the "latest stable" version+build (where automated)

Sourced from https://docker-minecraft-server.readthedocs.io/.
Types where automated latest-lookup isn't possible (or is messy enough that
shipping broken would be worse than shipping nothing) fall back to
auto_latest=None and the Updates tab tells the user to configure manually.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx


# ─────────────────────────────────────────────────────────────────────────────
# Type registry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ServerType:
    key: str               # itzg TYPE value
    display: str           # human label
    family: str            # "paper", "mod-loader", "vanilla", "hybrid", "legacy", "limbo", "custom"
    description: str
    # env vars beyond the universal {TYPE, VERSION, EULA, MEMORY, MOTD, ICON, OVERRIDE_ICON}
    # each tuple: (env_name, label, hint, kind), kind in {"text","build","loader_version"}
    extra_fields: list[tuple[str, str, str, str]] = field(default_factory=list)
    auto_latest: str | None = None   # name of an async fn in this module, or None
    notes: str = ""


SERVER_TYPES: list[ServerType] = [
    # ── Paper family ────────────────────────────────────────────────
    ServerType(
        key="PAPER", display="Paper", family="paper",
        description="PaperMC — high-performance Bukkit/Spigot-compatible server. The default for plugins.",
        extra_fields=[
            ("PAPER_BUILD", "Paper Build", 'leave blank or "latest" for newest', "build"),
            ("PAPER_CHANNEL", "Paper Channel", '"default" or "experimental"', "text"),
        ],
        auto_latest="paper_latest",
    ),
    ServerType(
        key="FOLIA", display="Folia", family="paper",
        description="PaperMC Folia — regionized multithreading. Plugin-compatible but experimental.",
        extra_fields=[
            ("FOLIA_BUILD", "Folia Build", "experimental builds only", "build"),
            ("FOLIA_CHANNEL", "Folia Channel", '"experimental"', "text"),
        ],
        auto_latest="folia_latest",
    ),
    ServerType(
        key="PURPUR", display="Purpur", family="paper",
        description="Drop-in Paper replacement with extra config knobs and gameplay tweaks.",
        extra_fields=[
            ("PURPUR_BUILD", "Purpur Build", '"LATEST" or specific number', "build"),
        ],
        auto_latest="purpur_latest",
    ),
    ServerType(
        key="LEAF", display="Leaf", family="paper",
        description="Paper fork focused on low-level performance optimizations.",
        extra_fields=[("LEAF_BUILD", "Leaf Build", "leave blank for latest", "build")],
        auto_latest=None,  # API endpoint unreliable as of writing
    ),
    ServerType(
        key="PUFFERFISH", display="Pufferfish", family="paper",
        description="Heavily optimized Paper fork tuned for large servers.",
        extra_fields=[("PUFFERFISH_BUILD", "Pufferfish Build", "Jenkins build number", "build")],
        auto_latest=None,
    ),

    # ── Bukkit/Spigot legacy ────────────────────────────────────────
    ServerType(
        key="SPIGOT", display="Spigot", family="legacy",
        description="Legacy Spigot. itzg docs recommend using Paper instead — getbukkit no longer supports automated downloads.",
        extra_fields=[],
        notes="getbukkit.org broke automation. Paper is fully Spigot-plugin-compatible — switch to PAPER.",
    ),
    ServerType(
        key="BUKKIT", display="Bukkit", family="legacy",
        description="Legacy Bukkit. itzg docs recommend Paper instead.",
        extra_fields=[],
        notes="getbukkit.org broke automation. Use PAPER.",
    ),

    # ── Vanilla / Mojang ────────────────────────────────────────────
    ServerType(
        key="VANILLA", display="Vanilla", family="vanilla",
        description="Official Minecraft server from Mojang. No plugin support.",
        extra_fields=[],
        auto_latest="vanilla_latest",
    ),

    # ── Mod loaders ─────────────────────────────────────────────────
    ServerType(
        key="FORGE", display="Forge", family="mod-loader",
        description="MinecraftForge mod loader. Mods, no Bukkit plugins.",
        extra_fields=[
            ("FORGE_VERSION", "Forge Version", '"latest", "recommended", or specific (51.0.33)', "loader_version"),
        ],
        auto_latest="forge_latest",
        notes="Forge installs run a one-time installer on first start; first boot is slow.",
    ),
    ServerType(
        key="NEOFORGE", display="NeoForge", family="mod-loader",
        description="NeoForge — community continuation of Forge, faster updates.",
        extra_fields=[
            ("NEOFORGE_VERSION", "NeoForge Version", '"latest", "beta", or specific (47.1.79)', "loader_version"),
        ],
        auto_latest="neoforge_latest",
    ),
    ServerType(
        key="FABRIC", display="Fabric", family="mod-loader",
        description="Lightweight mod loader. Mods, no Bukkit plugins. Fabric API mod usually required.",
        extra_fields=[
            ("FABRIC_LOADER_VERSION", "Fabric Loader", "blank for latest", "loader_version"),
            ("FABRIC_LAUNCHER_VERSION", "Fabric Launcher", "blank for latest", "loader_version"),
        ],
        auto_latest="fabric_latest",
    ),
    ServerType(
        key="QUILT", display="Quilt", family="mod-loader",
        description="Fork of Fabric with breaking changes for upstream improvements.",
        extra_fields=[
            ("QUILT_LOADER_VERSION", "Quilt Loader", "blank for latest", "loader_version"),
            ("QUILT_INSTALLER_VERSION", "Quilt Installer", "blank for latest", "loader_version"),
        ],
        auto_latest=None,
    ),

    # ── Hybrids (Forge + Bukkit plugins on one server) ──────────────
    ServerType(
        key="MAGMA", display="Magma", family="hybrid",
        description="Forge + PaperMC hybrid. Project is officially terminated — see Magma Maintained or Ketting.",
        extra_fields=[],
        notes="Project terminated. Use MAGMA_MAINTAINED for 1.12/1.18/1.19, or KETTING for 1.20+.",
    ),
    ServerType(
        key="MAGMA_MAINTAINED", display="Magma Maintained", family="hybrid",
        description="Community fork of Magma. Supports 1.12.2, 1.18.2, 1.19.3, 1.20.1 only.",
        extra_fields=[
            ("FORGE_VERSION", "Forge Version", "required", "loader_version"),
            ("MAGMA_MAINTAINED_TAG", "Magma Tag", "from releases page", "text"),
        ],
    ),
    ServerType(
        key="KETTING", display="Ketting", family="hybrid",
        description="Forge + Bukkit hybrid for 1.20.1+.",
        extra_fields=[
            ("FORGE_VERSION", "Forge Version", "optional", "loader_version"),
            ("KETTING_VERSION", "Ketting Version", "optional", "loader_version"),
        ],
    ),
    ServerType(
        key="MOHIST", display="Mohist", family="hybrid",
        description="Forge + Bukkit hybrid. Limited MC version support.",
        extra_fields=[("MOHIST_BUILD", "Mohist Build", "blank for latest", "build")],
    ),
    ServerType(
        key="YOUER", display="Youer", family="hybrid",
        description="MohistMC project — Mohist-style hybrid.",
        extra_fields=[("MOHIST_BUILD", "Build", "blank for latest", "build")],
    ),
    ServerType(
        key="BANNER", display="Banner", family="hybrid",
        description="MohistMC project — alternate Mohist build.",
        extra_fields=[("MOHIST_BUILD", "Build", "blank for latest", "build")],
    ),
    ServerType(
        key="ARCLIGHT", display="Arclight", family="hybrid",
        description="Forge/NeoForge/Fabric + Bukkit-plugin compatibility layer. The hub auto-downloads jars from arclight.izzel.io.",
        extra_fields=[
            ("ARCLIGHT_TYPE", "Arclight Loader", '"FORGE", "NEOFORGE", or "FABRIC"', "arclight_type"),
            # Hub-internal field rendered as a composite picker by the UI.
            # Backend exposes the schema so the frontend knows to render the
            # channel + build picker for this type. The actual value is
            # stored in HUB_LOADER_TAG; this field's "key" is just a marker.
            ("HUB_LOADER_TAG", "Arclight Build", "channel + build picker (auto-downloaded)", "arclight_build"),
        ],
        auto_latest="arclight_latest",
        notes="The hub silently translates Arclight to TYPE=CUSTOM under the hood — itzg's built-in Arclight deploy is broken on 1.21.x.",
    ),

    # ── Sponge ──────────────────────────────────────────────────────
    ServerType(
        key="SPONGEVANILLA", display="SpongeVanilla", family="other",
        description="Sponge API server. Plugins use Sponge's API, not Bukkit.",
        extra_fields=[
            ("SPONGEVERSION", "Sponge Version", "specific or blank", "text"),
            ("SPONGEBRANCH", "Sponge Branch", '"STABLE" or "EXPERIMENTAL"', "text"),
        ],
    ),

    # ── Limbo-style ─────────────────────────────────────────────────
    ServerType(
        key="LIMBO", display="Limbo", family="limbo",
        description="Minimal lobby/queue server. No worlds, no plugins.",
        extra_fields=[("LIMBO_BUILD", "Limbo Build", '"LATEST" or specific', "build")],
    ),
    ServerType(
        key="NANOLIMBO", display="NanoLimbo", family="limbo",
        description="Even lighter Limbo fork.",
        extra_fields=[],
    ),

    # ── Misc ────────────────────────────────────────────────────────
    ServerType(
        key="CRUCIBLE", display="Crucible", family="hybrid",
        description="Forge + Bukkit hybrid for 1.7.10 only.",
        extra_fields=[("CRUCIBLE_RELEASE", "Release", '"latest" or specific', "text")],
    ),
    ServerType(
        key="CANYON", display="Canyon", family="legacy",
        description="CraftBukkit fork for Beta 1.7.3.",
        extra_fields=[("CANYON_BUILD", "Build", "specific number", "build")],
        notes="Set VERSION=b1.7.3 and DISABLE_HEALTHCHECK=true.",
    ),
    ServerType(
        key="CUSTOM", display="Custom Jar", family="custom",
        description="Run an arbitrary server jar.",
        extra_fields=[
            ("CUSTOM_SERVER", "Custom Server", "URL or container path to jar", "text"),
            ("CUSTOM_JAR_EXEC", "Custom Exec", "alt to CUSTOM_SERVER", "text"),
        ],
    ),
]

BY_KEY: dict[str, ServerType] = {t.key: t for t in SERVER_TYPES}


def get_type(type_key: str) -> ServerType:
    return BY_KEY.get((type_key or "PAPER").upper(), BY_KEY["PAPER"])


# ─────────────────────────────────────────────────────────────────────────────
# Latest-stable lookups (one per family that has a clean API)
# ─────────────────────────────────────────────────────────────────────────────

UA = "test-fred-hub/0.5"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(15.0, connect=8.0),
        follow_redirects=True,
    )


def _version_sort_key(v: str) -> tuple:
    """Tuple-of-ints key for sorting MC versions newest-last."""
    return tuple(int(x) for x in re.findall(r"\d+", v))[:4]


def _is_stable(v: str) -> bool:
    return not re.search(r"(pre|rc|snapshot|beta|alpha)", v, re.I)


async def _papermc_latest(project: str) -> dict[str, Any] | None:
    """Fill API v3 — used by Paper and Folia."""
    async with _client() as c:
        # 1. List versions
        r = await c.get(f"https://fill.papermc.io/v3/projects/{project}")
        if r.status_code != 200:
            return None
        raw = r.json()
        flat: list[str] = []
        vs = raw.get("versions") or {}
        if isinstance(vs, dict):
            for _major, subs in vs.items():
                if isinstance(subs, list):
                    flat.extend(subs)
                elif isinstance(subs, str):
                    flat.append(subs)
        elif isinstance(vs, list):
            flat = list(vs)
        stable = sorted([v for v in flat if _is_stable(v)],
                        key=_version_sort_key, reverse=True)
        if not stable:
            return None
        latest_version = stable[0]

        # 2. Get latest build for that version
        r2 = await c.get(f"https://fill.papermc.io/v3/projects/{project}/versions/{latest_version}/builds")
        if r2.status_code != 200:
            return {"version": latest_version, "build": None}
        builds = r2.json()
        if not builds:
            return {"version": latest_version, "build": None}
        # API returns newest first
        latest = builds[0]
        return {
            "version": latest_version,
            "build": latest.get("id"),
            "channel": (latest.get("channel") or "default").lower(),
            "time": latest.get("time"),
        }


async def paper_latest() -> dict[str, Any] | None:
    return await _papermc_latest("paper")


async def folia_latest() -> dict[str, Any] | None:
    return await _papermc_latest("folia")


async def purpur_latest() -> dict[str, Any] | None:
    """api.purpurmc.org/v2/purpur"""
    async with _client() as c:
        r = await c.get("https://api.purpurmc.org/v2/purpur")
        if r.status_code != 200:
            return None
        data = r.json()
        versions = data.get("versions") or []
        stable = sorted([v for v in versions if _is_stable(v)],
                        key=_version_sort_key, reverse=True)
        if not stable:
            return None
        latest_v = stable[0]
        r2 = await c.get(f"https://api.purpurmc.org/v2/purpur/{latest_v}")
        if r2.status_code != 200:
            return {"version": latest_v, "build": None}
        v = r2.json()
        builds = (v.get("builds") or {}).get("all") or []
        latest_build = (v.get("builds") or {}).get("latest") or (builds[-1] if builds else None)
        return {"version": latest_v, "build": latest_build}


async def vanilla_latest() -> dict[str, Any] | None:
    """launchermeta.mojang.com — latest release."""
    async with _client() as c:
        r = await c.get("https://launchermeta.mojang.com/mc/game/version_manifest.json")
        if r.status_code != 200:
            return None
        data = r.json()
        return {"version": (data.get("latest") or {}).get("release"), "build": None}


async def fabric_latest() -> dict[str, Any] | None:
    """meta.fabricmc.net — latest stable game version."""
    async with _client() as c:
        r = await c.get("https://meta.fabricmc.net/v2/versions/game")
        if r.status_code != 200:
            return None
        games = r.json()
        stable = [g["version"] for g in games if g.get("stable")]
        if not stable:
            return None
        # Fabric API lists newest first
        return {"version": stable[0], "build": None}


async def forge_latest() -> dict[str, Any] | None:
    """files.minecraftforge.net promotions_slim.json — pick newest -recommended."""
    async with _client() as c:
        r = await c.get("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")
        if r.status_code != 200:
            return None
        promos = (r.json() or {}).get("promos") or {}
    # promos = {"1.21.4-recommended": "54.1.0", "1.21.4-latest": "54.1.16", ...}
    # Prefer -recommended over -latest, sorted by MC version newest-first.
    by_mc: dict[str, dict[str, str]] = {}
    for key, val in promos.items():
        if "-" not in key:
            continue
        mc, kind = key.rsplit("-", 1)
        if _is_stable(mc):
            by_mc.setdefault(mc, {})[kind] = val
    if not by_mc:
        return None
    newest_mc = sorted(by_mc, key=_version_sort_key, reverse=True)[0]
    entry = by_mc[newest_mc]
    forge_v = entry.get("recommended") or entry.get("latest")
    return {"version": newest_mc, "build": forge_v}


async def neoforge_latest() -> dict[str, Any] | None:
    """maven.neoforged.net — newest stable build (non-beta)."""
    async with _client() as c:
        r = await c.get("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
        if r.status_code != 200:
            return None
        versions = r.json().get("versions") or []
    stable = [v for v in versions if "-beta" not in v.lower()
              and "-alpha" not in v.lower() and "snapshot" not in v.lower()]
    if not stable:
        stable = versions
    stable.sort(key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v))[:4],
                reverse=True)
    if not stable:
        return None
    nf = stable[0]
    nums = re.findall(r"\d+", nf)
    if len(nums) < 2:
        return None
    major = int(nums[0])
    # New MC numbering started at 26 (e.g. NeoForge 26.1.X → MC 26.1)
    if major >= 26:
        mc = f"{nums[0]}.{nums[1]}"
    else:
        mc = f"1.{nums[0]}.{nums[1]}"
    return {"version": mc, "build": nf}


async def arclight_latest() -> dict[str, Any] | None:
    """Newest Arclight snapshot from arclight.izzel.io CDN for MC 1.21.1/forge.

    The custom_jar module owns the full picker; this thin wrapper exists so
    the generic get_latest(type_key) path works for ARCLIGHT too. Returns
    {version, build} where build is the source tag (e.g. "snapshot/1.0.2-SNAPSHOT-0769551").
    """
    try:
        # Late import to avoid circular dep at module load
        from . import custom_jar
    except ImportError:
        return None
    # Default to 1.21.1/forge — the only MC version Arclight actively targets.
    # The per-server config will override via /api/server/arclight/* endpoints.
    build = await custom_jar.arclight_latest("1.21.1", "forge", channel="snapshot")
    if not build:
        return None
    return {"version": build.mc_version, "build": build.tag}


# Map function names to coros (so type registry can reference by string)
LATEST_FNS = {
    "paper_latest": paper_latest,
    "folia_latest": folia_latest,
    "purpur_latest": purpur_latest,
    "vanilla_latest": vanilla_latest,
    "fabric_latest": fabric_latest,
    "forge_latest": forge_latest,
    "neoforge_latest": neoforge_latest,
    "arclight_latest": arclight_latest,
}


async def get_latest(type_key: str) -> dict[str, Any] | None:
    """Return {version, build} for the configured server type, or None."""
    t = get_type(type_key)
    if not t.auto_latest:
        return None
    fn = LATEST_FNS.get(t.auto_latest)
    if not fn:
        return None
    try:
        return await fn()
    except Exception:
        return None


def current_for_type(type_key: str, env: dict[str, str]) -> dict[str, str | None]:
    """Extract the installed version+build from compose env, per server type.

    For custom-routed types (Arclight) the "build" is the HUB_LOADER_TAG that
    the hub set when it downloaded the jar — TYPE=CUSTOM in the actual
    compose, so we ignore the itzg-side fields and read our own markers.
    """
    t = get_type(type_key)
    # Custom-routed: read hub-internal markers
    if (env.get("HUB_LOADER_TYPE") or "").upper() == "ARCLIGHT":
        return {
            "version": env.get("VERSION") or None,
            "build": env.get("HUB_LOADER_TAG") or None,
        }
    version = env.get("VERSION")
    build_key_by_family = {
        "PAPER": "PAPER_BUILD",
        "FOLIA": "FOLIA_BUILD",
        "PURPUR": "PURPUR_BUILD",
        "LEAF": "LEAF_BUILD",
        "PUFFERFISH": "PUFFERFISH_BUILD",
        "MOHIST": "MOHIST_BUILD",
        "YOUER": "MOHIST_BUILD",
        "BANNER": "MOHIST_BUILD",
        "LIMBO": "LIMBO_BUILD",
        "CANYON": "CANYON_BUILD",
        # Mod-loader "build" = loader version (different env var per loader)
        "FORGE": "FORGE_VERSION",
        "NEOFORGE": "NEOFORGE_VERSION",
        "FABRIC": "FABRIC_LOADER_VERSION",
        "QUILT": "QUILT_LOADER_VERSION",
    }
    build = env.get(build_key_by_family.get(t.key, ""), "")
    return {"version": version, "build": build or None}
