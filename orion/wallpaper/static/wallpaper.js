"use strict";
// Fond d'écran d'ORION : lecture seule de /stats (chaque seconde), horloge locale, visage d'ORION au centre.

const $ = (id) => document.getElementById(id);
const DAYS = ["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"];
const MONTHS = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre",
  "Novembre", "Décembre"];
const pad = (n) => String(n).padStart(2, "0");

// --- Charte de couleurs d'ORION (palette de base du visage : vert forêt, sauge, crème, doré sur noir) -------
const ACCENT = [128, 186, 160], SECOND = [201, 164, 92];
const rgb = (c) => `rgb(${c.join(",")})`, rgba = (c, a) => `rgba(${c.join(",")},${a})`;

// --- Mise à l'échelle du plateau 1920 x 1080 -------------------------------------------------------------
function fit() {
  const s = Math.min(innerWidth / 1920, innerHeight / 1080);
  $("stage").style.transform = `scale(${s}) translate(-960px, -540px)`;
  const c = $("particles");
  c.width = innerWidth; c.height = innerHeight;
}
addEventListener("resize", fit);

// --- Fond : particules lentes (30 images/s au plus, très peu de calcul) ------------------------------------
const dots = Array.from({ length: 70 }, () => ({ x: Math.random(), y: Math.random(), r: Math.random() * 1.8 + 0.4,
  vx: (Math.random() - 0.5) * 0.00012, vy: (Math.random() - 0.5) * 0.00012, a: Math.random() * 0.6 + 0.2 }));
let lastFrame = 0;
function particles(t) {
  requestAnimationFrame(particles);
  if (t - lastFrame < 33) return;
  lastFrame = t;
  const c = $("particles"), g = c.getContext("2d");
  g.clearRect(0, 0, c.width, c.height);
  for (const d of dots) {
    d.x = (d.x + d.vx + 1) % 1; d.y = (d.y + d.vy + 1) % 1;
    g.beginPath(); g.arc(d.x * c.width, d.y * c.height, d.r, 0, Math.PI * 2);
    g.fillStyle = rgba(ACCENT, d.a * (0.6 + 0.4 * Math.sin(t / 900 + d.x * 9))); g.fill();
  }
}

// --- Circuits décoratifs (traits qui relient les panneaux) --------------------------------------------------
function circuits() {
  const paths = [
    "M380 230 H520 L600 310 H640", "M380 470 H470 L540 540 H610", "M460 820 H530 L600 750 H650",
    "M1540 230 H1400 L1320 310 H1280", "M1460 560 H1390 L1320 490 H1290", "M1460 820 H1390 L1320 760 H1270",
    "M960 140 V128", "M960 940 V948", "M0 1000 H40", "M1880 1000 H1920",
  ];
  const svg = $("circuits");
  svg.innerHTML = paths.map((d, i) => `<path d="${d}"/><path class="pulse" d="${d}" style="animation-delay:-${i * 0.7}s"/>`)
    .join("") + paths.map((d) => { const m = d.match(/M(\d+) (\d+)/); return `<circle cx="${m[1]}" cy="${m[2]}" r="3"/>`; })
    .join("");
  const ticks = [];
  for (let i = 0; i < 120; i++) {
    const a = (i / 120) * Math.PI * 2, major = i % 10 === 0, r1 = 318, r2 = major ? 300 : 310;
    ticks.push(`<line class="${major ? "major" : ""}" x1="${400 + r1 * Math.cos(a)}" y1="${400 + r1 * Math.sin(a)}" ` +
      `x2="${400 + r2 * Math.cos(a)}" y2="${400 + r2 * Math.sin(a)}"/>`);
  }
  $("ticks").innerHTML = ticks.join("");
}

