/*
 * Visage de JARVIS : rendu canvas piloté uniquement par un état visuel.
 *
 *   JarvisFace.setVisualState("standby" | "listening" | "thinking" | "speaking")
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
    Object.assign(COLOR, palette);
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
                         vy: 0, va: 0, x: w / 2, y: h / 2, r: (0.5 + Math.random() * 0.9) * s, tw: Math.random() * 6 });
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
                    hyperspace: 70 }[effect] || 40;
    hyperStart = now();
    for (let i = 0; i < count; i++) fxParticles.push(spawn(effect, true));
    if (!fxRunning) { fxRunning = true; requestAnimationFrame(drawEffect); }
  }

  // Hyperespace comme dans les films, toutes les 54 s : ciel étoilé immobile (45 s), toutes les étoiles s'étirent
  // ensemble en longs traits partant du centre (1,6 s), traits qui défilent (6 s), retour aux étoiles (1,2 s).
  const HYPER = { cruise: 45, stretch: 1.6, tunnel: 6, exit: 1.2 };
  let hyperStart = 0, hyperPhase = "cruise", hyperK = 0;

  function hyperStep() {
    const cycle = HYPER.cruise + HYPER.stretch + HYPER.tunnel + HYPER.exit;
    let t = (now() - hyperStart) % cycle;
    const ease = (x) => x * x * (3 - 2 * x);
    const before = hyperPhase;
    if (t < HYPER.cruise) { hyperPhase = "cruise"; hyperK = 0; }
    else if ((t -= HYPER.cruise) < HYPER.stretch) { hyperPhase = "stretch"; hyperK = ease(t / HYPER.stretch); }
    else if ((t -= HYPER.stretch) < HYPER.tunnel) { hyperPhase = "tunnel"; hyperK = 1; }
    else { hyperPhase = "exit"; hyperK = 1 - ease((t - HYPER.tunnel) / HYPER.exit); }
    if (before === "exit" && hyperPhase === "cruise") {  // retour : nouveau ciel étoilé
      for (const p of fxParticles) Object.assign(p, spawn("hyperspace", true));
    }
    if (hyperPhase === "tunnel" || hyperPhase === "exit") {  // lueur bleue du tunnel
      const w = fx.width, h = fx.height;
      const glow = fxg.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.hypot(w, h) / 2);
      glow.addColorStop(0, `rgba(120, 170, 255, ${0.04 * hyperK})`);
      glow.addColorStop(1, "rgba(120, 170, 255, 0)");
      fxg.fillStyle = glow;
      fxg.fillRect(0, 0, w, h);
    }
  }

  // Bord extérieur du visage (anneau le plus large), en fraction de la demi-diagonale : étoiles et traits au-delà.
  function hyperInner(w, h) {
    return Math.min(0.95, (Math.min(w, h) * 0.47) / (Math.hypot(w, h) / 2));
  }

  function drawHyperStar(p, w, h) {
    const max = Math.hypot(w, h) / 2, cx = w / 2, cy = h / 2, dx = Math.cos(p.a), dy = Math.sin(p.a);
    const inner = hyperInner(w, h);
    if (hyperPhase === "tunnel") {  // les traits filent vers l'extérieur, depuis le bord du visage
      p.d += 0.004 + (p.d - inner) * 0.04;
      if (p.d > 1.15) p.d = inner + Math.random() * 0.04;
    }
    p.x = cx + dx * p.d * max; p.y = cy + dy * p.d * max;
    if (hyperK < 0.02) {  // ciel étoilé : points qui scintillent
      fxg.fillStyle = `rgba(255, 255, 255, ${0.6 + 0.15 * Math.sin(now() * 0.8 + p.tw)})`;
      fxg.beginPath(); fxg.arc(p.x, p.y, p.r, 0, Math.PI * 2); fxg.fill();
      return;
    }
    // Trait : de l'étoile vers le visage, jamais en deçà de son bord (le saut part de l'extérieur du visage).
    const tail = Math.min((p.d - inner) * max, (p.d - inner) * max * 0.9 * hyperK + p.r);
    const blue = hyperPhase === "tunnel" ? 1 : hyperK;
    fxg.strokeStyle = `rgba(${Math.round(255 - 50 * blue)}, ${Math.round(255 - 20 * blue)}, 255, ${0.4 + 0.45 * hyperK})`;
    fxg.lineWidth = Math.max(0.8, p.r * (0.8 + 0.6 * hyperK));
    fxg.lineCap = "round";
    fxg.beginPath(); fxg.moveTo(p.x - dx * tail, p.y - dy * tail); fxg.lineTo(p.x, p.y); fxg.stroke();
  }

  function drawEffect() {
    if (!effect) { fxRunning = false; return; }
    const w = fx.width, h = fx.height;
    fxg.clearRect(0, 0, w, h);
    if (effect === "hyperspace") hyperStep();
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
      } else if (effect === "hyperspace") {
        drawHyperStar(p, w, h);
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
    if (effect === "hyperspace") return;  // ciel étoilé immobile : pas de poussières en orbite par-dessus
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
          (DEMO ? "\n1 veille · 2 écoute · 3 réflexion · 4 parole · A cycle auto · T jour/nuit · C couleur · R arc-en-ciel"
            + " · N Noël · H Halloween · B anniversaire · Y nouvel an · V Saint-Valentin · P Saint-Patrick"
            + " · F 14 Juillet · O 1er avril · M 1er mai · W Star Wars (X : saut) · J JARVIS · U rendez-vous" : "");
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
                        w: ["starwars", [53, "night"], "Que la Force soit avec vous", "hyperspace", "starwars"],
                        j: ["naissancejarvis", [205, 44], "Joyeux anniversaire JARVIS : 1 an", "sparkle", "naissancejarvis"] };
      if ((event.key === "x" || event.key === "X") && effect === "hyperspace") {  // démo : sauter maintenant
        hyperStart = now() - HYPER.cruise + 0.5;
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
