/* Dashboard van de alarmcentrale.
 *
 * Bewust zonder framework en zonder buildstap: dit draait op een Raspberry Pi
 * die jaren moet meegaan, en een npm-boom die over twee jaar niet meer bouwt is
 * hier een reëel risico. Tijden worden in de browser omgezet, zodat de
 * weergave altijd in jouw lokale tijd staat zonder serverconfiguratie.
 */

// Lijniconen (24x24, stroke). Geen emoji: die verschillen per toestel en
// kleuren niet mee met de ernst.
const ICON_PATHS = {
  alarm: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  trouble: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  restore: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  heartbeat: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  unknown: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
  shieldOff: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 9 6 6"/><path d="m15 9-6 6"/>',
};

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `ico ico-${name}${cls ? ` ${cls}` : ""}`);
  svg.innerHTML = ICON_PATHS[name] || ICON_PATHS.unknown;
  return svg;
}

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
  socket: null, backoff: 1000, showHeartbeat: false, lastStatus: null,
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

function formatRelative(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 45) return "zojuist";
  if (seconds < 90) return "1 min geleden";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min geleden`;
  if (seconds < 7200) return "1 uur geleden";
  if (seconds < 86400) return `${Math.round(seconds / 3600)} uur geleden`;
  const days = Math.round(seconds / 86400);
  return days === 1 ? "gisteren" : `${days} dagen geleden`;
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
      partition.changed_at
        ? `sinds ${formatTime(partition.changed_at)} (${formatRelative(partition.changed_at)})`
        : "nog geen wijziging gezien",
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

/* ── Statusbalk ────────────────────────────────────────────────────────── */

// Eén samengesteld oordeel, van ernstig naar gerust: alarm, buiten dienst,
// beperkt, in orde. Alles wat hieronder als reden telt, staat ook ergens
// anders op het scherm; dit is de samenvatting die je van een afstand leest.
function renderOverall(status) {
  const bar = $("#overall");
  const iconBox = $("#overall-icon");
  const title = $("#overall-title");
  const detail = $("#overall-detail");
  iconBox.textContent = "";

  const limits = [];
  const troubles = status.troubles || [];
  if (troubles.length) limits.push(`${troubles.length} storing(en): ${troubles.map((t) => t.title).join(", ")}`);
  if (status.failed_notifications_24h > 0) limits.push(`${status.failed_notifications_24h} melding(en) niet verstuurd in het afgelopen etmaal`);
  if (status.selftest && status.selftest.warning) limits.push(`zelftest: ${status.selftest.state}`);

  let level, text, sub, iconName;
  if (status.open_alarms > 0) {
    level = "alarm"; iconName = "alarm";
    const last = status.last_alarm;
    text = status.open_alarms === 1 ? "ALARM" : `ALARM — ${status.open_alarms} openstaand`;
    sub = last ? `${last.summary} · ${formatRelative(last.received_at)}` : "bevestig hieronder";
  } else if (!status.hub_online) {
    level = "bad"; iconName = "shieldOff";
    text = "Buiten dienst";
    sub = `De hub is niet bereikbaar; laatste bericht ${formatDuration(status.seconds_since_contact)} geleden.`;
  } else if (!(status.channels || []).length) {
    level = "bad"; iconName = "shieldOff";
    text = "Buiten dienst";
    sub = "Er staat geen meldkanaal aan; bij een alarm gaat je telefoon niet.";
  } else if (limits.length) {
    level = "warn"; iconName = "trouble";
    text = "Beperkt";
    sub = limits.join(" · ");
  } else {
    level = "ok"; iconName = "shield";
    text = "Alles in orde";
    const parts = [
      status.any_armed ? "ingeschakeld" : "uitgeschakeld",
      `hub verbonden`,
      `meldkanaal ${(status.channels || []).map((n) => CHANNEL_NAMES[n] || n).join(" en ")}`,
    ];
    if (status.selftest && status.selftest.last && status.selftest.last.acknowledged_at) {
      parts.push(`zelftest bevestigd ${formatRelative(status.selftest.last.acknowledged_at)}`);
    }
    sub = parts.join(" · ");
  }

  bar.className = `status-bar ${level}`;
  iconBox.append(icon(iconName));
  title.textContent = text;
  detail.textContent = sub;
  document.title = level === "alarm" ? "ALARM — Alarmcentrale" : "Ajax Alarmcentrale";
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
      `Meldpad onbevestigd: ${selftest.state}. Stuur een testmelding via het tabblad Meldingen.`));
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
    const title = el("div", "title");
    title.append(icon(alarm.severity), document.createTextNode(` ${alarm.summary || `${alarm.title} — ${alarm.device_name}`}`));
    left.append(title);
    const phone = describeDelivery(alarm);
    left.append(el("div", `calls${phone.bad ? " bad" : ""}`,
      `${formatTime(alarm.received_at)} (${formatRelative(alarm.received_at)}) · ${phone.text}`));
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

// Wat er met de melding naar je telefoon gebeurd is. Bij Pushover is één
// noodmelding genoeg: de telefoon herhaalt zelf tot je bevestigt. Bij Matrix
// belt de centrale steeds opnieuw, en telt het aantal pogingen wel.
function describeDelivery(alarm) {
  const calls = alarm.calls || [];
  const notes = alarm.notifications || [];
  const sentCalls = calls.filter((c) => c.status === "sent");
  const failedCalls = calls.filter((c) => c.status === "failed");
  const pushover = sentCalls.some((c) => c.variants === "pushover");
  const rings = sentCalls.filter((c) => c.variants !== "pushover").length;
  if (notes.some((n) => n.status === "expired")) {
    return { text: "noodmelding verlopen zonder bevestiging op de telefoon", bad: true };
  }
  const parts = [];
  if (pushover) parts.push("noodmelding op je telefoon, wacht op bevestiging");
  if (rings) parts.push(`${rings} belpoging(en) via Matrix`);
  if (failedCalls.length) parts.push(`${failedCalls.length} poging(en) mislukt`);
  if (!parts.length) {
    const sent = notes.some((n) => n.status === "sent");
    parts.push(sent ? "bericht verstuurd" : "nog geen melding verstuurd");
    return { text: parts.join(", "), bad: !sent };
  }
  return { text: parts.join(", "), bad: !pushover && !rings };
}

$("#ack-all").addEventListener("click", async () => {
  await api("/api/alarms/acknowledge-all", { method: "POST" });
  await Promise.all([refreshStatus(), renderAlarms()]);
});

/* ── Logboek ───────────────────────────────────────────────────────────── */

function eventNode(event, isNew) {
  const node = el("div", `event sev-${event.severity}${isNew ? " new" : ""}`);
  node.id = `event-${event.id}`;
  const iconBox = el("div", "icon");
  iconBox.append(icon(event.severity));
  node.append(iconBox);

  const summary = event.summary || `${event.title} — ${event.device_name}`;
  node.append(el("div", "title", summary));
  const when = el("div", "when", formatTime(event.received_at));
  when.title = `${new Date(event.received_at).toLocaleString("nl-NL")} · ${formatRelative(event.received_at)}`;
  node.append(when);

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

// Datumvelden zijn lokale dagen; de API wil tijdstippen. "Tot en met" 5 maart
// is dus tot 6 maart 00:00 lokale tijd.
$("#filter-since").addEventListener("change", (event) => {
  state.filters.since = event.target.value ? new Date(`${event.target.value}T00:00`).toISOString() : "";
  loadEvents();
});
$("#filter-until").addEventListener("change", (event) => {
  if (!event.target.value) { state.filters.until = ""; return loadEvents(); }
  const next = new Date(`${event.target.value}T00:00`);
  next.setDate(next.getDate() + 1);
  state.filters.until = next.toISOString();
  return loadEvents();
});

$("#export").addEventListener("click", () => {
  const params = new URLSearchParams({ include_heartbeat: String(state.showHeartbeat) });
  Object.entries(state.filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  // Gewone navigatie: de browser stuurt de sessiecookie mee en slaat het
  // bestand op zoals hij dat met elke download doet.
  window.location.assign(`/api/events.csv?${params}`);
});

function fillCategoryFilter() {
  const select = $("#filter-category");
  Object.entries(CATEGORY_LABELS).forEach(([value, label]) => {
    const option = el("option", null, label);
    option.value = value;
    select.append(option);
  });
}

/* ── Meldingen ─────────────────────────────────────────────────────────── */

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
    if (payload.status) {
      // De WebSocket-status kent de kanalen en de zelftest niet; die halen we
      // met de volgende refreshStatus op. Tot die tijd de bekende waarden
      // hergebruiken, anders flitst de balk kort naar "buiten dienst".
      const merged = { ...(state.lastStatus || {}), ...payload.status };
      state.lastStatus = merged;
      renderOverall(merged); renderTiles(merged); renderBanners(merged);
    }
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
  state.lastStatus = status;
  renderOverall(status);
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