// --- Horloge et calendrier (heure du PC) ------------------------------------------------------------------
function clock() {
  const now = new Date();
  $("clock").textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  $("hms").textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  $("clock-sub").textContent = `${DAYS[now.getDay()]} · S${week(now)}`;
  $("clock-arc").style.strokeDasharray = `${(now.getSeconds() + now.getMilliseconds() / 1000) / 60 * 100} 100`;
  $("weekday").textContent = DAYS[now.getDay()];
  $("daynum").textContent = now.getDate();
  $("month").textContent = `${MONTHS[now.getMonth()]} ${now.getFullYear()}`;
  const last = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  $("date-arc").style.strokeDasharray = `${now.getDate() / last * 100} 100`;
  if ($("days").dataset.day !== String(now.getDate())) {
    $("days").dataset.day = now.getDate();
    $("days").innerHTML = Array.from({ length: last }, (_, i) => {
      const d = new Date(now.getFullYear(), now.getMonth(), i + 1), we = d.getDay() === 0 || d.getDay() === 6;
      return `<span class="${i + 1 === now.getDate() ? "today" : we ? "weekend" : ""}">${pad(i + 1)}</span>`;
    }).join("");
  }
}
function week(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  return Math.ceil(((t - new Date(Date.UTC(t.getUTCFullYear(), 0, 1))) / 86400000 + 1) / 7);
}

// --- Formats ---------------------------------------------------------------------------------------------
const gb = (b) => (b / 1e9).toFixed(b > 1e11 ? 0 : 1);
function rate(b) {
  if (b == null) return "–";
  if (b > 1e6) return `${(b / 1e6).toFixed(1)} Mo/s`;
  if (b > 1e3) return `${(b / 1e3).toFixed(1)} ko/s`;
  return `${b} o/s`;
}
function duration(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} j ${h} h` : h ? `${h} h ${pad(m)}` : `${m} min`;
}
function soon(s) {
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${pad(Math.floor(s % 3600 / 60))}`;
}
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function gauge(id, percent, label, extra, warnAt = 75, badAt = 90) {
  const el = $(id);
  el.querySelector(".arc").style.strokeDasharray = `${Math.max(0, Math.min(100, percent ?? 0))} 100`;
  // Unités dans la police du système : dans Michroma, « % » et « ° » ressemblent à « o/o » et « o ».
  const m = String(label).match(/^(-?[\d.]+)(\D*)$/);
  el.querySelector("b").innerHTML = m ? `${m[1]}<small class="u">${esc(m[2])}</small>` : esc(label);
  el.querySelector("em").textContent = extra || "";
  el.classList.toggle("hot", percent >= warnAt && percent < badAt);
  el.classList.toggle("bad", percent >= badAt);
}

function graph(canvas, series, colors, max) {
  const g = canvas.getContext("2d"), w = canvas.width, h = canvas.height;
  g.clearRect(0, 0, w, h);
  g.strokeStyle = rgba(ACCENT, 0.08); g.lineWidth = 1;
  for (let y = 0; y <= h; y += h / 4) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  const top = max || Math.max(1, ...series.flat());
  series.forEach((points, k) => {
    if (!points.length) return;
    g.beginPath();
    points.forEach((v, i) => {
      const x = w - (points.length - 1 - i) * (w / 89), y = h - (v / top) * (h - 4) - 2;
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    });
    g.strokeStyle = colors[k]; g.lineWidth = 1.6; g.shadowColor = colors[k]; g.shadowBlur = 6; g.stroke();
    g.shadowBlur = 0;
  });
}

// --- Icônes météo (traits seulement) ----------------------------------------------------------------------
function icon(slug) {
  const sun = '<circle cx="32" cy="32" r="10"/><path d="M32 8v7M32 49v7M8 32h7M49 32h7M15 15l5 5M44 44l5 5M15 49l5-5M44 20l5-5"/>';
  const cloud = '<path d="M20 46h26a10 10 0 0 0 0-20 14 14 0 0 0-27 3 9 9 0 0 0 1 17z"/>';
  const rain = cloud + '<path d="M24 52l-3 6M33 52l-3 6M42 52l-3 6"/>';
  const snow = cloud + '<path d="M24 54h.1M33 56h.1M42 54h.1"/>';
  const storm = cloud + '<path d="M34 48l-5 8h7l-5 8"/>';
  const fog = '<path d="M12 28h40M8 36h48M14 44h36"/>';
  const partly = '<circle cx="24" cy="22" r="8"/><path d="M24 6v4M8 22h4M12 10l3 3M36 10l-3 3"/>' + cloud;
  const s = slug || "";
  const body = s.includes("thunder") ? storm : s.includes("snow") ? snow : /rain|drizzle|shower/.test(s) ? rain
    : s === "fog" ? fog : s === "overcast" ? cloud : s === "partly_cloudy" || s === "mainly_clear" ? partly : sun;
  return `<svg class="wi" viewBox="0 0 64 64">${body}</svg>`;
}

