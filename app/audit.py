"""Append-only structured event log for the Test Fred Hub.

Every API request (via middleware) and explicit `event()` call lands here.
JSONL so it's grep-friendly. Auto-rotates at MAX_BYTES.

Locations:
  logs/api.jsonl     — all HTTP requests
  logs/events.jsonl  — explicit event() calls (cookies, downloads, errors)
  logs/console.log   — captured stdout/stderr from uvicorn (write via tee or shell)

Designed so a future debugging turn can curl `/api/logs/tail` and immediately
see what went wrong on the user's last interaction.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path
from typing import Any

HUB_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = HUB_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

API_LOG = LOG_DIR / "api.jsonl"
EVENT_LOG = LOG_DIR / "events.jsonl"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file before rotation


def _rotate_if_big(path: Path) -> None:
    """Rotate to .1.gz-style suffix when we cross MAX_BYTES."""
    try:
        if not path.exists():
            return
        if path.stat().st_size < MAX_BYTES:
            return
        # Find next available .N suffix
        i = 1
        while True:
            backup = path.with_suffix(path.suffix + f".{i}")
            if not backup.exists():
                path.rename(backup)
                return
            i += 1
            if i > 9:
                # Drop the oldest, shift down
                oldest = path.with_suffix(path.suffix + ".9")
                if oldest.exists():
                    oldest.unlink()
                return
    except Exception:
        pass  # logging must never raise


def _append(path: Path, record: dict) -> None:
    _rotate_if_big(path)
    record.setdefault("ts", time.time())
    record.setdefault("iso", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()))
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def api_request(method: str, path: str, status: int, latency_ms: float,
                error: str | None = None, query: str = "", client: str = "") -> None:
    """Called by the FastAPI middleware on every request."""
    _append(API_LOG, {
        "kind": "api",
        "method": method, "path": path, "query": query,
        "status": status, "latency_ms": round(latency_ms, 1),
        "error": error, "client": client,
    })


def event(kind: str, **fields: Any) -> None:
    """Record an arbitrary structured event. Use sparingly for notable actions.

    Examples:
      event("cookie_save", keys=["xf_user","cf_clearance"])
      event("spigot_download", spigot_id=64348, state="cf_blocked", http=403)
    """
    rec = {"kind": kind, **fields}
    _append(EVENT_LOG, rec)


def exception(kind: str, exc: BaseException, **fields: Any) -> None:
    """Log an exception with a stack trace, but never re-raise."""
    event(kind, error_type=type(exc).__name__, error=str(exc),
          traceback=traceback.format_exc()[-1500:], **fields)


def tail(which: str = "api", limit: int = 100,
         since_ts: float | None = None) -> list[dict]:
    """Return the last N entries from the named log (api|events)."""
    path = API_LOG if which == "api" else EVENT_LOG
    if not path.exists():
        return []
    lines: list[dict] = []
    try:
        # Read entire file (capped at 5MB) — simpler than reverse-seek for now
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if since_ts and rec.get("ts", 0) < since_ts:
                continue
            lines.append(rec)
    except Exception:
        return []
    return lines[-limit:]


def clear(which: str = "all") -> dict:
    """Truncate the named log(s)."""
    removed: list[str] = []
    targets = [API_LOG, EVENT_LOG] if which == "all" else (
        [API_LOG] if which == "api" else [EVENT_LOG]
    )
    for p in targets:
        if p.exists():
            p.write_text("")
            removed.append(p.name)
    return {"ok": True, "cleared": removed}
