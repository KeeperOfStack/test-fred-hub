"""Scheduled restart manager.

Holds **two** schedules per server:

  1. **One-shot intent** — fires once and clears itself. Created by "Restart now",
     "Restart at <specific time>", "Restart when no players online". Stored at
     ``data/scheduled-restart-<server>.json``.

  2. **Recurring schedule** — fires on a cadence forever until disabled.
     Cadences: ``daily`` / ``weekly`` / ``monthly``. Stored at
     ``data/recurring-restart-<server>.json``. May optionally also check for
     server-version updates and bump the compose env before firing
     (``include_server_updates=True``).

The single ``RestartScheduler._loop`` evaluates both kinds every tick.
Recurring schedules compute their next-fire UTC and persist it; once they fire,
they re-compute the following window and keep going.

Trigger types for one-shot:
  - now              fire immediately
  - at_time          fire when wall-clock UTC >= scheduled_utc
  - no_players       fire when mcstatus reports 0 players online

Scope for any restart:
  - plugins          docker restart (fast)
  - server           compose recreate (picks up image + env changes)
"""
from __future__ import annotations

import asyncio
import calendar
import json
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Callable, Awaitable


@dataclass
class RestartIntent:
    trigger: str                       # "now" | "at_time" | "no_players"
    scope: str = "plugins"             # "plugins" | "server"
    scheduled_utc: Optional[float] = None    # epoch seconds, for at_time
    max_players: int = 0               # gate: only fire when player count <= this (None means no gate)
    created_at: float = 0.0
    note: str = ""                     # human-readable reason ("BlueMap 5.20 upload")
    status: str = "pending"            # "pending" | "firing" | "done" | "cancelled" | "error"
    last_check: Optional[float] = None
    error: Optional[str] = None
    fired_at: Optional[float] = None
    waiting_for_players: bool = False  # true when primary trigger fired but we're waiting for player count


@dataclass
class RecurringSchedule:
    """A repeating restart that re-arms itself after each fire.

    The frontend sends the cadence in *local* time (HH:MM in user's tz, plus
    cadence-specific anchor like day-of-week). We convert each upcoming fire
    to a UTC epoch using the stored IANA tz name so DST flips are correct.
    """
    cadence: str                       # "daily" | "weekly" | "monthly"
    local_time: str                    # "HH:MM" 24h in user's local tz
    tz: str = "UTC"                    # IANA name e.g. "America/Chicago"
    weekday: Optional[int] = None      # 0=Mon..6=Sun, required for weekly
    day_of_month: Optional[int] = None # 1..31 (clamped to last day of month), required for monthly
    scope: str = "plugins"             # "plugins" | "server"
    include_server_updates: bool = False  # if scope=="server", auto-check & apply Paper updates
    max_players: int = 0               # gate: only fire when player count <= this
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    next_fire_utc: Optional[float] = None
    last_fire_utc: Optional[float] = None
    last_status: str = ""              # "ok" / "error: ..." from most recent fire
    note: str = ""

    def compute_next(self, after_utc: float | None = None) -> float:
        """Return the next fire-time as UTC epoch, anchored *after* the given
        UTC moment (default: now). DST-aware via zoneinfo.
        """
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.tz)
        now_local = datetime.fromtimestamp(after_utc or time.time(), tz=tz)
        hh, mm = (int(x) for x in self.local_time.split(":"))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if self.cadence == "daily":
            if candidate <= now_local:
                candidate += timedelta(days=1)
        elif self.cadence == "weekly":
            target = self.weekday if self.weekday is not None else now_local.weekday()
            # Move candidate to the next occurrence of `target` weekday at HH:MM
            days_ahead = (target - candidate.weekday()) % 7
            candidate += timedelta(days=days_ahead)
            if candidate <= now_local:
                candidate += timedelta(days=7)
        elif self.cadence == "monthly":
            dom = self.day_of_month or now_local.day
            # Clamp to last valid day of that month (e.g. day 31 in Feb → 28/29)
            def _clamp_to_month(year: int, month: int, day: int) -> datetime:
                last = calendar.monthrange(year, month)[1]
                return candidate.replace(year=year, month=month, day=min(day, last))
            candidate = _clamp_to_month(now_local.year, now_local.month, dom)
            if candidate <= now_local:
                y, m = (now_local.year + (1 if now_local.month == 12 else 0),
                        1 if now_local.month == 12 else now_local.month + 1)
                candidate = _clamp_to_month(y, m, dom)
        else:
            raise ValueError(f"unknown cadence {self.cadence!r}")
        return candidate.astimezone(timezone.utc).timestamp()


