// Test Fred Launcher — front-end logic (v0.3)
const $ = (id) => document.getElementById(id);

// ─────────────────────────────────────────────────────────────
// Shared restart-options picker
// ─────────────────────────────────────────────────────────────
// Builds a DOM block with four radio choices: now / at time / no players / stage only.
// Returns { node, getIntent(), reset() }. Caller wires its own submit button.
// When `showScope` is true, also exposes a Plugins-only vs Server+plugins toggle —
// only meaningful when a server version update is also staged.
function buildRestartPicker({ showScope = false, defaultTrigger = "none", allowStageOnly = true } = {}) {
  const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
  const localNow = new Date();
  const defaultLocal = new Date(localNow.getTime() + 30 * 60 * 1000); // +30min default
  const pad = (n) => String(n).padStart(2, "0");
  const defaultLocalStr = `${defaultLocal.getFullYear()}-${pad(defaultLocal.getMonth()+1)}-${pad(defaultLocal.getDate())}T${pad(defaultLocal.getHours())}:${pad(defaultLocal.getMinutes())}`;

  const wrap = document.createElement("div");
  wrap.className = "restart-picker";
  wrap.innerHTML = `
    <div class="rp-group">
      <label class="rp-row">
        <input type="radio" name="rp-trigger" value="now"> 🚀 <b>Restart now</b>
        <span class="rp-hint">Fires immediately, even if players are online.</span>
      </label>
      <label class="rp-row">
        <input type="radio" name="rp-trigger" value="at_time"> 🕒 <b>Restart at a specific time</b>
      </label>
      <div class="rp-sub rp-sub-at_time" style="display:none">
        <input type="datetime-local" class="rp-when" value="${defaultLocalStr}">
        <div class="rp-hint">Your time zone: <code>${tz}</code> · stored as UTC.</div>
      </div>
      <label class="rp-row">
        <input type="radio" name="rp-trigger" value="no_players"> 👥 <b>Restart when player count is at or below</b>
        <input type="number" class="rp-maxplayers" min="0" max="1000" value="0" style="width:70px;margin-left:6px">
        <span class="rp-hint">Default 0 = wait for an empty server. Raise if a Geyser/bot is always online.</span>
      </label>
      ${allowStageOnly ? `
      <label class="rp-row">
        <input type="radio" name="rp-trigger" value="none"> 📋 <b>Stage only, no restart</b>
        <span class="rp-hint">Add to the queue; you can restart later from the Updates tab.</span>
      </label>` : ""}

      ${showScope ? `
      <hr class="rp-sep">
      <div class="rp-scope">
        <div class="rp-scope-title">Scope:</div>
        <label class="rp-row">
          <input type="radio" name="rp-scope" value="plugins" checked> ⚡ <b>Plugins only</b>
          <span class="rp-hint">Fast docker restart, ~30 s. Picks up any staged jars.</span>
        </label>
        <label class="rp-row">
          <input type="radio" name="rp-scope" value="server"> 🛠 <b>Server + plugins</b>
          <span class="rp-hint">Full compose recreate. Required if a server-version change is staged.</span>
        </label>
      </div>` : ""}
    </div>`;
  // default selection
  const radios = wrap.querySelectorAll('input[name="rp-trigger"]');
  for (const r of radios) {
    if (r.value === defaultTrigger) r.checked = true;
    r.addEventListener("change", () => {
      for (const sub of wrap.querySelectorAll(".rp-sub")) sub.style.display = "none";
      const sub = wrap.querySelector(`.rp-sub-${r.value}`);
      if (sub && r.checked) sub.style.display = "block";
    });
  }
  // open the appropriate sub-block if the default was at_time
  const initialSub = wrap.querySelector(`.rp-sub-${defaultTrigger}`);
  if (initialSub) initialSub.style.display = "block";

  function getIntent() {
    const triggerEl = wrap.querySelector('input[name="rp-trigger"]:checked');
    if (!triggerEl) return null;
    const trigger = triggerEl.value;
    const scopeEl = wrap.querySelector('input[name="rp-scope"]:checked');
    const scope = scopeEl ? scopeEl.value : "plugins";
    const mpEl = wrap.querySelector(".rp-maxplayers");
    let maxPlayers = mpEl ? parseInt(mpEl.value, 10) : 0;
    if (!Number.isFinite(maxPlayers) || maxPlayers < 0) maxPlayers = 0;
    // Only the no_players trigger uses the gate. For at_time/now/none send 0.
    const payload = { trigger, scope, max_players: trigger === "no_players" ? maxPlayers : 0 };
    if (trigger === "at_time") {
      const whenStr = wrap.querySelector(".rp-when").value;
      if (!whenStr) { toast("Pick a date and time.", "err"); return null; }
      const dt = new Date(whenStr);
      if (isNaN(dt.getTime())) { toast("Invalid date.", "err"); return null; }
      if (dt.getTime() < Date.now() - 60_000) { toast("Time is in the past.", "err"); return null; }
      payload.scheduled_utc = Math.floor(dt.getTime() / 1000);
      payload.local_iso = whenStr;
      payload.tz = tz;
    }
    return payload;
  }
  return { node: wrap, getIntent };
}

// Convenience: POST the intent to the schedule endpoint and toast the result.
// Returns the parsed response (or null on failure).
async function submitRestartIntent(intent, { successMsg, errorMsg } = {}) {
  if (!intent) return null;
  try {
    const r = await api("/api/restart/schedule", { method: "POST", body: intent });
    const msg = successMsg || describeIntent(r.intent) || "Saved.";
    toast(msg, "ok");
    refreshSchedule();
    return r;
  } catch (e) {
    toast((errorMsg || "Schedule failed") + ": " + e.message, "err");
    return null;
  }
}

function describeIntent(intent) {
  if (!intent) return "Restart scheduling cancelled.";
  const scopeWord = intent.scope === "server" ? "server + plugins" : "plugins";
  if (intent.trigger === "now") return `Restarting (${scopeWord}) now…`;
  if (intent.trigger === "at_time") {
    const when = new Date(intent.scheduled_utc * 1000);
    return `Restart (${scopeWord}) scheduled for ${when.toLocaleString()}.`;
  }
  if (intent.trigger === "no_players") return `Restart (${scopeWord}) will fire when no players are online.`;
  if (intent.trigger === "none") return "Staged only — no restart scheduled.";
  return "Schedule updated.";
}

// Updates the "current schedule" banner on the Updates tab.
// Idempotent — safe to call after any schedule POST.
async function refreshSchedule() {
  const banner = $("schedule-banner");
  if (!banner) return;  // banner not on this page
  try {
    const r = await api("/api/restart/schedule");
    const intent = r.intent;
    if (!intent || intent.status !== "pending") {
      banner.hidden = true;
      banner.innerHTML = "";
      return;
    }
    const scopeWord = intent.scope === "server" ? "server + plugins" : "plugins";
    let when = "";
    if (intent.trigger === "at_time" && intent.scheduled_utc) {
      const dt = new Date(intent.scheduled_utc * 1000);
      const mins = Math.max(0, Math.floor((dt - Date.now()) / 60000));
      when = ` at ${dt.toLocaleString()} (in ${mins} min)`;
    } else if (intent.trigger === "no_players") {
      when = " — waiting for player gate";
    } else if (intent.trigger === "now") {
      when = " — firing now";
    }
    const gate = (intent.trigger !== "now")
      ? ` · gate: ≤ ${intent.max_players ?? 0} players`
      : "";
    const waitingTag = intent.waiting_for_players
      ? ' <span style="color:#f5b400">⏳ waiting for player gate…</span>'
      : "";
    banner.hidden = false;
    banner.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px">
        <div>📅 <b>Restart scheduled</b> (${scopeWord})${when}${gate}${waitingTag}${intent.note ? ` · ${intent.note}` : ""}</div>
        <button class="mc-btn" id="schedule-cancel-btn">Cancel</button>
      </div>`;
    $("schedule-cancel-btn").addEventListener("click", async () => {
      try {
        await api("/api/restart/schedule", { method: "DELETE" });
        toast("Scheduled restart cancelled.", "ok");
        refreshSchedule();
      } catch (e) { toast("Cancel failed: " + e.message, "err"); }
    });
  } catch (e) {
    banner.hidden = true;
  }
}

// Modal version of the picker, used by the Updates page "Apply Pending" button.
// `contextual.serverPending` enables the Plugins-vs-Server scope toggle (read
// from lastServer.pending_restart at open time when not explicitly set).

// ─────────────────────────────────────────────────────────────
// Recurring schedule UI
// ─────────────────────────────────────────────────────────────

const WEEKDAY_NAMES = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];

function describeRecurring(r) {
  if (!r) return "No recurring schedule.";
  const tParts = r.local_time.split(":");
  const hh = parseInt(tParts[0], 10);
  const mm = tParts[1];
  const ampm = hh >= 12 ? "PM" : "AM";
  const h12 = ((hh + 11) % 12) + 1;
  const timeStr = `${h12}:${mm} ${ampm}`;
  let when = "";
  if (r.cadence === "daily") when = `every day at ${timeStr}`;
  else if (r.cadence === "weekly") when = `every ${WEEKDAY_NAMES[r.weekday] || "?"} at ${timeStr}`;
  else if (r.cadence === "monthly") when = `on day ${r.day_of_month} of each month at ${timeStr}`;
  const scopeWord = r.scope === "server" ? "server + plugins" : "plugins";
  const upd = (r.scope === "server" && r.include_server_updates) ? " · auto-checks for server updates" : "";
  const plUpd = r.include_plugin_updates ? " · auto-stages plugin updates" : "";
  const gate = ` · gate: ≤ ${r.max_players ?? 0} players`;
  return `${when} (${r.tz}) — restart ${scopeWord}${upd}${plUpd}${gate}`;
}

async function refreshRecurring() {
  const summary = $("recurring-summary");
  const delBtn = $("btn-recurring-delete");
  if (!summary) return;
  try {
    const r = await api("/api/restart/recurring");
    const rec = r.recurring;
    if (!rec) {
      summary.innerHTML = '<span class="empty">No recurring schedule. Click Set / Edit to add one.</span>';
      delBtn.hidden = true;
      return;
    }
    let nextStr = "";
    if (rec.next_fire_utc) {
      const dt = new Date(rec.next_fire_utc * 1000);
      const mins = Math.max(0, Math.floor((dt - Date.now()) / 60000));
      const h = Math.floor(mins / 60);
      const d = Math.floor(h / 24);
      let rel;
      if (mins < 60) rel = `in ${mins} min`;
      else if (h < 48) rel = `in ${h} h`;
      else rel = `in ${d} d`;
      nextStr = `<div style="font-size:12px;opacity:0.7;margin-top:4px">Next fire: ${dt.toLocaleString()} (${rel})</div>`;
    }
    let lastStr = "";
    if (rec.last_fire_utc) {
      const dt = new Date(rec.last_fire_utc * 1000);
      const ok = rec.last_status === "ok";
      lastStr = `<div style="font-size:12px;opacity:0.6">Last fire: ${dt.toLocaleString()} — <span style="color:${ok?'#4ade80':'#f87171'}">${rec.last_status || "?"}</span></div>`;
    }
    summary.innerHTML = `
      <div><b>${describeRecurring(rec)}</b></div>
      ${nextStr}
      ${lastStr}
      ${rec.note ? `<div style="font-size:11px;opacity:0.6;margin-top:4px">Note: ${rec.note}</div>` : ""}`;
    delBtn.hidden = false;
  } catch (e) {
    summary.innerHTML = `<span class="empty">Failed to load: ${e.message}</span>`;
  }
}

function openRecurringModal(existing) {
  const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
  const cur = existing || { cadence: "weekly", local_time: "04:00", tz,
                            weekday: 6, day_of_month: 1, scope: "plugins",
                            include_server_updates: false, note: "" };
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:600px">
      <h2>🔁 Recurring Restart</h2>
      <p class="hint" style="margin:0 0 12px">Pick a cadence. The schedule re-arms itself after each fire and survives hub restarts. Times are interpreted in <code>${tz}</code>.</p>

      <div class="rec-form">
        <label class="rec-row">
          <span class="rec-label">Cadence</span>
          <select id="rec-cadence">
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>
        </label>

        <label class="rec-row rec-row-weekly" style="display:none">
          <span class="rec-label">Day of week</span>
          <select id="rec-weekday">
            <option value="0">Monday</option>
            <option value="1">Tuesday</option>
            <option value="2">Wednesday</option>
            <option value="3">Thursday</option>
            <option value="4">Friday</option>
            <option value="5">Saturday</option>
            <option value="6">Sunday</option>
          </select>
        </label>

        <label class="rec-row rec-row-monthly" style="display:none">
          <span class="rec-label">Day of month</span>
          <input type="number" id="rec-dom" min="1" max="31" value="${cur.day_of_month || 1}" style="width:80px">
          <span class="rp-hint" style="margin-left:8px">If month is shorter, clamps to last day.</span>
        </label>

        <label class="rec-row">
          <span class="rec-label">Time</span>
          <input type="time" id="rec-time" value="${cur.local_time}" step="60">
          <span class="rp-hint" style="margin-left:8px">Your time zone: <code>${tz}</code></span>
        </label>

        <hr class="rp-sep">

        <div class="rec-row" style="flex-direction:column;align-items:flex-start;gap:6px">
          <div class="rec-label" style="margin-bottom:4px">Scope</div>
          <label class="rp-row"><input type="radio" name="rec-scope" value="plugins"> ⚡ <b>Plugins only</b> <span class="rp-hint">Fast docker restart (~30 s).</span></label>
          <label class="rp-row"><input type="radio" name="rec-scope" value="server"> 🛠 <b>Server + plugins</b> <span class="rp-hint">Compose recreate; needed for server-image updates.</span></label>
        </div>

        <div class="rec-row rec-row-server-extra" style="display:none">
          <label class="rp-row" style="flex:1">
            <input type="checkbox" id="rec-include-updates">
            <b>Also check for server updates each cycle</b>
            <span class="rp-hint">Before the recreate, queries the Paper API for the latest stable. If newer than what's installed, bumps <code>VERSION</code>/<code>PAPER_BUILD</code> in the compose file and recreates onto the new build.</span>
          </label>
        </div>

        <div class="rec-row">
          <label class="rp-row" style="flex:1">
            <input type="checkbox" id="rec-include-plugin-updates">
            <b>Also check & stage plugin updates each cycle</b>
            <span class="rp-hint">Before the restart, runs a full plugin check across Modrinth/Hangar/Spiget/Geyser and stages every available update into <code>plugins/update/</code> so they apply on the same restart. Works with both scopes.</span>
          </label>
        </div>

        <hr class="rp-sep">

        <label class="rec-row">
          <span class="rec-label">Player gate</span>
          <input type="number" id="rec-maxplayers" min="0" max="1000" value="${cur.max_players ?? 0}" style="width:80px">
          <span class="rp-hint" style="margin-left:8px">
            Only fire when this many or fewer players are online. <b>0</b> = empty server. Raise it for Geyser bot accounts or other always-on connections.
          </span>
        </label>

        <label class="rec-row">
          <span class="rec-label">Note</span>
          <input type="text" id="rec-note" value="${cur.note || ""}" maxlength="200" placeholder="optional reminder">
        </label>
      </div>

      <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
        <button class="mc-btn" id="rec-cancel" style="background:linear-gradient(180deg,#6b7280 0%,#374151 100%)">Cancel</button>
        <button class="mc-btn mc-btn-warn" id="rec-save">${existing ? "Update" : "Save"}</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const cad = modal.querySelector("#rec-cadence");
  const wkRow = modal.querySelector(".rec-row-weekly");
  const moRow = modal.querySelector(".rec-row-monthly");
  const wkSel = modal.querySelector("#rec-weekday");
  const moInput = modal.querySelector("#rec-dom");
  const timeInput = modal.querySelector("#rec-time");
  const noteInput = modal.querySelector("#rec-note");
  const scopeRadios = modal.querySelectorAll('input[name="rec-scope"]');
  const serverExtra = modal.querySelector(".rec-row-server-extra");
  const incCheckbox = modal.querySelector("#rec-include-updates");
  const incPluginCheckbox = modal.querySelector("#rec-include-plugin-updates");

  // pre-fill from existing
  cad.value = cur.cadence;
  wkSel.value = String(cur.weekday ?? 6);
  for (const r of scopeRadios) r.checked = (r.value === cur.scope);
  incCheckbox.checked = !!cur.include_server_updates;
  incPluginCheckbox.checked = !!cur.include_plugin_updates;
  function syncRows() {
    wkRow.style.display = cad.value === "weekly" ? "" : "none";
    moRow.style.display = cad.value === "monthly" ? "" : "none";
    const scope = [...scopeRadios].find(r => r.checked)?.value || "plugins";
    serverExtra.style.display = scope === "server" ? "" : "none";
  }
  cad.addEventListener("change", syncRows);
  scopeRadios.forEach(r => r.addEventListener("change", syncRows));
  syncRows();

  modal.querySelector("#rec-cancel").addEventListener("click", () => modal.remove());
  modal.querySelector("#rec-save").addEventListener("click", async () => {
    const scope = [...scopeRadios].find(r => r.checked)?.value || "plugins";
    let maxPlayers = parseInt(modal.querySelector("#rec-maxplayers").value, 10);
    if (!Number.isFinite(maxPlayers) || maxPlayers < 0) maxPlayers = 0;
    const payload = {
      cadence: cad.value,
      local_time: timeInput.value,
      tz,
      scope,
      include_server_updates: scope === "server" ? incCheckbox.checked : false,
      include_plugin_updates: incPluginCheckbox.checked,
      max_players: maxPlayers,
      note: noteInput.value.trim(),
    };
    if (cad.value === "weekly") payload.weekday = parseInt(wkSel.value, 10);
    if (cad.value === "monthly") payload.day_of_month = parseInt(moInput.value, 10);
    try {
      const r = await api("/api/restart/recurring", { method: "POST", body: payload });
      toast(`Recurring restart ${existing ? "updated" : "set"}: ${describeRecurring(r.recurring)}.`, "ok");
      await refreshRecurring();
      modal.remove();
    } catch (e) {
      toast("Save failed: " + e.message, "err");
    }
  });
}


function openSchedulerModal({ contextual = "updates", note = "", defaultTrigger = "now" } = {}) {
  const serverPending = !!(lastServer && lastServer.pending_restart);
  const showScope = serverPending;
  // Updates page IS the staging page — "Stage only" would be a no-op there.
  const allowStageOnly = (contextual !== "updates");
  const picker = buildRestartPicker({ showScope, defaultTrigger, allowStageOnly });
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:560px">
      <h2>📅 Schedule Restart</h2>
      ${serverPending
        ? '<p class="hint" style="margin:0 0 10px">A server version change is staged. Choose whether to apply it now (server + plugins) or only restart plugins.</p>'
        : '<p class="hint" style="margin:0 0 10px">Pending plugin updates will apply on whichever restart fires.</p>'}
      <div id="rp-mount"></div>
      <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
        <button class="mc-btn" id="rp-cancel" style="background:linear-gradient(180deg,#6b7280 0%,#374151 100%)">Cancel</button>
        <button class="mc-btn mc-btn-warn" id="rp-submit">Confirm</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector("#rp-mount").appendChild(picker.node);
  modal.querySelector("#rp-cancel").addEventListener("click", () => modal.remove());
  modal.querySelector("#rp-submit").addEventListener("click", async () => {
    const intent = picker.getIntent();
    if (!intent) return;
    if (note) intent.note = note;
    const r = await submitRestartIntent(intent);
    if (r) modal.remove();
  });
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("toast-container").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

async function api(path, opts = {}) {
  // Convenience: if body is an object (not string/FormData), JSON-encode + set header
  if (opts.body && typeof opts.body === "object"
      && !(opts.body instanceof FormData)
      && !(opts.body instanceof URLSearchParams)
      && !(opts.body instanceof Blob)) {
    opts = { ...opts, body: JSON.stringify(opts.body),
             headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  }
  const r = await fetch(path, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!r.ok) throw new Error((data && data.detail) || text || "HTTP " + r.status);
  return data;
}

function busy(btn, on, label) {
  if (!btn) return;

  if (on) {
    btn._origText = btn._origText || btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${label || btn._origText}`;
  } else {
    btn.disabled = false;
    btn.innerHTML = btn._origText || btn.innerHTML;
  }
}

