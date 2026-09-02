/* Dashboard van de alarmcentrale.
 *
 * Bewust zonder framework en zonder buildstap: dit draait op een Raspberry Pi
 * die jaren moet meegaan, en een npm-boom die over twee jaar niet meer bouwt is
 * hier een reëel risico. Tijden worden in de browser omgezet, zodat de
 * weergave altijd in jouw lokale tijd staat zonder serverconfiguratie.
 */

const ICONS = {
  alarm: "🚨", trouble: "⚠️", restore: "✅",
  info: "ℹ️", heartbeat: "💓", unknown: "❓",
};

const CATEGORY_LABELS = {
  burglary: "Inbraak", fire: "Brand", gas: "Gas / CO", heat: "Hitte",
  water: "Water", panic: "Paniek", medical: "Medisch", tamper: "Sabotage",
  arming: "In- en uitschakelen", power: "Voeding", battery: "Batterij",
  rf: "Radio", communication: "Verbinding", supervision: "Supervisie",
  access: "Toegang", test: "Test", config: "Instellingen",
  system: "Systeem", unknown: "Onbekend",
};

// Hartslagen staan standaard uit: bij een ping van een minuut zijn dat ruim
// 1400 regels per dag, en dan zie je de gebeurtenissen die ertoe doen niet meer.
const state = {
  offset: 0, filters: {}, seen: new Set(),
  socket: null, backoff: 1000, showHeartbeat: false,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* ── Hulpfuncties ──────────────────────────────────────────────────────── */

function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return sameDay ? time : `${d.toLocaleDateString("nl-NL", { day: "2-digit", month: "2-digit" })} ${time}`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "nooit";
  if (seconds < 60) return `${Math.round(seconds)} sec`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} uur`;
  return `${Math.round(seconds / 86400)} dagen`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (response.status === 401) { showLogin(); throw new Error("niet ingelogd"); }
  if (!response.ok) {
    let detail = `fout ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* geen JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

/* ── Inloggen ──────────────────────────────────────────────────────────── */

function showLogin(hint) {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  if (hint) $("#login-hint").textContent = hint;
}

function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const error = $("#login-error");
  error.classList.add("hidden");
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
      }),
    });
    showApp();
    await start();
  } catch (exc) {
    error.textContent = exc.message;
    error.classList.remove("hidden");
  }
});

$("#logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  if (state.socket) state.socket.close();
  location.reload();
});

/* ── Statustegels ──────────────────────────────────────────────────────── */

function renderTiles(status) {
  const tiles = $("#tiles");
  tiles.textContent = "";

  const hubStale = !status.hub_online;
  tiles.append(tile(
    "Hub",
    hubStale ? "Niet bereikbaar" : "Verbonden",
    `laatste bericht ${formatDuration(status.seconds_since_contact)} geleden`,
    hubStale ? "bad" : "ok",
  ));

  tiles.append(tile(
    "Alarmen",
    status.open_alarms ? String(status.open_alarms) : "Geen",
    status.open_alarms ? "nog niet bevestigd" : "niets openstaand",
    status.open_alarms ? "bad" : "ok",
  ));

  const troubles = status.troubles || [];
  tiles.append(tile(
    "Storingen",
    troubles.length ? String(troubles.length) : "Geen",
    troubles.length ? troubles.map((t) => t.title).join(", ") : "alles in orde",
    troubles.length ? "warn" : "ok",
  ));

  (status.partitions || []).forEach((partition) => {
    tiles.append(tile(
      partition.name,
      partition.armed ? "Ingeschakeld" : "Uitgeschakeld",
      partition.changed_at ? `sinds ${formatTime(partition.changed_at)}` : "nog geen wijziging gezien",
      // Ingeschakeld krijgt een eigen kleur: in één oogopslag zien of het
      // huis bewaakt wordt, zonder de tekst te hoeven lezen.
      partition.armed ? "armed" : "plain",
    ));
  });
}

function tile(label, value, sub, kind) {
  const node = el("div", `tile ${kind}`);
  node.append(el("div", "label", label), el("div", "value", value), el("div", "sub", sub));
  return node;
}

/* ── Waarschuwingsbalken ───────────────────────────────────────────────── */