// --- Orbite : appareils et services autour du visage ------------------------------------------------------
function orbit(nodes) {
  const center = 400, radius = 395, el = $("orbit");
  const left = nodes.filter((n, i) => i % 2 === 0), right = nodes.filter((n, i) => i % 2 === 1);
  const place = (list, side) => list.map((n, i) => {
    const spread = 100, a = (side === "left" ? 180 : 0) + (i - (list.length - 1) / 2) * (spread / Math.max(1, list.length - 1 || 1));
    const rad = a * Math.PI / 180, x = center + radius * Math.cos(rad), y = center - radius * Math.sin(rad) * (side === "left" ? -1 : 1);
    return `<div class="node ${side === "right" ? "right" : ""} ${n.state}" style="left:${x}px;top:${y}px">` +
      `<span>${esc(n.label)}</span><i></i></div>`;
  }).join("");
  el.innerHTML = place(left, "left") + place(right, "right");
}

// --- Machines connectées (PC fixe, PC portable) -----------------------------------------------------------
function bar(label, percent, text, warnAt = 75, badAt = 90) {
  const p = percent == null ? 0 : Math.max(0, Math.min(100, percent));
  const level = percent >= badAt ? "bad" : percent >= warnAt ? "hot" : "";
  return `<div class="bar ${level}"><span>${label}</span><i style="--v:${p}%"></i><b>${text}</b></div>`;
}
function machine(m) {
  const name = esc(m.name.replace(/^votre /i, "").replace(/^./, (c) => c.toUpperCase()));
  if (!m.online) return `<div class="machine off"><h3><i class="dot"></i>${name}<small>hors ligne</small></h3>` +
    `<div class="off-note">Éteint ou agent arrêté</div></div>`;
  const up = m.uptime_hours == null ? "" : `allumé ${duration(m.uptime_hours * 3600)}`;
  const g = m.gpu || {};
  const gpuText = g.usage == null ? esc((g.name || "–").replace(/NVIDIA GeForce /, "")) :
    `${Math.round(g.usage)} %${g.temperature != null ? ` · ${Math.round(g.temperature)}°` : ""}`;
  const vram = g.memory_total_mb ? ` · ${(g.memory_used_mb / 1024).toFixed(1)}/${Math.round(g.memory_total_mb / 1024)} Go` : "";
  return `<div class="machine"><h3><i class="dot"></i>${name}<small>${up}</small></h3>` +
    bar("CPU", m.cpu_percent, m.cpu_percent == null ? "–" : `${Math.round(m.cpu_percent)} %`) +
    bar("RAM", m.ram_percent, m.ram_percent == null ? "–" : `${m.ram_percent} % · ${Math.round(m.ram_total)} Go`) +
    bar("GPU", g.usage, gpuText + (g.usage != null ? vram : "")) +
    bar("Disque", m.disk_total ? 100 - m.disk_free / m.disk_total * 100 : null,
        m.disk_free == null ? "–" : `${Math.round(m.disk_free)} Go libres`, 85, 95) + "</div>";
}

// --- Mise à jour depuis /stats ----------------------------------------------------------------------------
function val(stats, name) { return stats[name] && stats[name].value; }
function stale(id, stats, name) { const el = $(id); if (el && stats[name]) el.classList.toggle("stale", !stats[name].ok); }

