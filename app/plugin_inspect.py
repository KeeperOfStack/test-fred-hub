"""Read plugin version metadata from .jar files without extracting them.

Uses stdlib `zipfile` which only parses the central directory (~few KB at the
end of the file) to locate entries, then reads individual files on demand.
Reading `plugin.yml` from a 5 MB jar typically takes <10 ms and allocates a
few hundred bytes.

Results are cached by (absolute_path, st_mtime_ns, st_size). Jars are
immutable in practice — updating a plugin replaces the file, which changes
mtime. Cache lookup is O(1).

Priority order for version sources:
  1. plugin.yml          (Bukkit/Spigot/Paper — most common)
  2. paper-plugin.yml    (modern Paper plugins)
  3. bungee.yml          (BungeeCord)
  4. velocity-plugin.json (Velocity)
  5. META-INF/MANIFEST.MF → Implementation-Version
  6. None — caller should fall back to filename parsing

The yml files are NOT strict YAML — they're often hand-edited with tabs,
unquoted values, comments. We use a minimal regex parser instead of pulling
in PyYAML for one field. Robust against the malformed inputs we've seen
in the wild (Geyser, ViaVersion, BlueMap all have quirky plugin.yml).
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Optional


# (path_str, mtime_ns, size) -> result dict
_CACHE: dict[tuple[str, int, int], dict] = {}


def _read_text_entry(zf: zipfile.ZipFile, name: str) -> Optional[str]:
    """Read a small text entry; return None if absent or unreadable."""
    try:
        with zf.open(name) as f:
            data = f.read(16384)  # 16 KB cap — plugin metadata is never larger
        # Most plugin yml files are utf-8; some legacy ones are latin-1
        for enc in ("utf-8", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return None
    except Exception:
        return None


_VERSION_LINE = re.compile(
    r"""^\s*version\s*[:=]\s*['"]?([^'"\r\n#]+?)['"]?\s*(?:\#.*)?$""",
    re.IGNORECASE | re.MULTILINE,
)
_NAME_LINE = re.compile(
    r"""^\s*name\s*[:=]\s*['"]?([^'"\r\n#]+?)['"]?\s*(?:\#.*)?$""",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_yml_field(text: str, pattern: re.Pattern) -> Optional[str]:
    m = pattern.search(text)
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


def _parse_velocity_json(text: str) -> tuple[Optional[str], Optional[str]]:
    """velocity-plugin.json — small JSON, fast to parse."""
    import json
    try:
        d = json.loads(text)
        return (
            (str(d["version"]) if isinstance(d.get("version"), (str, int, float)) else None),
            (str(d["name"]) if isinstance(d.get("name"), str) else None),
        )
    except Exception:
        return None, None


def _parse_manifest(text: str) -> Optional[str]:
    """MANIFEST.MF — `Key: Value` lines, continuation lines start with space."""
    # Stitch continuation lines first
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith(" ") and lines:
            lines[-1] += raw.lstrip()
        else:
            lines.append(raw)
    for line in lines:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        if key.strip().lower() == "implementation-version":
            v = val.strip()
            return v or None
    return None


def inspect_jar(path: str | Path) -> dict:
    """Return metadata from a jar.

    Returns dict with keys:
      ok: bool
      source: one of "plugin.yml", "paper-plugin.yml", "bungee.yml",
              "velocity-plugin.json", "manifest", or None
      version: str | None
      plugin_name: str | None
      error: str | None
    """
    p = Path(path)
    try:
        st = p.stat()
    except (OSError, FileNotFoundError):
        return {"ok": False, "source": None, "version": None,
                "plugin_name": None, "error": "stat_failed"}

    cache_key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    result: dict = {
        "ok": False,
        "source": None,
        "version": None,
        "plugin_name": None,
        "error": None,
    }

    try:
        with zipfile.ZipFile(p, "r") as zf:
            # Try each source in priority order, return on first hit
            for entry_name, kind in (
                ("plugin.yml", "plugin.yml"),
                ("paper-plugin.yml", "paper-plugin.yml"),
                ("bungee.yml", "bungee.yml"),
                ("velocity-plugin.json", "velocity-plugin.json"),
            ):
                text = _read_text_entry(zf, entry_name)
                if text is None:
                    continue
                if kind == "velocity-plugin.json":
                    ver, name = _parse_velocity_json(text)
                else:
                    ver = _parse_yml_field(text, _VERSION_LINE)
                    name = _parse_yml_field(text, _NAME_LINE)
                if ver:
                    result.update({
                        "ok": True, "source": kind,
                        "version": ver, "plugin_name": name,
                    })
                    _CACHE[cache_key] = result
                    return result

            # Last resort: MANIFEST.MF Implementation-Version
            manifest = _read_text_entry(zf, "META-INF/MANIFEST.MF")
            if manifest:
                ver = _parse_manifest(manifest)
                if ver:
                    result.update({
                        "ok": True, "source": "manifest",
                        "version": ver, "plugin_name": None,
                    })
                    _CACHE[cache_key] = result
                    return result

            result["error"] = "no_metadata_found"
    except zipfile.BadZipFile:
        result["error"] = "not_a_jar"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    _CACHE[cache_key] = result
    return result


def clear_cache() -> None:
    _CACHE.clear()


def cache_stats() -> dict:
    return {"entries": len(_CACHE)}