// ── Tabs ───────────────────────────────────────────────────────
function activateTab(name) {
  document.querySelectorAll(".nav-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name)
  );
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name)
  );
  localStorage.setItem("tab", name);

  // Tab-specific kickers
  if (name === "console") startConsoleStream();
  else stopConsoleStream();
  if (name === "updates") refreshPaperCard();
}

// ── Server panel ───────────────────────────────────────────────
let hostInfo = null;
let lastServer = null;

async function loadHost() {
  try {
    hostInfo = await api("/api/server/host");
    const addr = hostInfo.lan_address || `localhost:${hostInfo.port}`;
    $("connect-addr").textContent = addr;
  } catch (e) {
    $("connect-addr").textContent = `localhost:25566`;
  }
}

async function refreshServer() {
  try {
    const s = await api("/api/server");
    lastServer = s;

    // hero
    $("hero-motd").textContent = s.motd || "We truly live in a society";
    $("hero-status").textContent = s.running
      ? (s.health ? s.health.toUpperCase() : "RUNNING")
      : "OFFLINE";
    $("hero-version").textContent = s.version || "?";

    if (s.players && s.players.online != null) {
      $("hero-players").textContent = `${s.players.online}/${s.players.max}`;
      $("hero-ping").textContent = s.players.latency_ms ? `${s.players.latency_ms} ms` : "—";
      const names = (s.players.names || []).filter(Boolean);
      $("player-names").textContent = names.length ? "Online: " + names.join(", ") : "";
    } else {
      $("hero-players").textContent = "—";
      $("hero-ping").textContent = "—";
      $("player-names").textContent = s.players?.error ? "⚠ " + s.players.error : "";
    }

    // sidebar pill — tri-state: starting (yellow) / healthy (green) / unhealthy (red) / offline (gray-red)
    const pill = $("server-pill");
    if (!s.running) {
      pill.className = "pill pill-down"; pill.textContent = "● OFFLINE";
    } else if (s.health === "starting") {
      pill.className = "pill pill-warn"; pill.textContent = "● STARTING";
    } else if (s.health === "unhealthy") {
      pill.className = "pill pill-bad"; pill.textContent = "● UNHEALTHY";
    } else {
      // "healthy" or null (no healthcheck defined) → treat as online
      pill.className = "pill pill-up"; pill.textContent = "● ONLINE";
    }
    $("foot-version").textContent = `${s.version || "?"}-${s.build || "?"}`;

    // pending
    renderPending(s.pending_updates || [], s.pending_deletions || []);
    const badge = $("nav-updates-badge");
    if ((s.pending_updates || []).length) {
      badge.hidden = false; badge.textContent = s.pending_updates.length;
    } else { badge.hidden = true; }

  } catch (e) {
    toast("Server status failed: " + e.message, "err");
  }
}

function renderPending(updates, deletions = []) {
  const root = $("pending-list");
  if (!updates.length && !deletions.length) {
    root.innerHTML = '<div class="empty">Nothing pending. Stage updates from the Plugins tab.</div>';
    return;
  }
  const updateRows = updates.map((f) =>
    `<div class="pending-item">
       <span>⏳ ${f}</span>
       <button class="mc-btn mc-btn-danger" data-action="cancel-pending" data-file="${f}">Cancel</button>
     </div>`
  ).join("");
  const deleteRows = deletions.map((f) =>
    `<div class="pending-item pending-deletion">
       <span>🗑 ${f} <small style="opacity:0.7">— scheduled for deletion</small></span>
       <button class="mc-btn" data-action="cancel-deletion" data-file="${f}">Keep</button>
     </div>`
  ).join("");
  root.innerHTML = updateRows + deleteRows;
}

async function refreshPaperCard() {
  try {
    const r = await api("/api/server/paper");
    $("paper-current").textContent = `${r.version} build ${r.current_build}`;
    $("paper-latest").textContent = r.latest
      ? `${r.version} build ${r.latest.id}` + (r.update_available ? "  ⚡" : "")
      : "—";
  } catch (e) {
    $("paper-latest").textContent = "lookup failed";
  }
}

async function checkPaper(btn) {
  busy(btn, true, "Checking…");
  try {
    const r = await api("/api/server/latest");
    const typeName = r.type.display;
    if (!r.auto_latest_supported) {
      toast(`${typeName}: automated latest-version lookup not supported. Configure manually on the Server tab.`, "");
      refreshPaperCard();
      return;
    }
    const cur = r.current;
    const lat = r.latest;
    const msg = r.update_available
      ? `${typeName} update available: ${cur.version}${cur.build ? " build " + cur.build : ""} → ${lat.version}${lat.build ? " build " + lat.build : ""}`
      : `${typeName} is up to date (${cur.version}${cur.build ? " build " + cur.build : ""})`;
    toast(msg, r.update_available ? "ok" : "");
    refreshPaperCard();
  } catch (e) { toast("Update check failed: " + e.message, "err"); }
  finally { busy(btn, false); }
}

async function updatePaper(btn) {
  // If we're in "recreate-pending-changes" mode, skip the stage step and just recreate
  if (btn.dataset.mode === "recreate") {
    if (!(await confirmModal({
      title: "Recreate container?",
      message: "Apply staged version changes by recreating the container.\nServer will go offline ~30s.",
      confirmText: "Recreate", danger: true,
    }))) return;
    busy(btn, true, "Recreating…");
    try {
      const r = await api("/api/server/recreate", { method: "POST" });
      const bits = ["Container recreated"];
      if (r.image_bumped && r.image_bump) {
        bits.push(`Java auto-bumped: :${r.image_bump.old_tag} → :${r.image_bump.new_tag}`);
      } else if (r.image_bumped) {
        bits.push(`auto-bumped image → ${r.image}`);
      }
      toast(bits.join(" · ") + ". Server is starting up.", "ok");
      if (r.image_bumped && r.image_bump) {
        toast(`☕ Java runtime updated: ${r.image_bump.reason}`, "ok");
      }
      setTimeout(() => { refreshServer(); refreshPaperCard(); loadConfig(); }, 4000);
      setTimeout(() => { refreshServer(); refreshPaperCard(); refreshPlugins(); }, 15000);
    } catch (e) { toast("Recreate failed: " + e.message, "err"); }
    finally { busy(btn, false); }
    return;
  }

  // Normal flow: stage latest, then offer to recreate
  let latestInfo = null;
  try { latestInfo = await api("/api/server/latest"); } catch {}
  const typeName = latestInfo?.type?.display || "the server";
  const lat = latestInfo?.latest;
  const summary = lat
    ? `${typeName} → ${lat.version}${lat.build ? " build " + lat.build : ""}`
    : `${typeName} (latest stable)`;
  if (!(await confirmModal({
    title: "Update server?",
    message: `Stage server update to ${summary} AND recreate the container.\nServer will restart.`,
    confirmText: "Update + Recreate", danger: true,
  }))) return;
  busy(btn, true, "Updating…");
  try {
    const r = await api("/api/server/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recreate: true }),
    });
    if (r.skipped) toast(`Already on latest (${r.current?.version || ""}).`);
    else {
      const to = r.to || {};
      const bits = [`Updated ${typeName} → ${to.version}${to.build ? " build " + to.build : ""}`];
      if (r.image_bumped && r.image_bump) {
        bits.push(`Java auto-bumped: :${r.image_bump.old_tag} → :${r.image_bump.new_tag}`);
      } else if (r.image_bumped) {
        bits.push(`auto-bumped image → ${r.image}`);
      }
      bits.push(r.recreated ? "Container recreated." : "Recreate pending.");
      toast(bits.join(". ") + ".", "ok");
      if (r.image_bumped && r.image_bump) {
        toast(`☕ Java runtime updated: ${r.image_bump.reason}`, "ok");
      }
    }
    setTimeout(() => { refreshServer(); refreshPaperCard(); loadConfig(); }, 4000);
    setTimeout(() => { refreshServer(); refreshPaperCard(); refreshPlugins(); }, 15000);
  } catch (e) { toast("Update failed: " + e.message, "err"); }
  finally { busy(btn, false); }
}

async function refreshPaperCard() {
  try {
    const r = await api("/api/server/latest");
    const typeName = r.type.display;
    $("paper-type").textContent = typeName;
    $("updates-server-heading").textContent = `📦 ${typeName} Server`;

    const run = r.running || r.current || {};
    const cfg = r.configured || {};
    const runStr = run.version ? `${run.version}${run.build ? " build " + run.build : ""}` : "—";
    const cfgStr = cfg.version ? `${cfg.version}${cfg.build ? " build " + cfg.build : ""}` : "—";

    $("paper-current").innerHTML = r.pending_restart
      ? `${runStr} <span class="badge-pending">STAGED → ${cfgStr} · recreate to apply</span>`
      : runStr;

    const lat = r.latest || {};
    if (!r.auto_latest_supported) {
      $("paper-latest").textContent = "not auto-detected";
      $("paper-update-notice").hidden = false;
      $("paper-update-notice").textContent = `Automated updates aren't supported for ${typeName}. ${r.notes || "Use the Server tab to set version/build manually."}`;
      $("btn-server-update").disabled = true;
    } else {
      $("paper-latest").innerHTML = lat.version
        ? `${lat.version}${lat.build ? " build " + lat.build : ""}` +
          (r.update_available ? ' <span class="badge-update">UPDATE</span>' : "")
        : "—";
      const noticeParts = [];
      if (r.pending_restart) noticeParts.push(`⚠ Compose is set to ${cfgStr} but container is still running ${runStr}. Click "Recreate Container" to apply.`);
      if (r.pending_java_change) {
        const pj = r.pending_java_change;
        noticeParts.push(`☕ Java runtime will switch on next recreate: :${pj.old_tag} → :${pj.new_tag} (Java ${pj.java}). ${pj.reason}`);
      }
      if (r.notes) noticeParts.push(r.notes);
      $("paper-update-notice").hidden = noticeParts.length === 0;
      $("paper-update-notice").textContent = noticeParts.join(" — ");
      $("btn-server-update").disabled = !r.update_available && !r.pending_restart && !r.pending_java_change;
      $("btn-server-update").textContent = (r.pending_restart || r.pending_java_change)
        ? "⟳ Recreate Container to Apply Staged"
        : "⬆ Update to Latest Stable";
      $("btn-server-update").dataset.mode = (r.pending_restart || r.pending_java_change) ? "recreate" : "update";
    }
  } catch (e) {
    $("paper-latest").textContent = "lookup failed: " + e.message;
  }
}

async function controlServer(action, btn) {
  const word = { restart: "Restart", start: "Start", stop: "Stop" }[action];
  const desc = {
    restart: "Stops and starts the container. Players will be disconnected briefly.",
    start: "Starts the container. Players can connect once Paper finishes loading (~30s–3min).",
    stop: "Stops the container. Players will be disconnected and won't be able to reconnect until you start it again.",
  }[action];
  if (!(await confirmModal({
    title: `${word} the server?`,
    message: desc,
    confirmText: word, danger: action !== "start",
  }))) return;
  busy(btn, true, `${word}ing…`);
  try {
    await api(`/api/server/${action}`, { method: "POST" });
    toast(`${word} issued.`, "ok");
    setTimeout(refreshServer, 2500);
    setTimeout(() => { refreshServer(); refreshPlugins(); }, 12000);
  } catch (e) { toast(`${word} failed: ` + e.message, "err"); }
  finally { busy(btn, false); }
}

// ── Plugin tab ─────────────────────────────────────────────────
let pluginCache = [];

function renderPlugins(list, updateInfo = {}) {
  const root = $("plugins-list");
  if (!list.length) {
    root.innerHTML = '<div class="empty">No plugins installed. Use the Catalog tab or upload a .jar.</div>';
    return;
  }
  root.innerHTML = "";
  for (const p of list) {
    const info = updateInfo[p.file];
    const card = document.createElement("div");
    card.className = "plugin" + (info?.update_available ? " plugin-update" : "");
    const iconHtml = info?.icon
      ? `<img class="plugin-icon" src="${info.icon}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'plugin-icon',textContent:'🧩'}))">`
      : `<div class="plugin-icon">🧩</div>`;
    const sourceTag = info?.source ? `<span class="source ${info.source}">${info.source}</span>` : "";
    const updateBadge = info?.update_available ? `<span class="badge-update">UPDATE</span>` : "";
    const pendingBadge = info?.pending_restart ? `<span class="badge-pending">STAGED · restart to apply</span>` : "";
    const appliedMarker = info?.applied_version ? ' <span class="applied-marker" title="version remembered from last stage — plugin.yml may report less precisely">✓</span>' : "";
    const latestLine = info?.latest_version
      ? `<div>Latest: <span class="ver-latest ${info.update_available ? "" : "stale"}">${info.latest_version}</span>${sourceTag}${updateBadge}${pendingBadge}</div>`
      : info ? `<div style="opacity:0.7;font-style:italic;">no upstream match</div>` : "";
    const stagedLine = info?.staged_version
      ? `<div style="opacity:0.85;font-size:0.85em;">↳ staged: <code>${info.staged_version}</code></div>` : "";
    const errLine = info?.error ? `<div class="plugin-error">⚠ ${info.error}</div>` : "";
    const projectLink = info?.project_url
      ? `<a href="${info.project_url}" target="_blank" rel="noopener"><button class="mc-btn">↗ Page</button></a>` : "";
    const updateBtn = info?.update_available
      ? `<button class="mc-btn mc-btn-warn" data-action="stage" data-file="${p.file}">Stage Update</button>` : "";
    // Use the remembered installed version when available (more precise than plugin.yml)
    const installedVersion = info?.current_version || p.version || "?";
    card.innerHTML = `
      ${iconHtml}
      <div class="plugin-body">
        <h3>${p.name || "(unknown)"}</h3>
        <div class="filename">${p.file}</div>
        <div class="plugin-meta">
          <div>Installed: <span class="ver-current">${installedVersion}</span>${appliedMarker}</div>
          ${latestLine}
          ${stagedLine}
        </div>
        ${errLine}
        <div class="plugin-actions">
          ${updateBtn}${projectLink}
          <button class="mc-btn mc-btn-danger" data-action="delete" data-file="${p.file}">Delete</button>
        </div>
      </div>`;
    root.appendChild(card);
  }
}