class RestartScheduler:
    """One scheduler per hub process. Stores at most ONE one-shot intent and
    ONE recurring schedule per server_id. The single _loop evaluates both."""

    def __init__(
        self,
        data_dir: Path,
        on_fire: Callable[[str, "RestartIntent | RecurringSchedule"], Awaitable[dict]],
        get_player_count: Callable[[str], Awaitable[Optional[int]]],
        poll_interval: float = 15.0,
    ):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._on_fire = on_fire
        self._get_player_count = get_player_count
        self._poll_interval = poll_interval
        self._intents: dict[str, RestartIntent] = {}
        self._recurring: dict[str, RecurringSchedule] = {}
        self._task: Optional[asyncio.Task] = None
        self._load_all()

    # ── paths ─────────────────────────────────────────────────────────────
    def _intent_path(self, server_id: str) -> Path:
        return self._data_dir / f"scheduled-restart-{server_id}.json"

    def _recurring_path(self, server_id: str) -> Path:
        return self._data_dir / f"recurring-restart-{server_id}.json"

    # ── load/save ─────────────────────────────────────────────────────────
    def _load_all(self) -> None:
        for p in self._data_dir.glob("scheduled-restart-*.json"):
            sid = p.stem.removeprefix("scheduled-restart-")
            try:
                self._intents[sid] = RestartIntent(**json.loads(p.read_text()))
            except Exception:
                pass
        for p in self._data_dir.glob("recurring-restart-*.json"):
            sid = p.stem.removeprefix("recurring-restart-")
            try:
                self._recurring[sid] = RecurringSchedule(**json.loads(p.read_text()))
            except Exception:
                pass

    def _save_intent(self, server_id: str) -> None:
        intent = self._intents.get(server_id)
        if intent is None:
            self._intent_path(server_id).unlink(missing_ok=True)
        else:
            self._intent_path(server_id).write_text(json.dumps(asdict(intent), indent=2))

    def _save_recurring(self, server_id: str) -> None:
        rec = self._recurring.get(server_id)
        if rec is None:
            self._recurring_path(server_id).unlink(missing_ok=True)
        else:
            self._recurring_path(server_id).write_text(json.dumps(asdict(rec), indent=2))

    # back-compat with the previous single-method save
    def _save(self, server_id: str) -> None:
        self._save_intent(server_id)

    # ── one-shot intent API ───────────────────────────────────────────────
    def get(self, server_id: str) -> Optional[RestartIntent]:
        return self._intents.get(server_id)

    def set_intent(self, server_id: str, intent: RestartIntent) -> RestartIntent:
        intent.created_at = time.time()
        intent.status = "pending"
        intent.last_check = None
        intent.error = None
        intent.fired_at = None
        self._intents[server_id] = intent
        self._save_intent(server_id)
        return intent

    def cancel(self, server_id: str) -> bool:
        if server_id in self._intents:
            self._intents.pop(server_id)
            self._save_intent(server_id)
            return True
        return False

    # ── recurring schedule API ────────────────────────────────────────────
    def get_recurring(self, server_id: str) -> Optional[RecurringSchedule]:
        return self._recurring.get(server_id)

    def set_recurring(self, server_id: str, sched: RecurringSchedule) -> RecurringSchedule:
        now = time.time()
        sched.updated_at = now
        if not sched.created_at:
            sched.created_at = now
        sched.next_fire_utc = sched.compute_next()
        self._recurring[server_id] = sched
        self._save_recurring(server_id)
        return sched

    def cancel_recurring(self, server_id: str) -> bool:
        if server_id in self._recurring:
            self._recurring.pop(server_id)
            self._save_recurring(server_id)
            return True
        return False

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] tick error: {e}")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        await self._tick_intents()
        await self._tick_recurring()

    async def _tick_intents(self) -> None:
        for sid in list(self._intents.keys()):
            intent = self._intents.get(sid)
            if intent is None or intent.status != "pending":
                continue
            intent.last_check = time.time()
            primary_ready = False
            try:
                if intent.trigger == "now":
                    primary_ready = True
                elif intent.trigger == "at_time":
                    primary_ready = (intent.scheduled_utc is not None
                                     and time.time() >= intent.scheduled_utc)
                elif intent.trigger == "no_players":
                    # legacy/redundant — equivalent to at_time=now + max_players gate.
                    # Keep it working: primary is always ready; gate alone decides.
                    primary_ready = True
            except Exception as e:  # noqa: BLE001
                intent.error = str(e)[:200]
                self._save_intent(sid)
                continue

            if not primary_ready:
                self._save_intent(sid)
                continue

            # Primary trigger is ready. The player-count gate applies ONLY to
            # the no_players trigger — at_time fires at its time regardless of
            # who's online (user picked that time deliberately). "now" also bypasses.
            if intent.trigger == "no_players":
                try:
                    count = await self._get_player_count(sid)
                except Exception as e:  # noqa: BLE001
                    intent.error = str(e)[:200]
                    self._save_intent(sid)
                    continue
                # count == None means server unreachable. Don't fire — wait until
                # we can query it. (Otherwise we'd fire blindly on a stuck server.)
                if count is None or count > intent.max_players:
                    intent.waiting_for_players = True
                    self._save_intent(sid)
                    continue
                intent.waiting_for_players = False

            self._save_intent(sid)
            await self._fire_intent(sid, intent)

    async def _tick_recurring(self) -> None:
        for sid in list(self._recurring.keys()):
            rec = self._recurring.get(sid)
            if rec is None or not rec.enabled:
                continue
            if rec.next_fire_utc is None:
                rec.next_fire_utc = rec.compute_next()
                self._save_recurring(sid)
                continue
            if time.time() < rec.next_fire_utc:
                continue
            # Time has arrived. Check player-count gate before firing.
            try:
                count = await self._get_player_count(sid)
            except Exception as e:  # noqa: BLE001
                rec.last_status = f"player-count probe failed: {str(e)[:100]}"
                self._save_recurring(sid)
                continue
            if count is None or count > rec.max_players:
                # Server unreachable or too many players online. Don't re-arm
                # yet — keep checking every tick until the gate opens.
                # Surface the wait in last_status so the UI shows progress.
                rec.last_status = (
                    f"waiting: {count if count is not None else '?'} players online "
                    f"(need ≤ {rec.max_players})"
                )
                self._save_recurring(sid)
                continue
            # Gate is open — fire and re-arm.
            fired_at = time.time()
            try:
                await self._on_fire(sid, rec)
                rec.last_status = "ok"
            except Exception as e:  # noqa: BLE001
                rec.last_status = f"error: {str(e)[:200]}"
            rec.last_fire_utc = fired_at
            rec.next_fire_utc = rec.compute_next(after_utc=fired_at)
            self._save_recurring(sid)

    async def _fire_intent(self, server_id: str, intent: RestartIntent) -> None:
        intent.status = "firing"
        intent.fired_at = time.time()
        self._save_intent(server_id)
        try:
            await self._on_fire(server_id, intent)
            intent.status = "done"
        except Exception as e:  # noqa: BLE001
            intent.status = "error"
            intent.error = str(e)[:500]
        self._save_intent(server_id)


def intent_to_dict(intent: Optional[RestartIntent]) -> Optional[dict]:
    if intent is None:
        return None
    d = asdict(intent)
    if intent.trigger == "at_time" and intent.scheduled_utc:
        d["seconds_until"] = max(0, int(intent.scheduled_utc - time.time()))
    return d


def recurring_to_dict(rec: Optional[RecurringSchedule]) -> Optional[dict]:
    if rec is None:
        return None
    d = asdict(rec)
    if rec.next_fire_utc:
        d["seconds_until_next"] = max(0, int(rec.next_fire_utc - time.time()))
    return d