let latest = {};
function render(stats) {
  latest = stats;
  if (typeof refreshDetail === "function") refreshDetail();
  const core = val(stats, "core");
  if (core) {
    gauge("g-cpu", core.cpu, core.cpu == null ? "–" : `${Math.round(core.cpu)}%`, `${core.cores} cœurs`);
    const ram = core.ram_used / core.ram_total * 100;
    gauge("g-ram", ram, `${Math.round(ram)}%`, `${gb(core.ram_used)} / ${gb(core.ram_total)} Go`);
    const disk = core.disk_used / core.disk_total * 100;
    gauge("g-disk", disk, `${Math.round(disk)}%`, `${gb(core.disk_total - core.disk_used)} Go libres`, 85, 95);
    gauge("g-temp", core.temperature, core.temperature == null ? "–" : `${Math.round(core.temperature)}°`, "processeur", 70, 85);
    $("load").textContent = core.load.join(" · ");
    $("uptime").textContent = duration(core.uptime);
    graph($("cpu-graph"), [core.cpu_history], [rgb(ACCENT)], 100);
    graph($("net-graph"), [core.rx_history, core.tx_history], [rgb(ACCENT), rgb(SECOND)]);
    $("rx").textContent = rate(core.rx); $("tx").textContent = rate(core.tx);
  }
  const orion = val(stats, "orion");
  const katana = val(stats, "katana");
  if (orion) {
    $("orion-uptime").textContent = duration(orion.uptime);
    const llm = $("llm"), state = orion.llm.state;
    llm.textContent = { ONLINE: "Katana en ligne", DEGRADED: "Katana ralenti", OFFLINE: "Secours local" }[state] || state;
    llm.className = state === "ONLINE" ? "ok" : state === "DEGRADED" ? "warn" : "bad";
    $("llm-model").textContent = (orion.llm.model || "").split("/").pop();
    const stt = $("stt");
    stt.textContent = orion.stt.remote ? (orion.stt.remote_up ? "GPU · large-v3" : `Local · ${orion.stt.model}`)
      : `Local · ${orion.stt.model}`;
    stt.className = orion.stt.remote && orion.stt.remote_up ? "ok" : orion.stt.remote ? "warn" : "";
    $("wake").textContent = `« ${orion.wake_word} »`;
  }
  const pcs = val(stats, "pcs");
  if (pcs) $("pcs").innerHTML = pcs.map((m, i) => machine(m).replace('<div class="machine',
    `<div data-pc="${i}" class="clickable machine`)).join("");
  stale("machines", stats, "pcs");

  // Orbite : appareils, puis services d'ORION
  const nodes = [];
  const short = (name) => name.replace(/^votre /i, "").replace(/^./, (c) => c.toUpperCase());
  for (const d of val(stats, "devices") || []) nodes.push({ label: d.core ? "Cœur ORION" : short(d.name), state: d.online ? "" : "off" });
  if (orion) {
    nodes.push({ label: "Réflexion", state: orion.llm.state === "ONLINE" ? "" : "off" });
    nodes.push({ label: "Transcription", state: !orion.stt.remote ? "idle" : orion.stt.remote_up ? "" : "off" });
  }
  const music = val(stats, "music");
  if (stats.music) nodes.push({ label: "Spotify", state: music && music.playing ? "" : "idle" });
  if (stats.weather) nodes.push({ label: "Météo", state: stats.weather.ok ? "" : "off" });
  const key = JSON.stringify(nodes);
  if ($("orbit").dataset.key !== key) { $("orbit").dataset.key = key; orbit(nodes); }

  // Musique
  $("music").classList.toggle("playing", !!(music && music.playing));
  $("play").innerHTML = music && music.playing ? "&#9208;" : "&#9654;";
  $("track").textContent = music && music.track ? music.track : "Aucune musique";
  $("artist").textContent = music && music.track ? music.artist : "";
  $("progress").firstElementChild.style.width = music && music.duration ? `${music.progress / music.duration * 100}%` : "0";

  // Maison
  const presence = val(stats, "presence");
  if (presence) {
    const house = { occupied: "Quelqu'un est à la maison", empty: "Maison vide" }[presence.house] || `Maison : ${presence.house || "inconnu"}`;
    $("house").textContent = house + (presence.doors_open ? ` · ${presence.doors_open} porte${presence.doors_open > 1 ? "s" : ""} ouverte${presence.doors_open > 1 ? "s" : ""}` : "");
  }
  const lights = val(stats, "lights");
  if (lights) $("lights").innerHTML = lights.slice(0, 6).map((l) => `<li data-room="${esc(l.key || "")}" ` +
    `class="${!l.online ? "down" : l.on ? "on" : ""}${l.online && CAN.actions ? " can" : ""}">` +
    `${esc(l.name.replace(/^(la |le |l')/i, ""))}${l.on && l.brightness != null ? ` · ${l.brightness}%` : ""}</li>`).join("");

  // Météo
  const w = val(stats, "weather");
  if (w) {
    $("place").textContent = w.place;
    $("w-icon").innerHTML = icon(w.slug);
    $("w-temp").innerHTML = `${w.temperature}<small class="u">°C</small>`;
    $("w-cond").textContent = w.condition;
    $("w-details").innerHTML = [["Ressenti", w.feels != null ? `${w.feels}°` : "–"], ["Humidité", w.humidity != null ? `${w.humidity} %` : "–"],
      ["Vent", w.wind != null ? `${Math.round(w.wind)} km/h` : "–"]].map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join("");
    $("w-days").innerHTML = w.days.map((d) => `<li>${icon(d.slug)}<div>${esc(d.label)}<small>${esc(d.condition)}` +
      `${d.rain != null ? ` · pluie ${d.rain} %` : ""}</small></div><b>${d.max}° / ${d.min}°</b></li>`).join("");
  }
  stale("weather", stats, "weather");

  // Agenda, rappels, minuteurs
  const agenda = val(stats, "agenda");
  if (agenda) $("agenda").innerHTML = agenda.length ? agenda.slice(0, 3).map((e) =>
    `<li><span class="t">${e.today ? "Auj." : e.day} ${e.time}</span><span class="x">${esc(e.title)}</span></li>`).join("")
    : '<li class="empty">Rien de prévu cette semaine</li>';
  const schedule = val(stats, "schedule");
  if (schedule) $("schedule").innerHTML = schedule.length ? schedule.slice(0, 2).map((s) =>
    `<li><span class="t">${s.at || "dans"} ${soon(s.seconds)}</span><span class="x">${esc(s.label)}</span></li>`).join("")
    : '<li class="empty">Aucun</li>';

  const mail = val(stats, "mail");
  $("mail").textContent = mail && mail.important ? `${mail.important} mail${mail.important > 1 ? "s" : ""} important${mail.important > 1 ? "s" : ""} en attente` : "";
  const offline = (val(stats, "devices") || []).filter((d) => !d.online).map((d) => d.name);
  $("status-line").textContent = offline.length ? `Hors ligne : ${offline.map(short).join(", ")}` : "Tous les appareils sont en ligne";
}