async function refreshPlugins() {
  const root = $("plugins-list");
  // Only flash "Loading…" when empty. Otherwise re-render in place.
  if (!root.querySelector(".plugin")) {
    root.innerHTML = '<div class="empty">Loading plugins…</div>';
  }
  try {
    const r = await api("/api/plugins");
    const updateInfo = Object.fromEntries(pluginCache.map((x) => [x.file, x]));
    renderPlugins(r.plugins, updateInfo);
    renderPending(r.pending_updates || []);
    const badge = $("nav-plugins-badge");
    const upd = Object.values(updateInfo).filter((x) => x.update_available).length;
    if (upd) { badge.hidden = false; badge.textContent = upd; } else { badge.hidden = true; }
  } catch (e) { toast("Failed to list plugins: " + e.message, "err"); }
}

async function checkAllPlugins(btn) {
  busy(btn, true, "Querying sources…");
  try {
    const r = await api("/api/plugins/check", { method: "POST" });
    pluginCache = r.results || [];
    const upd = pluginCache.filter((p) => p.update_available).length;
    const matched = pluginCache.filter((p) => p.latest_version).length;
    toast(`Matched ${matched}/${pluginCache.length} · ${upd} update${upd === 1 ? "" : "s"} available.`, upd ? "ok" : "");
    await refreshPlugins();
  } catch (e) { toast("Update check failed: " + e.message, "err"); }
  finally { busy(btn, false); }
}

async function stageAllPlugins(btn) {
  const upd = pluginCache.filter((p) => p.update_available);
  if (!upd.length) { toast("Nothing to stage. Run 'Check All' first."); return; }
  if (!(await confirmModal({
    title: "Stage all updates?",
    message: `Stage updates for ${upd.length} plugin${upd.length === 1 ? "" : "s"}. Jars will be downloaded to plugins/update/ and applied on next restart.`,
    confirmText: "Stage All", danger: false,
  }))) return;
  busy(btn, true, "Staging…");
  try {
    const r = await api("/api/plugins/stage-all", { method: "POST" });
    toast(`Staged ${r.staged.length}, failed ${r.failed.length}.`, "ok");
    if (r.failed.length) console.warn("staging failures:", r.failed);
  } catch (e) { toast("Batch stage failed: " + e.message, "err"); }
  finally { busy(btn, false); refreshServer(); }
}

async function stageOne(file, btn) {
  busy(btn, true, "Staging…");
  try {
    await api(`/api/plugins/${encodeURIComponent(file)}/stage-update`, { method: "POST" });
    toast(`Staged ${file}.`, "ok");
    refreshServer();
  } catch (e) { toast("Stage failed: " + e.message, "err"); }
  finally { busy(btn, false); }
}

async function deleteOne(file, btn) {
  // Server is probably running, so default to staged deletion — the jar gets
  // removed during the next restart, not while it's loaded in the JVM.
  const isRunning = lastServer && lastServer.running;
  let immediate = false;
  if (!isRunning) {
    if (!(await confirmModal({
      title: `Delete ${file}?`,
      message: "Server is offline so this happens immediately. The jar will be removed from plugins/.",
      confirmText: "Delete", danger: true,
    }))) return;
    immediate = true;
  } else {
    // Three-way choice: stage (safe) / force now (dangerous) / cancel.
    const choice = await new Promise((resolve) => {
      const modal = document.createElement("div");
      modal.className = "modal-overlay";
      modal.innerHTML = `
        <div class="modal-box" style="max-width:520px">
          <h2>Delete <code>${file}</code>?</h2>
          <p style="margin:0 0 14px;line-height:1.5">Server is online. Pick how you want to handle this:</p>
          <ul style="margin:0 0 14px 18px;padding:0;font-size:13px;line-height:1.6">
            <li><b>Stage Deletion</b> (recommended) — plugin keeps running, jar is removed on the next restart.</li>
            <li><b>Force Delete Now</b> — jar is removed immediately. Paper still has it open and will likely crash on the next plugin event.</li>
          </ul>
          <div class="btn-row" style="gap:8px;flex-wrap:wrap">
            <button class="mc-btn mc-btn-warn" id="del-stage">Stage Deletion</button>
            <button class="mc-btn mc-btn-danger" id="del-force">⚠ Force Delete Now</button>
            <button class="mc-btn" id="del-cancel" style="background:linear-gradient(180deg,#6b7280 0%,#374151 100%)">Cancel</button>
          </div>
        </div>`;
      document.body.appendChild(modal);
      const finish = (v) => { closeModal(modal); resolve(v); };
      modal.querySelector("#del-stage").addEventListener("click", () => finish("stage"));
      modal.querySelector("#del-force").addEventListener("click", () => finish("force"));
      modal.querySelector("#del-cancel").addEventListener("click", () => finish(null));
      modal.addEventListener("click", (e) => { if (e.target === modal) finish(null); });
      setTimeout(() => modal.querySelector("#del-stage").focus(), 0);
    });
    if (!choice) return;
    if (choice === "force") {
      // Second confirmation for the dangerous path.
      if (!(await confirmModal({
        title: `Force-delete ${file} now?`,
        message: "Last chance — the jar will disappear while the JVM still has it loaded. Plugin will likely break or crash the server.",
        confirmText: "Yes, Force Delete", danger: true,
      }))) return;
      immediate = true;
    } else {
      immediate = false;
    }
  }
  busy(btn, true, immediate ? "Deleting…" : "Staging delete…");
  try {
    const r = await api(`/api/plugins/${encodeURIComponent(file)}?immediate=${immediate}`, { method: "DELETE" });
    if (r.mode === "staged") {
      toast(`${file} staged for deletion on next restart.`, "ok");
    } else {
      toast(`Deleted ${file}.`, "ok");
      pluginCache = pluginCache.filter((p) => p.file !== file);
    }
    await Promise.all([refreshPlugins(), refreshServer()]);
  } catch (e) { toast("Delete failed: " + e.message, "err"); busy(btn, false); }
}

async function cancelPending(file, btn) {
  busy(btn, true, "…");
  try {
    await api(`/api/plugins/pending/${encodeURIComponent(file)}`, { method: "DELETE" });
    toast(`Cancelled ${file}.`, "ok");
    refreshServer();
  } catch (e) { toast("Cancel failed: " + e.message, "err"); busy(btn, false); }
}

async function uploadPlugin(file) {
  const fd = new FormData(); fd.append("file", file);
  toast(`Uploading ${file.name}…`);
  try {
    await api("/api/plugins/upload", { method: "POST", body: fd });
    toast(`Uploaded ${file.name}.`, "ok");
    await refreshPlugins();
  } catch (e) { toast("Upload failed: " + e.message, "err"); }
}

// ── Catalog ─────────────────────────────────────────────────────
async function refreshRegistry({ force = false } = {}) {
  const root = $("registry-list");
  // Only show the spinner when the list is empty (first load / explicit refresh).
  // Otherwise leave the existing cards in place so the user doesn't see a flash.
  const isEmpty = !root.querySelector(".reg-item");
  if (isEmpty) root.innerHTML = '<div class="empty">Loading catalog…</div>';
  try {
    // ?fast=1 → skip live Spiget calls, cache-only. Slow path runs only on
    // explicit refresh from the Refresh button.
    const path = force ? "/api/registry" : "/api/registry?fast=1";
    const r = await api(path);
    _lastRegistry = r;
    renderRegistry(r);
  } catch (e) { toast("Catalog load failed: " + e.message, "err"); }
}

// Snapshot of the last /api/registry response. Used so install/cancel can
// patch a single card without round-tripping the whole list.
let _lastRegistry = null;

function renderRegistry(r) {
  const root = $("registry-list");
  const items = r.items || [];
  const premium = r.premium || [];
  if (!items.length && !premium.length) {
    root.innerHTML = '<div class="empty">Catalog is empty.</div>';
    return;
  }
  root.innerHTML = "";
  for (const item of items) {
    root.appendChild(buildCatalogCard(item));
  }
  if (premium.length) {
    const sep = document.createElement("h3");
    sep.className = "sub-h";
    sep.style.cssText = "grid-column:1/-1;margin-top:18px;color:#fbbf24";
    sep.textContent = "💎 Premium Plugins (manual upload)";
    root.appendChild(sep);
  }
  for (const p of premium) {
    root.appendChild(buildPremiumCard(p));
  }
}

// Surgical update — replace a single catalog card after install/cancel so the
// user sees the new state instantly without re-fetching the whole catalog.
function patchCatalogItem(key, mutator) {
  if (!_lastRegistry) return;
  const items = _lastRegistry.items || [];
  const idx = items.findIndex((x) => x.key === key);
  if (idx < 0) return;
  mutator(items[idx]);
  const oldCard = $("registry-list").querySelector(
    `.reg-item [data-key="${CSS.escape(key)}"]`
  )?.closest(".reg-item");
  const newCard = buildCatalogCard(items[idx]);
  if (oldCard && oldCard.parentNode) {
    oldCard.parentNode.replaceChild(newCard, oldCard);
  }
}

function buildCatalogCard(item) {
  const card = document.createElement("div");
  card.className = "reg-item" + (item.installed ? " reg-installed" : "");
  const sources = item.sources.length
    ? item.sources.map((s) => `<span class="source ${s.source}">${s.source}</span>`).join(" ")
    : `<span class="source" style="background:#52525b">no sources</span>`;
  const installAction = item.installed
    ? `<span class="installed-tag">✓ installed</span>`
    : (item.sources.length
      ? `<button class="mc-btn" data-action="install" data-key="${item.key}">＋ Install</button>`
      : `<span class="installed-tag" style="background:#3f3f46;color:#a1a1aa" title="Add at least one source to enable auto-install">no source</span>`);
  const editBtn = `<button class="mc-btn catalog-edit-btn" data-key="${item.key}" data-display="${item.display}" data-sources='${JSON.stringify(item.sources).replace(/'/g, "&#39;")}'>✎ Edit</button>`;
  const removeBtn = `<button class="mc-btn catalog-remove-btn" data-key="${item.key}" data-display="${item.display}">✕</button>`;
  card.innerHTML = `
    <div class="reg-icon">🌿</div>
    <div class="reg-body">
      <h3>${item.display} <span style="opacity:0.5;font-size:11px;font-weight:normal">(${item.key})</span></h3>
      <div class="reg-sources">${sources}</div>
    </div>
    <div class="reg-action">
      ${installAction}
      ${editBtn}
      ${removeBtn}
    </div>`;
  return card;
}

function buildPremiumCard(p) {
  const card = document.createElement("div");
  card.className = "reg-item reg-premium";
  card.dataset.spigotId = p.spigot_id;
  const lv = p.latest?.version || null;
  const iv = p.installed_version || null;
  let versionLine = "";
  if (lv && iv) {
    if (p.update_available) {
      versionLine = `<span style="color:#f5b400">⬆ Update: <b>${iv}</b> → <b>${lv}</b></span>`;
    } else {
      versionLine = `<span style="color:#4ade80">✓ Up-to-date (v${iv})</span>`;
    }
  } else if (iv) {
    versionLine = `<span style="opacity:0.8">Installed: v${iv} · latest unknown</span>`;
  } else if (lv) {
    versionLine = `<span style="opacity:0.8">Not installed · latest: v${lv}</span>`;
  } else {
    versionLine = `<span style="opacity:0.6">Version unknown — Spiget lookup failed</span>`;
  }
  const releaseLine = p.latest?.release_date_utc
    ? ` · <span style="opacity:0.6">released ${new Date(p.latest.release_date_utc * 1000).toISOString().slice(0,10)}</span>`
    : "";
  const userTag = p.user_added ? ` <span class="source spiget" style="background:#7c3aed">user-added</span>` : "";
  const removeBtn = p.user_added
    ? `<button class="mc-btn premium-remove-btn" style="background:linear-gradient(180deg,#dc2626 0%,#7f1d1d 100%);font-size:11px;padding:4px 8px" title="Remove from catalog">✕ Remove</button>`
    : "";
  card.innerHTML = `
    <div class="reg-icon" title="premium">💎</div>
    <div class="reg-body">
      <h3>${p.display} <span class="source spiget">premium</span>${userTag}</h3>
      <div class="reg-sources" style="opacity:0.8;font-size:12px;line-height:1.35">${p.note || ""}</div>
      <div class="premium-status" style="margin-top:6px;font-size:12px">${versionLine}${releaseLine}</div>
      <div class="premium-fallbacks" style="display:none;margin-top:8px;display:flex;flex-direction:column;gap:6px"></div>
    </div>
    <div class="reg-action">
      <button class="mc-btn premium-try-btn" style="background:linear-gradient(180deg,#4ade80 0%,#16a34a 100%)">📁 Manual Upload</button>
      <a href="${p.url}" target="_blank" rel="noopener" style="font-size:11px;opacity:0.7">↗ View on Spigot</a>
      ${removeBtn}
    </div>`;
  card.querySelector(".premium-try-btn").addEventListener("click",
    () => openManualUploadModal(p, card));
  if (p.user_added) {
    card.querySelector(".premium-remove-btn").addEventListener("click", async () => {
      if (!(await confirmModal({
        title: `Remove ${p.display} from catalog?`,
        message: "Removes this premium plugin from your catalog. Already-installed jars are NOT touched.",
        confirmText: "Remove", danger: true,
      }))) return;
      try {
        await api(`/api/premium/${encodeURIComponent(p.spigot_id)}`, { method: "DELETE" });
        toast(`${p.display} removed from catalog.`, "ok");
        await refreshRegistry();
      } catch (e) { toast(`Remove failed: ${e.message}`, "err"); }
    });
  }
  return card;
}

async function installFromCatalog(key, btn, intent) {
  busy(btn, true, "Staging…");
  const noun = (btn?.closest(".reg-item")?.querySelector("h3")?.textContent || key).trim();
  const modal = openSchedulerPickerAfterStaging({ noun, file: null, pending: true, catalogKey: key });
  const timeoutId = setTimeout(() => {
    if (document.body.contains(modal.modal)) {
      toast("Install request timed out — closing modal. Check the Plugins tab.", "warn");
      modal.close();
      busy(btn, false);
    }
  }, 60000);
  try {
    const r = await api(`/api/plugins/install/${encodeURIComponent(key)}`, { method: "POST" });
    clearTimeout(timeoutId);
    const mode = r.staged ? "Staged" : "Installed";
    toast(`${mode} ${r.file} (${r.source} v${r.version}).`, "ok");
    // Surgically mark this catalog item as installed — no full re-fetch.
    patchCatalogItem(key, (it) => { it.installed = true; });
    if (intent && intent.trigger !== "none") {
      intent.note = `${key} install`;
      await submitRestartIntent(intent);
      modal.close();
    } else {
      modal.resolve({ noun: r.file.replace(/\.jar$/, ""), file: r.file, staged: !!r.staged });
    }
  } catch (e) {
    clearTimeout(timeoutId);
    toast("Install failed: " + e.message, "err");
    modal.close();
  } finally {
    busy(btn, false);
  }
}

async function installAllMissing(btn) {
  if (!(await confirmModal({
    title: "Install every missing plugin?",
    message: "Installs every plugin from the catalog that isn't already on the server. Fresh installs go to plugins/, ready on next restart.",
    confirmText: "Install All", danger: false,
  }))) return;
  busy(btn, true, "Installing all…");
  try {
    const r = await api("/api/plugins/install-all-missing", { method: "POST" });
    toast(`Installed ${r.installed.length}, failed ${r.failed.length}.`, r.installed.length ? "ok" : "err");
    if (r.failed.length) console.warn("install failures:", r.failed);
    await Promise.all([refreshRegistry(), refreshPlugins()]);
  } catch (e) { toast("Install-all failed: " + e.message, "err"); }
  finally { busy(btn, false); }
}


// =========================================================================
// PREMIUM PLUGIN FLOW
// =========================================================================
// Premium plugins (Spigot paid resources behind Cloudflare) can only be
// installed by the user manually downloading the .jar in their own logged-in
// browser, then dragging it into the Manual Upload modal. Every automated
// path we tried (cookie-paste, Playwright + noVNC, browser extension) got
// blocked by Cloudflare Turnstile on the /download endpoint. Drag-drop has
// 100% reliability and is one extra physical action versus the alternatives.


