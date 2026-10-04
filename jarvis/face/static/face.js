/*
 * Visage de JARVIS : rendu canvas piloté uniquement par un état visuel.
 *
 *   JarvisFace.setVisualState("standby" | "listening" | "thinking" | "speaking")
 *   JarvisFace.setAudioLevel(0..1)
 *   JarvisFace.standby()
 *   JarvisFace.setTheme("day" | "night" | "error")
 *
 * Connecté à JARVIS par /events (Server-Sent Events). Sans connexion : veille.
 * Le thème (jour en couleur, nuit en noir et blanc) suit l'horaire du Core, sauf ?theme=day|night.
 * ?demo=1 : démonstration autonome (touches 1-4, A = cycle auto, T = thème). ?debug=1 : état et FPS.
 */
(() => {
  "use strict";

  const TAU = Math.PI * 2;
  const params = new URLSearchParams(location.search);
  const DEMO = params.has("demo");
  const DEBUG = params.has("debug") || DEMO;

  const PROFILES = {
    standby:   { speed: 0.22, glow: 0.40, focus: 0.0, complexity: 0.12, pulseRate: 0.20, pulseDepth: 0.18 },
    listening: { speed: 0.50, glow: 0.68, focus: 1.0, complexity: 0.30, pulseRate: 0.80, pulseDepth: 0.24 },
    thinking:  { speed: 1.60, glow: 0.82, focus: 0.4, complexity: 1.00, pulseRate: 1.60, pulseDepth: 0.12 },
    speaking:  { speed: 0.70, glow: 0.95, focus: 0.6, complexity: 0.45, pulseRate: 0.00, pulseDepth: 0.00 },
  };

  const THEMES = {
    day: {
      deep: [12, 42, 110], blue: [34, 118, 255], cyan: [70, 214, 255], white: [214, 244, 255],
      pupil: [1, 4, 12], pupilEdge: [2, 8, 20], background: ["#061329", "#030a17", "#010308"],
    },
    night: {
      deep: [40, 40, 40], blue: [125, 125, 125], cyan: [205, 205, 205], white: [255, 255, 255],
      pupil: [0, 0, 0], pupilEdge: [3, 3, 3], background: ["#141414", "#080808", "#000000"],
    },
    // Erreur en cours (worker LLM hors ligne, appareil muet...) : rouge, puis retour au thème précédent.
    error: {
      deep: [110, 10, 14], blue: [230, 38, 46], cyan: [255, 110, 100], white: [255, 220, 214],
      pupil: [12, 1, 1], pupilEdge: [22, 3, 3], background: ["#2a0507", "#160204", "#080001"],
    },
  };
  const FORCED_THEME = THEMES[params.get("theme")] ? params.get("theme") : null;
  let theme = FORCED_THEME || "day";
  const COLOR = { ...THEMES[theme] };
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${Math.max(0, Math.min(1, a)).toFixed(3)})`;
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  const now = () => performance.now() / 1000;

  function rng(seed) {
    return () => {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // --- État ------------------------------------------------------------------------------------

  const face = {
    state: "standby",
    target: { ...PROFILES.standby },
    cur: { ...PROFILES.standby },
    level: 0,
    measuredUntil: -1,
    changedAt: now(),
    env: { core: 0, inner: 0, middle: 0, outer: 0 },
    pulsePhase: 0,
    lastMessage: -1,
  };

  function setVisualState(state) {
    const s = String(state || "").toLowerCase();
    if (!PROFILES[s]) return false;
    if (s !== face.state) {
      face.state = s;
      face.changedAt = now();
      face.target = PROFILES[s];
    }
    return true;
  }

  function setAudioLevel(level, measured = true) {
    const v = Number(level);
    face.level = Number.isFinite(v) ? clamp01(v) : 0;
    if (measured) face.measuredUntil = now() + 1.0;
  }

  function standby() {
    setVisualState("standby");
    setAudioLevel(0, false);
  }

  function setTheme(name) {
    if (!THEMES[name] || name === theme) return;
    theme = name;
    Object.assign(COLOR, THEMES[name]);
    paintPage();
    resize();
    buildLayers();
  }

  function paintPage() {
    document.documentElement.style.background = COLOR.background[2];
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", COLOR.background[2]);
  }

  window.JarvisFace = {
    setVisualState, setAudioLevel, standby, setTheme, reset: standby,
    get theme() { return theme; },
    get state() { return face.state; },
    get level() { return face.level; },
  };

  // Voix simulée (syllabes, mots, respirations) quand aucun niveau réel n'est disponible.
  function simulatedLevel(t) {
    const syllable = Math.max(0, Math.sin(t * TAU * 4.2 + Math.sin(t * 1.9) * 2.2));
    const word = 0.55 + 0.45 * Math.sin(t * TAU * 0.85 + Math.sin(t * 0.47) * 3);
    const breath = (Math.sin(t * TAU * 0.21) + 1) / 2 > 0.16 ? 1 : 0.08;
    return clamp01((0.2 + 0.8 * syllable) * word * breath * 0.95);
  }

  function follow(value, target, attack, release, dt) {
    const tau = target > value ? attack : release;
    return value + (target - value) * (1 - Math.exp(-dt / tau));
  }

  // --- Canvas ----------------------------------------------------------------------------------

  const canvas = document.getElementById("face");
  const ctx = canvas.getContext("2d", { alpha: false });
  const debugEl = document.getElementById("debug");
  const errorEl = document.getElementById("error");

  // Aperçu : ?erreur=texte affiche le thème rouge et ce message, sans erreur réelle (pour voir le rendu).
  const PREVIEW_ERROR = params.get("erreur") || params.get("error");

  function previewError() {
    setTheme("error");
    showErrors([PREVIEW_ERROR]);
  }

  function showErrors(messages) {
    const text = (messages || []).join("\n");
    if (!errorEl) return;
    if (text) {
      errorEl.textContent = text;
      errorEl.style.whiteSpace = "pre-line";
    }
    errorEl.hidden = !text;
  }
  let W = 0, H = 0, CX = 0, CY = 0, R = 0, DPR = 1;
  let background = null;
  const layers = {};

  function offscreen(draw, seed) {
    const size = Math.ceil(R * 2.1);
    const c = document.createElement("canvas");
    c.width = c.height = size;
    const g = c.getContext("2d");
    g.translate(size / 2, size / 2);
    g.lineCap = "butt";
    draw(g, rng(seed));
    return c;
  }

  function glowStroke(g, width, color, alpha, blur) {
    g.shadowColor = rgba(color, alpha);
    g.shadowBlur = blur;
    g.lineWidth = width;
    g.strokeStyle = rgba(color, alpha);
    g.stroke();
    g.shadowBlur = 0;
  }

  function arc(g, r, a0, a1) {
    g.beginPath();
    g.arc(0, 0, r, a0, a1);
  }

  const BUILDERS = {
    // Graduation extérieure : fines graduations, deux secteurs vides pour casser la symétrie.
    outerScale(g, rand) {
      const r = R * 0.965;
      arc(g, r, 0, TAU);
      glowStroke(g, Math.max(1, R * 0.0018), COLOR.cyan, 0.28, R * 0.01);
      for (let i = 0; i < 180; i++) {
        const deg = i * 2;
        if ((deg > 38 && deg < 58) || (deg > 214 && deg < 226)) continue;
        const a = (deg * Math.PI) / 180;
        const major = i % 5 === 0;
        const len = major ? R * 0.028 : R * 0.012;
        g.beginPath();
        g.moveTo(Math.cos(a) * r, Math.sin(a) * r);
        g.lineTo(Math.cos(a) * (r - len), Math.sin(a) * (r - len));
        glowStroke(g, Math.max(1, R * (major ? 0.0028 : 0.0016)), major ? COLOR.white : COLOR.cyan,
                   major ? 0.55 : 0.3, R * 0.008);
      }
      for (const [a0, a1] of [[0.66, 1.0], [3.74, 3.95]]) {
        arc(g, r + R * 0.018, a0, a1);
        glowStroke(g, Math.max(1, R * 0.004), COLOR.cyan, 0.5, R * 0.012);
      }
    },

    // Anneau extérieur segmenté irrégulier.
    outerDash(g, rand) {
      const r = R * 0.905;
      let a = 0;
      while (a < TAU - 0.05) {
        const len = 0.05 + rand() * 0.55;
        const gap = 0.025 + rand() * 0.12;
        const end = Math.min(TAU - 0.03, a + len);
        arc(g, r, a, end);
        const strong = rand() > 0.72;
        glowStroke(g, R * (strong ? 0.009 : 0.0035), strong ? COLOR.cyan : COLOR.blue, strong ? 0.6 : 0.35, R * 0.02);
        a = end + gap;
      }
      arc(g, R * 0.875, 0, TAU);
      glowStroke(g, Math.max(1, R * 0.0015), COLOR.blue, 0.2, 0);
    },

    // Grand anneau partiel épais (la signature du visage).
    ringA(g, rand) {
      const r = R * 0.76;
      const arcs = [[-0.35, 1.05], [1.3, 2.25], [2.55, 4.35], [4.62, 5.55]];
      for (const [a0, a1] of arcs) {
        arc(g, r, a0, a1);
        glowStroke(g, R * 0.05, COLOR.blue, 0.22, R * 0.04);
        arc(g, r, a0, a1);
        glowStroke(g, R * 0.05, COLOR.deep, 0.55, 0);
        arc(g, r + R * 0.022, a0, a1);
        glowStroke(g, Math.max(1, R * 0.004), COLOR.cyan, 0.75, R * 0.02);
        arc(g, r - R * 0.022, a0 + 0.02, a1 - 0.02);
        glowStroke(g, Math.max(1, R * 0.0025), COLOR.cyan, 0.45, R * 0.012);
        for (let k = 0; k < 3; k++) {
          const at = a0 + (a1 - a0) * (0.2 + 0.3 * k) + rand() * 0.05;
          arc(g, r, at, at + 0.018);
          glowStroke(g, R * 0.05, COLOR.white, 0.35, R * 0.02);
        }
      }
      arc(g, R * 0.715, 0, TAU);
      glowStroke(g, Math.max(1, R * 0.0015), COLOR.cyan, 0.16, 0);
    },

    // Blocs de données : barre segmentée, luminosités variables, un secteur absent.
    ringB(g, rand) {
      const r = R * 0.655;
      const blocks = 84;
      for (let i = 0; i < blocks; i++) {
        const a0 = (i / blocks) * TAU;
        if (a0 > 4.9 && a0 < 5.6) continue;
        const bright = rand();
        arc(g, r, a0, a0 + (TAU / blocks) * 0.62);
        glowStroke(g, R * 0.024, bright > 0.82 ? COLOR.white : COLOR.cyan,
                   bright > 0.82 ? 0.75 : 0.12 + bright * 0.35, R * 0.012);
      }
      arc(g, r + R * 0.028, 0.2, 2.9);
      glowStroke(g, Math.max(1, R * 0.002), COLOR.cyan, 0.4, R * 0.01);
      arc(g, r - R * 0.028, 3.3, 6.0);
      glowStroke(g, Math.max(1, R * 0.002), COLOR.cyan, 0.3, R * 0.01);
    },

    // Anneaux internes fins.
    inner1(g) {
      g.setLineDash([R * 0.012, R * 0.018]);
      arc(g, R * 0.415, 0, TAU);
      glowStroke(g, Math.max(1, R * 0.004), COLOR.cyan, 0.55, R * 0.012);
      g.setLineDash([]);
    },

    inner2(g, rand) {
      const r = R * 0.445;
      for (const [a0, a1] of [[0.1, 1.4], [2.0, 2.6], [3.2, 5.1]]) {
        arc(g, r, a0, a1);
        glowStroke(g, Math.max(1, R * 0.0035), COLOR.blue, 0.6, R * 0.015);
      }
      for (let i = 0; i < 6; i++) {
        const a = rand() * TAU;
        g.beginPath();
        g.arc(Math.cos(a) * r, Math.sin(a) * r, R * 0.006, 0, TAU);
        g.fillStyle = rgba(COLOR.white, 0.8);
        g.shadowColor = rgba(COLOR.cyan, 0.9);
        g.shadowBlur = R * 0.02;
        g.fill();
        g.shadowBlur = 0;
      }
    },

    // Arcs serrés autour du noyau, rapides en réflexion.
    coreArcs(g) {
      const r = R * 0.345;
      for (const [a0, a1] of [[0.2, 1.1], [2.3, 2.8], [3.6, 4.9]]) {
        arc(g, r, a0, a1);
        glowStroke(g, R * 0.011, COLOR.cyan, 0.8, R * 0.03);
      }
    },

    // Iris intérieur pointillé : le centre reste vide mais vivant.
    iris(g) {
      g.setLineDash([R * 0.004, R * 0.014]);
      arc(g, R * 0.2, 0, TAU);
      glowStroke(g, Math.max(1, R * 0.003), COLOR.cyan, 0.4, R * 0.01);
      g.setLineDash([]);
    },
  };

  // Couches pré-rendues : vitesse (rad/s), sens, réaction audio (enveloppe et gain), opacité,
  // multiplicateurs de vitesse par état (profondeur : chaque couche a son propre rythme).
  const LAYERS = [
    { key: "outerScale", speed: 0.018, dir: 1, react: "outer", gain: 0.010, alpha: 0.75, contract: 0.012, boost: {} },
    { key: "outerDash", speed: 0.045, dir: -1, react: "outer", gain: 0.018, alpha: 0.85, contract: 0.02, boost: { thinking: 1.4 } },
    { key: "ringA", speed: 0.085, dir: 1, react: "middle", gain: 0.035, alpha: 0.95, contract: 0.025, boost: { listening: 1.3 } },
    { key: "ringB", speed: 0.13, dir: -1, react: "middle", gain: 0.03, alpha: 0.9, contract: 0.02, boost: { thinking: 1.8 } },
    { key: "inner2", speed: 0.22, dir: -1, react: "inner", gain: 0.035, alpha: 0.8, contract: 0.0, boost: { listening: 1.8 } },
    { key: "inner1", speed: 0.3, dir: 1, react: "inner", gain: 0.05, alpha: 0.85, contract: 0.0, boost: { listening: 2.0, thinking: 1.5 } },
    { key: "coreArcs", speed: 0.7, dir: 1, react: "core", gain: 0.06, alpha: 1.0, contract: 0.0, boost: { thinking: 2.6, listening: 1.3 } },
    { key: "iris", speed: 0.1, dir: -1, react: "core", gain: 0.08, alpha: 0.7, contract: 0.0, boost: { thinking: 3 } },
  ].map((layer, i) => ({ ...layer, angle: i * 1.3, mul: 1 }));

  // --- Éléments dynamiques ---------------------------------------------------------------------

  const TRACKS = [0.58, 0.69, 0.79, 0.845];
  const comets = [];
  const orbitals = [
    { r: 0.935, speed: 0.10, shape: "tri", angle: 0.4 },
    { r: 0.83, speed: -0.17, shape: "dot", angle: 2.1 },
    { r: 0.615, speed: 0.26, shape: "diamond", angle: 4.0 },
    { r: 0.985, speed: -0.045, shape: "bracket", angle: 5.2 },
    { r: 0.52, speed: -0.38, shape: "dot", angle: 1.1 },
  ];
  const particles = [];
  const prand = rng(7);
  for (let i = 0; i < 90; i++) particles.push(newParticle(true));

  function newParticle(anywhere) {
    return {
      r: anywhere ? 0.18 + prand() * 0.86 : 0.95 + prand() * 0.1,
      a: prand() * TAU,
      drift: (prand() - 0.5) * 0.12,
      phase: prand() * TAU,
      freq: 0.4 + prand() * 1.6,
      size: 0.6 + prand() * 1.6,
      fall: 0.4 + prand() * 0.9,
    };
  }

  function spawnComet() {
    comets.push({
      r: TRACKS[Math.floor(Math.random() * TRACKS.length)] + (Math.random() - 0.5) * 0.01,
      a: Math.random() * TAU,
      speed: (0.5 + Math.random() * 1.2) * (Math.random() > 0.5 ? 1 : -1),
      len: 0.12 + Math.random() * 0.35,
      life: 0,
      span: 1.2 + Math.random() * 2.2,
    });
  }

  // --- Mise en page ----------------------------------------------------------------------------

  let layersR = 0;

  function buildLayers() {
    for (const layer of LAYERS) layers[layer.key] = offscreen(BUILDERS[layer.key], layer.key.length * 97 + 13);
    layersR = R;
  }

  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(1, Math.floor(window.innerWidth * DPR));
    H = Math.max(1, Math.floor(window.innerHeight * DPR));
    canvas.width = W;
    canvas.height = H;
    CX = W / 2;
    CY = H / 2;
    R = Math.min(W, H) * 0.44;
    background = ctx.createRadialGradient(CX, CY, 0, CX, CY, Math.hypot(W, H) / 2);
    background.addColorStop(0, COLOR.background[0]);
    background.addColorStop(0.45, COLOR.background[1]);
    background.addColorStop(1, COLOR.background[2]);
  }

  let resizeTimer = 0;

  function followWindow() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.floor(window.innerWidth * dpr), h = Math.floor(window.innerHeight * dpr);
    if (w < 2 || h < 2 || (w === W && h === H)) return;
    resize();
    clearTimeout(resizeTimer);
    if (layersR < 8) buildLayers();
    else resizeTimer = setTimeout(buildLayers, 150);
  }

  window.addEventListener("resize", followWindow);

  // --- Rendu -----------------------------------------------------------------------------------

  function drawLayer(layer, t) {
    const img = layers[layer.key];
    if (!img || layersR < 1) return;
    const env = face.env[layer.react];
    const scale = (1 + layer.gain * env - layer.contract * face.cur.focus * 0.6) * (R / layersR);
    const alpha = layer.alpha * (0.35 + 0.65 * face.cur.glow) + env * 0.25;
    ctx.save();
    ctx.globalAlpha = clamp01(alpha);
    ctx.translate(CX, CY);
    ctx.rotate(layer.angle);
    ctx.scale(scale, scale);
    ctx.drawImage(img, -img.width / 2, -img.height / 2);
    ctx.restore();
  }

  function drawHalo() {
    const g = ctx.createRadialGradient(CX, CY, R * 0.25, CX, CY, R * 1.2);
    const a = 0.05 + 0.09 * face.cur.glow + 0.12 * face.env.middle;
    g.addColorStop(0, rgba(COLOR.blue, a));
    g.addColorStop(0.5, rgba(COLOR.deep, a * 0.6));
    g.addColorStop(1, rgba(COLOR.deep, 0));
    ctx.fillStyle = g;
    ctx.fillRect(CX - R * 1.25, CY - R * 1.25, R * 2.5, R * 2.5);
  }

  function drawInnerTicks(t, angle) {
    const n = 96;
    const r0 = R * 0.475;
    const c = face.cur;
    ctx.save();
    ctx.translate(CX, CY);
    ctx.rotate(angle);
    ctx.lineWidth = Math.max(1, R * 0.004);
    for (let pass = 0; pass < 2; pass++) {
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const a = (i / n) * TAU;
        const wave = 0.5 + 0.5 * Math.sin(i * 1.7 + t * 3.1) * Math.sin(i * 0.61 - t * 1.9);
        const len = R * (0.014 + 0.016 * c.complexity * wave + 0.075 * face.env.inner * (0.3 + 0.7 * wave));
        const strong = (i % 8 === 0) === (pass === 1);
        if (!strong) continue;
        ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
        ctx.lineTo(Math.cos(a) * (r0 + len), Math.sin(a) * (r0 + len));
      }
      ctx.strokeStyle = pass === 1
        ? rgba(COLOR.white, 0.35 + 0.4 * c.glow + 0.3 * face.env.inner)
        : rgba(COLOR.cyan, 0.18 + 0.3 * c.glow + 0.35 * face.env.inner);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawCore(t) {
    const c = face.cur;
    const pulse = c.pulseDepth * (0.5 + 0.5 * Math.sin(face.pulsePhase));
    const lvl = face.env.core;
    const rc = R * 0.3 * (1 + 0.06 * pulse + 0.1 * lvl);
    const glow = c.glow;

    ctx.globalCompositeOperation = "source-over";
    const pupil = ctx.createRadialGradient(CX, CY, 0, CX, CY, rc * 0.92);
    pupil.addColorStop(0, rgba(COLOR.pupil, 0.92));
    pupil.addColorStop(0.6, rgba(COLOR.pupilEdge, 0.75));
    pupil.addColorStop(1, rgba(COLOR.pupilEdge, 0));
    ctx.fillStyle = pupil;
    ctx.beginPath();
    ctx.arc(CX, CY, rc * 0.92, 0, TAU);
    ctx.fill();
    ctx.globalCompositeOperation = "lighter";

    const inner = ctx.createRadialGradient(CX, CY, rc * 0.45, CX, CY, rc);
    inner.addColorStop(0, rgba(COLOR.deep, 0));
    inner.addColorStop(0.7, rgba(COLOR.blue, 0.03 + 0.06 * glow + 0.12 * lvl));
    inner.addColorStop(0.92, rgba(COLOR.blue, 0.1 + 0.18 * glow + 0.25 * lvl + 0.3 * pulse));
    inner.addColorStop(1, rgba(COLOR.cyan, 0.25 + 0.3 * glow + 0.35 * lvl));
    ctx.fillStyle = inner;
    ctx.beginPath();
    ctx.arc(CX, CY, rc, 0, TAU);
    ctx.fill();

    const reach = rc * (1.45 + 0.35 * lvl);
    const bloom = ctx.createRadialGradient(CX, CY, 0, CX, CY, reach);
    bloom.addColorStop(0, rgba(COLOR.cyan, 0));
    bloom.addColorStop((rc * 0.97) / reach, rgba(COLOR.cyan, 0));
    bloom.addColorStop(rc / reach, rgba(COLOR.cyan, 0.16 + 0.22 * glow + 0.35 * lvl + 0.25 * pulse));
    bloom.addColorStop(1, rgba(COLOR.cyan, 0));
    ctx.fillStyle = bloom;
    ctx.beginPath();
    ctx.arc(CX, CY, rc * (1.5 + 0.35 * lvl), 0, TAU);
    ctx.fill();

    for (const [width, color, alpha] of [[R * 0.028, COLOR.cyan, 0.1 + 0.12 * lvl],
                                        [R * 0.011, COLOR.cyan, 0.35 + 0.35 * glow],
                                        [Math.max(1.2, R * 0.0035), COLOR.white, 0.55 + 0.45 * Math.max(glow, lvl)]]) {
      ctx.beginPath();
      ctx.arc(CX, CY, rc, 0, TAU);
      ctx.lineWidth = width;
      ctx.strokeStyle = rgba(color, alpha);
      ctx.stroke();
    }
  }

  function drawComets(dt) {
    const c = face.cur;
    const wanted = Math.round(1 + c.complexity * 11);
    if (comets.length < wanted && Math.random() < dt * (0.8 + 4 * c.complexity)) spawnComet();
    ctx.save();
    ctx.translate(CX, CY);
    ctx.lineCap = "round";
    for (let i = comets.length - 1; i >= 0; i--) {
      const k = comets[i];
      k.life += dt;
      k.a += k.speed * dt * (0.4 + c.speed);
      const fade = Math.min(1, k.life / 0.4, (k.span - k.life) / 0.5);
      if (k.life > k.span) { comets.splice(i, 1); continue; }
      const steps = 6;
      for (let s = 0; s < steps; s++) {
        const f = s / steps;
        const dir = Math.sign(k.speed);
        const a1 = k.a - dir * k.len * f;
        const a0 = a1 - dir * (k.len / steps);
        ctx.beginPath();
        ctx.arc(0, 0, k.r * R, Math.min(a0, a1), Math.max(a0, a1));
        ctx.lineWidth = Math.max(1, R * 0.006 * (1 - f * 0.6));
        ctx.strokeStyle = rgba(s === 0 ? COLOR.white : COLOR.cyan, fade * (0.85 - f * 0.8) * (0.4 + 0.6 * c.glow));
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  function drawOrbitals(dt) {
    const c = face.cur;
    ctx.save();
    ctx.translate(CX, CY);
    for (const o of orbitals) {
      o.angle += o.speed * dt * (0.5 + c.speed);
      const r = o.r * R * (1 - 0.012 * c.focus);
      const x = Math.cos(o.angle) * r, y = Math.sin(o.angle) * r;
      const s = R * 0.014;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(o.angle + Math.PI / 2);
      ctx.fillStyle = rgba(COLOR.white, 0.55 + 0.4 * c.glow);
      ctx.strokeStyle = rgba(COLOR.cyan, 0.5 + 0.4 * c.glow);
      ctx.lineWidth = Math.max(1, R * 0.003);
      ctx.beginPath();
      if (o.shape === "tri") { ctx.moveTo(0, -s); ctx.lineTo(s * 0.8, s * 0.6); ctx.lineTo(-s * 0.8, s * 0.6); ctx.closePath(); ctx.fill(); }
      else if (o.shape === "diamond") { ctx.moveTo(0, -s); ctx.lineTo(s * 0.6, 0); ctx.lineTo(0, s); ctx.lineTo(-s * 0.6, 0); ctx.closePath(); ctx.stroke(); }
      else if (o.shape === "bracket") { ctx.moveTo(-s * 1.6, -s * 0.2); ctx.lineTo(-s * 1.6, s * 0.4); ctx.lineTo(s * 1.6, s * 0.4); ctx.lineTo(s * 1.6, -s * 0.2); ctx.stroke(); }
      else { ctx.arc(0, 0, s * 0.45, 0, TAU); ctx.fill(); }
      ctx.restore();
    }
    ctx.restore();
  }

  function drawParticles(t, dt) {
    const c = face.cur;
    ctx.save();
    ctx.translate(CX, CY);
    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];
      p.a += p.drift * dt * (0.3 + c.speed);
      p.r -= dt * 0.045 * p.fall * c.complexity * c.complexity;
      if (p.r < 0.34) particles[i] = newParticle(false);
      const tw = 0.5 + 0.5 * Math.sin(t * p.freq * TAU + p.phase);
      const alpha = (0.1 + 0.55 * tw) * (0.25 + 0.75 * c.complexity) * (0.5 + 0.5 * c.glow);
      ctx.fillStyle = rgba(tw > 0.9 ? COLOR.white : COLOR.cyan, alpha);
      const size = Math.max(1, p.size * DPR);
      ctx.fillRect(Math.cos(p.a) * p.r * R - size / 2, Math.sin(p.a) * p.r * R - size / 2, size, size);
    }
    ctx.restore();
  }

  // --- Boucle ----------------------------------------------------------------------------------

  let last = now();
  let innerAngle = 0;
  let frames = 0, fpsAt = now(), fps = 0;

  function update(t, dt) {
    const k = 1 - Math.exp(-dt / 0.6);
    for (const key in face.cur) face.cur[key] += (face.target[key] - face.cur[key]) * k;
    face.pulsePhase += TAU * face.cur.pulseRate * dt;

    let level = 0;
    if (face.state === "speaking") level = t < face.measuredUntil ? face.level : simulatedLevel(t);
    else if (t < face.measuredUntil) level = face.level * 0.5;
    const e = face.env;
    e.core = follow(e.core, level, 0.05, 0.16, dt);
    e.inner = follow(e.inner, level, 0.03, 0.09, dt);
    e.middle = follow(e.middle, level, 0.25, 0.55, dt);
    e.outer = follow(e.outer, level * 0.6, 0.8, 1.6, dt);

    for (const layer of LAYERS) {
      const target = layer.boost[face.state] || 1;
      layer.mul += (target - layer.mul) * k;
      layer.angle += dt * layer.speed * layer.dir * face.cur.speed * layer.mul;
    }
    innerAngle += dt * 0.12 * face.cur.speed * (face.state === "listening" ? 1.6 : 1);
  }

  function render(t, dt) {
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = "lighter";
    drawHalo();
    for (const layer of LAYERS.slice(0, 4)) drawLayer(layer, t);
    drawOrbitals(dt);
    drawComets(dt);
    drawParticles(t, dt);
    for (const layer of LAYERS.slice(4, 6)) drawLayer(layer, t);
    drawInnerTicks(t, innerAngle);
    drawLayer(LAYERS[6], t);
    drawCore(t);
    drawLayer(LAYERS[7], t);
    ctx.globalCompositeOperation = "source-over";
  }

  function frame() {
    const t = now();
    const dt = Math.min(0.05, Math.max(0, t - last));
    last = t;
    try {
      followWindow();
      update(t, dt);
      render(t, dt);
    } catch (err) {
      console.error("Visage JARVIS :", err);
    }
    frames++;
    if (t - fpsAt >= 0.5) {
      fps = frames / (t - fpsAt);
      frames = 0;
      fpsAt = t;
      window.JarvisFace.fps = fps;
      if (DEBUG) {
        debugEl.hidden = false;
        debugEl.textContent = `${face.state}  ${fps.toFixed(0)} FPS  niveau ${face.env.core.toFixed(2)}` +
          (DEMO ? "\n1 veille · 2 écoute · 3 réflexion · 4 parole · A cycle auto" : "");
      }
    }
    requestAnimationFrame(frame);
  }

  // --- Connexion à JARVIS ----------------------------------------------------------------------

  function connect() {
    if (DEMO || location.protocol === "file:" || !("EventSource" in window)) return;
    const source = new EventSource("events");
    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setVisualState(data.state);
        // L'erreur passe avant tout (même un thème imposé par ?theme=) ; ensuite, retour au thème précédent.
        if (data.theme === "error") setTheme("error");
        else setTheme(FORCED_THEME || data.theme || theme);
        showErrors(data.theme === "error" ? data.error_messages : []);
        if (PREVIEW_ERROR) previewError();
        if (data.audio_source === "measured" || data.audio_source === "external") setAudioLevel(data.audio_level);
        face.lastMessage = now();
      } catch (err) {
        console.warn("Visage JARVIS : message ignoré", err);
      }
    };
    setInterval(() => {
      if (face.lastMessage > 0 && now() - face.lastMessage > 3) standby();
    }, 1000);
  }

  function demo() {
    if (!DEMO) return;
    const order = ["standby", "listening", "thinking", "speaking"];
    let auto = true, index = 0;
    setInterval(() => {
      if (!auto) return;
      index = (index + 1) % order.length;
      setVisualState(order[index]);
    }, 5000);
    window.addEventListener("keydown", (event) => {
      const i = "1234".indexOf(event.key);
      if (i >= 0) { auto = false; setVisualState(order[i]); }
      if (event.key === "a" || event.key === "A") auto = !auto;
      if (event.key === "t" || event.key === "T") setTheme(theme === "day" ? "night" : "day");
    });
  }

  if (DEBUG) {
    window.JarvisFace.bench = (count = 120) => {
      const start = performance.now();
      followWindow();
      for (let i = 0; i < count; i++) {
        const t = now();
        update(t, 1 / 60);
        render(t, 1 / 60);
      }
      ctx.getImageData(0, 0, 1, 1);
      return (performance.now() - start) / count;
    };
  }

  paintPage();
  resize();
  buildLayers();
  connect();
  if (PREVIEW_ERROR) previewError();
  demo();
  requestAnimationFrame(frame);
})();