async function poll() {
  try {
    const r = await fetch("stats", { cache: "no-store" });
    if (r.ok) render(await r.json());
  } catch (e) { /* Core redémarré : nouvel essai à la prochaine seconde */ }
  setTimeout(poll, 1000);
}

// --- Interactions : détails, commandes rapides, conversation ------------------------------------------------
const CAN = { token: "", actions: false };
let toastTimer = null, idleTimer = null, detailView = null;

function toast(text, bad = false) {
  const el = $("toast");
  el.textContent = text; el.classList.toggle("bad", bad); el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), 4000);
}

async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Orion-Token": CAN.token },
    body: JSON.stringify(body) });
  let data = {};
  try { data = await r.json(); } catch (e) { /* réponse vide */ }
  return { ok: r.ok, data };
}

async function act(tool, parameters = {}) {
  if (!CAN.actions) return;
  try {
    const { ok, data } = await post("action", { tool, parameters });
    toast(data.message || data.error || (ok ? "C'est fait." : "Action refusée."), !ok || data.success === false);
  } catch (e) { toast("ORION ne répond pas.", true); }
  setTimeout(async () => {
    try { render(await (await fetch("stats", { cache: "no-store" })).json()); } catch (e) { /* prochain relevé */ }
    if (detailView === VIEWS.house) refreshDetail(true);  // fiche des lumières : état réel après l'action
  }, 1500);
}

function openDetail(view) {
  detailView = view;
  $("detail").hidden = false;
  refreshDetail(true);
  clearTimeout(idleTimer); idleTimer = setTimeout(closeDetail, 45000);  // fond d'écran : se referme seul
}
function closeDetail() { detailView = null; $("detail").hidden = true; }