// --- Fallback A: manual upload ----------------------------------------------
function openManualUploadModal(plugin, card) {
  const purchaseUrl = `https://www.spigotmc.org/resources/${plugin.spigot_id}/`;
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:560px">
      <h2>📁 Upload Plugin — ${plugin.display}</h2>
      <ol style="font-size:13px;line-height:1.6;margin:8px 0 14px 18px;padding:0">
        <li>Open <a href="${purchaseUrl}" target="_blank" rel="noopener" style="color:#fbbf24">the Spigot page</a> in your browser.</li>
        <li>Click <b>Download</b> there — your browser handles the Cloudflare check.</li>
        <li>Drag the <code>.jar</code> file into the dropzone below (or click to browse).</li>
      </ol>

      <div id="dropzone" style="
        border: 3px dashed rgba(74,222,128,0.6);
        border-radius: 8px;
        padding: 30px;
        text-align: center;
        background: rgba(74,222,128,0.07);
        cursor: pointer;
        transition: background 0.15s;
      ">
        <div style="font-size:32px;margin-bottom:6px">📦</div>
        <div style="font-size:13px">Drop the <code>.jar</code> here, or click to choose</div>
        <div id="dz-file" style="font-size:12px;opacity:0.7;margin-top:6px"></div>
      </div>
      <input type="file" id="dz-input" accept=".jar" style="display:none">

      <div id="dz-status" style="font-size:12px;margin-top:10px;min-height:18px;opacity:0.85"></div>

      <!-- Restart options appear here AFTER a successful upload. -->
      <div id="post-upload-options" style="display:none;margin-top:14px"></div>

      <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end" id="dz-actions">
        <button class="mc-btn" style="background:linear-gradient(180deg,#6b7280 0%,#374151 100%)" id="dz-cancel">Close</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const dz = modal.querySelector("#dropzone");
  const dzInput = modal.querySelector("#dz-input");
  const dzStatus = modal.querySelector("#dz-status");
  const dzFile = modal.querySelector("#dz-file");

  modal.querySelector("#dz-cancel").addEventListener("click", () => modal.remove());
  dz.addEventListener("click", () => dzInput.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.style.background = "rgba(74,222,128,0.2)"; });
  dz.addEventListener("dragleave", () => { dz.style.background = "rgba(74,222,128,0.07)"; });
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.style.background = "rgba(74,222,128,0.07)";
    if (e.dataTransfer.files.length) doUpload(e.dataTransfer.files[0]);
  });
  dzInput.addEventListener("change", () => {
    if (dzInput.files.length) doUpload(dzInput.files[0]);
  });

  async function doUpload(file) {
    if (!file.name.toLowerCase().endsWith(".jar")) {
      dzStatus.innerHTML = `<span style="color:#f87171">⚠ Must be a .jar file (got ${file.name}).</span>`;
      return;
    }
    dzFile.textContent = `${file.name} (${(file.size/1024).toFixed(1)} KB)`;
    dzStatus.textContent = "Uploading…";

    const fd = new FormData();
    fd.append("file", file);
    const params = new URLSearchParams();
    params.set("spigot_id", String(plugin.spigot_id));
    // Backend auto-detects: existing file → stages; new → installs.
    // No checkbox; we trust the filesystem check.

    try {
      const r = await fetch(`/api/plugins/upload?${params.toString()}`, {
        method: "POST", body: fd,
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || r.statusText);
      const mode = j.staged ? (j.is_update ? "Update staged" : "Install staged") : "Installed";
      dzStatus.innerHTML = `✅ <b>${mode}</b> — <code>${j.file}</code> (${(j.size/1024).toFixed(1)} KB)`;
      const statusEl = card?.querySelector(".premium-status");
      if (statusEl) statusEl.textContent = `✅ ${mode}: ${j.file}`;
      toast(`${plugin.display} ${mode.toLowerCase()}.`, "ok");
      await Promise.all([refreshPlugins(), refreshServer(), refreshSchedule()]);
      // Show the picker so the user can decide WHEN to restart — the upload
      // itself is already staged-by-default, so this is purely scheduling.
      showPostUploadPicker(j.staged, j.is_update, j.file);
    } catch (e) {
      dzStatus.innerHTML = `<span style="color:#f87171">❌ Upload failed: ${e.message}</span>`;
    }
  }

  function showPostUploadPicker(wasStaged, isUpdate, uploadedFile) {
    const mount = modal.querySelector("#post-upload-options");
    const dzActions = modal.querySelector("#dz-actions");
    const picker = buildRestartPicker({
      showScope: !!(lastServer && lastServer.pending_restart),
      defaultTrigger: "none",
      allowStageOnly: true,
    });
    const noun = wasStaged ? "Staged" : "Installed";
    const where = wasStaged ? "plugins/update/" : "plugins/";
    const detail = isUpdate
      ? "Existing jar will be replaced on next restart."
      : "New plugin will load on next restart.";
    mount.innerHTML = `
      <hr style="border:none;border-top:1px solid rgba(255,255,255,0.1);margin:0 0 10px">
      <div style="font-size:13px;margin-bottom:8px">
        <b>${noun}</b> in <code>${where}</code>. ${detail}<br>
        <span style="opacity:0.75">Pick a restart schedule below, or just click Done to leave it queued for later.</span>
      </div>`;
    mount.appendChild(picker.node);
    mount.style.display = "block";
    dzActions.innerHTML = `
      ${wasStaged
        ? `<button class="mc-btn" id="dz-cancel-upload" style="background:linear-gradient(180deg,#dc2626 0%,#7f1d1d 100%)">Cancel (remove staged jar)</button>`
        : ""}
      <button class="mc-btn mc-btn-warn" id="dz-confirm">Confirm</button>`;
    if (wasStaged) {
      modal.querySelector("#dz-cancel-upload").addEventListener("click", async () => {
        try {
          await api(`/api/plugins/${encodeURIComponent(uploadedFile)}/staged-install`, { method: "DELETE" });
          toast(`${plugin.display} upload cancelled — staged jar removed.`, "ok");
        } catch (e) {
          toast(`Could not remove staged jar: ${e.message}`, "err");
        }
        await Promise.all([refreshPlugins(), refreshServer(), refreshSchedule()]);
        modal.remove();
      });
    }
    modal.querySelector("#dz-confirm").addEventListener("click", async () => {
      const intent = picker.getIntent();
      if (!intent) return;
      intent.note = `${plugin.display} upload`;
      if (intent.trigger === "none") {
        toast(`${plugin.display} stays in queue. Restart from Updates tab when ready.`, "ok");
        modal.remove();
        return;
      }
      const r = await submitRestartIntent(intent);
      if (r) modal.remove();
    });
  }
}



// --- Fallback B: Spigot Tools panel (Playwright via noVNC) ------------------