function renderBanners(status) {
  const banners = $("#banners");
  banners.textContent = "";

  if (!status.hub_online) {
    banners.append(banner("bad",
      "De hub is niet bereikbaar. Dat kan stroomuitval, een verbroken " +
      "netwerkverbinding of sabotage zijn — controleer het systeem."));
  }
  if (status.failed_notifications_24h > 0) {
    banners.append(banner("warn",
      `${status.failed_notifications_24h} melding(en) konden het afgelopen etmaal ` +
      "niet verstuurd worden. Zolang dit speelt kun je niet op de alarmering vertrouwen."));
  }
  const selftest = status.selftest;
  if (selftest && selftest.warning) {
    banners.append(banner("warn",
      `Meldpad onbevestigd: ${selftest.state}. Stuur een testmelding via het tabblad Belpad.`));
  }
  if (!status.matrix_enabled && !status.pushover_enabled) {
    banners.append(banner("bad",
      "Geen meldkanaal aan (Matrix en Pushover staan uit): er worden geen meldingen " +
      "verstuurd en je telefoon gaat niet bij een alarm."));
  }
}

function banner(kind, text) {
  return el("div", `banner ${kind}`, text);
}

/* ── Openstaande alarmen ───────────────────────────────────────────────── */

async function renderAlarms() {
  const { alarms } = await api("/api/alarms");
  const panel = $("#alarm-panel");
  const list = $("#alarm-list");
  panel.hidden = alarms.length === 0;
  list.textContent = "";

  alarms.forEach((alarm) => {
    const row = el("div", "alarm-row");
    const left = el("div");
    left.append(el("div", "title", `${ICONS[alarm.severity] || "•"} ${alarm.title} — ${alarm.device_name}`));
    const calls = alarm.calls || [];
    const sent = calls.filter((c) => c.status === "sent").length;
    const failed = calls.filter((c) => c.status === "failed").length;
    left.append(el("div", "calls",
      `${formatTime(alarm.received_at)} · ${sent} oproep(en) verstuurd` +
      (failed ? `, ${failed} mislukt` : "")));
    const button = el("button", "danger", "Bevestigen");
    button.addEventListener("click", async () => {
      button.disabled = true;
      await api(`/api/events/${alarm.id}/acknowledge`, { method: "POST" });
      await Promise.all([refreshStatus(), renderAlarms()]);
    });
    row.append(left, button);
    list.append(row);
  });
}

$("#ack-all").addEventListener("click", async () => {
  await api("/api/alarms/acknowledge-all", { method: "POST" });
  await Promise.all([refreshStatus(), renderAlarms()]);
});

/* ── Logboek ───────────────────────────────────────────────────────────── */

function eventNode(event, isNew) {
  const node = el("div", `event sev-${event.severity}${isNew ? " new" : ""}`);
  node.id = `event-${event.id}`;
  node.append(el("div", "icon", ICONS[event.severity] || "•"));

  const summary = event.summary || `${event.title} — ${event.device_name}`;
  node.append(el("div", "title", summary));
  node.append(el("div", "when", formatTime(event.received_at)));

  const bits = [CATEGORY_LABELS[event.category] || event.category, `code ${event.code}`];
  // De groep staat vaak al in de samenvatting; twee keer "Begane grond" op
  // dezelfde regel leest als een fout.
  if (event.partition_name && event.partition_name !== "systeem"
      && !summary.includes(event.partition_name)) {
    bits.push(event.partition_name);
  }
  if (event.source === "internal") bits.push("door de centrale zelf gemeld");
  if (event.source === "test") bits.push("testalarm vanuit het dashboard");
  if (event.acknowledged_at) bits.push(`bevestigd door ${event.acknowledged_by}`);
  node.append(el("div", "meta", bits.join(" · ")));
  return node;
}

async function loadEvents(append = false) {
  if (!append) { state.offset = 0; state.seen.clear(); }
  const params = new URLSearchParams({
    limit: "50",
    offset: String(state.offset),
    include_heartbeat: String(state.showHeartbeat),
  });
  Object.entries(state.filters).forEach(([key, value]) => { if (value) params.set(key, value); });

  const { events } = await api(`/api/events?${params}`);
  const list = $("#events");
  if (!append) list.textContent = "";
  events.forEach((event) => {
    state.seen.add(event.id);
    list.append(eventNode(event, false));
  });
  state.offset += events.length;
  $("#load-more").classList.toggle("hidden", events.length < 50);
  $("#log-count").textContent = `${state.offset} getoond`;
}

