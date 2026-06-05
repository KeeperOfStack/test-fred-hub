"""Multi-server registry — track multiple Minecraft servers in one hub.

A "server record" is the minimum metadata needed to operate on one server:
  - id          short slug (used in URLs and as the dict key)
  - display     human label shown in the dropdown
  - container   docker container name (must match the `container_name:` in compose)
  - compose     absolute path to the compose file managing this server
  - data_dir    absolute path to the host volume mounted at /data
  - port        the Minecraft port mapped on the host (for the connect card)
  - color       optional accent color shown in the UI

The "current server" is just a marker — the API endpoints look up the record
each request, so switching is instant and stateless.

Persisted at ~/.hermes/test-fred-hub/servers.json (chmod 600).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(os.environ.get(
    "HERMES_HOME", str(Path.home() / ".hermes")
)) / "test-fred-hub" / "servers.json"


# ─────────────────────────────────────────────────────────────────────────────
# Data shape + persistence
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)


def _default_record() -> dict[str, Any]:
    """Bootstrap record for test-fred — the server we already know about."""
    return {
        "id": "test-fred",
        "display": "Test Fred",
        "container": "test-fred",
        "compose": "/home/kratos/docker-composes/test-fred.yaml",
        "data_dir": "/media/Minecraft/test-fred",
        "port": 25566,
        "color": "#6cbf3a",
    }


def _load() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        data = {"servers": [_default_record()], "current": "test-fred"}
        _save(data)
        return data
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except Exception:
        return {"servers": [], "current": None}


def _save(data: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(REGISTRY_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def list_all() -> list[dict[str, Any]]:
    return _load().get("servers", [])


def get_current_id() -> str | None:
    return _load().get("current")


def set_current(server_id: str) -> dict[str, Any]:
    data = _load()
    if not any(s["id"] == server_id for s in data.get("servers", [])):
        raise KeyError(f"unknown server id: {server_id}")
    data["current"] = server_id
    _save(data)
    return data


def get(server_id: str | None = None) -> dict[str, Any] | None:
    data = _load()
    sid = server_id or data.get("current")
    if not sid:
        return None
    for s in data.get("servers", []):
        if s["id"] == sid:
            return s
    return None


def add(record: dict[str, Any]) -> dict[str, Any]:
    """Add a new server record. Validates required fields."""
    required = {"id", "display", "container", "compose", "data_dir"}
    missing = required - set(record)
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")
    sid = re.sub(r"[^a-z0-9_-]", "-", record["id"].lower()).strip("-")
    if not sid:
        raise ValueError("id must contain alphanumerics")
    record["id"] = sid
    record.setdefault("port", 25565)
    record.setdefault("color", "#6cbf3a")
    record["compose"] = str(Path(record["compose"]).expanduser().resolve())
    record["data_dir"] = str(Path(record["data_dir"]).expanduser().resolve())

    data = _load()
    if any(s["id"] == sid for s in data.get("servers", [])):
        raise ValueError(f"server '{sid}' already exists")
    data.setdefault("servers", []).append(record)
    if not data.get("current"):
        data["current"] = sid
    _save(data)
    return record


def remove(server_id: str) -> dict[str, Any]:
    """Remove tracking only — never touches the actual container or data."""
    data = _load()
    before = len(data.get("servers", []))
    data["servers"] = [s for s in data.get("servers", []) if s["id"] != server_id]
    if len(data["servers"]) == before:
        raise KeyError(f"unknown server id: {server_id}")
    if data.get("current") == server_id:
        data["current"] = data["servers"][0]["id"] if data["servers"] else None
    _save(data)
    return {"removed": server_id, "remaining": len(data["servers"])}


def update(server_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Patch an existing record. Returns the updated record."""
    data = _load()
    target = next((s for s in data.get("servers", []) if s["id"] == server_id), None)
    if not target:
        raise KeyError(f"unknown server id: {server_id}")
    # Disallow id change to keep references stable; everything else is editable.
    for k, v in changes.items():
        if k == "id":
            continue
        if k in ("compose", "data_dir") and v:
            target[k] = str(Path(v).expanduser().resolve())
        else:
            target[k] = v
    _save(data)
    return target


# ─────────────────────────────────────────────────────────────────────────────
# Scaffolding: write a fresh compose file for a brand-new server
# ─────────────────────────────────────────────────────────────────────────────

def scaffold_compose(
    compose_path: Path,
    container_name: str,
    data_dir: Path,
    host_port: int,
    server_type: str = "PAPER",
    version: str = "LATEST",
    memory: str = "2G",
    motd: str = "A Minecraft server managed by Test Fred Hub",
    icon: str | None = None,
    image_tag: str = "java21",
    extra_env: dict | None = None,
) -> None:
    """Write a brand-new compose file. Won't overwrite existing files.

    ``extra_env`` may carry additional KEY=VALUE pairs (e.g. DIFFICULTY,
    MAX_PLAYERS, type-specific BUILD/FORGE_VERSION/etc.). Empty values are
    skipped so the generated compose stays clean.
    """
    if compose_path.exists():
        raise FileExistsError(f"compose file already exists at {compose_path}")
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    env_lines = [
        f"TYPE={server_type}",
        f"VERSION={version}",
        "EULA=TRUE",
        f"MEMORY={memory}",
        f"MOTD={motd}",
    ]
    if icon:
        env_lines += [f"ICON={icon}", "OVERRIDE_ICON=TRUE"]
    # Additional env vars (difficulty, max_players, type-specific extras).
    # Skip anything already set above and anything empty.
    already = {line.split("=", 1)[0] for line in env_lines}
    for k, v in (extra_env or {}).items():
        if not k or k.upper() in already:
            continue
        v = str(v).strip() if v is not None else ""
        if not v:
            continue
        env_lines.append(f"{k.upper()}={v}")

    image = f"itzg/minecraft-server:{(image_tag or 'java21').strip()}"
    yaml_text = f"""services:
  {container_name}:
    image: {image}
    container_name: {container_name}
    restart: unless-stopped
    ports:
    - {host_port}:25565
    - {host_port}:25565/udp
    volumes:
    - {data_dir}:/data
    environment:
""" + "\n".join(f"    - {line}" for line in env_lines) + "\n"
    compose_path.write_text(yaml_text, encoding="utf-8")