function humanAge(seconds) {
  if (seconds == null) return "?";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds/60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds/3600)}h`;
  return `${Math.floor(seconds/86400)}d`;
}



// ── Console (SSE) ───────────────────────────────────────────────
let consoleES = null;
let consoleFollow = true;
let consolePaused = false;

function consoleAppend(line) {
  const win = $("console-window");
  const div = document.createElement("div");
  div.className = "console-line";
  const upper = line.toUpperCase();
  if (upper.includes("WARN")) div.classList.add("warn");
  else if (upper.includes("ERROR") || upper.includes("EXCEPTION") || upper.includes("SEVERE")) div.classList.add("err");
  else if (upper.includes("INFO")) div.classList.add("info");
  div.textContent = line;
  win.appendChild(div);
  // cap lines
  while (win.children.length > 800) win.removeChild(win.firstChild);
  if (consoleFollow) win.scrollTop = win.scrollHeight;
}

function startConsoleStream() {
  if (consoleES) return;
  $("console-window").innerHTML = '<div class="console-line dim">Connecting to log stream…</div>';
  consoleES = new EventSource("/api/server/logs/stream");
  consoleES.onmessage = (e) => { if (!consolePaused) consoleAppend(e.data); };
  consoleES.onerror = () => {
    consoleAppend("⚠ log stream disconnected. Click Console tab to retry.");
    stopConsoleStream();
  };
}
function stopConsoleStream() {
  if (consoleES) { consoleES.close(); consoleES = null; }
}

// ── Search tab ──────────────────────────────────────────────────
let _searchPremiumOnly = false;
let _searchLastHits = [];

async function runSearch(btn) {
  const q = $("search-input").value.trim();
  if (!q) { toast("Type something to search."); return; }
  busy(btn, true, "Searching…");
  $("search-results").innerHTML = '<div class="empty">Querying Modrinth · Hangar · Spiget…</div>';
  try {
    const r = await api(`/api/plugins/search?q=${encodeURIComponent(q)}&limit=20`);
    _searchLastHits = r.hits || [];
    renderSearchResults();
  } catch (e) {
    toast("Search failed: " + e.message, "err");
    $("search-results").innerHTML = '<div class="empty">Search failed.</div>';
  } finally { busy(btn, false); }
}

function renderSearchResults() {
  const root = $("search-results");
  const all = _searchLastHits || [];
  const hits = _searchPremiumOnly ? all.filter(h => h.premium) : all;
  if (!hits.length) {
    root.innerHTML = `<div class="empty">${_searchPremiumOnly ? "No premium matches in this search." : "No matches found."}</div>`;
    return;
  }
  root.innerHTML = "";
  for (const h of hits) {
    const card = document.createElement("div");
    card.className = "search-hit" + (h.premium ? " search-hit-premium" : "");
    const iconHtml = h.icon
      ? `<img class="search-hit-icon" src="${h.icon}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'search-hit-icon',textContent:'🧩'}))">`
      : `<div class="search-hit-icon">${h.premium ? "💎" : "🧩"}</div>`;
    const dl = h.downloads ? `<span class="dl-count">⬇ ${h.downloads.toLocaleString()}</span>` : "";
    const premiumBadge = h.premium ? `<span class="badge-premium">💎 PREMIUM</span>` : "";
    const priceText = h.premium && h.price ? `<span class="dl-count">$${h.price}</span>` : "";
    // Premium hits: offer "+ Add to Catalog" (curates the plugin for periodic
    // version checks) AND "↗ Buy + Upload" (opens Spigot to actually purchase).
    // Both are needed — adding to catalog without uploading the jar doesn't
    // install anything; uploading without adding means no future update alerts.
    const actionBtn = h.premium
      ? `<button class="mc-btn mc-btn-warn search-premium-add-btn"
                 data-spigot-id="${h.ref}"
                 data-title="${(h.title || "").replace(/"/g, "&quot;")}"
                 data-url="${(h.url || "").replace(/"/g, "&quot;")}"
                 data-icon="${(h.icon || "").replace(/"/g, "&quot;")}"
                 title="Add this premium plugin to your catalog so the hub tracks new releases">＋ Add to Catalog</button>
         ${h.url ? `<a href="${h.url}" target="_blank" rel="noopener">↗ Buy on Spigot</a>` : ""}`
      : `<button class="mc-btn search-install-btn"
                data-source="${h.source}"
                data-ref="${h.ref}"
                data-title="${(h.title || "").replace(/"/g, "&quot;")}">＋ Install</button>
         ${h.url ? `<a href="${h.url}" target="_blank" rel="noopener">↗ View</a>` : ""}`;
    card.innerHTML = `
      ${iconHtml}
      <div class="search-hit-body">
        <h3>${h.title || "(no title)"} ${premiumBadge}</h3>
        <div class="search-hit-summary">${h.summary || ""}</div>
        <div class="search-hit-meta">
          <span class="source ${h.source}">${h.source}</span>
          ${dl}${priceText}
        </div>
      </div>
      <div class="search-hit-action">${actionBtn}</div>`;
    root.appendChild(card);
  }
}

async function installFromSearch(source, ref, title, btn) {
  busy(btn, true, "Staging…");
  const modal = openSchedulerPickerAfterStaging({ noun: title, file: null, pending: true });
  const timeoutId = setTimeout(() => {
    if (document.body.contains(modal.modal)) {
      toast("Install request timed out — closing modal. Check the Plugins tab.", "warn");
      modal.close();
      busy(btn, false);
    }
  }, 60000);
  try {
    const r = await api("/api/plugins/install-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, ref, display: title }),
    });
    clearTimeout(timeoutId);
    const mode = r.staged ? "Staged" : "Installed";
    toast(`${mode} ${r.file} (${r.source} v${r.version}).`, "ok");
    modal.resolve({ noun: title, file: r.file, staged: !!r.staged });
    // Just refresh the plugins list — search results don't need re-fetching.
    refreshPlugins();
  } catch (e) {
    clearTimeout(timeoutId);
    toast("Install failed: " + e.message, "err");
    modal.close();
  } finally { busy(btn, false); }
}

// Shared post-staging picker — used by installFromCatalog, installFromSearch,
// and the manual upload modal's "what next?" flow. Two buttons:
//   • Cancel (remove the jar)      → DELETE the staged or installed jar + close
//   • Confirm                       → if picker.trigger === "none", just close
//                                     (jar stays in place); else submit restart
//
// Caller flow:
//   const modal = openSchedulerPickerAfterStaging({ noun, file: null, pending: true });
//   // … await backend …
//   modal.resolve({ noun, file, staged });    // staged=true → jar in plugins/update/
//                                              // staged=false → jar in plugins/ (fresh install)
//
// Returns { close(), resolve({noun,file,staged}), modal }.
function openSchedulerPickerAfterStaging({ noun, file, staged = true, pending = false, catalogKey = null }) {
  const showScope = !!(lastServer && lastServer.pending_restart);
  const picker = buildRestartPicker({
    showScope,
    defaultTrigger: "none",
    allowStageOnly: true,
  });
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:560px">
      <h2 id="rp-title">${pending ? "⏳" : "✅"} ${noun} ${pending ? "installing…" : "installed"}</h2>
      <p class="hint" id="rp-sub" style="margin:0 0 10px;text-align:left">
        <em>Working on it…</em>
      </p>
      <div id="rp-mount" style="${pending ? "opacity:0.45;pointer-events:none" : ""}"></div>
      <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end;flex-wrap:wrap">
        <button class="mc-btn" id="rp-cancel" style="background:linear-gradient(180deg,#dc2626 0%,#7f1d1d 100%)" ${pending ? "disabled" : ""}>Cancel (remove jar)</button>
        <button class="mc-btn mc-btn-warn" id="rp-submit" ${pending ? "disabled" : ""}>Confirm</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector("#rp-mount").appendChild(picker.node);

  modal.dataset.noun = noun;
  if (file) modal.dataset.file = file;
  modal.dataset.staged = String(staged);
  if (catalogKey) modal.dataset.catalogKey = catalogKey;

  const close = () => modal.remove();

  modal.querySelector("#rp-cancel").addEventListener("click", async () => {
    const fileNow = modal.dataset.file;
    const stagedNow = modal.dataset.staged === "true";
    const nounNow = modal.dataset.noun || noun;
    const keyNow = modal.dataset.catalogKey || null;
    if (!fileNow) { close(); return; }
    const cancelBtn = modal.querySelector("#rp-cancel");
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Removing…";
    const url = stagedNow
      ? `/api/plugins/${encodeURIComponent(fileNow)}/staged-install`
      : `/api/plugins/${encodeURIComponent(fileNow)}?immediate=true`;
    try {
      // Block on the DELETE before any UI cleanup. The previous behavior fired
      // refreshes BEFORE the response, which is why "cancelled" entries still
      // showed installed — the cached registry response was already in flight.
      await api(url, { method: "DELETE" });
      // Patch the catalog card back to uninstalled. No round-trip.
      if (keyNow) patchCatalogItem(keyNow, (it) => { it.installed = false; });
      toast(`${nounNow} cancelled — jar removed.`, "ok");
    } catch (e) {
      toast(`Could not remove jar: ${e.message}`, "err");
    }
    close();
  });

  modal.querySelector("#rp-submit").addEventListener("click", async () => {
    const intent = picker.getIntent();
    if (!intent) return;
    const nounNow = modal.dataset.noun || noun;
    intent.note = `${nounNow} install`;
    if (intent.trigger === "none") {
      toast(`${nounNow} ready. Will load on next restart.`, "ok");
      close();
      return;
    }
    const r = await submitRestartIntent(intent);
    if (r) close();
  });

  const resolve = ({ noun: realNoun, file: realFile, staged: realStaged }) => {
    modal.dataset.noun = realNoun;
    modal.dataset.file = realFile;
    modal.dataset.staged = String(!!realStaged);
    const where = realStaged
      ? `Staged in <code>plugins/update/</code>. Paper will swap it in on the next restart.`
      : `Installed in <code>plugins/</code>. New plugin will load on next restart.`;
    modal.querySelector("#rp-title").innerHTML = `✅ ${realNoun} ${realStaged ? "staged" : "installed"}`;
    modal.querySelector("#rp-sub").innerHTML =
      `<code>${realFile}</code> — ${where}<br>Pick a restart schedule below, or just click <b>Confirm</b> to leave it queued for later.`;
    const mount = modal.querySelector("#rp-mount");
    mount.style.opacity = "";
    mount.style.pointerEvents = "";
    for (const b of modal.querySelectorAll("button")) b.disabled = false;
  };

  if (!pending && file) {
    resolve({ noun, file, staged });
  }

  return { close, resolve, modal };
}


// ── Server config tab ───────────────────────────────────────────
let configState = null;
let typesByKey = {};

// Render the read-only Java runtime card (Server tab).
// rec = { tag, java, reason, label?, type_key, mc_version } from backend.
function renderJavaRecommendation(rec, currentImage) {
  const tagEl = $("cfg-java-tag");
  const reasonEl = $("cfg-java-reason");
  if (!tagEl || !rec) return;
  tagEl.textContent = `☕ Java ${rec.java} — :${rec.tag}`;
  reasonEl.textContent = rec.reason || "";
  // If the running image disagrees with the recommendation, warn the user.
  const hintEl = $("cfg-java-hint");
  if (currentImage && !currentImage.endsWith(":" + rec.tag)) {
    hintEl.innerHTML = `<strong>⚠ Running image is <code>${currentImage}</code> — will switch to <code>itzg/minecraft-server:${rec.tag}</code> on next recreate.</strong><br>Change the MC Version (or Server Type) to switch Java runtimes.`;
    hintEl.classList.add("java-mismatch");
  } else {
    hintEl.textContent = "Change the MC Version (or Server Type) to switch Java runtimes.";
    hintEl.classList.remove("java-mismatch");
  }
}

// Live re-fetch the Java recommendation when type/version change in the
// Server tab (before the user has saved/recreated). Backend endpoint
// /api/java-runtime is pure (no side effects).
async function refreshJavaRecommendation() {
  const t = $("cfg-type")?.value || "PAPER";
  const v = ($("cfg-version")?.value || "").trim();
  if (!v) return;
  try {
    const rec = await api(`/api/java-runtime?type_key=${encodeURIComponent(t)}&version=${encodeURIComponent(v)}`);
    renderJavaRecommendation(rec, configState?.image || "");
  } catch { /* fall back to whatever was last shown */ }
}

async function loadConfig() {
  try {
    const r = await api("/api/server/config");
    configState = r;
    typesByKey = Object.fromEntries((r.supported_types || []).map((t) => [t.key, t]));
    $("compose-path").textContent = r.compose_path;

    // Type dropdown — grouped by family
    const typeSel = $("cfg-type");
    const families = {};
    for (const t of r.supported_types || []) {
      (families[t.family] = families[t.family] || []).push(t);
    }
    const familyOrder = ["paper", "vanilla", "mod-loader", "hybrid", "legacy", "limbo", "other", "custom"];
    const familyLabels = {
      "paper": "Paper-compatible (plugins)",
      "vanilla": "Vanilla",
      "mod-loader": "Mod Loaders (no plugins)",
      "hybrid": "Hybrid (mods + plugins)",
      "legacy": "Legacy",
      "limbo": "Limbo / Lobby",
      "other": "Other",
      "custom": "Custom",
    };
    let html = "";
    for (const fam of familyOrder) {
      const list = families[fam] || [];
      if (!list.length) continue;
      html += `<optgroup label="${familyLabels[fam] || fam}">`;
      for (const t of list) {
        html += `<option value="${t.key}">${t.display}</option>`;
      }
      html += `</optgroup>`;
    }
    typeSel.innerHTML = html;
    typeSel.value = r.current_type.key;

    // Docker image / Java runtime — read-only display, auto-selected
    // by backend from current type + MC version. We just render whatever
    // the backend's java_recommended block says.
    renderJavaRecommendation(r.java_recommended, r.image);

    // Common inputs
    $("cfg-memory").value = r.env.MEMORY || "";
    $("cfg-motd").value = r.env.MOTD || "";
    $("cfg-icon").value = r.env.ICON || "";
    $("cfg-difficulty").value = (r.env.DIFFICULTY || "").toLowerCase();
    $("cfg-maxplayers").value = r.env.MAX_PLAYERS || "";

    // Render type-specific extra fields + description
    renderTypeFields(r.current_type.key, r.env);

    // Raw env
    $("config-raw").textContent = Object.entries(r.env)
      .map(([k, v]) => `${k}=${v}`).join("\n");

    // Load version dropdown for this type
    await loadVersionDropdown(r.current_type.key, r.env.VERSION);
  } catch (e) {
    toast("Failed to load config: " + e.message, "err");
  }
}

function renderTypeFields(typeKey, env) {
  const t = typesByKey[typeKey];
  const root = $("type-extra-fields");
  if (!t) { root.innerHTML = ""; return; }
  $("type-desc").textContent = t.description || "";
  if (t.notes) {
    $("type-notes").hidden = false;
    $("type-notes").textContent = "⚠ " + t.notes;
  } else {
    $("type-notes").hidden = true;
    $("type-notes").textContent = "";
  }
  const fields = t.extra_fields || [];
  if (!fields.length) { root.innerHTML = ""; return; }
  root.innerHTML = fields.map((f) => {
    const val = (env[f.key] || "").replace(/"/g, "&quot;");
    // "build" / "loader_version" both render as a dropdown wrapped in a
    // stable slot so we can swap the inner control without orphaning event
    // handlers or the element ID.
    if (f.kind === "build" || f.kind === "loader_version") {
      return `
        <label class="config-field">
          <span>${f.label}</span>
          <span class="build-slot" data-build-key="${f.key}">
            <select data-extra-key="${f.key}" data-build-select="1">
              <option value="${val}">${val || "(latest)"}</option>
            </select>
          </span>
          <small class="field-desc">${f.hint || ""}</small>
        </label>`;
    }
    // Arclight gets a composite picker: channel toggle + build dropdown.
    // The actual jar is auto-downloaded server-side via /api/server/arclight/install
    // when the user saves — but the read-only display + selector live here.
    if (f.kind === "arclight_build") {
      return `
        <div class="config-field config-field-wide arclight-picker" data-arclight-picker="1">
          <span>${f.label}</span>
          <div class="arclight-channel-row">
            <label class="radio-pill">
              <input type="radio" name="arclight-channel" value="snapshot" data-arc-channel checked>
              <span>Snapshot <small>(recommended for 1.21.x)</small></span>
            </label>
            <label class="radio-pill">
              <input type="radio" name="arclight-channel" value="stable" data-arc-channel>
              <span>Stable</span>
            </label>
          </div>
          <select data-arc-build data-extra-key="${f.key}">
            <option value="latest">(latest in channel)</option>
          </select>
          <small class="field-desc arc-build-meta">
            Builds load from <code>arclight.izzel.io</code>. The hub downloads the jar to the server's data dir and switches <code>TYPE=CUSTOM</code> automatically.
          </small>
        </div>`;
    }
    if (f.kind === "arclight_type") {
      const opts = ["FORGE", "NEOFORGE", "FABRIC"];
      return `
        <label class="config-field">
          <span>${f.label}</span>
          <select data-extra-key="${f.key}">
            ${opts.map((o) => `<option value="${o}"${val === o ? " selected" : ""}>${o}</option>`).join("")}
          </select>
          <small class="field-desc">${f.hint || ""}</small>
        </label>`;
    }
    return `
      <label class="config-field">
        <span>${f.label}</span>
        <input type="text" data-extra-key="${f.key}" value="${val}" placeholder="${f.hint || ""}">
        <small class="field-desc">${f.hint || ""}</small>
      </label>`;
  }).join("");
}

async function loadBuildsForCurrentTypeVersion(typeKey, version, preferredBuild) {
  // Operate on the stable slot wrappers, not the inner control (which may
  // have been replaced with an <input> on a previous run).
  const slots = document.querySelectorAll(".build-slot");
  if (!slots.length) return;
  slots.forEach((s) => {
    const k = s.dataset.buildKey;
    s.innerHTML = `<select data-extra-key="${k}" data-build-select="1"><option>loading…</option></select>`;
  });
  if (!version) {
    slots.forEach((s) => {
      const k = s.dataset.buildKey;
      s.innerHTML = `<input type="text" data-extra-key="${k}" placeholder="set MC version first">`;
    });
    return;
  }
  try {
    const r = await api(`/api/server/paper?type_key=${encodeURIComponent(typeKey)}&version=${encodeURIComponent(version)}`);
    const builds = r.builds || [];
    if (!builds.length) {
      // Type doesn't expose builds for this version — fall back to free-text
      slots.forEach((s) => {
        const k = s.dataset.buildKey;
        const cur = preferredBuild || "";
        s.innerHTML = `<input type="text" data-extra-key="${k}" value="${cur}" placeholder="no auto-list; type manually">`;
      });
      return;
    }
    const opts = ['<option value="">(latest stable)</option>']
      .concat(builds.slice(0, 50).map((b) => {
        const channel = (b.channel || "STABLE").toLowerCase();
        return `<option value="${b.id}">${b.id}${channel === "stable" ? "" : " (" + channel + ")"}</option>`;
      }));
    slots.forEach((s) => {
      const k = s.dataset.buildKey;
      s.innerHTML = `<select data-extra-key="${k}" data-build-select="1">${opts.join("")}</select>`;
      const sel = s.querySelector("select");
      if (preferredBuild && Array.from(sel.options).some((o) => o.value === String(preferredBuild))) {
        sel.value = String(preferredBuild);
      }
    });
  } catch (e) {
    slots.forEach((s) => {
      const k = s.dataset.buildKey;
      s.innerHTML = `<input type="text" data-extra-key="${k}" placeholder="lookup failed; type manually">`;
    });
  }
}

// ── Arclight build picker — populates the composite widget rendered by
// renderTypeFields when the active type is ARCLIGHT. Reads channel + subtype
// directly from the DOM so a re-render keeps state correct.
async function loadArclightBuilds(version, preferredTag) {
  const picker = document.querySelector("[data-arclight-picker]");
  if (!picker) return;
  const sel = picker.querySelector("[data-arc-build]");
  const meta = picker.querySelector(".arc-build-meta");
  if (!sel) return;
  const channel = picker.querySelector('[data-arc-channel]:checked')?.value || "snapshot";
  const subtype = (document.querySelector('[data-extra-key="ARCLIGHT_TYPE"]')?.value || "FORGE").toLowerCase();
  if (!version) {
    sel.innerHTML = `<option value="latest">(set MC version first)</option>`;
    return;
  }
  sel.innerHTML = `<option>loading…</option>`;
  if (meta) meta.textContent = `Loading ${channel} builds for ${version}/${subtype}…`;
  try {
    const r = await api(`/api/server/arclight/builds?version=${encodeURIComponent(version)}&subtype=${encodeURIComponent(subtype)}&channel=${encodeURIComponent(channel)}`);
    const builds = r.builds || [];
    if (!builds.length) {
      sel.innerHTML = `<option value="latest">(no builds for this combo)</option>`;
      if (meta) meta.textContent = `No ${channel} builds available for MC ${version}/${subtype}.`;
      return;
    }
    const opts = [`<option value="latest">(latest ${channel})</option>`].concat(
      builds.map((b) => {
        const short = b.tag.replace(/^(snapshot|stable)\//, "");
        const date = b.published_at ? b.published_at.slice(0, 10) : "";
        return `<option value="${b.tag}">${short}${date ? "  ·  " + date : ""}</option>`;
      })
    );
    sel.innerHTML = opts.join("");
    if (preferredTag) {
      // Match exact tag, or substring (so "0769551" finds "snapshot/1.0.2-SNAPSHOT-0769551")
      let match = Array.from(sel.options).find((o) => o.value === preferredTag);
      if (!match) match = Array.from(sel.options).find((o) => o.value.includes(preferredTag));
      if (match) sel.value = match.value;
    }
    if (meta) {
      meta.innerHTML = `${builds.length} ${channel} build(s) from <code>arclight.izzel.io</code>. The hub downloads the picked jar to the server's data dir on save.`;
    }
  } catch (e) {
    sel.innerHTML = `<option value="latest">(lookup failed)</option>`;
    if (meta) meta.textContent = `Lookup failed: ${e.message}`;
  }
}

// Wire up channel + subtype change listeners on the Arclight picker.
// Called once per renderTypeFields run.
function wireArclightPicker(version) {
  const picker = document.querySelector("[data-arclight-picker]");
  if (!picker) return;
  picker.querySelectorAll("[data-arc-channel]").forEach((r) =>
    r.addEventListener("change", () => loadArclightBuilds(version, ""))
  );
  const subtypeSel = document.querySelector('[data-extra-key="ARCLIGHT_TYPE"]');
  if (subtypeSel) {
    subtypeSel.addEventListener("change", () => loadArclightBuilds(
      $("cfg-version")?.value || version, ""
    ));
  }
}

async function loadVersionDropdown(typeKey, preferredVersion) {
  // Always reset the slot to a fresh <select> so the bug where
  // outerHTML-replacing the element on one type prevents subsequent
  // type switches from populating it can't recur.
  const slot = $("cfg-version-slot");
  slot.innerHTML = `<select id="cfg-version"><option>${preferredVersion || ""}</option></select>`;
  const verSel = $("cfg-version");
  // Re-bind change handler since the element is fresh each call
  verSel.addEventListener("change", (e) => {
    const t = $("cfg-type").value;
    loadBuildsForCurrentTypeVersion(t, e.target.value, "");
    if (t === "ARCLIGHT") loadArclightBuilds(e.target.value, "");
  });
  const note = $("version-source");
  note.textContent = "loading…";
  try {
    const v = await api(`/api/server/paper/versions?type_key=${encodeURIComponent(typeKey)}`);
    const versions = v.versions || [];
    if (!versions.length) {
      // Replace the slot with a free-text input instead of replacing the
      // <select> directly (so the parent slot stays addressable).
      slot.innerHTML = `<input type="text" id="cfg-version" value="${preferredVersion || ""}" placeholder="e.g. 1.20.1">`;
      $("cfg-version").addEventListener("change", (e) =>
        loadBuildsForCurrentTypeVersion(typeKey, e.target.value, ""));
      note.textContent = "no auto-list for this type — type a version manually";
      return;
    }
    verSel.innerHTML = versions
      .map((x) => `<option value="${x}">${x}</option>`).join("");
    verSel.value = preferredVersion && versions.includes(preferredVersion)
      ? preferredVersion : versions[0];
    note.textContent = `${versions.length} stable versions from ${typeKey.toLowerCase()} upstream`;
    const buildKey = (typesByKey[typeKey]?.extra_fields || [])
      .find((f) => f.kind === "build" || f.kind === "loader_version")?.key;
    const preferredBuild = buildKey ? (configState?.env?.[buildKey] || "") : "";
    await loadBuildsForCurrentTypeVersion(typeKey, verSel.value, preferredBuild);
    // If this is the Arclight picker, populate the channel + build composite too.
    if (typeKey === "ARCLIGHT") {
      // Seed channel radio + subtype from compose env
      const env = configState?.env || {};
      const preferredChannel = (env.HUB_LOADER_CHANNEL || "snapshot").toLowerCase();
      const picker = document.querySelector("[data-arclight-picker]");
      if (picker) {
        picker.querySelectorAll("[data-arc-channel]").forEach((r) => {
          r.checked = (r.value === preferredChannel);
        });
      }
      const preferredTag = env.HUB_LOADER_TAG || "";
      await loadArclightBuilds(verSel.value, preferredTag);
      wireArclightPicker(verSel.value);
    }
  } catch (e) {
    note.textContent = "version lookup failed; type manually";
  }
}

function collectConfigChanges() {
  if (!configState) return {};
  const env = configState.env;
  const changes = {};
  // Universal fields
  const fields = [
    ["TYPE", "cfg-type"], ["VERSION", "cfg-version"],
    ["MEMORY", "cfg-memory"], ["MOTD", "cfg-motd"], ["ICON", "cfg-icon"],
    ["DIFFICULTY", "cfg-difficulty"], ["MAX_PLAYERS", "cfg-maxplayers"],
  ];
  for (const [key, id] of fields) {
    const el = $(id);
    if (!el) continue;
    const val = (el.value || "").trim();
    const cur = env[key] || "";
    if (val !== cur) changes[key] = val;
  }
  // Type-specific extras
  document.querySelectorAll("[data-extra-key]").forEach((el) => {
    const key = el.dataset.extraKey;
    const val = (el.value || "").trim();
    const cur = env[key] || "";
    if (val !== cur) changes[key] = val;
  });
  return changes;
}