$("#refresh").addEventListener("click", () => loadEvents());
$("#filter-heartbeat").addEventListener("change", (event) => {
  state.showHeartbeat = event.target.checked;
  loadEvents();
});
$("#load-more").addEventListener("click", () => loadEvents(true));
["severity", "category"].forEach((name) => {
  $(`#filter-${name}`).addEventListener("change", (event) => {
    state.filters[name] = event.target.value;
    loadEvents();
  });
});

function fillCategoryFilter() {
  const select = $("#filter-category");
  Object.entries(CATEGORY_LABELS).forEach(([value, label]) => {
    const option = el("option", null, label);
    option.value = value;
    select.append(option);
  });
}

/* ── Belpad ────────────────────────────────────────────────────────────── */

const CHANNEL_NAMES = { pushover: "Pushover", matrix: "Matrix / Element X" };

function renderChannels(status) {
  const target = $("#channel-status");
  target.textContent = "";
  const channels = (status.channels || []).map((name) => CHANNEL_NAMES[name] || name);
  if (channels.length === 0) {
    target.append(banner("bad", "Geen meldkanaal aan. Zet Pushover of Matrix aan in config.yaml."));
    return;
  }
  target.append(banner("ok", `Actief meldkanaal: ${channels.join(" en ")}`));
}

function renderSelftest(status) {
  const target = $("#selftest-status");
  target.textContent = "";
  const selftest = status.selftest;
  if (!selftest) {
    target.append(banner("warn", "De zelftest staat uit of er is geen meldkanaal geconfigureerd."));
    return;
  }
  target.append(banner(selftest.warning ? "warn" : "ok", `Status: ${selftest.state}`));
  if (selftest.last) {
    const table = el("table", "kv");
    [
      ["Laatste testmelding", formatTime(selftest.last.started_at)],
      ["Soort", selftest.last.kind === "manual" ? "handmatig" : "gepland"],
      ["Verstuurd", selftest.last.ring_status === "sent" ? "ja" : "nee"],
      ["Bevestigd", selftest.last.acknowledged_at ? formatTime(selftest.last.acknowledged_at) : "nog niet"],
      ["Toelichting", selftest.last.detail || "—"],
    ].forEach(([key, value]) => {
      const row = el("tr");
      row.append(el("td", null, key), el("td", null, value));
      table.append(row);
    });
    target.append(table);
  }
}

$("#test-ring").addEventListener("click", async (event) => {
  const button = event.target;
  button.disabled = true;
  button.textContent = "Bezig met versturen…";
  try {
    const result = await api("/api/selftest/ring", { method: "POST" });
    button.textContent = result.ok ? "Verstuurd — gaat je telefoon?" : "Versturen mislukt";
  } catch (exc) {
    button.textContent = `Mislukt: ${exc.message}`;
  }
  await refreshStatus();
  setTimeout(() => { button.disabled = false; button.textContent = "Testmelding versturen"; }, 4000);
});

$("#test-ack").addEventListener("click", async () => {
  await api("/api/selftest/acknowledge", { method: "POST" });
  await refreshStatus();
});

/* ── Testalarm ─────────────────────────────────────────────────────────── */

const TEST_ALARM_LABELS = { fire: "Brandalarm", burglary: "Inbraakalarm" };

function describeNotifications(event) {
  const notes = event.notifications || [];
  if (!notes.length) return "nog niets verstuurd";
  const failed = notes.filter((n) => n.status === "failed");
  const sent = notes.filter((n) => n.status === "sent");
  const parts = [];
  if (sent.length) parts.push(`verstuurd via ${[...new Set(sent.map((n) => CHANNEL_NAMES[n.channel] || n.channel))].join(", ")}`);
  if (failed.length) parts.push(`mislukt via ${[...new Set(failed.map((n) => CHANNEL_NAMES[n.channel] || n.channel))].join(", ")}`);
  if (notes.some((n) => n.status === "expired")) parts.push("verlopen op de telefoon");
  return parts.join("; ") || "onbekend";
}

