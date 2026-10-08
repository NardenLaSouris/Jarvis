/*
 * Visage d'ORION (univers Aureon) : rendu canvas piloté uniquement par un état visuel. Un système de cercles
 * concentriques, centre vide : ni texte, ni logo, ni symbole sur le visage.
 *
 *   OrionFace (alias JarvisFace).setVisualState("standby" | "listening" | "thinking" | "speaking")
 *   JarvisFace.setAudioLevel(0..1)
 *   JarvisFace.standby()
 *   JarvisFace.setTheme("day" | "night" | "error" | "<couleur>", teinte?)   (couleur : palette tirée de la teinte)
 *
 * Connecté à JARVIS par /events (Server-Sent Events). Sans connexion : veille.
 * Le thème (jour en couleur, nuit en noir et blanc, ou la couleur choisie à la voix) vient du Core, sauf
 * ?theme=day|night|<teinte 0-360>|arcenciel.
 * ?demo=1 : démonstration autonome (touches 1-4, A = cycle auto, T = thème). ?debug=1 : état et FPS.
 */
(() => {
  "use strict";

  const TAU = Math.PI * 2;
  const params = new URLSearchParams(location.search);
  const DEMO = params.has("demo");
  const DEBUG = params.has("debug") || DEMO;

  const PROFILES = {
    standby:   { speed: 0.18, glow: 0.55, focus: 0.0, complexity: 0.10, pulseRate: 0.15, pulseDepth: 0.15 },
    listening: { speed: 0.45, glow: 0.66, focus: 1.0, complexity: 0.30, pulseRate: 0.70, pulseDepth: 0.22 },
    thinking:  { speed: 1.50, glow: 0.80, focus: 0.4, complexity: 1.00, pulseRate: 1.20, pulseDepth: 0.10 },
    speaking:  { speed: 0.60, glow: 0.92, focus: 0.6, complexity: 0.40, pulseRate: 0.00, pulseDepth: 0.00 },
  };

  const THEMES = {
    // Charte Aureon : vert forêt (structure), vert secondaire (actif), crème (important), doré (accents rares),
    // noir profond (fond).
    day: {
      deep: [15, 61, 46], blue: [60, 139, 111], cyan: [128, 186, 160], white: [244, 239, 228], gold: [201, 164, 92],
      pupil: [10, 13, 11], pupilEdge: [14, 16, 15], background: ["#111814", "#0f110f", "#0e0e0e"],
    },
    night: {
      deep: [40, 40, 40], blue: [125, 125, 125], cyan: [205, 205, 205], white: [255, 255, 255],
      pupil: [0, 0, 0], pupilEdge: [3, 3, 3], background: ["#141414", "#080808", "#000000"],
    },
    // Fêtes : plusieurs couleurs à la fois (anneaux, arcs, graduations), comme le tricolore.
    starwars: {  // jaune du logo Star Wars (#FFE81F) sur noir
      deep: [80, 66, 0], blue: [255, 214, 20], cyan: [255, 232, 31], white: [255, 248, 200],
      pupil: [0, 0, 0], pupilEdge: [3, 3, 0], background: ["#060604", "#020201", "#000000"],
    },
    noel: {  // rouge, vert sapin et or
      deep: [100, 12, 18], blue: [210, 30, 45], cyan: [40, 200, 80], white: [255, 210, 110],
      pupil: [4, 1, 1], pupilEdge: [10, 3, 3], background: ["#0a1f10", "#051008", "#010402"],
    },
    halloween: {  // orange citrouille, violet et vert poison
      deep: [50, 14, 70], blue: [150, 60, 230], cyan: [255, 135, 20], white: [170, 255, 80],
      pupil: [3, 1, 5], pupilEdge: [8, 3, 12], background: ["#150a20", "#0a0512", "#030106"],
    },
    nouvelan: {  // or, bleu glacier et blanc
      deep: [70, 52, 12], blue: [235, 185, 60], cyan: [120, 185, 255], white: [255, 255, 255],
      pupil: [3, 2, 0], pupilEdge: [8, 6, 2], background: ["#15120a", "#0a0805", "#030201"],
    },
    paques: {  // pastels : lilas, rose, menthe, jaune
      deep: [80, 50, 110], blue: [255, 150, 205], cyan: [150, 235, 150], white: [255, 240, 150],
      pupil: [4, 2, 6], pupilEdge: [10, 6, 14], background: ["#1a1426", "#0e0a17", "#05030a"],
    },
    saintvalentin: {  // rouge, rose et blanc
      deep: [95, 10, 40], blue: [225, 30, 85], cyan: [255, 125, 175], white: [255, 232, 242],
      pupil: [4, 0, 2], pupilEdge: [10, 2, 6], background: ["#1c0712", "#0f030a", "#040103"],
    },
    saintpatrick: {  // vert trèfle et or
      deep: [10, 65, 25], blue: [30, 175, 75], cyan: [130, 255, 130], white: [255, 210, 90],
      pupil: [0, 3, 1], pupilEdge: [2, 8, 3], background: ["#06180b", "#030d06", "#010402"],
    },
    muguet: {  // vert tendre et blanc
      deep: [20, 75, 32], blue: [60, 175, 85], cyan: [235, 255, 235], white: [255, 255, 255],
      pupil: [0, 3, 1], pupilEdge: [2, 8, 3], background: ["#071a0c", "#040e07", "#010402"],
    },
    poissonavril: {  // bleu océan et orange poisson
      deep: [5, 55, 75], blue: [20, 165, 205], cyan: [255, 145, 45], white: [220, 250, 255],
      pupil: [0, 2, 4], pupilEdge: [2, 6, 10], background: ["#04161e", "#020b10", "#000305"],
    },
    naissancejarvis: {  // le bleu de JARVIS et l'or
      deep: [12, 42, 110], blue: [34, 118, 255], cyan: [70, 214, 255], white: [255, 200, 80],
      pupil: [1, 4, 12], pupilEdge: [2, 8, 20], background: ["#061329", "#030a17", "#010308"],
    },
    // 14 Juillet : bleu, blanc et rouge ensemble (anneaux bleus, arcs rouges, graduations blanches).
    tricolore: {
      deep: [0, 35, 120], blue: [0, 85, 164], cyan: [239, 65, 53], white: [255, 255, 255],
      pupil: [1, 2, 10], pupilEdge: [3, 5, 18], background: ["#0a1430", "#050a1c", "#01030a"],
    },
    // Erreur en cours (worker LLM hors ligne, appareil muet...) : rouge, puis retour au thème précédent.
    error: {
      deep: [110, 10, 14], blue: [230, 38, 46], cyan: [255, 110, 100], white: [255, 220, 214],
      pupil: [12, 1, 1], pupilEdge: [22, 3, 3], background: ["#2a0507", "#160204", "#080001"],
    },
  };
  // Palette d'une couleur choisie (« mets ton visage en vert ») : mêmes rôles que le bleu d'origine, tirés de la
  // teinte (0-360) envoyée par le Core.
  function hsl(h, s, l) {
    s /= 100; l /= 100;
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
  }
  const hex = (c) => "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
  function huePalette(h) {
    return {
      deep: hsl(h, 80, 24), blue: hsl(h, 100, 57), cyan: hsl((h + 12) % 360, 100, 66), white: hsl(h, 100, 92),
      pupil: hsl(h, 80, 3), pupilEdge: hsl(h, 70, 6),
      background: [hex(hsl(h, 70, 10)), hex(hsl(h, 75, 5)), hex(hsl(h, 80, 2))],
    };
  }
  const RAINBOW_SECONDS = 4;  // arc-en-ciel : nouvelle teinte toutes les 4 s
  let rainbow = null, rainbowName = "";
  const forcedHue = Number(params.get("theme"));
  if (params.get("theme") && Number.isFinite(forcedHue)) THEMES["teinte"] = huePalette(forcedHue % 360);
  const FORCED_THEME = params.get("theme") === "arcenciel" ? "arcenciel"
    : Number.isFinite(forcedHue) && params.get("theme") ? "teinte"
    : THEMES[params.get("theme")] ? params.get("theme") : null;
  let theme = FORCED_THEME && THEMES[FORCED_THEME] ? FORCED_THEME : "day";
  // Doré : rôle propre à Aureon ; les autres palettes (fêtes, couleurs) reprennent leur couleur la plus claire.
  const COLOR = { ...THEMES[theme], gold: THEMES[theme].gold || THEMES[theme].white };
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

  function setTheme(name, hue, hues, palette) {
    if (palette && THEMES[palette]) {  // fête à palette fixe (14 Juillet) : pas d'alternance
      if (name === theme && !rainbow) return;
      stopRainbow();
      return applyPalette(name, THEMES[palette]);
    }
    if (name === "arcenciel" || hues === "rainbow") return startRainbow(name);
    if (Array.isArray(hues) && hues.length) return startCycle(name, hues);
    if (!THEMES[name] && Number.isFinite(hue)) THEMES[name] = huePalette(hue);
    if (!THEMES[name] || name === theme) return;
    stopRainbow();
    applyPalette(name, THEMES[name]);
  }

  function applyPalette(name, palette) {
    theme = name;
    Object.assign(COLOR, palette, { gold: palette.gold || palette.white });
    paintPage();
    resize();
    buildLayers();
  }

  function startRainbow(name = "arcenciel") {
    if (rainbow && rainbowName === name) return;
    stopRainbow();
    rainbowName = name;
    let h = 0;
    const step = () => { applyPalette(name, huePalette(h)); h = (h + 30) % 360; };
    step();
    rainbow = setInterval(step, RAINBOW_SECONDS * 1000);
  }

  // Fête : ses couleurs à tour de rôle (teintes, « day » ou « night »).
  function startCycle(name, hues) {
    if (rainbow && rainbowName === name) return;
    stopRainbow();
    rainbowName = name;
    let i = 0;
    const step = () => {
      const h = hues[i % hues.length];
      applyPalette(name, THEMES[h] || huePalette(Number(h) || 0));
      i += 1;
    };
    step();
    rainbow = setInterval(step, RAINBOW_SECONDS * 1000);
  }

  function stopRainbow() {
    if (rainbow) clearInterval(rainbow);
    rainbow = null;
    rainbowName = "";
  }

  // --- Jours de fête : message et effet animé (neige, braises, confettis, étincelles, cœurs) ------------

  const greetingEl = document.getElementById("greeting");
  const fx = document.getElementById("effects");
  const fxg = fx ? fx.getContext("2d") : null;
  let effect = "", fxParticles = [], fxRunning = false;
  const BEHIND = new Set(["hyperspace"]);  // effets dessinés derrière le visage

  // --- Échéance proche : anneau ambre et « Dentiste dans 12 min » ---------------------------------------
  const upcomingEl = document.getElementById("upcoming");
  const upcomingLabel = document.getElementById("upcoming-label");

  function spokenLeft(seconds) {
    if (seconds < 60) return "dans moins d'une minute";
    const minutes = Math.ceil(seconds / 60);
    return `dans ${minutes} min`;
  }

  function showUpcoming(next) {
    if (!upcomingEl || !upcomingLabel) return;
    const on = !!(next && next.seconds > 0 && next.window > 0);
    // Élément SVG : « hidden » est un attribut (la propriété n'existe que sur les éléments HTML).
    upcomingEl.toggleAttribute("hidden", !on);
    upcomingLabel.hidden = !on;
    if (!on) return;
    const done = Math.max(0, Math.min(1, 1 - next.seconds / next.window));
    upcomingEl.querySelector(".fill").style.strokeDashoffset = String(100 - done * 100);
    upcomingEl.classList.toggle("close", next.seconds <= 60);
    const what = next.kind === "timer" ? `Fin du ${next.label}` : next.label;
    upcomingLabel.textContent = `${what} ${spokenLeft(next.seconds)}`;
  }

  function showGreeting(text) {
    if (!greetingEl) return;
    if (text) greetingEl.textContent = text;
    greetingEl.hidden = !text;
    greetingEl.style.setProperty("--greeting", rgba(COLOR.cyan, 1));
  }

  function spawn(kind, fresh) {
    const w = fx.width, h = fx.height, s = Math.max(w, h) / 900;
    const rising = kind === "embers" || kind === "hearts";
    const p = { x: Math.random() * w, y: fresh ? Math.random() * h : (rising ? h + 10 : -10),
                r: (1 + Math.random() * 3) * s, vx: (Math.random() - 0.5) * 0.4 * s, vy: (0.4 + Math.random()) * s,
                a: Math.random() * Math.PI * 2, va: (Math.random() - 0.5) * 0.1, life: Math.random(),
                hue: Math.floor(Math.random() * 360) };
    if (rising) p.vy = -p.vy * 0.8;
    if (kind === "fish") {
      const right = Math.random() < 0.5;
      Object.assign(p, { x: fresh ? Math.random() * w : (right ? w + 50 : -50), y: h * (0.1 + Math.random() * 0.8),
                         vx: (right ? -1 : 1) * (0.6 + Math.random()) * s, vy: 0, r: (6 + Math.random() * 8) * s });
    }
    if (kind === "hyperspace") {
      // Étoile fixe : direction depuis le centre et distance (fraction du demi-écran).
      const inner = hyperInner(w, h);
      // Répartition uniforme sur l'écran (racine : autant d'étoiles loin du centre que près).
      // va = 0 : « a » est ici la direction depuis le centre ; la boucle commune la faisait tourner (étoiles en orbite).
      Object.assign(p, { a: Math.random() * Math.PI * 2, d: inner + Math.sqrt(Math.random()) * (1.02 - inner), vx: 0,
                         vy: 0, va: 0, x: w / 2, y: h / 2, r: (0.5 + Math.random() * 0.9) * s, tw: Math.random() * 6,
                         sp: 0.5 + Math.random() * 1.3, len: 0.45 + Math.random() * 0.9 });
    }
    if (kind === "lily") { p.vy *= 0.5; p.r = (5 + Math.random() * 4) * s; }
    if (kind === "clovers") { p.vy *= 0.6; p.r *= 1.6; }
    if (kind === "sparkle") { p.vy = 0; p.vx = 0; }
    return p;
  }

  function setEffect(kind) {
    kind = fxg ? kind || "" : "";  // comme le visage, qui s'anime toujours
    if (kind === effect) return;
    effect = kind;
    fxParticles = [];
    if (!effect) { fxg && fxg.clearRect(0, 0, fx.width, fx.height); return; }
    fx.width = innerWidth; fx.height = innerHeight;
    const count = { snow: 90, embers: 50, confetti: 70, sparkle: 60, hearts: 40, clovers: 30, fish: 9, lily: 22,
                    hyperspace: 180 }[effect] || 40;
    nextJump();
    for (let i = 0; i < count; i++) fxParticles.push(spawn(effect, true));
    if (!fxRunning) { fxRunning = true; requestAnimationFrame(drawEffect); }
  }

  // Hyperespace comme dans les films : ciel étoilé immobile, puis toutes les étoiles s'étirent ensemble en longs
  // traits sortant de derrière le visage, qui défilent, puis retour aux étoiles.
  // Saut de 1 min 20 (étirement 1,6 s, défilement, sortie 1,2 s), à un moment tiré au hasard entre 15 min et 1 h
  // après le précédent (ou après l'arrivée du thème).
  const HYPER = { stretch: 1.6, tunnel: 80 - 1.6 - 1.2, exit: 1.2, minWait: 15 * 60, maxWait: 60 * 60 };
  let hyperJumpAt = 0, hyperPhase = "cruise", hyperK = 0, hctx = null;

  function nextJump() {
    hyperJumpAt = now() + HYPER.minWait + Math.random() * (HYPER.maxWait - HYPER.minWait);
  }

  // Appelé par le rendu du visage : ciel et traits dessinés derrière lui, dans son propre canevas.
  function paintBehind(target, w, h) {
    hctx = target;
    hyperStep(w, h);
    for (const p of fxParticles) drawHyperStar(p, w, h);
    hctx = null;
  }

  function hyperStep(w, h) {
    let t = now() - hyperJumpAt;
    const ease = (x) => x * x * (3 - 2 * x);
    const before = hyperPhase;
    if (t < 0) { hyperPhase = "cruise"; hyperK = 0; }
    else if (t < HYPER.stretch) { hyperPhase = "stretch"; hyperK = ease(t / HYPER.stretch); }
    else if ((t -= HYPER.stretch) < HYPER.tunnel) { hyperPhase = "tunnel"; hyperK = 1; }
    else if ((t -= HYPER.tunnel) < HYPER.exit) { hyperPhase = "exit"; hyperK = 1 - ease(t / HYPER.exit); }
    else { hyperPhase = "cruise"; hyperK = 0; nextJump(); }
    if (before === "exit" && hyperPhase === "cruise") {  // retour : nouveau ciel étoilé
      for (const p of fxParticles) Object.assign(p, spawn("hyperspace", true));
    }
    if (hyperPhase === "tunnel" || hyperPhase === "exit") {  // lueur bleue du tunnel
      const glow = hctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.hypot(w, h) / 2);
      glow.addColorStop(0, `rgba(120, 170, 255, ${0.04 * hyperK})`);
      glow.addColorStop(1, "rgba(120, 170, 255, 0)");
      hctx.fillStyle = glow;
      hctx.fillRect(0, 0, w, h);
    }
  }

  // Bord extérieur du visage (anneau le plus large), en fraction de la demi-diagonale : étoiles et traits au-delà.
  function hyperInner(w, h) {
    return Math.min(0.95, (Math.min(w, h) * (BEHIND.has(effect) ? 0.36 : 0.47)) / (Math.hypot(w, h) / 2));
  }

  function drawHyperStar(p, w, h) {
    const max = Math.hypot(w, h) / 2, cx = w / 2, cy = h / 2, dx = Math.cos(p.a), dy = Math.sin(p.a);
    const k = w / Math.max(1, fx.width);  // canevas du visage : pixels physiques
    const inner = hyperInner(w, h);
    if (hyperPhase === "tunnel") {  // les traits filent vers l'extérieur, depuis le bord du visage
      p.d += (0.004 + (p.d - inner) * 0.04) * p.sp;
      if (p.d > 1.15 + p.len * 0.2) {
        Object.assign(p, { a: Math.random() * Math.PI * 2, d: inner + Math.random() * Math.random() * 0.35,
                           sp: 0.5 + Math.random() * 1.3, len: 0.45 + Math.random() * 0.9 });
      }
    }
    p.x = cx + dx * p.d * max; p.y = cy + dy * p.d * max;
    if (hyperK < 0.02) {  // ciel étoilé : points qui scintillent
      hctx.fillStyle = `rgba(255, 255, 255, ${0.6 + 0.15 * Math.sin(now() * 0.8 + p.tw)})`;
      hctx.beginPath(); hctx.arc(p.x, p.y, p.r * k, 0, Math.PI * 2); hctx.fill();
      return;
    }
    // Trait : de l'étoile vers le visage, jamais en deçà de son bord (le saut part de l'extérieur du visage).
    const tail = Math.min((p.d - inner) * max, (p.d - inner) * max * 0.9 * hyperK * p.len + p.r);
    const blue = hyperPhase === "tunnel" ? 1 : hyperK;
    hctx.strokeStyle = `rgba(${Math.round(255 - 50 * blue)}, ${Math.round(255 - 20 * blue)}, 255, ${0.4 + 0.45 * hyperK})`;
    hctx.lineWidth = Math.max(0.8, p.r * k * (0.8 + 0.6 * hyperK));
    hctx.lineCap = "round";
    hctx.beginPath(); hctx.moveTo(p.x - dx * tail, p.y - dy * tail); hctx.lineTo(p.x, p.y); hctx.stroke();
  }

  function drawEffect() {
    if (!effect) { fxRunning = false; return; }
    const w = fx.width, h = fx.height;
    fxg.clearRect(0, 0, w, h);
    if (BEHIND.has(effect)) { fxRunning = false; return; }
    for (const p of fxParticles) {
      p.x += p.vx + (effect === "snow" ? Math.sin(p.a) * 0.3 : 0);
      p.y += p.vy; p.a += p.va; p.life += 0.01;
      if (p.y > h + 60 || p.y < -60 || p.x < -60 || p.x > w + 60) Object.assign(p, spawn(effect, false));
      if (effect === "snow") {
        fxg.fillStyle = "rgba(255,255,255,0.75)";
        fxg.beginPath(); fxg.arc(p.x, p.y, p.r, 0, Math.PI * 2); fxg.fill();
      } else if (effect === "embers") {
        fxg.fillStyle = rgba(COLOR.cyan, 0.35 + 0.35 * Math.sin(p.life * 6));
        fxg.beginPath(); fxg.arc(p.x, p.y, p.r * 0.8, 0, Math.PI * 2); fxg.fill();
      } else if (effect === "confetti") {
        fxg.save(); fxg.translate(p.x, p.y); fxg.rotate(p.a);
        fxg.fillStyle = `hsla(${p.hue}, 90%, 60%, 0.8)`;
        fxg.fillRect(-p.r, -p.r * 0.5, p.r * 2.4, p.r);
        fxg.restore();
      } else if (effect === "sparkle") {
        const glow = Math.max(0, Math.sin(p.life * 4 + p.a));
        fxg.fillStyle = rgba(COLOR.white, glow * 0.9);
        fxg.beginPath(); fxg.arc(p.x, p.y, p.r * glow, 0, Math.PI * 2); fxg.fill();
        if (p.life > 3) Object.assign(p, spawn(effect, true), { life: 0 });
      } else if (effect === "fish") {
        // Poisson d'avril : corps, queue et œil, nage en ondulant.
        const r = p.r, dir = Math.sign(p.vx) || 1;
        p.y += Math.sin(p.life * 3) * 0.3;
        fxg.save(); fxg.translate(p.x, p.y); fxg.scale(dir, 1);
        fxg.fillStyle = rgba(COLOR.cyan, 0.75);
        fxg.beginPath(); fxg.ellipse(0, 0, r * 1.6, r * 0.8, 0, 0, Math.PI * 2); fxg.fill();
        fxg.beginPath(); fxg.moveTo(-r * 1.4, 0); fxg.lineTo(-r * 2.6, -r * 0.8); fxg.lineTo(-r * 2.6, r * 0.8);
        fxg.closePath(); fxg.fill();
        fxg.fillStyle = "rgba(0,0,0,0.7)";
        fxg.beginPath(); fxg.arc(r * 0.9, -r * 0.2, r * 0.18, 0, Math.PI * 2); fxg.fill();
        fxg.restore();
      } else if (effect === "lily") {
        // Brin de muguet : tige verte, feuille, clochettes blanches.
        const r = p.r * 1.4;
        fxg.save(); fxg.translate(p.x, p.y); fxg.rotate(p.a * 0.3);
        fxg.strokeStyle = "rgba(70, 180, 90, 0.75)"; fxg.lineWidth = Math.max(1, r * 0.15);
        fxg.beginPath(); fxg.moveTo(0, r * 2.4); fxg.quadraticCurveTo(r * 0.2, 0, r * 1.2, -r * 1.6); fxg.stroke();
        fxg.fillStyle = "rgba(70, 180, 90, 0.35)";
        fxg.beginPath(); fxg.ellipse(-r * 0.5, r * 1.2, r * 0.35, r * 1.3, -0.3, 0, Math.PI * 2); fxg.fill();
        fxg.fillStyle = "rgba(255, 255, 250, 0.85)";
        for (let k = 0; k < 4; k++) {
          const t = k / 4;
          fxg.beginPath(); fxg.arc(r * (0.25 + t), -r * (0.1 + t * 1.3) + r * 0.45, r * (0.28 - t * 0.04), 0, Math.PI * 2);
          fxg.fill();
        }
        fxg.restore();
      } else if (effect === "clovers") {
        // Trèfle à trois feuilles qui tombe en tournant (Saint-Patrick).
        const r = p.r * 1.6;
        fxg.save(); fxg.translate(p.x, p.y); fxg.rotate(p.a);
        fxg.fillStyle = "rgba(60, 200, 90, 0.6)";
        for (let k = 0; k < 3; k++) {
          const t = (k / 3) * Math.PI * 2 - Math.PI / 2;
          fxg.beginPath(); fxg.arc(Math.cos(t) * r * 0.75, Math.sin(t) * r * 0.75, r * 0.62, 0, Math.PI * 2); fxg.fill();
        }
        fxg.strokeStyle = "rgba(60, 200, 90, 0.6)"; fxg.lineWidth = Math.max(1, r * 0.18);
        fxg.beginPath(); fxg.moveTo(0, r * 0.4); fxg.quadraticCurveTo(r * 0.3, r * 1.2, r * 0.1, r * 1.7); fxg.stroke();
        fxg.restore();
      } else if (effect === "hearts") {
        const r = p.r * 2.2;
        fxg.fillStyle = rgba(COLOR.cyan, 0.45);
        fxg.beginPath();
        fxg.moveTo(p.x, p.y + r * 0.6);
        fxg.bezierCurveTo(p.x - r * 1.2, p.y - r * 0.2, p.x - r * 0.4, p.y - r, p.x, p.y - r * 0.35);
        fxg.bezierCurveTo(p.x + r * 0.4, p.y - r, p.x + r * 1.2, p.y - r * 0.2, p.x, p.y + r * 0.6);
        fxg.fill();
      }
    }
    requestAnimationFrame(drawEffect);
  }
  addEventListener("resize", () => { if (effect && fx) { fx.width = innerWidth; fx.height = innerHeight; } });

  function paintPage() {
    document.documentElement.style.background = COLOR.background[2];
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", COLOR.background[2]);
  }

  window.OrionFace = window.JarvisFace = {
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
    const size = Math.ceil(R * 2.5);  // axes : au-delà du cercle extérieur
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
    g.shadowBlur = blur * GLOW;
    // Traits fins : un peu plus épais sur grand écran (1 pixel se perdait en 1080p).
    g.lineWidth = width < R * 0.004 ? Math.max(width, Math.min(2, R / 260)) : width;
    g.strokeStyle = rgba(color, alpha);
    g.stroke();
    g.shadowBlur = 0;
  }

  function arc(g, r, a0, a1) {
    g.beginPath();
    g.arc(0, 0, r, a0, a1);
  }

  // Identité ORION (Aureon) : cercles fins, axes, anneau central crème et or, larges arcs vert forêt. Le centre
  // reste vide (ni texte, ni logo, ni symbole).
  const GLOW = 0.5;  // lueur des traits : sobre
  const SPIN = 2.5;  // rythme global des rotations (anneaux, satellites, reflet)

  function dots(g, r, count, size, color, alpha, skip = () => false) {
    g.fillStyle = rgba(color, alpha);
    for (let i = 0; i < count; i++) {
      const a = (i / count) * TAU;
      if (skip(a)) continue;
      g.beginPath();
      g.arc(Math.cos(a) * r, Math.sin(a) * r, size, 0, TAU);
      g.fill();
    }
  }

  // Point lumineux : halo doré large et doux, anneau de lumière, cœur crème presque blanc.
  function glowDot(g, x, y, size, color, alpha) {
    const halo = g.createRadialGradient(x, y, 0, x, y, size * 8);
    halo.addColorStop(0, rgba(color, 0.9 * alpha));
    halo.addColorStop(0.18, rgba(color, 0.35 * alpha));
    halo.addColorStop(0.5, rgba(color, 0.08 * alpha));
    halo.addColorStop(1, rgba(color, 0));
    g.fillStyle = halo;
    g.beginPath(); g.arc(x, y, size * 8, 0, TAU); g.fill();
    g.fillStyle = rgba(COLOR.white, Math.min(1, alpha * 1.1));
    g.beginPath(); g.arc(x, y, size, 0, TAU); g.fill();
  }

  // Arc d'un seul trait dont la couleur et l'opacité varient le long de l'arc (dégradé conique) : des petits
  // morceaux juxtaposés se chevauchaient et dessinaient des rayures en 1080p. ``at(k)`` -> [couleur, opacité].
  function gradientArc(g, r, a0, a1, width, at, steps = 24) {
    arc(g, r, a0, a1);
    g.lineWidth = width;
    if (!g.createConicGradient) {  // navigateur ancien : couleur du milieu
      const [color, alpha] = at(0.5);
      g.strokeStyle = rgba(color, alpha);
    } else {
      const grad = g.createConicGradient(a0, 0, 0);
      const f = (a1 - a0) / TAU;
      for (let i = 0; i <= steps; i++) {
        const [color, alpha] = at(i / steps);
        grad.addColorStop(Math.min(1, (i / steps) * f), rgba(color, alpha));
      }
      if (f < 1) grad.addColorStop(Math.min(1, f + 0.001), rgba(at(1)[0], 0));
      g.strokeStyle = grad;
    }
    g.stroke();
  }

  // Couleur entre deux teintes (dégradé de l'arc doré).
  const mix = (a, b, k) => a.map((v, i) => Math.round(v + (b[i] - v) * k));

  const BUILDERS = {
    // Axes horizontal et vertical, au-delà du visage, avec leurs points et graduations (fixes).
    axes(g) {
      const reach = R * 1.2;
      g.lineWidth = Math.max(1, R * 0.0016);
      for (const [dx, dy] of [[1, 0], [0, 1]]) {
        for (const [from, to] of [[-reach, -R * 0.62], [R * 0.62, reach]]) {
          const grad = g.createLinearGradient(dx * from, dy * from, dx * to, dy * to);
          const outward = from < 0;
          grad.addColorStop(outward ? 0 : 1, rgba(COLOR.gold, 0));
          grad.addColorStop(outward ? 1 : 0, rgba(COLOR.gold, 0.5));
          g.strokeStyle = grad;
          g.beginPath(); g.moveTo(dx * from, dy * from); g.lineTo(dx * to, dy * to); g.stroke();
        }
        for (const k of [-1.1, -0.98, -0.83, -0.66, 0.66, 0.83, 0.98, 1.1]) {
          const far = Math.abs(k) > 1;
          glowDot(g, dx * k * R, dy * k * R, R * (far ? 0.005 : 0.0068), COLOR.gold, far ? 0.7 : 1);
        }
      }
    },

    // Cercles extérieurs très fins.
    outerRings(g) {
      for (const [a0, a1] of [[0.15, 2.0], [2.12, 4.3], [4.45, 6.1]]) {
        arc(g, R, a0, a1);
        glowStroke(g, Math.max(1, R * 0.0014), COLOR.blue, 0.45, 0);
      }
      for (const [a0, a1] of [[0.6, 1.5], [2.6, 3.0], [3.4, 5.2], [5.5, 5.9]]) {
        arc(g, R * 0.965, a0, a1);
        glowStroke(g, Math.max(1, R * 0.0014), COLOR.blue, 0.32, 0);
      }
      for (const a of [2.06, 4.37]) {  // repères crème dans les ouvertures
        g.beginPath(); g.arc(Math.cos(a) * R, Math.sin(a) * R, R * 0.0045, 0, TAU);
        g.fillStyle = rgba(COLOR.white, 0.6); g.fill();
      }
    },

    // Larges arcs vert forêt (la signature extérieure), aux extrémités adoucies.
    outerArcs(g) {
      const r = R * 0.905;
      for (const [a0, a1] of [[-1.25, -0.42], [2.75, 3.55], [1.75, 2.45], [0.35, 1.05]]) {
        // Extrémités en fondu (trois passes en escalier se voyaient en 1080p).
        const fade = (k) => { const e = Math.min(1, k / 0.18, (1 - k) / 0.18); return e * e * (3 - 2 * e); };
        gradientArc(g, r, a0, a1, R * 0.046, (k) => [COLOR.deep, 0.55 * fade(k)]);
        gradientArc(g, r, a0, a1, R * 0.03, (k) => [COLOR.blue, 0.34 * fade(k)]);
      }
    },

    // Cercle doré, ouvert sur un côté, point crème à son sommet.
    goldRing(g) {
      const r = R * 0.79;
      arc(g, r, 0, TAU);
      glowStroke(g, Math.max(1, R * 0.0014), COLOR.blue, 0.4, 0);
      // Arc en dégradé : crème brillant en tête, doré, puis il s'efface vers le vert (comme la référence).
      const a0 = -Math.PI / 2 - 0.35, span = Math.PI * 1.45;
      const golden = (k, strength) => {
        const rise = Math.min(1, k / 0.12);
        const light = rise * rise * (3 - 2 * rise) * Math.pow(1 - k, 1.3);  // montée rapide, longue extinction
        const color = k < 0.25 ? mix(COLOR.white, COLOR.gold, k / 0.25) : mix(COLOR.gold, COLOR.blue, (k - 0.25) / 0.75);
        return [color, strength * light];
      };
      gradientArc(g, r, a0, a0 + span, R * 0.016, (k) => golden(k, 0.24), 32);
      gradientArc(g, r, a0, a0 + span, Math.max(1.5, R * 0.0032), (k) => golden(k, 1), 32);
      glowDot(g, 0, -r, R * 0.012, COLOR.gold, 1);
    },

    // Graduations fines (couronne de petits traits).
    ticks(g) {
      const r = R * 0.7;
      g.lineWidth = Math.max(1, R * 0.0018);
      g.strokeStyle = rgba(COLOR.white, 0.35);
      g.beginPath();
      for (let i = 0; i < 120; i++) {
        const a = (i / 120) * TAU;
        if (Math.abs(Math.sin(a * 2)) < 0.04) continue;  // ouverture sur les axes
        g.moveTo(Math.cos(a) * r, Math.sin(a) * r);
        g.lineTo(Math.cos(a) * (r + R * 0.014), Math.sin(a) * (r + R * 0.014));
      }
      g.stroke();
    },

    // Cercles fins et cercle pointillé intermédiaires.
    midRings(g) {
      for (const [r, a, arcs] of [[0.6, 0.5, [[0.1, 1.9], [2.1, 3.3], [3.5, 6.0]]],
                                   [0.52, 0.35, [[0.9, 2.8], [3.1, 5.0], [5.25, 6.95]]]]) {
        for (const [a0, a1] of arcs) {
          arc(g, R * r, a0, a1);
          glowStroke(g, Math.max(1, R * 0.0013), COLOR.blue, a, 0);
        }
      }
      // Pointillé qui s'interrompt sur deux secteurs : sa rotation se voit.
      dots(g, R * 0.565, 96, R * 0.0035, COLOR.white, 0.45, (a) => (a > 1.2 && a < 1.55) || (a > 4.3 && a < 4.5));
    },

    // Anneau vert juste autour de l'anneau central.
    innerRing(g) {
      for (const [a0, a1] of [[0.0, 2.7], [2.9, 5.0], [5.15, 6.2]]) {
        arc(g, R * 0.445, a0, a1);
        glowStroke(g, Math.max(1, R * 0.0035), COLOR.blue, 0.55, R * 0.02);
      }
      arc(g, R * 0.47, 0.3, 2.6);
      glowStroke(g, Math.max(1, R * 0.0014), COLOR.cyan, 0.35, 0);
    },

    // Cercle pointillé très fin le plus proche du centre (le centre lui-même reste vide).
    iris(g) {
      dots(g, R * 0.2, 72, R * 0.0022, COLOR.cyan, 0.35, (a) => a > 2.4 && a < 3.0);
    },
  };

  // Couches pré-rendues : vitesse (rad/s), sens, réaction audio (enveloppe et gain), opacité,
  // multiplicateurs de vitesse par état. Les axes restent fixes.
  const LAYERS = [
    { key: "axes", speed: 0, dir: 1, react: "outer", gain: 0.004, alpha: 0.9, contract: 0.0, boost: {} },
    { key: "outerRings", speed: 0.02, dir: 1, react: "outer", gain: 0.006, alpha: 0.9, contract: 0.008, boost: {} },
    { key: "outerArcs", speed: 0.09, dir: -1, react: "outer", gain: 0.01, alpha: 1.0, contract: 0.014, boost: { thinking: 2.6, listening: 1.3 } },
    { key: "goldRing", speed: 0.07, dir: 1, react: "middle", gain: 0.014, alpha: 0.95, contract: 0.012, boost: { thinking: 2.4 } },
    { key: "ticks", speed: 0.05, dir: -1, react: "middle", gain: 0.016, alpha: 0.8, contract: 0.01, boost: { thinking: 2.6 } },
    { key: "midRings", speed: 0.04, dir: 1, react: "inner", gain: 0.02, alpha: 0.9, contract: 0.0, boost: { listening: 1.6, thinking: 2.2 } },
    { key: "innerRing", speed: 0.12, dir: -1, react: "inner", gain: 0.025, alpha: 0.95, contract: 0.0, boost: { thinking: 3.0, listening: 1.4 } },
    { key: "iris", speed: 0.06, dir: 1, react: "core", gain: 0.03, alpha: 0.8, contract: 0.0, boost: { thinking: 3 } },
  ].map((layer, i) => ({ ...layer, angle: layer.speed ? i * 1.3 : 0, mul: 1 }));

  // --- Éléments dynamiques ---------------------------------------------------------------------

  const TRACKS = [0.52, 0.6, 0.7, 0.79, 0.905, 0.965];
  const comets = [];
  // Points lumineux sur certaines orbites (crème, un doré), comme des satellites.
  const orbitals = [
    { r: 0.445, speed: 0.05, shape: "dot", angle: -0.95 },
    { r: 0.445, speed: 0.05, shape: "dot", angle: 2.05 },
    { r: 0.36, speed: -0.07, shape: "dot", angle: 0.55 },
    { r: 0.6, speed: -0.03, shape: "gold", angle: 3.9 },
    { r: 0.79, speed: 0.02, shape: "small", angle: 0.9 },
  ];
  const particles = [];
  const prand = rng(7);
  const CENTER_FREE = 0.4;  // aucune poussière dans le centre : il reste vide
  for (let i = 0; i < 80; i++) particles.push(newParticle(true));

  function newParticle(anywhere) {
    return {
      r: anywhere ? CENTER_FREE + prand() * 0.6 : 0.95 + prand() * 0.1,
      a: prand() * TAU,
      drift: (prand() - 0.5) * 0.08,
      phase: prand() * TAU,
      freq: 0.3 + prand() * 1.5,
      size: 0.6 + prand() * 1.0,
      fall: 0.4 + prand() * 0.9,
    };
  }

  function spawnComet() {
    comets.push({
      r: TRACKS[Math.floor(Math.random() * TRACKS.length)],
      a: Math.random() * TAU,
      speed: (0.3 + Math.random() * 0.6) * (Math.random() > 0.5 ? 1 : -1),
      len: 0.15 + Math.random() * 0.35,
      life: 0,
      span: 1.6 + Math.random() * 2.4,
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
    R = Math.min(W, H) * 0.36;  // place pour les axes et la ligne de voix
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
    const alpha = layer.alpha * (0.35 + 0.65 * face.cur.glow) + env * 0.15;
    ctx.save();
    ctx.globalAlpha = clamp01(alpha);
    ctx.translate(CX, CY);
    ctx.rotate(layer.angle);
    ctx.scale(scale, scale);
    ctx.drawImage(img, -img.width / 2, -img.height / 2);
    ctx.restore();
  }

  function drawHalo() {
    const g = ctx.createRadialGradient(CX, CY, R * 0.2, CX, CY, R * 1.1);
    const a = 0.05 + 0.07 * face.cur.glow + 0.06 * face.env.middle;
    g.addColorStop(0, rgba(COLOR.deep, a));
    g.addColorStop(0.6, rgba(COLOR.deep, a * 0.5));
    g.addColorStop(1, rgba(COLOR.deep, 0));
    ctx.fillStyle = g;
    ctx.fillRect(CX - R * 1.2, CY - R * 1.2, R * 2.4, R * 2.4);
  }

  // Cercle de points qui s'allument avec l'activité et la voix (jamais un égaliseur).
  function drawInnerTicks(t, angle) {
    const n = 84;
    const r0 = R * 0.645;
    const c = face.cur;
    ctx.save();
    ctx.translate(CX, CY);
    ctx.rotate(angle);
    for (let i = 0; i < n; i++) {
      const a = (i / n) * TAU;
      const wave = 0.5 + 0.5 * Math.sin(i * 0.45 - t * (0.6 + c.speed));
      const alpha = 0.12 + 0.3 * c.glow * wave + 0.45 * face.env.inner * wave;
      ctx.fillStyle = rgba(wave > 0.9 ? COLOR.white : COLOR.cyan, alpha);
      ctx.beginPath();
      ctx.arc(Math.cos(a) * r0, Math.sin(a) * r0, Math.max(0.8, R * 0.0035), 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  }

  // Anneau central crème et or, lumineux, qui respire avec la voix ; à l'intérieur, l'obscurité.
  let coreSweep = 0;

  function drawCore(t, dt = 1 / 60) {
    const c = face.cur;
    coreSweep += dt * (0.25 + 0.55 * c.speed + 1.2 * face.env.core) * SPIN * 0.6;
    const pulse = c.pulseDepth * (0.5 + 0.5 * Math.sin(face.pulsePhase));
    const lvl = face.env.core;
    const rc = R * 0.36 * (1 + 0.02 * pulse + 0.035 * lvl);
    const glow = c.glow;

    ctx.globalCompositeOperation = "source-over";
    const pupil = ctx.createRadialGradient(CX, CY, 0, CX, CY, rc);
    // Intérieur sombre (sans « donut » clair au milieu), lueur verte seulement contre l'anneau.
    pupil.addColorStop(0, rgba(COLOR.pupil, 0.9));
    pupil.addColorStop(0.72, rgba(COLOR.pupil, 0.86));
    pupil.addColorStop(1, rgba(COLOR.deep, 0.55 + 0.2 * glow + 0.2 * lvl));
    ctx.fillStyle = pupil;
    ctx.beginPath();
    ctx.arc(CX, CY, rc, 0, TAU);
    ctx.fill();
    ctx.globalCompositeOperation = "lighter";

    // Halo vert de part et d'autre de l'anneau.
    const halo = ctx.createRadialGradient(CX, CY, rc * 0.8, CX, CY, rc * 1.28);
    halo.addColorStop(0, rgba(COLOR.blue, 0));
    halo.addColorStop(0.45, rgba(COLOR.blue, 0.1 + 0.12 * glow + 0.16 * lvl + 0.08 * pulse));
    halo.addColorStop(1, rgba(COLOR.blue, 0));
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(CX, CY, rc * 1.28, 0, TAU);
    ctx.fill();

    for (const [width, color, alpha] of [[R * 0.026, COLOR.gold, 0.1 + 0.12 * glow + 0.15 * lvl],
                                        [R * 0.01, COLOR.gold, 0.5 + 0.35 * glow + 0.2 * lvl],
                                        [Math.max(1, R * 0.0026), COLOR.white, 0.3 + 0.35 * Math.max(glow, lvl)]]) {
      ctx.beginPath();
      ctx.arc(CX, CY, rc, 0, TAU);
      ctx.lineWidth = width;
      ctx.strokeStyle = rgba(color, alpha);
      ctx.stroke();
    }
    ctx.lineCap = "round";
    for (let s = 0; s < 8; s++) {  // reflet : tête crème, traîne dorée qui s'efface
      const a1 = coreSweep - s * 0.09, a0 = a1 - 0.09;
      ctx.beginPath();
      ctx.arc(CX, CY, rc, a0, a1);
      ctx.lineWidth = Math.max(1, R * 0.006 * (1 - s / 10));
      ctx.strokeStyle = rgba(s === 0 ? COLOR.white : COLOR.gold, (0.5 + 0.4 * glow) * (1 - s / 8));
      ctx.stroke();
    }
    ctx.lineCap = "butt";
  }

  // Segments qui glissent le long des orbites (surtout en réflexion).
  function drawComets(dt) {
    const c = face.cur;
    const wanted = Math.round(1 + c.complexity * 9);
    if (comets.length < wanted && Math.random() < dt * (0.8 + 4 * c.complexity)) spawnComet();
    ctx.save();
    ctx.translate(CX, CY);
    ctx.lineCap = "round";
    for (let i = comets.length - 1; i >= 0; i--) {
      const k = comets[i];
      k.life += dt;
      k.a += k.speed * dt * (0.4 + c.speed);
      const fade = Math.min(1, k.life / 0.6, (k.span - k.life) / 0.8);
      if (k.life > k.span) { comets.splice(i, 1); continue; }
      const steps = 6;
      for (let s = 0; s < steps; s++) {
        const f = s / steps;
        const dir = Math.sign(k.speed);
        const a1 = k.a - dir * k.len * f;
        const a0 = a1 - dir * (k.len / steps);
        ctx.beginPath();
        ctx.arc(0, 0, k.r * R, Math.min(a0, a1), Math.max(a0, a1));
        ctx.lineWidth = Math.max(1.2, R * 0.0045 * (1 - f * 0.6));
        ctx.strokeStyle = rgba(s === 0 ? COLOR.white : s < 3 ? COLOR.gold : COLOR.blue,
                               fade * (0.9 - f * 0.8) * (0.45 + 0.55 * c.glow));
        ctx.stroke();
      }
      // Tête de la traînée : petite étincelle.
      glowDot(ctx, Math.cos(k.a) * k.r * R, Math.sin(k.a) * k.r * R, R * 0.004, COLOR.gold, fade * (0.5 + 0.5 * c.glow));
    }
    ctx.restore();
  }

  function drawOrbitals(dt) {
    const c = face.cur;
    ctx.save();
    ctx.translate(CX, CY);
    for (const o of orbitals) {
      o.angle += o.speed * dt * (0.5 + c.speed) * SPIN;
      const r = o.r * R * (1 - 0.01 * c.focus);
      const x = Math.cos(o.angle) * r, y = Math.sin(o.angle) * r;
      const s = R * (o.shape === "small" ? 0.008 : 0.012);
      const twinkle = 0.85 + 0.15 * Math.sin(now() * 2.3 + o.r * 40);
      glowDot(ctx, x, y, s * 0.8, o.shape === "gold" ? COLOR.gold : mix(COLOR.white, COLOR.gold, 0.35),
              (0.7 + 0.3 * c.glow) * twinkle);
    }
    ctx.restore();
  }

  function drawParticles(t, dt) {
    if (effect === "hyperspace") return;  // ciel étoilé immobile : pas de poussières en orbite par-dessus
    const c = face.cur;
    ctx.save();
    ctx.translate(CX, CY);
    for (let i = 0; i < particles.length; i++) {
      const p = particles[i];
      p.a += p.drift * dt * (0.3 + c.speed);
      p.r -= dt * 0.03 * p.fall * c.complexity * c.complexity;
      if (p.r < CENTER_FREE) particles[i] = newParticle(false);
      const tw = 0.5 + 0.5 * Math.sin(t * p.freq * TAU + p.phase);
      const alpha = (0.12 + 0.75 * tw) * (0.6 + 0.4 * c.complexity) * (0.6 + 0.4 * c.glow);
      ctx.fillStyle = rgba(tw > 0.92 ? COLOR.white : tw > 0.75 ? COLOR.gold : COLOR.cyan, alpha);
      const size = Math.max(1.5, p.size * DPR * Math.max(1, R / 260));  // visibles aussi en 1080p
      const x = Math.cos(p.a) * p.r * R, y = Math.sin(p.a) * p.r * R;
      if (tw > 0.88) glowDot(ctx, x, y, size * 0.6, COLOR.gold, alpha);  // éclat : petit halo
      else ctx.fillRect(x - size / 2, y - size / 2, size, size);
    }
    ctx.restore();
  }

  // Ligne de voix sous le visage : points fins, quelques traits dorés qui suivent la parole, jamais agressifs.
  const voiceBars = Array.from({ length: 41 }, (_, i) => ({ h: 0, seed: Math.sin(i * 12.9898) * 43758.5453 % 1 }));

  function drawVoiceLine(t, dt) {
    const y = CY + R * 1.28;
    if (y > H - R * 0.05) return;  // écran trop bas : pas de place
    const c = face.cur;
    const lvl = Math.max(face.env.core, face.state === "listening" ? 0.15 : 0);
    const half = R * 0.62, n = voiceBars.length;
    ctx.save();
    ctx.lineCap = "round";
    for (let i = 0; i < n; i++) {
      const f = i / (n - 1) * 2 - 1;  // -1 .. 1
      const x = CX + f * half;
      const shape = Math.pow(1 - Math.abs(f), 1.4);
      const bar = voiceBars[i];
      const wiggle = 0.5 + 0.5 * Math.sin(t * (3 + Math.abs(bar.seed) * 5) + i);
      bar.h = follow(bar.h, lvl * shape * wiggle, 0.08, 0.25, dt);
      const h = R * (0.006 + 0.11 * bar.h);
      const strong = i % 4 === 0 || bar.h > 0.25;
      ctx.strokeStyle = rgba(strong ? COLOR.gold : COLOR.blue, (0.18 + 0.5 * c.glow * shape + 0.4 * bar.h) * (0.4 + 0.6 * shape));
      ctx.lineWidth = Math.max(1, R * 0.003);
      ctx.beginPath(); ctx.moveTo(x, y - h); ctx.lineTo(x, y + h); ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(CX, y, R * 0.007, 0, TAU);
    ctx.fillStyle = rgba(COLOR.white, 0.5 + 0.4 * c.glow);
    ctx.fill();
    ctx.restore();
  }

  // --- Boucle ----------------------------------------------------------------------------------

  let last = now();
  let innerAngle = 0;
  let frames = 0, fpsAt = now(), fps = 0;

  function update(t, dt) {
    const k = 1 - Math.exp(-dt / 0.9);
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
      layer.angle += dt * layer.speed * layer.dir * face.cur.speed * layer.mul * SPIN;
    }
    innerAngle += dt * 0.12 * face.cur.speed * (face.state === "listening" ? 1.6 : 1) * SPIN;
  }

  function render(t, dt) {
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, W, H);
    if (BEHIND.has(effect)) {
      // Effet derrière le visage (hyperespace) : dessiné dans ce canevas, puis caché par un disque sous le visage.
      paintBehind(ctx, W, H);
      const disc = ctx.createRadialGradient(CX, CY, R * 0.9, CX, CY, R * 1.06);
      disc.addColorStop(0, COLOR.background[1]);
      disc.addColorStop(1, "rgba(0, 0, 0, 0)");
      ctx.fillStyle = COLOR.background[1];
      ctx.beginPath(); ctx.arc(CX, CY, R * 0.9, 0, TAU); ctx.fill();
      ctx.fillStyle = disc;
      ctx.beginPath(); ctx.arc(CX, CY, R * 1.06, 0, TAU); ctx.fill();
    }
    ctx.globalCompositeOperation = "lighter";
    drawHalo();
    for (const layer of LAYERS.slice(0, 7)) drawLayer(layer, t);
    drawInnerTicks(t, innerAngle);
    drawCore(t, dt);
    drawLayer(LAYERS[7], t);
    drawComets(dt);
    drawParticles(t, dt);
    drawOrbitals(dt);
    drawVoiceLine(t, dt);
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
      console.error("Visage ORION :", err);
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
          (DEMO ? "\n1 veille · 2 écoute · 3 réflexion · 4 parole · A cycle auto · T jour/nuit · C couleur · R arc-en-ciel"
            + " · N Noël · H Halloween · B anniversaire · Y nouvel an · V Saint-Valentin · P Saint-Patrick"
            + " · F 14 Juillet · O 1er avril · M 1er mai · W Star Wars (X : saut) · J naissance · U rendez-vous" : "");
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
        else if (FORCED_THEME) setTheme(FORCED_THEME);
        else setTheme(data.theme || theme, Number.isFinite(data.hue) ? data.hue : undefined, data.hues || undefined,
                      data.palette || undefined);
        const party = data.theme !== "error" && !FORCED_THEME;
        showGreeting(party ? data.greeting : "");
        setEffect(party ? data.effect : "");
        showUpcoming(data.upcoming);
        showErrors(data.theme === "error" ? data.error_messages : []);
        if (PREVIEW_ERROR) previewError();
        if (data.audio_source === "measured" || data.audio_source === "external") setAudioLevel(data.audio_level);
        face.lastMessage = now();
      } catch (err) {
        console.warn("Visage ORION : message ignoré", err);
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
      if (event.key === "c" || event.key === "C") {  // démo : une couleur au hasard
        const h = Math.floor(Math.random() * 360);
        setTheme(`teinte${h}`, h);
      }
      if (event.key === "r" || event.key === "R") setTheme("arcenciel");
      const parties = { n: ["noel", [0, 130], "Joyeux Noël", "snow", "noel"], h: ["halloween", [28, 272], "Joyeux Halloween", "embers", "halloween"],
                        b: ["anniversaire", "rainbow", "Joyeux anniversaire !", "confetti"],
                        y: ["nouvelan", [44, "night", 44, 205], "Bonne année !", "sparkle", "nouvelan"],
                        v: ["saintvalentin", [342, 325], "Joyeuse Saint-Valentin", "hearts", "saintvalentin"],
                        p: ["saintpatrick", [130, 155], "Joyeuse Saint-Patrick", "clovers", "saintpatrick"],
                        f: ["quatorzejuillet", null, "Bonne fête nationale", "sparkle", "tricolore"],
                        o: ["poissonavril", [188, 28], "Poisson d'avril !", "fish", "poissonavril"],
                        m: ["muguet", [130, "night"], "Joyeux 1er mai", "lily", "muguet"],
                        w: ["starwars", [53, "night"], "", "hyperspace", "starwars"],
                        j: ["naissancejarvis", [205, 44], "Joyeux anniversaire JARVIS : 1 an", "sparkle", "naissancejarvis"] };
      if ((event.key === "x" || event.key === "X") && effect === "hyperspace") {  // démo : sauter maintenant
        hyperJumpAt = now();
      }
      if (event.key === "u" || event.key === "U") {  // démo : échéance dans 9 minutes
        showUpcoming({ label: "Dentiste", kind: "event", seconds: 540, window: 900 });
      }
      const party = parties[event.key.toLowerCase()];
      if (party) { setTheme(party[0], undefined, party[1], party[4]); showGreeting(party[2]); setEffect(party[3]); }
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