async function saveConfig(btn, recreate = false) {
  const changes = collectConfigChanges();
  const typeNow = $("cfg-type")?.value;

  // Arclight has its own install endpoint that handles jar download +
  // TYPE=CUSTOM rewrite atomically. Route there if the user is on ARCLIGHT.
  if (typeNow === "ARCLIGHT") {
    return saveArclightConfig(btn, recreate, changes);
  }

  // Java tag is no longer a user-controlled dropdown — the backend bumps
  // the image automatically on recreate based on (TYPE, VERSION). So we
  // only ever send the changed env keys; the server picks the image.
  if (!Object.keys(changes).length) {
    toast("No changes to save.");
    return;
  }
  let force = false;
  if (changes.TYPE && changes.TYPE.toUpperCase() !== (configState.env.TYPE || "PAPER").toUpperCase()) {
    if (!(await confirmModal({
      title: "Change server TYPE?",
      message: `You're changing server TYPE from ${configState.env.TYPE} to ${changes.TYPE}.\n\n⚠ This will likely break the existing world and plugin format. Backup first if you care about the data.`,
      confirmText: "Change Type", danger: true,
    }))) return;
    force = true;
  }
  if (recreate && !(await confirmModal({
    title: "Save and recreate?",
    message: "Save changes and recreate the container. Server will go offline briefly.",
    confirmText: "Save + Recreate", danger: true,
  }))) return;

  busy(btn, true, recreate ? "Saving + recreating…" : "Saving…");
  try {
    const body = { changes, recreate, force };
    const r = await api("/api/server/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const parts = [];
    if (Object.keys(changes).length) parts.push(`${Object.keys(changes).length} env change(s)`);
    if (r.image_bumped && r.image_bump) {
      parts.push(`Java auto-bumped: ${r.image_bump.old_tag} → ${r.image_bump.new_tag}`);
    } else if (r.image) {
      parts.push(`image → ${r.image}`);
    }
    if (r.recreated) parts.push("container recreated");
    toast(`Saved · ${parts.join(" · ")}.`, "ok");
    if (r.image_bumped && r.image_bump) {
      toast(`☕ Java runtime updated: ${r.image_bump.reason}`, "ok");
    }
    $("config-hint").textContent = recreate ? "Container recreated. Refreshing status…" : "Saved. Click 'Recreate Container' to apply.";
    await loadConfig();
    if (recreate) {
      setTimeout(refreshServer, 3000);
      setTimeout(() => { refreshServer(); refreshPlugins(); }, 15000);
    }
  } catch (e) {
    toast("Save failed: " + e.message, "err");
    $("config-hint").textContent = "Save failed: " + e.message;
  } finally { busy(btn, false); }
}

// Save the Arclight-specific picker. Two-phase:
//   1. POST any non-Arclight env changes via /api/server/config (no recreate).
//   2. POST /api/server/arclight/install with channel/subtype/tag (+ recreate flag).
// Both phases are idempotent; if there are no env changes phase 1 is skipped.
async function saveArclightConfig(btn, recreate, changes) {
  const version = ($("cfg-version")?.value || "").trim();
  if (!version) {
    toast("Pick an MC version first.", "err");
    return;
  }
  const picker = document.querySelector("[data-arclight-picker]");
  const channel = picker?.querySelector('[data-arc-channel]:checked')?.value || "snapshot";
  const subtype = ($('[data-extra-key="ARCLIGHT_TYPE"]')?.value || "FORGE").toLowerCase();
  const tag = picker?.querySelector("[data-arc-build]")?.value || "latest";

  // Type change (e.g. from PAPER → ARCLIGHT) needs the same warning as the
  // generic Save path. We skip the second confirm so the user doesn't get a
  // double dialog.
  if (changes.TYPE && changes.TYPE.toUpperCase() !== (configState.env.TYPE || "PAPER").toUpperCase()) {
    if (!(await confirmModal({
      title: "Change server TYPE to Arclight?",
      message: `You're switching from ${configState.env.TYPE} to Arclight (${subtype}, ${channel}/${tag}).\n\n⚠ This will likely break the existing world and plugin format. Backup first.`,
      confirmText: "Switch to Arclight", danger: true,
    }))) return;
  } else if (recreate && !(await confirmModal({
    title: "Apply Arclight build?",
    message: `Download ${channel}/${tag} for MC ${version}/${subtype} and recreate the container. Server will go offline briefly.`,
    confirmText: "Download + Recreate", danger: true,
  }))) return;

  busy(btn, true, recreate ? "Downloading + recreating…" : "Saving Arclight config…");
  try {
    // Phase 1: apply non-Arclight env changes (memory / motd / icon / etc.)
    // Strip out the Arclight-specific keys — those flow through phase 2.
    const arclightKeys = new Set([
      "TYPE", "VERSION", "HUB_LOADER_TAG", "HUB_LOADER_JAR",
      "HUB_LOADER_SUBTYPE", "HUB_LOADER_CHANNEL", "HUB_LOADER_SOURCE",
      "HUB_LOADER_TYPE", "CUSTOM_SERVER",
    ]);
    const envChanges = {};
    for (const [k, v] of Object.entries(changes)) {
      if (!arclightKeys.has(k)) envChanges[k] = v;
    }
    if (Object.keys(envChanges).length) {
      await api("/api/server/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes: envChanges, recreate: false, force: true }),
      });
    }
    // Phase 2: install the Arclight build (writes HUB_LOADER_* + jar + recreate)
    const r = await api("/api/server/arclight/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version, subtype, channel, tag, recreate }),
    });
    const parts = [];
    if (Object.keys(envChanges).length) parts.push(`${Object.keys(envChanges).length} env change(s)`);
    parts.push(`jar: ${r.jar}`);
    parts.push(`tag: ${r.tag}`);
    if (r.recreated) parts.push("container recreated");
    toast(`Arclight applied · ${parts.join(" · ")}.`, "ok");
    $("config-hint").textContent = recreate
      ? "Container recreated. Refreshing status…"
      : "Saved. Click 'Recreate Container' to apply.";
    await loadConfig();
    if (recreate) {
      setTimeout(refreshServer, 3000);
      setTimeout(() => { refreshServer(); refreshPlugins(); }, 15000);
    }
  } catch (e) {
    toast("Arclight save failed: " + e.message, "err");
    $("config-hint").textContent = "Arclight save failed: " + e.message;
  } finally { busy(btn, false); }
}

async function recreateContainer(btn) {
  if (!(await confirmModal({
    title: "Recreate container?",
    message: "Recreate the container with the current compose settings. Server will go offline briefly while the new container spins up.",
    confirmText: "Recreate", danger: true,
  }))) return;
  busy(btn, true, "Recreating…");
  try {
    const r = await api("/api/server/recreate", { method: "POST" });
    if (r.image_bumped && r.image_bump) {
      toast(`☕ Java runtime updated: ${r.image_bump.old_tag} → ${r.image_bump.new_tag}. ${r.image_bump.reason}`, "ok");
    }
    toast("Container recreated.", "ok");
    await loadConfig();
    setTimeout(refreshServer, 3000);
    setTimeout(() => { refreshServer(); refreshPlugins(); }, 15000);
  } catch (e) {
    toast("Recreate failed: " + e.message, "err");
  } finally { busy(btn, false); }
}

// ── Multi-server switcher ───────────────────────────────────────
let serversCache = { servers: [], current: null };

async function refreshServers() {
  try {
    const r = await api("/api/servers");
    serversCache = r;
    const sel = $("server-select");
    if (!sel) return;
    sel.innerHTML = "";
    for (const s of r.servers || []) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.display || s.id;
      if (s.id === r.current) opt.selected = true;
      sel.appendChild(opt);
    }
    if (!r.servers || r.servers.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "(no servers — + Add)";
      sel.appendChild(opt);
    }
  } catch (e) {
    console.error("refreshServers failed", e);
  }
}

async function selectServer(sid) {
  try {
    await api("/api/servers/select", { method: "POST", body: { id: sid } });
    toast("Switched to " + sid, "ok");
    // Refresh everything for new server context
    await refreshServers();
    refreshServer();
    refreshPlugins();
    refreshRegistry();
    if (typeof loadConfig === "function") loadConfig();
    if (typeof loadHost === "function") loadHost();
  } catch (e) {
    toast("Switch failed: " + e.message, "err");
  }
}

function closeModal(el) { el.remove(); }

