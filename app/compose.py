"""Read/edit a server's docker compose file.

Per-server now: every public helper takes a compose path or a server record
(via the multi-server registry) instead of the old module-level COMPOSE_PATH.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

# Default service name used in scaffolded composes. Real lookups pull the
# service from the compose file's first key under `services:`.
DEFAULT_SERVICE_NAME = "test-fred"

# Fields the UI is allowed to edit (everything else is preserved verbatim)
EDITABLE_ENV = {
    # Universal
    "TYPE", "VERSION", "MEMORY", "MOTD", "ICON", "OVERRIDE_ICON",
    "DIFFICULTY", "MODE", "PVP", "ONLINE_MODE", "WHITELIST",
    "MAX_PLAYERS", "VIEW_DISTANCE", "SIMULATION_DISTANCE",
    # Paper family
    "PAPER_BUILD", "PAPER_CHANNEL", "PURPUR_BUILD",
    "FOLIA_BUILD", "FOLIA_CHANNEL", "LEAF_BUILD", "PUFFERFISH_BUILD",
    # Mod loaders
    "FORGE_VERSION", "NEOFORGE_VERSION",
    "FABRIC_LOADER_VERSION", "FABRIC_LAUNCHER_VERSION",
    "QUILT_LOADER_VERSION", "QUILT_INSTALLER_VERSION",
    # Hybrids
    "MOHIST_BUILD", "MAGMA_MAINTAINED_TAG", "KETTING_VERSION",
    "ARCLIGHT_TYPE",
    # Other
    "SPONGEVERSION", "SPONGEBRANCH", "LIMBO_BUILD", "CANYON_BUILD",
    "CRUCIBLE_RELEASE", "CUSTOM_SERVER", "CUSTOM_JAR_EXEC",
    # Hub-internal markers for custom-routed types (Arclight, etc.). NOT itzg
    # env vars — itzg ignores them. Tells the hub which logical type the user
    # picked even though TYPE=CUSTOM is what reaches the container.
    "HUB_LOADER_TYPE", "HUB_LOADER_TAG", "HUB_LOADER_JAR",
    "HUB_LOADER_SUBTYPE", "HUB_LOADER_CHANNEL", "HUB_LOADER_SOURCE",
}


# ─────────────────────────────────────────────────────────────────────────────
# Per-compose helpers
# ─────────────────────────────────────────────────────────────────────────────

def read_compose(compose_path: Path | str) -> dict[str, Any]:
    p = Path(compose_path)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def _service(data: dict, service_name: str | None = None) -> tuple[str, dict]:
    """Return (service_name, service_dict). Picks first service if name omitted."""
    services = data.setdefault("services", {})
    if service_name and service_name in services:
        return service_name, services[service_name]
    if services:
        first = next(iter(services))
        return first, services[first]
    # bootstrap
    services[DEFAULT_SERVICE_NAME] = {}
    return DEFAULT_SERVICE_NAME, services[DEFAULT_SERVICE_NAME]


def get_env(compose_path: Path | str, service_name: str | None = None) -> dict[str, str]:
    data = read_compose(compose_path)
    if not data:
        return {}
    _, svc = _service(data, service_name)
    env = svc.get("environment", [])
    out: dict[str, str] = {}
    if isinstance(env, list):
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                out[k.strip()] = v
    elif isinstance(env, dict):
        for k, v in env.items():
            out[str(k)] = "" if v is None else str(v)
    return out


def get_image(compose_path: Path | str, service_name: str | None = None) -> str | None:
    """Read the `image:` field for the service (e.g. itzg/minecraft-server:java21)."""
    data = read_compose(compose_path)
    if not data:
        return None
    _, svc = _service(data, service_name)
    img = svc.get("image")
    return str(img) if img else None


def set_image(
    compose_path: Path | str,
    image: str,
    service_name: str | None = None,
) -> str:
    """Set the `image:` field. Atomic with .bak. Returns the new image string."""
    if not image or not isinstance(image, str):
        raise ValueError("image must be a non-empty string")
    # Sanity check: must look like a docker reference
    if not re.match(r"^[a-z0-9][a-z0-9._/:-]*$", image):
        raise ValueError(f"refusing suspicious image string: {image!r}")
    p = Path(compose_path)
    data = read_compose(p)
    if not data:
        raise FileNotFoundError(f"no compose file at {p}")
    _, svc = _service(data, service_name)
    svc["image"] = image
    bak = p.with_suffix(p.suffix + ".bak")
    shutil.copy2(p, bak)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False,
                       allow_unicode=True)
    tmp.replace(p)
    return image


def update_env(
    compose_path: Path | str,
    changes: dict[str, str],
    service_name: str | None = None,
) -> dict[str, str]:
    """Apply `changes` to the named service's environment. Atomic with .bak."""
    bad = [k for k in changes if k not in EDITABLE_ENV]
    if bad:
        raise ValueError(f"refusing to edit protected keys: {bad}")
    p = Path(compose_path)
    data = read_compose(p)
    if not data:
        raise FileNotFoundError(f"no compose file at {p}")
    sname, svc = _service(data, service_name)

    current = get_env(p, sname)
    for k, v in changes.items():
        if v == "" or v is None:
            current.pop(k, None)
        else:
            current[k] = str(v)

    existing = svc.get("environment", [])
    if isinstance(existing, list):
        svc["environment"] = [f"{k}={v}" for k, v in current.items()]
    else:
        svc["environment"] = current

    bak = p.with_suffix(p.suffix + ".bak")
    shutil.copy2(p, bak)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False,
                       allow_unicode=True)
    tmp.replace(p)
    return current


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def compose_up(compose_path: Path | str) -> tuple[int, str]:
    return await _run("docker", "compose", "-f", str(compose_path), "up", "-d")


async def compose_stop(compose_path: Path | str) -> tuple[int, str]:
    return await _run("docker", "compose", "-f", str(compose_path), "stop")


async def compose_recreate(compose_path: Path | str) -> tuple[int, str]:
    """Tear the container down and bring it back up so env changes take effect.

    `compose stop` + `up -d` does NOT pick up env edits because compose sees a
    matching container and reuses it. `up -d --force-recreate` removes the
    container first, which is what we actually want for version/type changes.
    `--remove-orphans` cleans up stale entries if the service name changed.
    """
    return await _run(
        "docker", "compose", "-f", str(compose_path),
        "up", "-d", "--force-recreate", "--remove-orphans",
    )