function refreshDetail(force = false) {
  if (!detailView || (detailView.static && !force)) return;
  const [title, body] = detailView.draw(latest);
  $("detail-title").textContent = title;
  $("detail-body").innerHTML = body;
  if (detailView.after) detailView.after();
}

const VIEWS = {
  weather: { draw(s) {
    const w = val(s, "weather");
    if (!w) return ["Météo", "<p>Indisponible pour le moment.</p>"];
    const cell = (p) => `<div class="cell">${icon(p.slug)}<small>${esc(p.label)}</small><b>${p.temperature ?? p.max}°</b>` +
      `<small>${esc(p.condition)}${p.rain != null ? ` · pluie ${p.rain} %` : ""}</small></div>`;
    return [`Météo · ${w.place}`, `<div class="grid">${(w.periods || []).map(cell).join("")}</div>` +
      `<h3>Trois jours</h3><div class="grid">${w.days.map((d) => `<div class="cell">${icon(d.slug)}<small>${esc(d.label)}</small>` +
      `<b>${d.max}° / ${d.min}°</b><small>${esc(d.condition)}</small></div>`).join("")}</div>`];
  } },
  agenda: { draw(s) {
    const a = val(s, "agenda") || [], r = val(s, "schedule") || [];
    return ["Agenda, rappels et minuteurs",
      "<h3>Cette semaine</h3><ul>" + (a.length ? a.map((e) => `<li><span class="t">${e.today ? "Aujourd'hui" : e.day} ${e.time}</span>` +
      `<span>${esc(e.title)}</span></li>`).join("") : '<li class="empty">Rien de prévu</li>') + "</ul>" +
      "<h3>Rappels et minuteurs</h3><ul>" + (r.length ? r.map((x) => `<li><span class="t">${x.at || ""} · ${soon(x.seconds)}</span>` +
      `<span>${esc(x.label)}</span></li>`).join("") : '<li class="empty">Aucun</li>') + "</ul>"];
  } },
  core: { draw(s) {
    const c = val(s, "core");
    if (!c) return ["Cœur · mini-PC", ""];
    return ["Cœur · mini-PC", `<canvas id="detail-cpu" width="640" height="140"></canvas><dl>` +
      `<dt>Processeur</dt><dd>${c.cpu ?? "–"} % · ${c.cores} cœurs</dd><dt>Charge (1, 5, 15 min)</dt><dd>${c.load.join(" · ")}</dd>` +
      `<dt>Mémoire</dt><dd>${gb(c.ram_used)} / ${gb(c.ram_total)} Go</dd><dt>Disque</dt><dd>${gb(c.disk_total - c.disk_used)} Go libres sur ${gb(c.disk_total)}</dd>` +
      `<dt>Température</dt><dd>${c.temperature ?? "–"} °C</dd><dt>Réseau</dt><dd>↓ ${rate(c.rx)} · ↑ ${rate(c.tx)}</dd>` +
      `<dt>En service depuis</dt><dd>${duration(c.uptime)}</dd></dl>`];
  }, after() { const c = val(latest, "core"); if (c) graph($("detail-cpu"), [c.cpu_history], [rgb(ACCENT)], 100); } },
  house: { draw(s) {
    const lights = val(s, "lights") || [], p = val(s, "presence");
    const rows = lights.map((l) => `<div class="room ${l.online ? "" : "down"}"><span>${esc(l.name)}</span>` +
      (CAN.actions && l.online ? `<button class="${l.on ? "on" : ""}" data-toggle="${esc(l.key)}">${l.on ? "Allumée" : "Éteinte"}</button>` +
        `<input type="range" min="10" max="100" step="10" value="${l.brightness ?? 50}" data-room="${esc(l.key)}">` +
        `<b>${l.on && l.brightness != null ? l.brightness + " %" : "–"}</b>` : `<span>${l.online ? (l.on ? "allumée" : "éteinte") : "hors tension"}</span><span></span><span></span>`) +
      "</div>").join("");
    return ["Maison", `<p>${esc($("house").textContent)}</p><h3>Lumières</h3>${rows || "<p>Aucune lumière.</p>"}`];
  }, after() {
    document.querySelectorAll("#detail [data-toggle]").forEach((b) => b.onclick = () => act("light_toggle", { room: b.dataset.toggle }));
    document.querySelectorAll("#detail input[type=range]").forEach((r) => r.onchange = () =>
      act("set_brightness", { room: r.dataset.room, brightness: Number(r.value) }));
  }, static: true },
};