// Site-styled replacement for the native window.confirm(). Returns a Promise<boolean>.
// Usage:  if (!(await confirmModal({ title: "Stop server?", message: "..." }))) return;
// Options:
//   title       — heading (string, required)
//   message     — body text or HTML (string). Plain text is escaped; HTML allowed via opts.html=true.
//   confirmText — button label (default "Confirm")
//   cancelText  — cancel label (default "Cancel")
//   danger      — true → red Confirm button; false → yellow/warn (default true since most confirms are destructive)
//   html        — true to render message as HTML; default false (text escaped)
function confirmModal({ title, message = "", confirmText = "Confirm", cancelText = "Cancel",
                       danger = true, html = false } = {}) {
  return new Promise((resolve) => {
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
    const bodyHtml = html ? message : esc(message).replace(/\n/g, "<br>");
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.innerHTML = `
      <div class="modal-box" style="max-width:480px">
        <h2>${esc(title)}</h2>
        ${message ? `<p style="margin:0 0 14px;line-height:1.5">${bodyHtml}</p>` : ""}
        <div class="btn-row">
          <button class="mc-btn ${danger ? "mc-btn-danger" : "mc-btn-warn"}" id="cm-ok">${esc(confirmText)}</button>
          <button class="mc-btn" id="cm-cancel" style="background:linear-gradient(180deg,#6b7280 0%,#374151 100%)">${esc(cancelText)}</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    const finish = (ok) => { closeModal(modal); document.removeEventListener("keydown", onKey); resolve(ok); };
    const onKey = (e) => {
      if (e.key === "Escape") finish(false);
      else if (e.key === "Enter") finish(true);
    };
    document.addEventListener("keydown", onKey);
    modal.querySelector("#cm-ok").addEventListener("click", () => finish(true));
    modal.querySelector("#cm-cancel").addEventListener("click", () => finish(false));
    // Click outside the box to cancel
    modal.addEventListener("click", (e) => { if (e.target === modal) finish(false); });
    // Focus the confirm button so Enter works immediately
    setTimeout(() => modal.querySelector("#cm-ok").focus(), 0);
  });
}

// ─────────────────────────────────────────────────────────────
// Add Server modal — full config form mirroring the Server tab.
// Used for both "Track Existing" and "Create New" flows, with a
// per-tab toggle for whether the compose file already exists.
// ─────────────────────────────────────────────────────────────

// Tiny builder that injects the full Server-tab field set into a container.
// Returns { collect() -> { type, version, memory, motd, icon, difficulty,
//   max_players, image_tag, extra_env } } so the caller can serialize the
// form into a payload. typesData / imageTags / cfg are fetched once and
// shared across both tabs.
function buildServerConfigForm(root, { typesData, imageTags, defaults = {} } = {}) {
  const familyOrder = ["paper", "vanilla", "mod-loader", "hybrid", "legacy", "limbo", "other", "custom"];
  const familyLabels = {
    "paper": "Paper-compatible (plugins)", "vanilla": "Vanilla",
    "mod-loader": "Mod Loaders (no plugins)", "hybrid": "Hybrid (mods + plugins)",
    "legacy": "Legacy", "limbo": "Limbo / Lobby", "other": "Other", "custom": "Custom",
  };
  const byKey = Object.fromEntries(typesData.map(t => [t.key, t]));
  // Group types by family for the dropdown.
  const families = {};
  for (const t of typesData) (families[t.family] = families[t.family] || []).push(t);
  const typeOpts = familyOrder.flatMap(fam => {
    const list = families[fam] || [];
    if (!list.length) return [];
    return [`<optgroup label="${familyLabels[fam] || fam}">`]
      .concat(list.map(t => `<option value="${t.key}"${t.key === (defaults.type || "PAPER") ? " selected" : ""}>${t.display}</option>`))
      .concat(["</optgroup>"]);
  }).join("");
  // Java runtime card — read-only, auto-picked from type+version.
  // We render a placeholder here; the actual recommendation is fetched
  // and rendered live whenever type or version change.
  root.innerHTML = `
    <div class="config-grid config-grid-top">
      <label class="config-field config-field-wide">
        <span>Server Type</span>
        <select class="asf-type">${typeOpts}</select>
        <small class="field-desc asf-type-desc"></small>
        <small class="field-notes asf-type-notes" hidden></small>
      </label>
      <label class="config-field config-field-wide">
        <span>Java Runtime (auto)</span>
        <div class="java-runtime-card">
          <span class="java-runtime-tag asf-java-tag">—</span>
          <small class="field-desc asf-java-reason">Pick a Server Type and MC Version to see the runtime.</small>
          <small class="field-desc field-notes">Change the MC Version (or Server Type) to switch Java runtimes.</small>
        </div>
      </label>
    </div>
    <div class="config-grid">
      <label class="config-field">
        <span>MC Version</span>
        <span class="asf-version-slot">
          <input type="text" class="asf-version" value="${defaults.version || "LATEST"}" placeholder="1.21.10 or LATEST">
        </span>
        <small class="field-desc asf-version-source">type LATEST to track newest stable</small>
      </label>
      <div class="asf-extras" style="display:contents"></div>
      <label class="config-field">
        <span>Memory</span>
        <input type="text" class="asf-memory" value="${defaults.memory || "2G"}" placeholder="2G">
      </label>
      <label class="config-field config-field-wide">
        <span>MOTD</span>
        <input type="text" class="asf-motd" value="${(defaults.motd || "").replace(/"/g, "&quot;")}" placeholder="A Minecraft server">
      </label>
      <label class="config-field config-field-wide">
        <span>Icon URL</span>
        <input type="text" class="asf-icon" value="${(defaults.icon || "").replace(/"/g, "&quot;")}" placeholder="https://example.com/icon.png (64×64 .png)">
      </label>
      <label class="config-field">
        <span>Difficulty</span>
        <select class="asf-difficulty">
          <option value="">(default)</option>
          <option${defaults.difficulty === "peaceful" ? " selected" : ""}>peaceful</option>
          <option${defaults.difficulty === "easy" ? " selected" : ""}>easy</option>
          <option${defaults.difficulty === "normal" ? " selected" : ""}>normal</option>
          <option${defaults.difficulty === "hard" ? " selected" : ""}>hard</option>
        </select>
      </label>
      <label class="config-field">
        <span>Max Players</span>
        <input type="number" class="asf-maxplayers" min="1" max="200" value="${defaults.max_players || ""}" placeholder="20">
      </label>
    </div>`;

  const typeSel = root.querySelector(".asf-type");
  const verSlot = root.querySelector(".asf-version-slot");
  const verSrc = root.querySelector(".asf-version-source");
  const extrasRoot = root.querySelector(".asf-extras");
  const typeDesc = root.querySelector(".asf-type-desc");
  const typeNotes = root.querySelector(".asf-type-notes");

  function renderExtras() {
    const t = byKey[typeSel.value];
    typeDesc.textContent = t?.description || "";
    if (t?.notes) { typeNotes.hidden = false; typeNotes.textContent = "⚠ " + t.notes; }
    else { typeNotes.hidden = true; typeNotes.textContent = ""; }
    const fields = (t?.extra_fields) || [];
    if (!fields.length) { extrasRoot.innerHTML = ""; return; }
    extrasRoot.innerHTML = fields.map(f => {
      const val = ((defaults.extra_env || {})[f.key] || "").replace(/"/g, "&quot;");
      if (f.kind === "build" || f.kind === "loader_version") {
        return `
          <label class="config-field">
            <span>${f.label}</span>
            <span class="asf-build-slot" data-build-key="${f.key}">
              <input type="text" data-extra-key="${f.key}" value="${val}" placeholder="${f.hint || "auto-detected"}">
            </span>
            <small class="field-desc">${f.hint || ""}</small>
          </label>`;
      }
      if (f.kind === "arclight_build") {
        return `
          <div class="config-field config-field-wide arclight-picker" data-arclight-picker="1">
            <span>${f.label}</span>
            <div class="arclight-channel-row">
              <label class="radio-pill">
                <input type="radio" name="asf-arclight-channel" value="snapshot" data-arc-channel checked>
                <span>Snapshot <small>(recommended for 1.21.x)</small></span>
              </label>
              <label class="radio-pill">
                <input type="radio" name="asf-arclight-channel" value="stable" data-arc-channel>
                <span>Stable</span>
              </label>
            </div>
            <select data-arc-build data-extra-key="HUB_LOADER_TAG">
              <option value="latest">(latest snapshot — auto)</option>
            </select>
            <small class="field-desc arc-build-meta">
              Builds load from <code>arclight.izzel.io</code>. The hub downloads the picked jar on create.
            </small>
          </div>`;
      }
      if (f.kind === "arclight_type") {
        const opts = ["FORGE", "NEOFORGE", "FABRIC"];
        return `
          <label class="config-field">
            <span>${f.label}</span>
            <select data-extra-key="${f.key}">
              ${opts.map(o => `<option value="${o}"${val === o ? " selected" : ""}>${o}</option>`).join("")}
            </select>
            <small class="field-desc">${f.hint || ""}</small>
          </label>`;
      }
      return `
        <label class="config-field">
          <span>${f.label}</span>
          <input type="text" data-extra-key="${f.key}" value="${val}" placeholder="${f.hint || ""}">
          <small class="field-desc">${f.hint || ""}</small>
        </label>`;
    }).join("");
    // If the Arclight picker is now mounted, wire its channel + subtype
    // changes to re-load builds for the current MC version.
    const picker = root.querySelector("[data-arclight-picker]");
    if (picker) {
      const refresh = () => {
        const v = (root.querySelector(".asf-version")?.value || "").trim();
        loadBuilds(typeSel.value, v);
      };
      picker.querySelectorAll("[data-arc-channel]").forEach(r =>
        r.addEventListener("change", refresh)
      );
      const subSel = root.querySelector('[data-extra-key="ARCLIGHT_TYPE"]');
      if (subSel) subSel.addEventListener("change", refresh);
    }
  }

  async function loadBuilds(typeKey, version) {
    // Arclight gets its own composite picker fed from /api/server/arclight/builds.
    if (typeKey === "ARCLIGHT") {
      const picker = root.querySelector("[data-arclight-picker]");
      if (picker && version) {
        const channel = picker.querySelector('[data-arc-channel]:checked')?.value || "snapshot";
        const subtype = (root.querySelector('[data-extra-key="ARCLIGHT_TYPE"]')?.value || "FORGE").toLowerCase();
        const sel = picker.querySelector("[data-arc-build]");
        const meta = picker.querySelector(".arc-build-meta");
        sel.innerHTML = `<option>loading…</option>`;
        try {
          const r = await api(`/api/server/arclight/builds?version=${encodeURIComponent(version)}&subtype=${encodeURIComponent(subtype)}&channel=${encodeURIComponent(channel)}`);
          const builds = r.builds || [];
          if (!builds.length) {
            sel.innerHTML = `<option value="latest">(no builds for this combo)</option>`;
            if (meta) meta.textContent = `No ${channel} builds for MC ${version}/${subtype}.`;
          } else {
            sel.innerHTML = [`<option value="latest">(latest ${channel})</option>`].concat(
              builds.map(b => {
                const short = b.tag.replace(/^(snapshot|stable)\//, "");
                const date = b.published_at ? b.published_at.slice(0, 10) : "";
                return `<option value="${b.tag}">${short}${date ? "  ·  " + date : ""}</option>`;
              })
            ).join("");
            if (meta) meta.innerHTML = `${builds.length} ${channel} build(s) from <code>arclight.izzel.io</code>.`;
          }
        } catch (e) {
          sel.innerHTML = `<option value="latest">(lookup failed)</option>`;
          if (meta) meta.textContent = "Lookup failed: " + e.message;
        }
      }
      // Fall through so any legacy build/loader_version slots also populate
      // — Arclight currently has none but the code stays uniform.
    }
    // Populate every build/loader_version slot for this type. Backend
    // /api/server/paper returns a list of {id, channel} regardless of
    // whether it's a paper build, a forge loader version, a fabric loader, etc.
    const slots = root.querySelectorAll(".asf-build-slot");
    if (!slots.length) return;
    slots.forEach(s => {
      const k = s.dataset.buildKey;
      s.innerHTML = `<select data-extra-key="${k}"><option>loading…</option></select>`;
    });
    if (!version) {
      slots.forEach(s => {
        const k = s.dataset.buildKey;
        s.innerHTML = `<input type="text" data-extra-key="${k}" placeholder="set MC version first">`;
      });
      return;
    }
    try {
      const r = await api(`/api/server/paper?type_key=${encodeURIComponent(typeKey)}&version=${encodeURIComponent(version)}`);
      const builds = r.builds || [];
      if (!builds.length) {
        slots.forEach(s => {
          const k = s.dataset.buildKey;
          const cur = ((defaults.extra_env || {})[k] || "").replace(/"/g, "&quot;");
          s.innerHTML = `<input type="text" data-extra-key="${k}" value="${cur}" placeholder="no auto-list; type manually">`;
        });
        return;
      }
      const opts = ['<option value="">(latest stable)</option>']
        .concat(builds.slice(0, 50).map(b => {
          const channel = (b.channel || "STABLE").toLowerCase();
          return `<option value="${b.id}">${b.id}${channel === "stable" ? "" : " (" + channel + ")"}</option>`;
        }));
      slots.forEach(s => {
        const k = s.dataset.buildKey;
        const preferred = (defaults.extra_env || {})[k] || "";
        s.innerHTML = `<select data-extra-key="${k}">${opts.join("")}</select>`;
        const sel = s.querySelector("select");
        if (preferred && Array.from(sel.options).some(o => o.value === String(preferred))) {
          sel.value = String(preferred);
        }
      });
    } catch {
      slots.forEach(s => {
        const k = s.dataset.buildKey;
        s.innerHTML = `<input type="text" data-extra-key="${k}" placeholder="lookup failed; type manually">`;
      });
    }
  }

  async function loadVersions(preferred) {
    verSlot.innerHTML = `<select class="asf-version"><option>loading…</option></select>`;
    verSrc.textContent = "loading versions…";
    let chosenVersion = preferred || "";
    try {
      const r = await api(`/api/server/paper/versions?type_key=${encodeURIComponent(typeSel.value)}`);
      const versions = r.versions || [];
      if (!versions.length) {
        verSlot.innerHTML = `<input type="text" class="asf-version" value="${preferred || ""}" placeholder="e.g. 1.20.1">`;
        verSrc.textContent = "no auto-list — type a version manually (or 'LATEST')";
      } else {
        verSlot.innerHTML = `<select class="asf-version">${versions.map(v => `<option value="${v}">${v}</option>`).join("")}</select>`;
        const sel = verSlot.querySelector("select");
        chosenVersion = (preferred && versions.includes(preferred)) ? preferred : versions[0];
        sel.value = chosenVersion;
        verSrc.textContent = `${versions.length} stable versions from ${typeSel.value.toLowerCase()} upstream`;
      }
    } catch {
      verSlot.innerHTML = `<input type="text" class="asf-version" value="${preferred || ""}" placeholder="1.21.10 or LATEST">`;
      verSrc.textContent = "version lookup failed — type a version manually";
    }
    // Re-bind change handler against whatever element ended up in the slot,
    // then trigger the build lookup for the initial version.
    const verEl = root.querySelector(".asf-version");
    verEl.addEventListener("change", (e) => {
      loadBuilds(typeSel.value, (e.target.value || "").trim());
      // refreshJava is hoisted but only defined after loadVersions is declared;
      // by the time this listener fires, the function exists.
      if (typeof refreshJava === "function") refreshJava();
    });
    await loadBuilds(typeSel.value, chosenVersion);
  }

  // Refresh the Java card from the backend's java-runtime endpoint.
  async function refreshJava() {
    const v = (root.querySelector(".asf-version")?.value || "").trim();
    const t = typeSel.value;
    if (!t) return;
    try {
      const rec = await api(`/api/java-runtime?type_key=${encodeURIComponent(t)}&version=${encodeURIComponent(v)}`);
      root.querySelector(".asf-java-tag").textContent = `☕ Java ${rec.java} — :${rec.tag}`;
      root.querySelector(".asf-java-reason").textContent = rec.reason || "";
    } catch { /* leave previous render */ }
  }

  typeSel.addEventListener("change", () => {
    renderExtras();
    loadVersions(defaults.version || "").then(refreshJava);
    refreshJava();
  });

  // Initial render
  renderExtras();
  loadVersions(defaults.version || "").then(refreshJava);
  refreshJava();

  function collect() {
    const verEl = root.querySelector(".asf-version");
    const extra_env = {};
    root.querySelectorAll("[data-extra-key]").forEach(el => {
      const v = (el.value || "").trim();
      if (v) extra_env[el.dataset.extraKey] = v;
    });
    // For Arclight: stash channel + subtype + tag in extra_env so the create
    // endpoint can hand them straight to /api/server/arclight/install after
    // scaffolding the compose file.
    const picker = root.querySelector("[data-arclight-picker]");
    if (picker && typeSel.value === "ARCLIGHT") {
      extra_env.HUB_LOADER_CHANNEL = picker.querySelector('[data-arc-channel]:checked')?.value || "snapshot";
      extra_env.HUB_LOADER_SUBTYPE = (root.querySelector('[data-extra-key="ARCLIGHT_TYPE"]')?.value || "FORGE").toLowerCase();
      // HUB_LOADER_TAG already populated via data-extra-key
    }
    return {
      type: typeSel.value,
      version: (verEl?.value || "LATEST").trim() || "LATEST",
      memory: (root.querySelector(".asf-memory").value || "").trim() || "2G",
      motd: (root.querySelector(".asf-motd").value || "").trim(),
      icon: (root.querySelector(".asf-icon").value || "").trim(),
      difficulty: root.querySelector(".asf-difficulty").value || "",
      max_players: (root.querySelector(".asf-maxplayers").value || "").trim(),
      // image_tag intentionally omitted — backend picks it from (type, version).
      extra_env,
    };
  }
  return { collect, refreshJava };
}

function openAddServerModal() {
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:720px;max-height:90vh;overflow-y:auto">
      <h2>＋ Add Server</h2>
      <div class="modal-tabs">
        <button class="modal-tab active" data-mtab="track">Track Existing</button>
        <button class="modal-tab" data-mtab="create">Create New</button>
      </div>

      <div class="modal-tab-content active" data-mcontent="track">
        <p class="hint">Register an existing container with the hub.</p>
        <label class="modal-form-field"><span>Display Name</span><input id="track-name" type="text" placeholder="My Survival"></label>
        <label class="modal-form-field"><span>Container Name</span><input id="track-container" type="text" placeholder="my-survival"></label>
        <label class="modal-form-field"><span>Data Directory</span><input id="track-data" type="text" placeholder="/media/Minecraft/my-survival"></label>
        <label class="modal-form-field"><span>Port</span><input id="track-port" type="number" value="25565"></label>

        <label class="modal-form-field" style="display:flex;align-items:center;gap:8px;margin-top:8px">
          <input type="checkbox" id="track-has-compose" checked style="width:auto">
          <span style="margin:0">I have an existing compose file</span>
        </label>
        <div id="track-compose-wrap">
          <label class="modal-form-field"><span>Compose File Path</span><input id="track-compose" type="text" placeholder="/home/kratos/docker-composes/my-survival.yaml"></label>
        </div>
        <div id="track-config-wrap" hidden>
          <p class="hint" style="margin-top:8px">No existing compose? The hub will scaffold one at <code>~/docker-composes/&lt;slug&gt;.yaml</code> using these settings:</p>
          <div id="track-config-form"></div>
        </div>

        <div class="btn-row">
          <button class="mc-btn mc-btn-warn" id="track-submit">💾 Track</button>
          <button class="mc-btn" id="track-cancel">Cancel</button>
        </div>
        <div class="hint" id="track-hint"></div>
      </div>

      <div class="modal-tab-content" data-mcontent="create">
        <p class="hint">Scaffold a brand-new compose file and register the server. Container will not auto-start.</p>
        <label class="modal-form-field"><span>Display Name</span><input id="create-name" type="text" placeholder="Creative World"></label>
        <label class="modal-form-field"><span>Data Directory</span><input id="create-data" type="text" placeholder="/media/Minecraft/creative-world"></label>
        <label class="modal-form-field"><span>Port</span><input id="create-port" type="number" value="25567"></label>

        <label class="modal-form-field" style="display:flex;align-items:center;gap:8px;margin-top:8px">
          <input type="checkbox" id="create-custom-compose" style="width:auto">
          <span style="margin:0">Override compose file path (default: <code>~/docker-composes/&lt;slug&gt;.yaml</code>)</span>
        </label>
        <div id="create-compose-wrap" hidden>
          <label class="modal-form-field"><span>Compose File Path</span><input id="create-compose" type="text" placeholder="/home/kratos/docker-composes/creative.yaml"></label>
        </div>

        <hr class="rp-sep" style="margin:14px 0">
        <div id="create-config-form"></div>

        <div class="btn-row">
          <button class="mc-btn mc-btn-warn" id="create-submit">💾 Create</button>
          <button class="mc-btn" id="create-cancel">Cancel</button>
        </div>
        <div class="hint" id="create-hint"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  // Tab switching
  modal.querySelectorAll(".modal-tab").forEach(t =>
    t.addEventListener("click", () => {
      modal.querySelectorAll(".modal-tab").forEach(x => x.classList.toggle("active", x === t));
      modal.querySelectorAll(".modal-tab-content").forEach(c =>
        c.classList.toggle("active", c.dataset.mcontent === t.dataset.mtab));
    })
  );

  // Track-compose checkbox toggles compose-path vs full-config UI.
  const trackHasCompose = modal.querySelector("#track-has-compose");
  const trackComposeWrap = modal.querySelector("#track-compose-wrap");
  const trackConfigWrap = modal.querySelector("#track-config-wrap");
  trackHasCompose.addEventListener("change", () => {
    trackComposeWrap.hidden = !trackHasCompose.checked;
    trackConfigWrap.hidden = trackHasCompose.checked;
  });

  // Create-compose checkbox toggles custom path input.
  const createCustomCompose = modal.querySelector("#create-custom-compose");
  const createComposeWrap = modal.querySelector("#create-compose-wrap");
  createCustomCompose.addEventListener("change", () => {
    createComposeWrap.hidden = !createCustomCompose.checked;
  });

  // Auto-fill data_dir based on name (Create tab)
  modal.querySelector("#create-name").addEventListener("input", e => {
    const slug = e.target.value.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    const dd = modal.querySelector("#create-data");
    if (slug && (!dd.value || dd._auto)) {
      dd.value = `/media/Minecraft/${slug}`;
      dd._auto = true;
    }
  });

  // Cancel buttons
  modal.querySelector("#track-cancel").addEventListener("click", () => closeModal(modal));
  modal.querySelector("#create-cancel").addEventListener("click", () => closeModal(modal));

  // Fetch types + image_tags once, then mount the config forms in both tabs.
  let trackForm = null, createForm = null;
  (async () => {
    let typesData = [], imageTags = [];
    try {
      const cfg = await api("/api/server/config").catch(() => null);
      typesData = (cfg && cfg.supported_types) || [];
      imageTags = (cfg && cfg.image_tags) || [];
    } catch {}
    // Fallback so the modal is still usable if there's no currently-selected
    // server yet (cfg returns null because /api/server/config requires one).
    if (!typesData.length) typesData = [{ key: "PAPER", display: "Paper", family: "paper", extra_fields: [] }];
    if (!imageTags.length) imageTags = [{ tag: "java21", label: "java21" }];
    trackForm = buildServerConfigForm(modal.querySelector("#track-config-form"), { typesData, imageTags });
    createForm = buildServerConfigForm(modal.querySelector("#create-config-form"), { typesData, imageTags });
  })();

  modal.querySelector("#track-submit").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    busy(btn, true, "Tracking…");
    const hint = modal.querySelector("#track-hint");
    hint.textContent = "";
    try {
      const payload = {
        name: modal.querySelector("#track-name").value.trim(),
        container: modal.querySelector("#track-container").value.trim(),
        data_dir: modal.querySelector("#track-data").value.trim(),
        port: parseInt(modal.querySelector("#track-port").value || "25565", 10),
      };
      if (trackHasCompose.checked) {
        payload.compose = modal.querySelector("#track-compose").value.trim();
      } else if (trackForm) {
        Object.assign(payload, trackForm.collect());
      }
      const rec = await api("/api/servers/track", { method: "POST", body: payload });
      toast("Tracking " + rec.id, "ok");
      closeModal(modal);
      await refreshServers();
      await selectServer(rec.id);
    } catch (err) {
      hint.textContent = "Error: " + err.message;
    } finally { busy(btn, false); }
  });

  modal.querySelector("#create-submit").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    busy(btn, true, "Creating…");
    const hint = modal.querySelector("#create-hint");
    hint.textContent = "";
    try {
      const payload = {
        name: modal.querySelector("#create-name").value.trim(),
        data_dir: modal.querySelector("#create-data").value.trim(),
        port: parseInt(modal.querySelector("#create-port").value || "25565", 10),
      };
      if (createCustomCompose.checked) {
        const cp = modal.querySelector("#create-compose").value.trim();
        if (cp) payload.compose = cp;
      }
      if (createForm) Object.assign(payload, createForm.collect());
      const rec = await api("/api/servers/create", { method: "POST", body: payload });
      hint.innerHTML = `Scaffolded <code>${rec.compose}</code>. ` +
        `<button class="mc-btn mc-btn-warn" id="create-start">▶ Start container now</button>`;
      modal.querySelector("#create-start").addEventListener("click", async (ev) => {
        const sb = ev.currentTarget;
        busy(sb, true, "Starting…");
        try {
          await api(`/api/servers/${encodeURIComponent(rec.id)}/start`, { method: "POST" });
          toast("Container started", "ok");
          closeModal(modal);
          await refreshServers();
          await selectServer(rec.id);
        } catch (err) {
          toast("Start failed: " + err.message, "err");
        } finally { busy(sb, false); }
      });
      await refreshServers();
    } catch (err) {
      hint.textContent = "Error: " + err.message;
    } finally { busy(btn, false); }
  });
}

// ─────────────────────────────────────────────────────────────
// Catalog entry add/edit modal — opens for both "+ Add Entry" and
// the per-card ✎ Edit buttons. Same form, edit pre-fills, add starts blank.
// ─────────────────────────────────────────────────────────────
const CATALOG_SOURCES = [
  { value: "modrinth", label: "Modrinth", hint: "Project slug (e.g. 'luckperms', 'chunky-pregenerator')" },
  { value: "spiget",   label: "Spiget (SpigotMC)", hint: "Numeric resource ID (e.g. '64348' for mcMMO)" },
  { value: "hangar",   label: "Hangar (PaperMC)", hint: "Owner/Project (e.g. 'ViaVersion/ViaBackwards')" },
  { value: "geyser",   label: "Geyser official", hint: "Exactly 'geyser' or 'floodgate'" },
  { value: "github",   label: "GitHub Releases", hint: "owner/repo or owner/repo/*.jar (glob)" },
];

function openCatalogEntryModal({ key = "", display = "", sources = [] } = {}) {
  const isEdit = !!key;
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:640px;max-height:90vh;overflow-y:auto">
      <h2>${isEdit ? "✎ Edit" : "＋ Add"} Catalog Entry</h2>
      <p class="hint" style="margin:0 0 12px">
        Entries map a plugin (by its <code>plugin.yml</code> name) to one or more upstream sources.
        The first source that yields a real jar wins, so list reliable ones first.
      </p>
      <label class="modal-form-field">
        <span>Key (normalized plugin.yml name)</span>
        <input id="cat-key" type="text" placeholder="luckperms" value="${key.replace(/"/g, "&quot;")}" ${isEdit ? "disabled" : ""}>
      </label>
      <label class="modal-form-field">
        <span>Display Name</span>
        <input id="cat-display" type="text" placeholder="LuckPerms" value="${display.replace(/"/g, "&quot;")}">
      </label>
      <div style="margin:10px 0 4px;font-size:13px;font-weight:600">Sources</div>
      <div id="cat-sources" style="display:flex;flex-direction:column;gap:6px"></div>
      <button class="mc-btn" id="cat-add-source" style="margin-top:8px;background:linear-gradient(180deg,#6b7280 0%,#374151 100%);font-size:12px;padding:5px 10px">＋ Add Source</button>
      <p class="hint" style="margin-top:10px;font-size:11px">
        Tip: drag rows by the ☰ handle to reorder — the order is the install fallback order.
        An entry with zero sources is shown in the catalog but can't be auto-installed (useful for tracking premium uploads).
      </p>
      <div class="btn-row">
        <button class="mc-btn mc-btn-warn" id="cat-save">💾 ${isEdit ? "Save" : "Add"}</button>
        <button class="mc-btn" id="cat-cancel" style="background:linear-gradient(180deg,#6b7280 0%,#374151 100%)">Cancel</button>
      </div>
      <div class="hint" id="cat-hint"></div>
    </div>`;
  document.body.appendChild(modal);

  const list = modal.querySelector("#cat-sources");
  function addSourceRow(src = "", ref = "") {
    const row = document.createElement("div");
    row.className = "cat-source-row";
    row.draggable = true;
    row.style.cssText = "display:flex;gap:6px;align-items:center;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);padding:6px;border-radius:4px";
    row.innerHTML = `
      <span style="cursor:grab;opacity:0.5;user-select:none">☰</span>
      <select class="cat-src" style="flex:0 0 140px">
        ${CATALOG_SOURCES.map(s => `<option value="${s.value}"${s.value === src ? " selected" : ""}>${s.label}</option>`).join("")}
      </select>
      <input class="cat-ref" type="text" placeholder="ref" value="${ref.replace(/"/g, "&quot;")}" style="flex:1">
      <button class="mc-btn cat-src-rm" style="background:linear-gradient(180deg,#dc2626 0%,#7f1d1d 100%);font-size:11px;padding:4px 8px" title="Remove this source">✕</button>`;
    const refInput = row.querySelector(".cat-ref");
    const srcSel = row.querySelector(".cat-src");
    const updateHint = () => {
      const meta = CATALOG_SOURCES.find(s => s.value === srcSel.value);
      refInput.placeholder = meta?.hint || "ref";
    };
    updateHint();
    srcSel.addEventListener("change", updateHint);
    row.querySelector(".cat-src-rm").addEventListener("click", () => row.remove());
    // Drag-and-drop reorder
    row.addEventListener("dragstart", (e) => {
      row._dragging = true;
      row.style.opacity = "0.4";
      e.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragend", () => { row._dragging = false; row.style.opacity = "1"; });
    row.addEventListener("dragover", (e) => {
      e.preventDefault();
      const dragging = list.querySelector(".cat-source-row[style*='opacity: 0.4']");
      if (!dragging || dragging === row) return;
      const after = (e.clientY - row.getBoundingClientRect().top) > row.offsetHeight / 2;
      list.insertBefore(dragging, after ? row.nextSibling : row);
    });
    list.appendChild(row);
  }
  if (sources.length) sources.forEach(s => addSourceRow(s.source, s.ref));
  else addSourceRow();

  modal.querySelector("#cat-add-source").addEventListener("click", () => addSourceRow());
  modal.querySelector("#cat-cancel").addEventListener("click", () => closeModal(modal));
  modal.querySelector("#cat-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const hint = modal.querySelector("#cat-hint");
    hint.textContent = "";
    const payload = {
      key: modal.querySelector("#cat-key").value.trim().toLowerCase(),
      display: modal.querySelector("#cat-display").value.trim(),
      sources: [],
    };
    if (!payload.key) { hint.textContent = "Key is required."; return; }
    if (!payload.display) { hint.textContent = "Display name is required."; return; }
    for (const row of list.querySelectorAll(".cat-source-row")) {
      const source = row.querySelector(".cat-src").value;
      const ref = row.querySelector(".cat-ref").value.trim();
      if (!ref) continue;
      payload.sources.push({ source, ref });
    }
    busy(btn, true, "Saving…");
    try {
      await api("/api/catalog", { method: "POST", body: payload });
      toast(`${payload.display} ${isEdit ? "updated" : "added"}.`, "ok");
      closeModal(modal);
      await refreshRegistry();
    } catch (err) {
      hint.textContent = "Error: " + err.message;
    } finally { busy(btn, false); }
  });
}


