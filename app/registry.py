"""Curated plugin registry.

Maps a stable plugin key → ordered list of sources to try.
First source that yields a real jar wins.

Sources are tuples: (source_name, ref)
  modrinth: ref = project slug
  hangar:   ref = "Owner/Project" or "Project"
  geyser:   ref = "geyser" or "floodgate"
  spiget:   ref = numeric resource ID (as string)
  github:   ref = "owner/repo" or "owner/repo/asset-glob"
"""

from __future__ import annotations

# key = lowercase plugin.yml name (normalized)
REGISTRY: dict[str, dict] = {
    "bluemap": {
        "display": "BlueMap",
        "sources": [("modrinth", "bluemap"), ("spiget", "83557")],
    },
    "coreprotect": {
        "display": "CoreProtect",
        "sources": [("modrinth", "coreprotect")],
    },
    "discordsrv": {
        "display": "DiscordSRV",
        "sources": [("spiget", "18494"), ("modrinth", "discordsrv")],
    },
    "easywhitelist": {
        "display": "EasyWhitelist",
        "sources": [("spiget", "65222")],
    },
    "viabackwards": {
        "display": "ViaBackwards",
        "sources": [("hangar", "ViaVersion/ViaBackwards"), ("modrinth", "viabackwards")],
    },
    "viaversion": {
        "display": "ViaVersion",
        "sources": [("hangar", "ViaVersion/ViaVersion"), ("modrinth", "viaversion")],
    },
    "geyser-spigot": {
        "display": "Geyser",
        "sources": [("geyser", "geyser"), ("modrinth", "geyser")],
    },
    "floodgate": {
        "display": "Floodgate",
        "sources": [("geyser", "floodgate"), ("modrinth", "floodgate")],
    },
    "openinv": {
        "display": "OpenInv",
        "sources": [("modrinth", "openinv")],
    },
    # mcMMO is intentionally NOT in the auto-install registry.
    # The maintained version (Spigot ID 64348) is PREMIUM and requires a
    # SpigotMC purchase + login to download. The old GitHub repo
    # (mcMMO-Dev/mcMMO) is abandoned at 1.4.06 from 2017 with broken
    # build-template variables. Users wanting mcMMO should buy + manually
    # upload the jar, or wait for premium-auth support to land.
}


# Plugins we know are premium-only / require manual purchase. Exposed in the
# UI so users see them as available with a clear "buy + upload" path instead
# of silently failing or getting stale free alternatives.
PREMIUM_PLUGINS = [
    {
        "display": "mcMMO (Official)",
        "spigot_id": "64348",
        "url": "https://www.spigotmc.org/resources/official-mcmmo-original-author-returns.64348/",
        "note": "Premium plugin on SpigotMC — requires purchase + login. Use one of the fallback options if auto-download fails.",
        # Curated free alternatives — same category, free, audited as actively maintained.
        # Each entry: {label, source, id_or_url, why}
        # source ∈ {"spiget","hangar","modrinth","github","catalog_key"}
        "alternatives": [
            {
                "label": "Classic mcMMO (free fork, Spigot 2445)",
                "source": "spiget",
                "id": "2445",
                "why": "The original free mcMMO before the maintained fork went premium. Same gameplay, slightly older feature set.",
            },
            {
                "label": "StormMMO (Hangar)",
                "source": "hangar",
                "id": "AugmentedThunder/StormMMO",
                "why": "Active mcMMO-style 19-skill RPG plugin on Paper's official repo, MIT-licensed, recent commits.",
            },
            {
                "label": "AureliumSkills (Modrinth)",
                "source": "modrinth",
                "id": "aurelium-skills",
                "why": "Popular skills + leveling alternative with strong community + frequent updates.",
            },
        ],
    },
]


def normalize(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "").replace("_", "-")


def find(name: str) -> tuple[str, dict] | None:
    if not name:
        return None
    key = normalize(name)
    if key in REGISTRY:
        return key, REGISTRY[key]
    for k, v in REGISTRY.items():
        if k == key or k in key or key in k:
            return k, v
    return None


# ─────────────────────────────────────────────────────────────
# Default catalog seed — applied to NEWLY tracked/created servers only.
# test-fred's catalog stays seeded from REGISTRY (preserved on first read).
# This list mirrors the URLs the user supplied and uses the most reliable
# source per plugin (Modrinth tends to give the latest stable; Spiget for
# Spigot-exclusive resources; Hangar for Via*; geyser:* for Geyser/Floodgate).
# ─────────────────────────────────────────────────────────────
DEFAULT_CATALOG_SEED: dict[str, dict] = {
    "automatedcrafting": {
        "display": "Automated Crafting",
        "sources": [("spiget", "70432")],
    },
    "chunky": {
        "display": "Chunky",
        "sources": [("modrinth", "chunky-pregenerator"), ("spiget", "81534")],
    },
    "chestsort": {
        "display": "ChestSort + API",
        "sources": [("spiget", "59773")],
    },
    "coordinatesplus": {
        "display": "CoordinatesPlus",
        "sources": [("spiget", "80736")],
    },
    "coreprotect": {
        # CoreProtect Community Edition — actively maintained fork of the
        # original. Same plugin.yml name ("CoreProtect") so it matches.
        "display": "CoreProtect (Community Edition)",
        "sources": [("spiget", "8631")],
    },
    "discordsrv": {
        "display": "DiscordSRV",
        "sources": [("spiget", "18494"), ("modrinth", "discordsrv")],
    },
    "essentials": {
        # plugin.yml name is "Essentials" — EssentialsX uses the same name.
        "display": "EssentialsX",
        "sources": [("modrinth", "essentialsx"), ("spiget", "9089")],
    },
    "graves": {
        # plugin.yml for GravesX is "Graves" — using the lower-cased key.
        "display": "GravesX",
        "sources": [("modrinth", "gravesx"), ("spiget", "118271")],
    },
    "keepchunks": {
        "display": "KeepChunks",
        "sources": [("spiget", "23307")],
    },
    "luckperms": {
        # Modrinth keeps current with luckperms.net; Spigot lags far behind.
        "display": "LuckPerms",
        "sources": [("modrinth", "luckperms")],
    },
    "nochatreports": {
        "display": "No Chat Reports",
        "sources": [("modrinth", "no-chat-reports"), ("spiget", "102931")],
    },
    "viabackwards": {
        "display": "ViaBackwards",
        "sources": [("hangar", "ViaVersion/ViaBackwards"), ("modrinth", "viabackwards")],
    },
    "viaversion": {
        "display": "ViaVersion",
        "sources": [("hangar", "ViaVersion/ViaVersion"), ("modrinth", "viaversion")],
    },
    "bluemap": {
        "display": "BlueMap",
        "sources": [("modrinth", "bluemap"), ("spiget", "83557")],
    },
    "easywhitelist": {
        "display": "EasyWhitelist (name-based)",
        "sources": [("spiget", "65222")],
    },
    "geyser-spigot": {
        "display": "Geyser",
        "sources": [("geyser", "geyser"), ("modrinth", "geyser")],
    },
    "floodgate": {
        "display": "Floodgate",
        "sources": [("geyser", "floodgate"), ("modrinth", "floodgate")],
    },
    "openinv": {
        "display": "OpenInv",
        "sources": [("modrinth", "openinv")],
    },
    # mcMMO is premium-only — surfaced via PREMIUM_PLUGINS (see below), not
    # the auto-install registry. Every server already sees it in the premium
    # column of the catalog, so no per-server seeding needed for it here.
}