// Alleen melders van het gekozen soort: een rookmelder hoort niet bij een
// inbraaktest. Apparaten zonder ingesteld soort blijven kiesbaar, met een hint
// dat je ze in config.yaml onder device_types kunt indelen.
function fillTestDevices() {
  const kind = $("#test-alarm-kind").value;
  const select = $("#test-alarm-device");
  const current = select.value;
  select.textContent = "";
  const none = el("option", null, "Testmelder (geen apparaat)");
  none.value = "";
  select.append(none);
  (state.testDevices || []).forEach((device) => {
    if (device.type && device.type !== kind) return;
    const hint = device.type ? "" : " — soort niet ingesteld";
    const option = el("option", null, `${device.name} (${device.id})${hint}`);
    option.value = device.id;
    select.append(option);
  });
  select.value = [...select.options].some((o) => o.value === current) ? current : "";
}

$("#test-alarm-kind").addEventListener("change", fillTestDevices);

async function renderTestAlarms() {
  const data = await api("/api/selftest/alarms");

  state.testDevices = data.devices || [];
  fillTestDevices();

  const target = $("#test-alarm-list");
  target.textContent = "";
  const alarms = data.alarms || [];
  if (!alarms.length) {
    target.append(el("p", "muted", "Nog geen testalarm gestuurd."));
    return;
  }
  const table = el("table", "kv");
  alarms.forEach((event) => {
    const status = event.acknowledged_at
      ? `bevestigd door ${event.acknowledged_by} om ${formatTime(event.acknowledged_at)}`
      : "NOG NIET BEVESTIGD";
    const row = el("tr");
    row.append(
      el("td", null, `${formatTime(event.received_at)} · ${event.summary}`),
      el("td", null, `${describeNotifications(event)}; ${status}`),
    );
    table.append(row);
  });
  target.append(table);
}

$("#test-alarm").addEventListener("click", async (event) => {
  const button = event.target;
  const kind = $("#test-alarm-kind").value;
  const label = TEST_ALARM_LABELS[kind] || kind;
  const ok = window.confirm(
    `${label} als test sturen? Je telefoon krijgt een echte noodmelding met sirene ` +
    "die blijft herhalen tot je hem bevestigt.");
  if (!ok) return;

  const status = $("#test-alarm-status");
  status.textContent = "";
  button.disabled = true;
  button.textContent = "Bezig met versturen…";
  try {
    const result = await api("/api/selftest/alarm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, device_id: $("#test-alarm-device").value || null }),
    });
    status.append(banner("warn",
      `${result.event.summary} is onderweg. Bevestig hem in de Pushover-app of bij de open alarmen.`));
  } catch (exc) {
    status.append(banner("bad", `Testalarm mislukt: ${exc.message}`));
  }
  // De pijplijn verwerkt het event los van dit verzoek; even wachten zodat
  // het logboek en de meldingen er al staan als de lijst ververst.
  setTimeout(async () => {
    await Promise.all([renderTestAlarms(), renderAlarms()]);
    button.disabled = false;
    button.textContent = "Testalarm sturen";
  }, 1500);
});

/* ── Diagnostiek ───────────────────────────────────────────────────────── */