function openRemoveServerModal() {
  const current = (serversCache.servers || []).find(s => s.id === serversCache.current);
  if (!current) { toast("No server selected.", "err"); return; }
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-box" style="max-width:480px">
      <h2>Stop tracking <code>${current.display || current.id}</code>?</h2>
      <p>By default this just removes the server from the hub. Your compose file and world data are preserved on disk.</p>
      <label class="modal-form-field" style="display:flex;align-items:center;gap:8px">
        <input type="checkbox" id="rm-delete-files" style="width:auto"> <span style="margin:0">Also delete the compose file (world data is always kept)</span>
      </label>
      <div class="btn-row">
        <button class="mc-btn mc-btn-danger" id="rm-confirm">Remove</button>
        <button class="mc-btn" id="rm-cancel">Cancel</button>
      </div>
      <div class="hint" id="rm-hint"></div>
    </div>`;
  document.body.appendChild(modal);

  modal.querySelector("#rm-cancel").addEventListener("click", () => closeModal(modal));
  modal.querySelector("#rm-confirm").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    busy(btn, true, "Removing…");
    try {
      const delFiles = modal.querySelector("#rm-delete-files").checked;
      await api(`/api/servers/${encodeURIComponent(current.id)}`, {
        method: "DELETE", body: { delete_files: delFiles },
      });
      toast("Removed " + current.id, "ok");
      closeModal(modal);
      await refreshServers();
      refreshServer(); refreshPlugins(); refreshRegistry();
      if (typeof loadConfig === "function") loadConfig();
    } catch (err) {
      modal.querySelector("#rm-hint").textContent = "Error: " + err.message;
    } finally { busy(btn, false); }
  });
}

// ── Wiring ──────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  // Tab nav
  document.querySelectorAll(".nav-btn").forEach((b) =>
    b.addEventListener("click", () => activateTab(b.dataset.tab))
  );
  const initial = localStorage.getItem("tab") || "play";
  activateTab(initial);

  // Server controls
  $("btn-refresh").addEventListener("click", () => { refreshServer(); refreshPlugins(); refreshRegistry(); });
  $("btn-restart").addEventListener("click", (e) => controlServer("restart", e.currentTarget));
  $("btn-start").addEventListener("click", (e) => controlServer("start", e.currentTarget));
  $("btn-stop").addEventListener("click", (e) => controlServer("stop", e.currentTarget));

  // Copy address
  $("btn-copy-addr").addEventListener("click", async () => {
    const addr = $("connect-addr").textContent;
    try {
      await navigator.clipboard.writeText(addr);
      toast(`Copied ${addr} to clipboard.`, "ok");
    } catch { toast("Copy failed — select manually.", "err"); }
  });

  // Plugins
  $("btn-plugins-refresh").addEventListener("click", refreshPlugins);
  $("btn-plugins-check").addEventListener("click", (e) => checkAllPlugins(e.currentTarget));
  $("btn-plugins-stage-all").addEventListener("click", (e) => stageAllPlugins(e.currentTarget));
  $("upload-input").addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) uploadPlugin(f);
    e.target.value = "";
  });
  $("plugins-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const file = btn.dataset.file;
    if (btn.dataset.action === "stage") stageOne(file, btn);
    else if (btn.dataset.action === "delete") deleteOne(file, btn);
  });

  // Catalog
  $("btn-registry-refresh").addEventListener("click", () => refreshRegistry({ force: true }));
  $("btn-install-all").addEventListener("click", (e) => installAllMissing(e.currentTarget));
  $("btn-catalog-add").addEventListener("click", () => openCatalogEntryModal());
  $("btn-catalog-reset").addEventListener("click", async () => {
    if (!(await confirmModal({
      title: "Reset catalog to defaults?",
      message: "Wipes all user edits to this server's catalog and re-seeds it from the built-in defaults. Installed jars are NOT touched.",
      confirmText: "Reset", danger: true,
    }))) return;
    try {
      const r = await api("/api/catalog/reset-defaults", { method: "POST" });
      toast(`Catalog reset (${r.entries} entries).`, "ok");
      await refreshRegistry();
    } catch (e) { toast(`Reset failed: ${e.message}`, "err"); }
  });
  $("registry-list").addEventListener("click", async (e) => {
    const installBtn = e.target.closest("button[data-action='install']");
    if (installBtn) {
      installFromCatalog(installBtn.dataset.key, installBtn, null);
      return;
    }
    const editBtn = e.target.closest(".catalog-edit-btn");
    if (editBtn) {
      let sources = [];
      try { sources = JSON.parse(editBtn.dataset.sources.replace(/&#39;/g, "'")); } catch {}
      openCatalogEntryModal({ key: editBtn.dataset.key, display: editBtn.dataset.display, sources });
      return;
    }
    const removeBtn = e.target.closest(".catalog-remove-btn");
    if (removeBtn) {
      const k = removeBtn.dataset.key;
      const d = removeBtn.dataset.display;
      if (!(await confirmModal({
        title: `Remove ${d} from catalog?`,
        message: "Removes only the catalog entry. Already-installed jars are NOT touched — you'd just lose the auto-install/update wiring for this plugin.",
        confirmText: "Remove", danger: true,
      }))) return;
      try {
        await api(`/api/catalog/${encodeURIComponent(k)}`, { method: "DELETE" });
        toast(`${d} removed from catalog.`, "ok");
        await refreshRegistry();
      } catch (err) { toast(`Remove failed: ${err.message}`, "err"); }
      return;
    }
  });

  // Updates tab
  $("btn-paper-check").addEventListener("click", (e) => checkPaper(e.currentTarget));
  $("btn-server-update").addEventListener("click", (e) => updatePaper(e.currentTarget));
  $("btn-apply-pending").addEventListener("click", () => openSchedulerModal({ contextual: "updates" }));
  $("btn-recurring-edit").addEventListener("click", async () => {
    let existing = null;
    try { const r = await api("/api/restart/recurring"); existing = r.recurring; } catch {}
    openRecurringModal(existing);
  });
  $("btn-recurring-delete").addEventListener("click", async () => {
    if (!(await confirmModal({
      title: "Remove recurring restart?",
      message: "Disables and deletes the recurring restart schedule. You can create a new one any time.",
      confirmText: "Remove", danger: true,
    }))) return;
    try {
      await api("/api/restart/recurring", { method: "DELETE" });
      toast("Recurring schedule removed.", "ok");
      await refreshRecurring();
    } catch (e) { toast("Remove failed: " + e.message, "err"); }
  });
  $("btn-clear-pending").addEventListener("click", async () => {
    if (!(await confirmModal({
      title: "Cancel all pending updates?",
      message: "Removes every jar in plugins/update/ and clears the staged-memory entries. Already-running plugins are not affected.",
      confirmText: "Clear All", danger: true,
    }))) return;
    const s = lastServer; if (!s) return;
    for (const f of s.pending_updates || []) {
      try { await api(`/api/plugins/pending/${encodeURIComponent(f)}`, { method: "DELETE" }); }
      catch { toast("Cancel " + f + " failed", "err"); }
    }
    toast("Cleared pending.", "ok");
    refreshServer();
  });
  $("pending-list").addEventListener("click", async (e) => {
    const cancelBtn = e.target.closest("button[data-action='cancel-pending']");
    if (cancelBtn) { cancelPending(cancelBtn.dataset.file, cancelBtn); return; }
    const keepBtn = e.target.closest("button[data-action='cancel-deletion']");
    if (keepBtn) {
      const file = keepBtn.dataset.file;
      busy(keepBtn, true, "Cancelling…");
      try {
        await api(`/api/plugins/${encodeURIComponent(file)}/staged-delete`, { method: "DELETE" });
        toast(`Kept ${file}.`, "ok");
        await refreshServer();
      } catch (err) { toast("Cancel failed: " + err.message, "err"); busy(keepBtn, false); }
    }
  });

  // Console
  $("console-follow").addEventListener("change", (e) => { consoleFollow = e.target.checked; });
  $("btn-console-clear").addEventListener("click", () => { $("console-window").innerHTML = ""; });
  $("btn-console-toggle").addEventListener("click", (e) => {
    consolePaused = !consolePaused;
    e.currentTarget.textContent = consolePaused ? "▶ Resume" : "⏸ Pause";
  });

  // Initial loads
  refreshServers().then(() => {
    loadHost();
    refreshServer();
    refreshPlugins();
    refreshRegistry();
  });

  // Server switcher wiring
  $("server-select").addEventListener("change", (e) => {
    if (e.target.value) selectServer(e.target.value);
  });
  $("btn-server-add").addEventListener("click", openAddServerModal);
  $("btn-server-remove").addEventListener("click", openRemoveServerModal);

  // Server config tab
  $("btn-config-reload").addEventListener("click", loadConfig);
  $("btn-recreate").addEventListener("click", (e) => recreateContainer(e.currentTarget));
  $("btn-config-save").addEventListener("click", (e) => saveConfig(e.currentTarget, false));
  $("btn-config-save-recreate").addEventListener("click", (e) => saveConfig(e.currentTarget, true));
  // When user picks a different type, re-render extras + reload version list
  $("cfg-type").addEventListener("change", async (e) => {
    const newType = e.target.value;
    renderTypeFields(newType, configState?.env || {});
    await loadVersionDropdown(newType, configState?.env?.VERSION || "");
    refreshJavaRecommendation();
  });
  // When user picks a different version, reload the build dropdown
  $("cfg-version").addEventListener("change", (e) => {
    const t = $("cfg-type").value;
    loadBuildsForCurrentTypeVersion(t, e.target.value, "");
    refreshJavaRecommendation();
  });
  loadConfig();

  // Search tab
  $("search-btn").addEventListener("click", (e) => runSearch(e.currentTarget));
  $("search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch($("search-btn"));
  });
  $("search-premium-only").addEventListener("change", (e) => {
    _searchPremiumOnly = e.target.checked;
    renderSearchResults();
  });
  $("search-results").addEventListener("click", async (e) => {
    const installBtn = e.target.closest(".search-install-btn");
    if (installBtn) {
      installFromSearch(installBtn.dataset.source, installBtn.dataset.ref, installBtn.dataset.title, installBtn);
      return;
    }
    const addBtn = e.target.closest(".search-premium-add-btn");
    if (addBtn) {
      const sid = addBtn.dataset.spigotId;
      const title = addBtn.dataset.title || `Spigot #${sid}`;
      if (!(await confirmModal({
        title: `Add ${title} to catalog?`,
        message: `Adds Spigot ID ${sid} to your premium catalog. The hub will check for new versions every hour but cannot auto-download — you'll still need to buy + upload the jar yourself.`,
        confirmText: "Add to Catalog", danger: false,
      }))) return;
      busy(addBtn, true, "Adding…");
      try {
        const r = await api("/api/premium", {
          method: "POST",
          body: { spigot_id: sid, display: title, url: addBtn.dataset.url || "", icon: addBtn.dataset.icon || null },
        });
        if (r.premium_confirmed === false) {
          toast(`${title} added — but Spiget says it isn't actually premium. You may be able to install it normally from search.`, "ok");
        } else {
          toast(`${title} added to catalog.`, "ok");
        }
        await refreshRegistry();
      } catch (err) {
        toast(`Add failed: ${err.message}`, "err");
      } finally { busy(addBtn, false); }
      return;
    }
  });

  // tab-jump links in body
  document.body.addEventListener("click", (e) => {
    const link = e.target.closest("a.tab-jump");
    if (!link) return;
    e.preventDefault();
    activateTab(link.dataset.tab);
  });

  // Adaptive polling: fast (3s) during transitional states, normal (15s) once stable.
  function scheduleNextPoll() {
    const h = lastServer && lastServer.health;
    const transitional = lastServer && lastServer.running && (h === "starting" || h === "unhealthy");
    // Also poll fast when the container isn't running yet but might be coming up
    const delay = transitional ? 3000 : 15000;
    setTimeout(async () => {
      await refreshServer();
      await refreshSchedule();
      await refreshRecurring();
      scheduleNextPoll();
    }, delay);
  }
  scheduleNextPoll();
  refreshSchedule();
  refreshRecurring();
});