function pcView(i) {
  return { draw(s) {
    const m = (val(s, "pcs") || [])[i];
    if (!m) return ["PC", ""];
    const name = m.name.replace(/^votre /i, "").replace(/^./, (c) => c.toUpperCase());
    if (!m.online) return [name, "<p>Hors ligne : éteint, ou son agent ORION est arrêté.</p>"];
    const g = m.gpu || {};
    return [name, `<dl><dt>Processeur</dt><dd>${esc(m.cpu || "–")}</dd><dt>Charge</dt><dd>${m.cpu_percent ?? "–"} %</dd>` +
      `<dt>Mémoire</dt><dd>${m.ram_percent ?? "–"} % de ${Math.round(m.ram_total)} Go</dd>` +
      `<dt>Carte graphique</dt><dd>${esc(g.name || "–")}</dd>` +
      (g.usage != null ? `<dt>Utilisation</dt><dd>${g.usage} % · ${g.temperature ?? "–"} °C</dd>` : "") +
      (g.memory_total_mb ? `<dt>Mémoire graphique</dt><dd>${(g.memory_used_mb / 1024).toFixed(1)} / ${(g.memory_total_mb / 1024).toFixed(1)} Go</dd>` : "") +
      `<dt>Disque système</dt><dd>${Math.round(m.disk_free)} Go libres sur ${Math.round(m.disk_total)}</dd>` +
      `<dt>Allumé depuis</dt><dd>${duration((m.uptime_hours || 0) * 3600)}</dd></dl>`];
  } };
}

function wire() {
  $("detail").querySelector(".close").onclick = closeDetail;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });
  const views = [["weather", "weather"], ["agenda-panel", "agenda"], ["core-panel", "core"], ["gauges", "core"], ["presence-panel", "house"]];
  for (const [id, name] of views) {
    $(id).classList.add("clickable");
    $(id).addEventListener("click", (e) => {
      if (e.target.closest("[data-room]") && CAN.actions) return;  // clic sur une lumière : commande, pas détail
      openDetail(VIEWS[name]);
    });
  }
  $("lights").addEventListener("click", (e) => {
    const li = e.target.closest("li.can");
    if (li && li.dataset.room) act("light_toggle", { room: li.dataset.room });
  });
  $("pcs").addEventListener("click", (e) => {
    const el = e.target.closest("[data-pc]");
    if (el) openDetail(pcView(Number(el.dataset.pc)));
  });
  $("music").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const music = val(latest, "music");
    act(b.dataset.act === "play" ? (music && music.playing ? "spotify_pause" : "spotify_play") : b.dataset.act);
  });
  // Les anneaux suivent un peu la souris.
  document.addEventListener("mousemove", (e) => {
    const dx = (e.clientX / innerWidth - 0.5) * 16, dy = (e.clientY / innerHeight - 0.5) * 16;
    $("rings").style.transform = `translate(${dx}px, ${dy}px)`;
  });
}

async function start() {
  fit(); circuits(); clock();
  setInterval(clock, 250);
  requestAnimationFrame(particles);
  wire();
  let facePort = 8765;
  try {
    const config = await (await fetch("config", { cache: "no-store" })).json();
    facePort = config.face_port || facePort;
    CAN.token = config.token || "";
    CAN.actions = (config.actions || []).length > 0;
  } catch (e) { /* défaut : lecture seule */ }
  document.querySelector("#music .controls").hidden = !CAN.actions;
  // ?face=<port> : visage joint par un autre port (aperçu à travers un tunnel SSH, par exemple).
  const asked = new URLSearchParams(location.search).get("face");
  if (asked && /^\d{2,5}$/.test(asked)) facePort = Number(asked);
  // Thème de base imposé (le visage seul garde ses thèmes : fêtes, couleur demandée, nuit).
  $("face").src = `${location.protocol}//${location.hostname}:${facePort}/?theme=day`;
  poll();
}
start();
