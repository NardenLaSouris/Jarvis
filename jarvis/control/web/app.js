/*
 * JARVIS Control : interface. Toutes les demandes passent par le pont local (/bridge), qui seul parle au Core.
 * Aucun texte venu du Core n'est inséré comme HTML : le DOM est construit élément par élément.
 */
(() => {
  "use strict";

  const KEY = new URLSearchParams(location.hash.slice(1)).get("key") || sessionStorage.getItem("jc-key") || "";
  sessionStorage.setItem("jc-key", KEY);
  history.replaceState(null, "", location.pathname);

  const DAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"];
  const TOOL_LABELS = {
    light_on: "Allumer la lumière", light_off: "Éteindre la lumière", light_toggle: "Inverser la lumière",
    set_brightness: "Régler la luminosité", set_color: "Changer la couleur", set_color_temperature: "Température de blanc",
    open_application: "Ouvrir une application", close_application: "Fermer une application", open_url: "Ouvrir une page Web",
    set_volume: "Régler le volume", mute_volume: "Couper le son", unmute_volume: "Remettre le son",
    create_timer: "Lancer un minuteur", create_reminder: "Programmer un rappel", cancel_timer: "Annuler un minuteur",
    cancel_reminder: "Annuler un rappel", lock_pc: "Verrouiller le PC",
  };
  const INFO_TOOLS = new Set(["get_time", "get_date", "get_weather", "system_info", "list_running_applications",
    "list_timers", "list_reminders"]);
  const PARAM_LABELS = {
    room: "Pièce", device: "Appareil", brightness: "Luminosité (%)", color: "Couleur", temperature: "Kelvins",
    application: "Application", url: "Adresse", volume: "Volume (%)", duration: "Durée (« 10 minutes »)",
    delay: "Délai (« 20 minutes »)", message: "Message", timer_id: "Numéro", reminder_id: "Numéro",
  };
  const CHOICE_LABELS = { all: "Toutes les pièces" };

  const state = { view: "dashboard", tools: null, settings: null, timer: null, devices: null };
  const view = document.getElementById("view");

  // --- Outils ---------------------------------------------------------------------------------------

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "class") node.className = value;
      else if (key === "value") node.value = value;
      else if (key === "checked") node.checked = Boolean(value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(method, path, body) {
    const response = await fetch("/bridge" + path, {
      method,
      headers: { "X-Control-Key": KEY, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw new Error(response.status === 403 ? "Session expirée : rouvrez JARVIS Control." : `Erreur ${response.status}`);
    const data = await response.json();
    if (!data.ok) throw new Error(data.error);
    return data.data;
  }

  function toast(message, kind = "") {
    const node = el("div", { class: `toast ${kind}` }, message);
    document.getElementById("toasts").append(node);
    setTimeout(() => node.remove(), kind === "err" ? 7000 : 3500);
  }

  async function act(promise, success) {
    try {
      const result = await promise;
      if (success) toast(success, "ok");
      return result;
    } catch (error) {
      toast(error.message, "err");
      throw error;
    }
  }

  const cap = (text) => text ? text[0].toUpperCase() + text.slice(1) : "";
  const dot = (online) => el("span", { class: `dot ${online === true ? "ok" : online === false ? "err" : ""}` });
  const when = (iso) => iso ? new Date(iso).toLocaleString("fr-FR", { weekday: "short", hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }) : "—";
  const clock = (iso) => iso ? new Date(iso).toLocaleTimeString("fr-FR") : "—";

  function setConnection(online, text) {
    const node = document.getElementById("connection");
    node.replaceChildren(dot(online), text);
  }

  async function tools() {
    if (!state.tools) state.tools = await api("GET", "/tools");
    return state.tools;
  }

  function triggerText(trigger) {
    if (trigger.type === "time") {
      const days = trigger.days.length === 0 || trigger.days.length === 7 ? "Tous les jours"
        : trigger.days.join() === "0,1,2,3,4" ? "Du lundi au vendredi"
        : trigger.days.join() === "5,6" ? "Le week-end" : trigger.days.map((d) => DAYS[d]).join(", ");
      return `${days} à ${trigger.time}`;
    }
    if (trigger.type === "interval") return `Toutes les ${trigger.minutes} min`;
    return "Manuellement";
  }

  function actionText(action) {
    if (action.type === "say") return `JARVIS dit « ${action.text} »`;
    if (action.type === "wait") return `Attendre ${action.seconds} s`;
    const params = Object.entries(action.parameters || {}).map(([k, v]) => `${PARAM_LABELS[k] || k} : ${CHOICE_LABELS[v] || v}`);
    return `${TOOL_LABELS[action.tool] || action.tool}${params.length ? " (" + params.join(", ") + ")" : ""}`;
  }

  // --- Tableau de bord ------------------------------------------------------------------------------

  async function dashboard() {
    const status = await api("GET", "/status");
    setConnection(true, "Core connecté");
    const llm = status.llm;
    const lights = status.lights;
    const schedule = [...status.schedule.timers.map((t) => `Minuteur : encore ${t.remaining} (${t.ends_at})`),
      ...status.schedule.reminders.map((r) => `Rappel « ${r.message} » à ${r.at}`)];
    view.replaceChildren(
      el("h1", {}, "Tableau de bord"),
      el("div", { class: "grid" },
        el("div", { class: "card" }, el("h3", {}, "JARVIS"), el("div", { class: "big" }, dot(true), "Opérationnel"),
          el("div", { class: "muted small" }, `Démarré depuis ${Math.round(status.core.uptime_s / 60)} min`)),
        el("div", { class: "card" }, el("h3", {}, "LLM"),
          el("div", { class: "big" }, dot(llm.online), llm.model || "—"),
          el("div", { class: "muted small" }, llm.online ? `Chargé : ${(llm.loaded || []).join(", ") || "aucun"}` : "Worker LLM injoignable",
            llm.fallback_model ? ` · secours : ${llm.fallback_model}` : "")),
        el("div", { class: "card" }, el("h3", {}, "Agents"),
          status.agents.length ? status.agents.map((a) => el("div", { class: "row" }, dot(a.online), cap(a.name))) : el("div", { class: "muted" }, "Aucun appareil configuré")),
        el("div", { class: "card" }, el("h3", {}, "Recherche Web"),
          el("div", { class: "big" }, dot(status.search.online), status.search.enabled === false ? "Désactivée" : status.search.online ? "SearXNG joignable" : "SearXNG injoignable")),
        el("div", { class: "card" }, el("h3", {}, "Lumières"),
          el("div", { class: "big" }, dot(lights.total ? lights.online === lights.total : null), `${lights.online}/${lights.total} connectées`),
          lights.items.map((l) => el("div", { class: "muted small" }, `${cap(l.name)} : ${l.online ? (l.on ? `allumée, ${l.brightness} %` : "éteinte") : "injoignable"}`))),
        el("div", { class: "card" }, el("h3", {}, "Routines"),
          el("div", { class: "big" }, `${status.routines.enabled} active${status.routines.enabled > 1 ? "s" : ""}`, el("span", { class: "muted small" }, ` / ${status.routines.total}`)),
          status.routines.next.map((r) => el("div", { class: "muted small" }, `${r.name} : ${when(r.next_run)}`))),
        el("div", { class: "card" }, el("h3", {}, "Minuteurs et rappels"),
          schedule.length ? schedule.map((s) => el("div", { class: "small" }, s)) : el("div", { class: "muted" }, "Rien de prévu")),
        el("div", { class: "card" }, el("h3", {}, "Dernière activité"),
          status.last_activity ? [el("div", {}, status.last_activity.subject), el("div", { class: "muted small" }, clock(status.last_activity.time))]
            : el("div", { class: "muted" }, "Aucune")),
      ),
    );
  }

  // --- Routines -------------------------------------------------------------------------------------

  async function routines() {
    const list = await api("GET", "/routines");
    const rows = list.map((r) => el("div", { class: "card row" },
      el("label", { class: "switch", title: r.enabled ? "Désactiver" : "Activer" },
        el("input", { type: "checkbox", checked: r.enabled, onchange: async (e) => {
          await act(api("POST", `/routines/${r.id}/${e.target.checked ? "enable" : "disable"}`), e.target.checked ? "Routine activée" : "Routine désactivée").catch(() => {});
          show("routines");
        } }), el("span")),
      el("div", { class: "grow" },
        el("div", {}, el("strong", {}, r.name), r.running ? el("span", { class: "chip" }, "en cours") : null),
        el("div", { class: "muted small" }, triggerText(r.trigger), " · ", `${r.actions.length} action${r.actions.length > 1 ? "s" : ""}`,
          r.next_run ? ` · prochaine : ${when(r.next_run)}` : "",
          r.last_run ? ` · dernière : ${when(r.last_run)} ${r.last_status === "success" ? "✓" : "✗"}` : "")),
      el("button", { class: "btn", onclick: () => act(api("POST", `/routines/${r.id}/run`), `« ${r.name} » lancée`).then(() => setTimeout(() => show("routines"), 1500)).catch(() => {}) }, "▶ Tester"),
      el("button", { class: "btn", onclick: () => editor(r) }, "Modifier"),
      el("button", { class: "btn", onclick: () => act(api("POST", `/routines/${r.id}/duplicate`), "Routine dupliquée").then(() => show("routines")).catch(() => {}) }, "Dupliquer"),
      el("button", { class: "btn danger", onclick: async () => {
        if (!confirm(`Supprimer la routine « ${r.name} » ?`)) return;
        await act(api("DELETE", `/routines/${r.id}`), "Routine supprimée").catch(() => {});
        show("routines");
      } }, "Supprimer")));
    view.replaceChildren(
      el("div", { class: "toolbar" }, el("h1", { class: "grow" }, "Routines"),
        el("button", { class: "btn primary", onclick: () => editor(null) }, "+ Nouvelle routine")),
      rows.length ? el("div", { class: "list" }, rows) : el("div", { class: "empty" }, "Aucune routine. Créez la première avec « Nouvelle routine »."),
    );
  }

  function paramInput(name, spec, value, onchange, names = {}) {
    const label = PARAM_LABELS[name] || spec.description;
    let input;
    if (spec.choices && spec.choices.length) {
      const options = [...(spec.required || name === "room" ? [] : [el("option", { value: "" }, name === "device" ? "Appareil par défaut" : "—")]),
        ...spec.choices.map((c) => el("option", { value: c }, CHOICE_LABELS[c] || names[c] || c))];
      input = el("select", { onchange: (e) => onchange(e.target.value || undefined) }, options);
      input.value = value ?? (name === "room" ? spec.choices[0] : "");
      if (name === "room" && value === undefined) onchange(spec.choices[0]);
    } else if (spec.type === "int") {
      input = el("input", { type: "number", min: spec.minimum ?? undefined, max: spec.maximum ?? undefined, value: value ?? "",
        oninput: (e) => onchange(e.target.value === "" ? undefined : Number(e.target.value)) });
    } else {
      input = el("input", { type: "text", value: value ?? "", oninput: (e) => onchange(e.target.value.trim() || undefined) });
    }
    return el("label", { class: "field" }, label + (spec.required ? " *" : ""), input);
  }

  async function editor(routine) {
    const catalog = (await tools()).filter((t) => t.safe && !INFO_TOOLS.has(t.name));
    const names = Object.fromEntries((await api("GET", "/devices")).map((d) => [d.id, cap(d.name)]));
    const draft = routine ? JSON.parse(JSON.stringify({ name: routine.name, description: routine.description, enabled: routine.enabled, trigger: routine.trigger, actions: routine.actions }))
      : { name: "", description: "", enabled: true, trigger: { type: "time", time: "21:00", days: [] }, actions: [] };

    const render = () => {
      const trigger = draft.trigger;
      const triggerFields = trigger.type === "time"
        ? [el("label", { class: "field" }, "Heure", el("input", { type: "time", value: trigger.time, oninput: (e) => { trigger.time = e.target.value; } })),
          el("div", { class: "field" }, el("span", { class: "muted small" }, "Jours (aucun = tous)"),
            el("div", { class: "days" }, DAYS.map((d, i) => el("label", {}, el("input", { type: "checkbox", checked: trigger.days.includes(i),
              onchange: (e) => { trigger.days = e.target.checked ? [...trigger.days, i].sort() : trigger.days.filter((x) => x !== i); } }), d))))]
        : trigger.type === "interval"
          ? [el("label", { class: "field" }, "Toutes les (minutes)", el("input", { type: "number", min: 1, max: 1440, value: trigger.minutes, oninput: (e) => { trigger.minutes = Number(e.target.value); } }))]
          : [el("div", { class: "muted small" }, "Lancée seulement avec « Tester » (ou plus tard par une phrase ou un événement).")];

      const blocks = draft.actions.flatMap((action, index) => {
        const move = (delta) => { const [a] = draft.actions.splice(index, 1); draft.actions.splice(index + delta, 0, a); render(); };
        const controls = el("div", { class: "row" },
          el("select", { onchange: (e) => { draft.actions[index] = newAction(e.target.value, catalog); render(); } },
            [["tool", "Action"], ["say", "JARVIS dit"], ["wait", "Attendre"]].map(([v, t]) => el("option", { value: v, selected: action.type === v }, t))),
          el("div", { class: "grow" }),
          el("button", { class: "btn icon", disabled: index === 0, onclick: () => move(-1), title: "Monter" }, "↑"),
          el("button", { class: "btn icon", disabled: index === draft.actions.length - 1, onclick: () => move(1), title: "Descendre" }, "↓"),
          el("button", { class: "btn icon danger", onclick: () => { draft.actions.splice(index, 1); render(); }, title: "Supprimer" }, "✕"));
        let body;
        if (action.type === "say") {
          body = el("label", { class: "field" }, "Phrase", el("input", { type: "text", maxlength: 200, value: action.text, oninput: (e) => { action.text = e.target.value; } }));
        } else if (action.type === "wait") {
          body = el("label", { class: "field" }, "Secondes", el("input", { type: "number", min: 1, max: 600, value: action.seconds, oninput: (e) => { action.seconds = Number(e.target.value); } }));
        } else {
          const tool = catalog.find((t) => t.name === action.tool) || catalog[0];
          action.tool = tool.name;
          action.parameters = action.parameters || {};
          body = el("div", { class: "fields" },
            el("label", { class: "field" }, "Outil", el("select", { onchange: (e) => { action.tool = e.target.value; action.parameters = {}; render(); } },
              catalog.map((t) => el("option", { value: t.name, selected: t.name === tool.name }, TOOL_LABELS[t.name] || t.name)))),
            Object.entries(tool.parameters).map(([name, spec]) => paramInput(name, spec, action.parameters[name], (v) => {
              if (v === undefined) delete action.parameters[name]; else action.parameters[name] = v;
            }, names)));
        }
        return [el("div", { class: "arrow" }, "↓"), el("div", { class: `block ${action.type === "tool" ? "action" : action.type}` }, controls, body)];
      });

      view.replaceChildren(
        el("div", { class: "toolbar" }, el("h1", { class: "grow" }, routine ? `Modifier « ${routine.name} »` : "Nouvelle routine"),
          el("button", { class: "btn", onclick: () => show("routines") }, "Annuler"),
          el("button", { class: "btn primary", onclick: save }, "Enregistrer")),
        el("div", { class: "flow" },
          el("div", { class: "block" }, el("div", { class: "fields" },
            el("label", { class: "field" }, "Nom *", el("input", { type: "text", maxlength: 60, value: draft.name, oninput: (e) => { draft.name = e.target.value; } })),
            el("label", { class: "field" }, "Description", el("input", { type: "text", maxlength: 200, value: draft.description, oninput: (e) => { draft.description = e.target.value; } }))),
            el("label", { class: "row", style: "margin-top:10px" }, el("input", { type: "checkbox", checked: draft.enabled, onchange: (e) => { draft.enabled = e.target.checked; } }), "Activée")),
          el("div", { class: "arrow" }, "↓"),
          el("div", { class: "block trigger" }, el("h3", {}, "Déclencheur"),
            el("div", { class: "fields" }, el("label", { class: "field" }, "Type", el("select", { onchange: (e) => { draft.trigger = newTrigger(e.target.value); render(); } },
              [["time", "Heure"], ["interval", "Intervalle"], ["manual", "Manuel"]].map(([v, t]) => el("option", { value: v, selected: trigger.type === v }, t)))),
              triggerFields)),
          blocks,
          el("div", { class: "arrow" }, "↓"),
          el("div", { class: "row" },
            el("button", { class: "btn", onclick: () => { draft.actions.push(newAction("tool", catalog)); render(); } }, "+ Action"),
            el("button", { class: "btn", onclick: () => { draft.actions.push(newAction("wait")); render(); } }, "+ Attente"),
            el("button", { class: "btn", onclick: () => { draft.actions.push(newAction("say")); render(); } }, "+ JARVIS dit"))));
    };

    async function save() {
      try {
        await (routine ? api("PUT", `/routines/${routine.id}`, draft) : api("POST", "/routines", draft));
        toast("Routine enregistrée", "ok");
        show("routines");
      } catch (error) {
        toast(error.message, "err");
      }
    }

    render();
  }

  function newTrigger(type) {
    if (type === "time") return { type, time: "21:00", days: [] };
    if (type === "interval") return { type, minutes: 30 };
    return { type: "manual" };
  }

  function newAction(type, catalog = []) {
    if (type === "say") return { type, text: "" };
    if (type === "wait") return { type, seconds: 2 };
    return { type: "tool", tool: (catalog[0] || {}).name, parameters: {} };
  }

  // --- Appareils ------------------------------------------------------------------------------------

  async function devices() {
    const list = await api("GET", "/devices");
    const pcs = list.filter((d) => d.kind !== "light");
    const lights = list.filter((d) => d.kind === "light");
    const light = (d) => {
      const call = (tool, parameters, message) => act(api("POST", `/tools/${tool}`, { parameters: { room: d.id, ...parameters } }), message)
        .then(() => setTimeout(() => show("devices"), 300)).catch(() => {});
      const state = d.state || {};
      return el("div", { class: "card" },
        el("div", { class: "row" }, el("div", { class: "grow" }, el("h2", {}, dot(d.online), cap(d.name)),
          el("div", { class: "muted small" }, `Ampoule · ${d.address}`, d.online ? ` · ${state.on ? `allumée, ${state.brightness} %` : "éteinte"}` : " · injoignable")),
          el("button", { class: "btn", disabled: !d.online, onclick: () => call(state.on ? "light_off" : "light_on", {}) }, state.on ? "Éteindre" : "Allumer")),
        el("div", { class: "fields", style: "margin-top:12px" },
          el("label", { class: "field" }, "Luminosité", el("input", { type: "range", min: 1, max: 100, value: state.brightness || 100, disabled: !d.online,
            onchange: (e) => call("set_brightness", { brightness: Number(e.target.value) }) })),
          el("label", { class: "field" }, "Couleur", el("select", { disabled: !d.online, onchange: (e) => e.target.value && call("set_color", { color: e.target.value }) },
            el("option", { value: "" }, "—"), ["blanc chaud", "blanc neutre", "blanc froid", "rouge", "orange", "jaune", "vert", "cyan", "bleu", "violet", "magenta", "rose"]
              .map((c) => el("option", { value: c }, c)))),
          el("label", { class: "field" }, "Température (K)", el("input", { type: "number", min: 2700, max: 6500, step: 100, placeholder: "2700 à 6500", disabled: !d.online,
            onchange: (e) => e.target.value && call("set_color_temperature", { temperature: Number(e.target.value) }) }))),
        el("div", { class: "muted small", style: "margin-top:8px" }, "Alias : ", (d.aliases || []).map((a) => el("span", { class: "chip" }, a))));
    };
    view.replaceChildren(
      el("h1", {}, "Appareils"),
      el("h3", {}, "Ordinateurs"),
      el("div", { class: "grid" }, pcs.map((d) => el("div", { class: "card" },
        el("h2", {}, d.kind === "core" ? dot(null) : dot(d.online), cap(d.name), d.default ? el("span", { class: "chip" }, "par défaut") : null),
        el("div", { class: "muted small" }, d.kind === "core" ? "Core JARVIS · aucune action autorisée" : `PC · ${d.address} · ${d.online ? "agent connecté" : "agent injoignable"}`),
        el("div", { style: "margin-top:8px" }, d.features.map((f) => el("span", { class: "chip" }, f))),
        d.aliases.length ? el("div", { class: "muted small", style: "margin-top:6px" }, "Alias : ", d.aliases.join(", ")) : null))),
      el("h3", { style: "margin-top:22px" }, "Lumières"),
      lights.length ? el("div", { class: "grid" }, lights.map(light)) : el("div", { class: "empty" }, "Aucune lumière configurée."),
    );
  }

  // --- Historique -----------------------------------------------------------------------------------

  async function historyView() {
    const filters = state.historyFilters || (state.historyFilters = { kind: "routine", day: "", routine: "", result: "", search: "" });
    const items = await api("GET", `/history?kind=${filters.kind}&limit=500`);
    const routinesSeen = [...new Set(items.map((i) => i.routine).filter(Boolean))];
    const shown = items.filter((i) => (!filters.day || (i.time || "").startsWith(filters.day))
      && (!filters.routine || i.routine === filters.routine)
      && (!filters.result || (filters.result === "ok" ? i.success === true : i.success === false))
      && (!filters.search || `${i.subject} ${i.action || ""} ${i.message || ""}`.toLowerCase().includes(filters.search.toLowerCase())));
    const refresh = () => show("history");
    view.replaceChildren(
      el("h1", {}, "Historique"),
      el("div", { class: "toolbar" },
        el("select", { onchange: (e) => { filters.kind = e.target.value; refresh(); } },
          [["routine", "Routines"], ["tool", "Outils"], ["all", "Tout"]].map(([v, t]) => el("option", { value: v, selected: filters.kind === v }, t))),
        el("input", { type: "date", value: filters.day, onchange: (e) => { filters.day = e.target.value; refresh(); } }),
        el("select", { onchange: (e) => { filters.routine = e.target.value; refresh(); } },
          el("option", { value: "" }, "Toutes les routines"), routinesSeen.map((r) => el("option", { value: r, selected: filters.routine === r }, r))),
        el("select", { onchange: (e) => { filters.result = e.target.value; refresh(); } },
          [["", "Succès et erreurs"], ["ok", "Succès"], ["err", "Erreurs"]].map(([v, t]) => el("option", { value: v, selected: filters.result === v }, t))),
        el("input", { type: "search", placeholder: "Appareil, action…", value: filters.search, onchange: (e) => { filters.search = e.target.value; refresh(); } })),
      shown.length ? el("table", {},
        el("thead", {}, el("tr", {}, ["Heure", "Routine", "Événement", "Résultat"].map((h) => el("th", {}, h)))),
        el("tbody", {}, shown.map((i) => el("tr", {},
          el("td", { class: "small" }, when(i.time)),
          el("td", {}, i.routine || "—"),
          el("td", {}, i.action || i.subject, i.message ? el("div", { class: "muted small" }, i.message) : null),
          el("td", { class: i.success === true ? "ok" : i.success === false ? "err" : "" },
            i.success === true ? "✓ Succès" : i.success === false ? "✗ Erreur" : i.type.endsWith(".started") ? "Démarrée" : "—")))))
        : el("div", { class: "empty" }, "Aucun événement pour ces filtres."),
    );
  }

  // --- Paramètres -----------------------------------------------------------------------------------

  async function settingsView() {
    const s = await api("GET", "/settings");
    const form = { ...s, token: "" };
    const field = (label, input) => el("label", { class: "field" }, label, input);
    const check = (key, label) => el("label", { class: "row" }, el("input", { type: "checkbox", checked: form[key], onchange: (e) => { form[key] = e.target.checked; } }), label);
    view.replaceChildren(
      el("h1", {}, "Paramètres"),
      el("div", { class: "card", style: "max-width:720px" },
        el("div", { class: "fields" },
          field("Adresse du Core", el("input", { type: "text", value: form.core_url, oninput: (e) => { form.core_url = e.target.value; } })),
          field(`Jeton API ${s.token_configured ? "(configuré)" : "(manquant)"}`, el("input", { type: "password", placeholder: "Laisser vide pour garder l'actuel", autocomplete: "off", oninput: (e) => { form.token = e.target.value; } })),
          field("Nom du PC", el("input", { type: "text", maxlength: 40, value: form.pc_name, oninput: (e) => { form.pc_name = e.target.value; } })),
          field("Thème", el("select", { onchange: (e) => { form.theme = e.target.value; } },
            [["system", "Système"], ["dark", "Sombre"], ["light", "Clair"]].map(([v, t]) => el("option", { value: v, selected: form.theme === v }, t)))),
          field("Rafraîchissement (s)", el("input", { type: "number", min: 5, max: 3600, value: form.refresh_seconds, oninput: (e) => { form.refresh_seconds = Number(e.target.value); } }))),
        el("div", { class: "list", style: "margin-top:14px" },
          check("autostart", "Démarrer avec Windows (dans la zone de notification)"),
          check("notifications", "Notifications"),
          check("auto_connect", "Connexion automatique au Core")),
        el("div", { class: "row", style: "margin-top:16px" }, el("div", { class: "grow muted small" }, "Ctrl+Shift+J ouvre ou masque JARVIS Control."),
          el("button", { class: "btn primary", onclick: async () => {
            const body = { ...form };
            if (!body.token) delete body.token;
            delete body.token_configured;
            state.settings = await act(api("POST", "/settings", body), "Paramètres enregistrés").catch(() => null) || state.settings;
            applyTheme();
            show("settings");
          } }, "Enregistrer"))));
  }

  // --- Navigation -----------------------------------------------------------------------------------

  const VIEWS = { dashboard, routines, devices, history: historyView, settings: settingsView };

  function applyTheme() {
    document.documentElement.dataset.theme = (state.settings && state.settings.theme) || "system";
  }

  async function show(name) {
    state.view = name;
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    try {
      await VIEWS[name]();
    } catch (error) {
      if (name === "dashboard") setConnection(false, "Core injoignable");
      view.replaceChildren(el("h1", {}, name === "settings" ? "Paramètres" : "Connexion au Core"),
        el("div", { class: "banner" }, error.message),
        name === "settings" ? null : el("button", { class: "btn", onclick: () => show("settings") }, "Vérifier les paramètres"));
    }
  }

  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  document.addEventListener("keydown", (e) => { if (e.key === "F5") { e.preventDefault(); show(state.view); } });

  (async () => {
    try {
      state.settings = await api("GET", "/settings");
    } catch (error) {
      toast(error.message, "err");
    }
    applyTheme();
    await show("dashboard");
    setInterval(() => { if (state.view === "dashboard" && document.visibilityState === "visible") show("dashboard"); },
      ((state.settings && state.settings.refresh_seconds) || 15) * 1000);
  })();
})();