async function loadDiagnostics() {
  const data = await api("/api/diagnostics");
  const summary = $("#diag-summary");
  summary.textContent = "";

  const table = el("table", "kv");
  const counters = data.counters || {};
  const rows = [
    ["Luistert op", `${data.sia.host}:${data.sia.port} (${data.sia.protocol.toUpperCase()})`],
    ["Objectnummer", data.sia.account_id],
    ["Versleuteld", data.sia.encrypted ? "ja" : "nee — sterk afgeraden"],
    ["Ping-interval", `${data.sia.ping_interval_seconds} sec`],
    ["Meldkanalen", (data.channels || []).map((n) => CHANNEL_NAMES[n] || n).join(", ") || "geen"],
    ...(data.ring_variants && data.ring_variants.length
      ? [["Matrix-belvarianten", data.ring_variants.join(", ")]] : []),
    ["Berichten ontvangen", counters.events ?? 0],
    ["Waarvan geldig", counters.valid_events ?? 0],
    ["Afgekeurd op objectnummer", counters.error_account ?? 0],
    ["Afgekeurd op CRC", counters.error_crc ?? 0],
    ["Afgekeurd op formaat", counters.error_format ?? 0],
    ["Afgekeurd op tijdstempel", counters.error_timestamp ?? 0],
  ];
  rows.forEach(([key, value]) => {
    const row = el("tr");
    row.append(el("td", null, key), el("td", null, String(value)));
    table.append(row);
  });
  summary.append(table);

  if (counters.error_account) {
    summary.append(banner("warn",
      "Er zijn berichten geweigerd op het objectnummer. Controleer of het " +
      "objectnummer in de Ajax-app exact overeenkomt met dat hierboven."));
  }
  if (counters.error_format || counters.error_crc) {
    summary.append(banner("warn",
      "Er zijn berichten geweigerd op formaat of CRC. Dat wijst meestal op een " +
      "encryptiesleutel die niet overeenkomt met die in de Ajax-app."));
  }

  const frames = $("#raw-frames");
  frames.textContent = "";
  (data.raw_frames || []).forEach((frame) => {
    const node = el("div", `frame${frame.accepted ? "" : " rejected"}`);
    node.append(el("div", "frame-head",
      `${formatTime(frame.at)} · antwoord: ${frame.response}`));
    node.append(document.createTextNode(frame.line));
    frames.append(node);
  });
  if (!(data.raw_frames || []).length) {
    frames.append(el("p", "muted", "Nog geen berichten ontvangen."));
  }
}

/* ── Tabbladen ─────────────────────────────────────────────────────────── */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.dataset.panel !== tab.dataset.tab);
    });
    if (tab.dataset.tab === "diag") loadDiagnostics();
    if (tab.dataset.tab === "test") renderTestAlarms();
  });
});

/* ── Live verbinding ───────────────────────────────────────────────────── */

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/ws`);
  state.socket = socket;

  socket.addEventListener("open", () => {
    state.backoff = 1000;
    setConnection("live", "pill-ok");
  });

  socket.addEventListener("message", (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === "event") {
      const list = $("#events");
      const hidden = payload.data.severity === "heartbeat" && !state.showHeartbeat;
      if (!hidden && !state.seen.has(payload.data.id)) {
        state.seen.add(payload.data.id);
        list.prepend(eventNode(payload.data, true));
      }
      if (payload.data.severity === "alarm") renderAlarms();
      if (payload.data.source === "test") renderTestAlarms();
    }
    if (payload.status) { renderTiles(payload.status); renderBanners(payload.status); }
  });

  socket.addEventListener("close", () => {
    setConnection("verbinding weg", "pill-bad");
    // Oplopend opnieuw proberen, maar nooit opgeven: dit scherm hoort
    // vanzelf bij te trekken zodra het netwerk terug is.
    setTimeout(connect, state.backoff);
    state.backoff = Math.min(state.backoff * 2, 30000);
  });
}

function setConnection(text, cls) {
  const pill = $("#connection");
  pill.textContent = text;
  pill.className = `pill ${cls}`;
}

/* ── Opstarten ─────────────────────────────────────────────────────────── */

async function refreshStatus() {
  const status = await api("/api/status");
  renderTiles(status);
  renderBanners(status);
  renderChannels(status);
  renderSelftest(status);
  return status;
}

async function start() {
  fillCategoryFilter();
  await refreshStatus();
  await Promise.all([loadEvents(), renderAlarms()]);
  connect();
  // Vangnet voor het geval de WebSocket stilvalt zonder close-event.
  setInterval(refreshStatus, 30000);
}

(async () => {
  const session = await (await fetch("/api/session", { credentials: "same-origin" })).json();
  if (session.authenticated) {
    showApp();
    await start();
  } else {
    showLogin(session.password_set
      ? "Log in om verder te gaan."
      : "Er is nog geen wachtwoord ingesteld. Zet AJAXCENTRAL_WEB_PASSWORD_HASH in .env.");
  }
})();
