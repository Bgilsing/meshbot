// meshbot control panel. Plain JS, no build step. Each tab is a render function below.
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const state = { schema: null, settings: null, status: null, drafts: {}, tab: null, timers: [], pg: null };

// ---------- helpers ----------
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
async function api(method, path, body) {
  const r = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {},
                                body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) { const msg = (data && data.error) || `${r.status} ${r.statusText}`; toast(msg, true); throw new Error(msg); }
  return data;
}
function toast(msg, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t); setTimeout(() => t.remove(), bad ? 7000 : 3000);
}
const bytes = s => new TextEncoder().encode(s).length;
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDT = ts => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
function ago(ts) { if (!ts) return "never"; const s = Date.now() / 1000 - ts;
  return s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; }
function outcomeBadge(o, onclick) {
  const cls = (o || "").split(":")[0].replace(/_after_\d+/, "").replace("send_failed", "error");
  return h("span", { class: "badge " + cls + (onclick ? " click" : ""), onclick, title: o }, o || "-");
}
function every(ms, fn) { fn(); state.timers.push(setInterval(fn, ms)); }
function modal(content) { $("#modal-body").replaceChildren(content); $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal-x").onclick = closeModal;
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
function channelNames() { return (state.settings?.channels || []).map(c => c.name); }
function dmNames() { return state.status?.direct?.conversations || []; }
async function loadDMs() { try { state.status = await api("GET", "/api/status"); } catch (e) { /* keep old */ } }
function channelSelect(value, { all = false, onchange, dm = false, extra = [] } = {}) {
  const s = h("select", { onchange });
  if (all) s.append(h("option", { value: "" }, "All channels"));
  for (const n of channelNames()) s.append(h("option", { value: n }, n));
  if (dm) for (const n of [...new Set([...dmNames(), ...extra])]) s.append(h("option", { value: n }, `${n} (DM)`));
  if (value && ![...s.options].some(o => o.value === value)) s.append(h("option", { value }, value));
  if (value !== undefined) s.value = value;
  return s;
}
async function loadSettings() { state.settings = await api("GET", "/api/settings"); }

// Render one schema field. Returns {el, get()}.
function fieldControl(f, value, { inherit = false, inheritValue } = {}) {
  let input, get;
  const ph = inherit ? `inherit (${fmtVal(inheritValue)})` : (f.placeholder || "");
  if (f.type === "bool" && !inherit) {
    const cb = h("input", { type: "checkbox", checked: !!value });
    input = h("label", { class: "switch" }, cb, h("span"));
    get = () => cb.checked;
  } else if (f.type === "choice" || (f.type === "bool" && inherit)) {
    const choices = f.type === "bool" ? ["true", "false"] : f.choices;
    input = h("select", {}, inherit ? h("option", { value: "" }, ph) : null, ...choices.map(c => h("option", { value: c }, c)));
    input.value = value === null || value === undefined ? "" : String(value);
    get = () => input.value === "" ? null : (f.type === "bool" ? input.value === "true" : input.value);
  } else if (f.type === "int" || f.type === "float") {
    input = h("input", { type: "number", step: f.type === "int" ? "1" : "any", min: f.min, max: f.max, placeholder: ph,
                         value: value ?? "" });
    get = () => input.value === "" ? null : Number(input.value);
  } else if (f.type === "list") {
    input = h("textarea", { rows: 3, placeholder: "one per line" });
    input.value = (value || []).join("\n");
    get = () => input.value.split("\n").map(s => s.trim()).filter(Boolean);
  } else if (f.type === "model") {
    ensureModelList();
    input = h("input", { type: "text", value: value ?? "", placeholder: ph, list: "model-list", autocomplete: "off" });
    get = () => inherit ? (input.value.trim() === "" ? null : input.value.trim()) : input.value.trim();
  } else {
    input = h("input", { type: "text", value: value ?? "", placeholder: ph });
    get = () => input.value;
  }
  return { el: input, get, input };
}
// One shared <datalist> of LiteLLM's models for every model field.
function ensureModelList() {
  if (document.getElementById("model-list")) return;
  const dl = h("datalist", { id: "model-list" });
  document.body.append(dl);
  api("GET", "/api/models").then(r => { dl.append(h("option", { value: "none" }, "no fallback"),
    ...r.models.map(m => h("option", { value: m }))); }).catch(() => {});
}
const inheritOf = (f, vals) => vals[f.inherit_from || "defaults." + f.key];
const fmtVal = v => Array.isArray(v) ? v.join(", ") : String(v);

// Mesh data card: source health, radio query budget and the last few transmitting queries.
function directCard(s) {
  const d = s.direct || {}, a = s.advert || {};
  const sel = h("select", { onchange: async e => {
    await api("POST", "/api/settings", { values: { "direct.mode": e.target.value } });
    toast(`Direct messages: ${e.target.value}`); e.target.blur(); } },
    ...["live", "shadow", "off"].map(m => h("option", { value: m }, m)));
  sel.value = d.mode || "shadow";
  return h("div", { class: "card" }, h("h2", {}, "Direct messages & adverts"),
    h("div", { class: "kv" },
      h("div", {}, "DM mode"), h("div", {}, sel),
      h("div", {}, "DM replies"), h("div", {}, `${d.replies_last_hour || 0} this hour · ${(d.conversations || []).length} conversations (30 d)`),
      h("div", {}, "Last advert"), h("div", {}, a.last ? `${ago(a.last)} (${a.last_source})` : "none yet"),
      h("div", {}, "Schedule"), h("div", {}, a.interval_h ? `every ${a.interval_h} h, ${a.flood ? "flood" : "zero-hop"}` : "off")),
    ...(a.recent || []).filter(r => r.error).slice(-2).map(r => h("div", { class: "small", style: "color:var(--bad)" }, `${ago(r.ts)}: ${r.error}`)),
    h("div", { class: "row", style: "margin-top:6px" }, h("button", { onclick: async e => {
      if (!confirm(`Send a ${a.flood ? "flood" : "zero-hop"} advert now?`)) return;
      e.target.disabled = true;
      try { await api("POST", "/api/advert"); toast("Advert sent"); } finally { e.target.disabled = false; } } }, "Send advert"),
      h("span", { class: "muted small" }, "Nodes need the bot's advert before they can DM it.")));
}

function meshCard(m) {
  if (!m) return "";
  const out = h("div", { class: "muted small" });
  return h("div", { class: "card" }, h("h2", {}, "Mesh data"),
    h("div", { class: "kv" },
      h("div", {}, "openHop key"), h("div", {}, m.openhop_key_set ? "set" : h("span", { style: "color:var(--warn)" }, "not set")),
      h("div", {}, "Radio queries"), h("div", {}, `${m.radio_last_hour} / ${m.radio_max_per_hour} this hour`)),
    m.error ? h("div", { class: "small", style: "color:var(--bad);margin-top:4px" }, m.error) : "",
    ...(m.radio_recent || []).slice().reverse().map(r => h("div", { class: "small muted" },
      `${ago(r.ts)}: ${r.kind} ${r.node} - `, h("span", { style: r.outcome === "ok" ? "color:var(--ok)" : "color:var(--bad)" }, r.outcome))),
    h("div", { class: "row", style: "margin-top:6px" }, h("button", { onclick: async e => {
      e.target.disabled = true;
      try { const r = await api("POST", "/api/test_mesh");
        out.replaceChildren(...r.results.map(x => h("div", { style: x.ok ? "" : "color:var(--bad)" }, `${x.source}: ${x.ok ? "OK, " : ""}${x.detail}`)));
      } finally { e.target.disabled = false; } } }, "Test mesh data"),
      h("span", { class: "muted small" }, "Read-only check, nothing transmitted.")), out);
}

// ---------- tabs ----------
const TABS = {};

TABS.dashboard = async main => {
  await loadSettings();
  const wrap = h("div");
  // The send box lives outside the refreshed area, so typing survives the 5 s refresh.
  main.append(wrap, sendCard());
  const draw = async (force) => {
    // Don't redraw under the user's hands (e.g. an open mode dropdown).
    const a = document.activeElement;
    const typing = a && wrap.contains(a) && (a.tagName === "SELECT" || a.tagName === "TEXTAREA" ||
      (a.tagName === "INPUT" && !["checkbox", "radio", "button"].includes(a.type)));
    if (force !== true && typing) return;
    const s = state.status = await api("GET", "/api/status");
    updateHeader(s);
    const c = s.companion, llm = s.llm, d24 = s.last_24h.decisions;
    const enabled = h("input", { type: "checkbox", checked: s.enabled, onchange: async e => {
      await api("POST", "/api/settings", { values: { enabled: e.target.checked } });
      toast(e.target.checked ? "Transmit enabled" : "Transmit disabled (kill switch)"); draw(true); } });
    const nameMismatch = c.connected && c.name && c.name !== s.bot_name;
    wrap.replaceChildren(
      h("div", { class: "hero" }, h("img", { src: "/static/logo.svg", alt: "meshbot", width: 244, height: 120 }),
        h("div", {}, h("div", { class: "hero-name" }, s.bot_name),
          h("div", { class: "muted" }, "AI on the mesh · ", (s.channels || []).map(x => x.name).join(", ") || "no channels"),
          h("div", { class: "row small", style: "margin-top:6px" },
            h("span", { class: "dot " + (c.connected ? "ok" : "bad") }), c.connected ? "on air" : "companion disconnected",
            h("span", { class: "muted" }, ` · up ${Math.round(s.uptime_s / 3600)}h`)))),
      s.config_error ? h("div", { class: "card", style: "border-color: var(--bad)" },
        h("b", {}, "config.yaml has an error; the last good config is still in use: "), s.config_error) : "",
      h("div", { class: "grid" },
        h("div", { class: "card" }, h("h2", {}, "Transmit"),
          h("div", { class: "row" }, h("label", { class: "switch" }, enabled, h("span")),
            h("span", { class: "big" }, s.enabled ? "On" : "Off")),
          h("div", { class: "muted small", style: "margin-top:8px" },
            "Off = keep deciding and logging, never transmit. Channels in shadow mode never transmit either way.")),
        h("div", { class: "card" }, h("h2", {}, "Companion"),
          h("div", { class: "row" }, h("span", { class: "dot " + (c.connected ? "ok" : "bad") }),
            h("span", { class: "big" }, c.connected ? (c.name || "connected") : "disconnected")),
          h("div", { class: "muted small", style: "margin-top:8px" }, c.address, " · ", c.sent_packets, " packets sent since start",
            c.path_hash_bytes ? ` · ${c.path_hash_bytes}-byte path hash` : ""),
          c.error && !c.connected ? h("div", { class: "small", style: "color:var(--bad);margin-top:4px" }, c.error) : "",
          nameMismatch ? h("div", { class: "small", style: "margin-top:8px" },
            `Companion is "${c.name}" but bot_name is "${s.bot_name}". `,
            h("button", { onclick: async () => { await api("POST", "/api/rename_companion"); toast("Companion renamed"); draw(true); } },
              `Rename companion to ${s.bot_name}`)) : ""),
        h("div", { class: "card" }, h("h2", {}, "Models"),
          ...s.models.map(m => {
            const state = m.held_for_s ? "bad" : m.ok === false ? "warn" : m.ok ? "ok" : "";
            return h("div", { style: "margin-bottom:8px" },
              h("div", { class: "row" }, h("span", { class: "dot " + state }), h("b", {}, m.model),
                h("span", { class: "muted small" }, m.roles.join(" + "))),
              h("div", { class: "muted small" },
                m.held_for_s ? h("span", { style: "color:var(--bad)" }, `down: using fallback for ${Math.ceil(m.held_for_s / 60)} more min`)
                  : m.last_ok ? `ok ${ago(m.last_ok)}` : "not used yet",
                m.ok === false && m.last_error ? h("div", { style: "color:var(--bad)" }, `${ago(m.error_time)}: ${m.last_error}`) : ""));
          }),
          llm.key_set ? "" : h("div", { class: "small", style: "color:var(--warn)" }, "No API key set"),
          h("div", { class: "row", style: "margin-top:4px" }, h("button", { onclick: async e => {
            e.target.disabled = true; e.target.textContent = "Testing...";
            try { const r = await api("POST", "/api/test_llm");
              r.results.forEach(x => x.ok ? toast(`${x.model}: OK in ${x.latency_s}s`) : toast(`${x.model}: ${x.error}`, true));
            } finally { e.target.disabled = false; e.target.textContent = "Test models"; draw(true); } } }, "Test models"),
            h("span", { class: "muted small" }, "A passing test also ends a failover hold."))),
        meshCard(s.mesh_data),
        directCard(s),
        h("div", { class: "card" }, h("h2", {}, "Last 24 hours"),
          h("div", { class: "kv" },
            h("div", {}, "Heard"), h("div", {}, s.last_24h.heard),
            h("div", {}, "Sent"), h("div", {}, d24.sent || 0),
            h("div", {}, "Shadow"), h("div", {}, d24.shadow || 0),
            h("div", {}, "Silent"), h("div", {}, d24.silent || 0),
            h("div", {}, "Blocked/err"), h("div", {}, Object.entries(d24).filter(([k]) => /blocked|error|fail/.test(k)).reduce((a, [, v]) => a + v, 0)),
          ))),
      h("div", { class: "card" }, h("h2", {}, "Channels"),
        s.channels.length ? h("table", {},
          h("tr", {}, h("th", {}, "Channel"), h("th", {}, "Slot"), h("th", {}, "Mode"), h("th", {}, "Eagerness"),
            h("th", {}, "Models"), h("th", {}, "Link"), h("th", {}, "Replies / hr"), h("th", {}, "Last reply")),
          ...s.channels.map(ch => {
            const sel = h("select", { onchange: e => setChannelValue(ch.name, "mode", e.target.value).then(() => { e.target.blur(); draw(true); }) },
              ...["live", "shadow", "off"].map(m => h("option", { value: m }, m)));
            sel.value = ch.mode;
            return h("tr", {}, h("td", {}, h("b", {}, ch.name), ch.private ? h("span", { class: "muted small" }, " private") : ""),
              h("td", {}, ch.slot), h("td", {}, sel), h("td", {}, ch.eagerness),
              h("td", { class: "small" }, ch.model, ch.fallback_model ? h("span", { class: "muted" }, ` → ${ch.fallback_model}`) : ""),
              h("td", {}, ch.link === "ok" ? h("span", { class: "dot ok", title: "ok" }) :
                ch.link ? h("span", { class: "small", style: "color:var(--bad)" }, ch.link) : h("span", { class: "muted" }, "-")),
              h("td", {}, ch.replies_last_hour), h("td", {}, ago(ch.last_reply)));
          })) : h("div", { class: "muted" }, "No channels yet. Add one on the Channels tab.")),
    );
  };
  every(5000, draw);
};

function sendCard() {
  const chan = channelSelect();
  const txt = h("input", { type: "text", placeholder: "Message to send as the bot", style: "flex:1;min-width:240px" });
  const counter = h("span", { class: "counter" });
  const upd = () => {
    const ch = state.status?.channels.find(c => c.name === chan.value);
    const ptb = state.settings.channels.find(c => c.name === chan.value)?.packet_text_bytes ?? state.settings.values["defaults.packet_text_bytes"];
    const budget = ptb - bytes(state.settings.values.bot_name) - 2;
    const n = bytes(txt.value);
    counter.textContent = `${n}/${budget} bytes`;
    counter.classList.toggle("over", n > budget);
    return ch;
  };
  txt.oninput = upd; chan.onchange = upd; upd();
  return h("div", { class: "card" }, h("h2", {}, "Send manually"),
    h("div", { class: "row" }, chan, txt, counter,
      h("button", { class: "primary", onclick: async () => {
        if (!txt.value.trim()) return;
        if (!confirm(`Transmit on ${chan.value} as ${state.settings.values.bot_name}?`)) return;
        await api("POST", "/api/send", { channel: chan.value, text: txt.value });
        toast("Sent"); txt.value = ""; upd(); } }, "Send")),
    h("div", { class: "muted small", style: "margin-top:6px" }, "One packet, sent right away. Ignores the transmit switch, shadow mode and rate limits."));
}

async function setChannelValue(name, key, value) {
  const chans = state.settings.channels.map(c => ({ ...c }));
  const c = chans.find(x => x.name === name);
  c[key] = value;
  state.settings = await api("PUT", "/api/channels", { channels: chans });
  toast(`${name}: ${key} = ${value}`);
}

function updateHeader(s) {
  $("#hdr-name").textContent = s.bot_name;
  $("#hdr-dot").className = "dot " + (s.companion.connected ? (s.enabled ? "ok" : "warn") : "bad");
  $("#hdr-sub").textContent = s.companion.connected ? (s.enabled ? "" : "transmit off") : "companion disconnected";
}

// ----- Live -----
TABS.live = async main => {
  await loadSettings();
  await loadDMs();
  const filter = channelSelect(state.liveChannel ?? "", { all: true, dm: true, onchange: () => { state.liveChannel = filter.value; reset(); } });
  const list = h("div", { class: "feed" });
  const empty = h("div", { class: "muted", style: "padding:8px" }, "Nothing heard yet.");
  const more = h("button", { onclick: () => loadOlder() }, "Load older");
  main.append(h("div", { class: "card" }, h("h2", {}, h("span", {}, "Live channel traffic"), h("span", { class: "row" }, filter)),
    h("div", { class: "muted small" }, "Newest at the top, refreshes every 3 s. Click a badge to see why the bot did or didn't reply."),
    list, h("div", { class: "row", style: "margin-top:10px" }, more)));
  let newest = 0, oldest = null;
  const rows = new Map();  // message id -> element
  const row = m => {
    const el = h("div", { class: "msg" + (m.own ? " own" : "") },
      h("span", { class: "muted small", title: fmtDT(m.ts) }, fmtTime(m.ts)),
      h("span", { class: "who", title: m.sender }, m.sender, h("div", { class: "chan" }, m.channel,
        m.hops != null ? ` · ${m.hops} hop${m.hops === 1 ? "" : "s"}` : "", m.snr != null ? ` · ${m.snr} dB` : "")),
      h("span", {}, m.text),
      m.decision ? outcomeBadge(m.decision.outcome, () => showDecision(m.decision.id)) : h("span"));
    el.dataset.dec = m.decision ? m.decision.id : "";
    rows.set(m.id, el);
    return el;
  };
  const q = () => (state.liveChannel ? `&channel=${encodeURIComponent(state.liveChannel)}` : "");
  async function poll() {
    const got = await api("GET", `/api/messages?limit=100&after=${newest}${q()}`);
    if (got.length) {
      empty.remove();
      newest = Math.max(newest, ...got.map(r => r.id));
      if (oldest === null) oldest = Math.min(...got.map(r => r.id));
      list.prepend(...got.map(row));
    } else if (!rows.size) list.replaceChildren(empty);
    // Decisions land a few seconds after their message: refresh the badges of recent rows.
    const recent = await api("GET", `/api/messages?limit=30${q()}`);
    for (const m of recent) {
      const el = rows.get(m.id);
      if (el && m.decision && el.dataset.dec !== String(m.decision.id)) el.replaceWith(row(m));
    }
  }
  async function loadOlder() {
    if (oldest === null) return;
    const got = await api("GET", `/api/messages?limit=100&before=${oldest}${q()}`);
    if (!got.length) { more.disabled = true; more.textContent = "No more"; return; }
    oldest = Math.min(...got.map(r => r.id));
    list.append(...got.map(row));
  }
  function reset() { newest = 0; oldest = null; rows.clear(); list.replaceChildren(); more.disabled = false; more.textContent = "Load older"; poll(); }
  every(3000, poll);
};

async function showDecision(id) {
  const d = await api("GET", `/api/decisions/${id}`);
  modal(decisionView(d));
}

function decisionView(d) {
  const thread = (d.thread || []).map((m, i, a) =>
    `${i === a.length - 1 ? ">>> " : "    "}[${fmtTime(m.ts)}] ${m.own ? d.channel && "(bot) " : ""}${m.sender}: ${m.text}`).join("\n");
  const parts = d.parts || [];
  return h("div", {},
    h("h2", {}, h("span", {}, `Decision #${d.id} · ${d.channel}`), outcomeBadge(d.outcome)),
    h("div", { class: "kv", style: "margin-bottom:12px" },
      h("div", {}, "Time"), h("div", {}, d.time),
      h("div", {}, "Respond"), h("div", {}, String(d.respond)),
      h("div", {}, "Reason"), h("div", {}, d.reason || "-"),
      h("div", {}, "Model"), h("div", {}, modelLine(d)),
      h("div", {}, "Mode"), h("div", {}, `${d.mode} · eagerness ${d.eagerness}`),
      d.error ? h("div", {}, "Error") : "", d.error ? h("div", { style: "color:var(--bad)" }, d.error) : ""),
    searchesView(d.searches),
    h("h3", {}, "Thread the model saw"), h("div", { class: "thread" }, thread),
    parts.length ? h("div", {}, h("h3", { style: "margin-top:12px" }, d.outcome === "sent" ? "Sent" : "Would have sent"), packetsView(parts)) : "",
    d.shortened ? h("div", { class: "muted small" }, "Answer was too long; the model was asked to shorten it.") : "",
    d.truncated ? h("div", { class: "muted small" }, "Still too long after shortening; it was cut off.") : "",
    laterPages(d.later_pages),
    h("details", {}, h("summary", {}, "Raw model output"), h("pre", { class: "mono" }, d.raw || "")),
    h("div", { class: "row end", style: "margin-top:12px" },
      h("button", { onclick: () => { closeModal(); openInPlayground(d.channel, d.thread); } }, "Open in playground"),
      h("button", { class: "primary", onclick: async e => {
        e.target.disabled = true; e.target.textContent = "Replaying...";
        try { const [r] = await api("POST", "/api/replay", { ids: [d.id], ...draftPayload(d.channel) });
          e.target.replaceWith(h("div", { class: "card", style: "width:100%;margin:0" }, h("h3", {}, "With current prompt + settings"), resultView(r.new)));
        } catch { e.target.disabled = false; e.target.textContent = "Replay with current prompt"; } } }, "Replay with current prompt")));
}

function modelLine(d) {
  const tried = (d.attempts || []).map(a => h("div", { class: "muted small" },
    `${a.model}: ${a.skipped ? "skipped (" + a.skipped + ")" : "failed (" + a.error + ")"}`));
  return h("span", {}, h("b", {}, d.model || "?"), d.fallback ? h("span", { class: "badge superseded", style: "margin-left:6px" }, "fallback") : "",
    ` · ${d.latency_s}s`, d.usage?.total_tokens ? ` · ${d.usage.total_tokens} tokens` : "", ...tried);
}

function searchesView(searches) {
  if (!searches || !searches.length) return "";
  const what = x => x.error ? x.error : x.results != null ? `${x.results} results` : x.chars != null ? `${x.chars} chars` : "ok";
  return h("div", { class: "small", style: "margin:8px 0" }, h("b", {}, "Tools used:"),
    ...searches.map(x => h("div", {}, x.action && !x.error ? h("b", { style: "color:var(--warn)" }, "ACTION ") : "",
      `${x.tool || "web_search"}: ${x.query} `,
      h("span", { class: "muted", style: x.error ? "color:var(--bad)" : "" }, `(${what(x)}, ${x.ms} ms)`))));
}

function packetsView(parts) {
  const name = state.settings?.values.bot_name || "Bot";
  return h("div", { class: "packets" }, ...parts.map(p => h("div", { class: "packet" },
    h("div", {}, h("b", {}, name + ": "), p), h("div", { class: "meta" }, `${bytes(name + ": " + p)} bytes`))));
}

// ----- Decisions -----
TABS.decisions = async main => {
  await loadSettings();
  await loadDMs();
  const chan = channelSelect("", { all: true, dm: true, onchange: () => reload() });
  const outcome = h("select", { onchange: () => reload() }, h("option", { value: "" }, "All outcomes"),
    ...["sent", "shadow", "silent", "blocked", "superseded", "disabled", "error"].map(o => h("option", { value: o }, o)));
  const search = h("input", { type: "text", placeholder: "Search text...", onkeydown: e => { if (e.key === "Enter") reload(); } });
  const table = h("table");
  const more = h("button", { onclick: () => load(true) }, "Load more");
  const selected = new Set();
  const replayBtn = h("button", { class: "primary", disabled: true, onclick: () => replaySelected() }, "Replay selected");
  main.append(h("div", { class: "card" },
    h("h2", {}, h("span", {}, "Decisions"), h("span", { class: "row" }, chan, outcome, search, replayBtn)),
    h("div", { class: "muted small" }, "Every time the model was asked. Tick up to 20 and replay them against the current prompt (or your unsaved drafts) to see what would change."),
    table, h("div", { class: "row", style: "margin-top:10px" }, more), h("div", { id: "replay-out" })));
  let oldest = null;
  async function load(append = false) {
    const qs = new URLSearchParams({ limit: 50 });
    if (chan.value) qs.set("channel", chan.value);
    if (outcome.value) qs.set("outcome", outcome.value);
    if (search.value) qs.set("q", search.value);
    if (append && oldest) qs.set("before", oldest);
    const rows = await api("GET", "/api/decisions?" + qs);
    if (!append) table.replaceChildren(h("tr", {}, h("th"), h("th", {}, "Time"), h("th", {}, "Channel"), h("th", {}, "Trigger"),
      h("th", {}, "Outcome"), h("th", {}, "Reason"), h("th", {}, "Reply")));
    rows.forEach(d => {
      const cb = h("input", { type: "checkbox", onchange: e => { e.target.checked ? selected.add(d.id) : selected.delete(d.id);
        replayBtn.disabled = !selected.size || selected.size > 20; replayBtn.textContent = `Replay selected (${selected.size})`; } });
      table.append(h("tr", {}, h("td", {}, cb), h("td", { class: "small" }, d.time?.slice(5, 16).replace("T", " ")), h("td", {}, d.channel),
        h("td", {}, h("b", {}, d.trigger?.sender + ": "), d.trigger?.text),
        h("td", {}, outcomeBadge(d.outcome, () => showDecision(d.id))), h("td", { class: "small" }, d.reason),
        h("td", { class: "small" }, (d.parts || []).join(" | "))));
    });
    if (rows.length) oldest = rows[rows.length - 1].id;
    more.disabled = rows.length < 50;
  }
  function reload() { oldest = null; selected.clear(); replayBtn.disabled = true; replayBtn.textContent = "Replay selected"; load(); }
  async function replaySelected() {
    const out = $("#replay-out"); out.replaceChildren(h("div", { class: "muted", style: "margin-top:12px" }, `Replaying ${selected.size}... (one model call each)`));
    replayBtn.disabled = true;
    try {
      const res = await api("POST", "/api/replay", { ids: [...selected], ...draftPayload() });
      const flips = res.filter(r => r.old.respond !== r.new.respond).length;
      out.replaceChildren(h("h3", { style: "margin-top:16px" }, `Replay: ${flips} of ${res.length} decisions changed`),
        h("table", {}, h("tr", {}, h("th", {}, "Trigger"), h("th", {}, "Before"), h("th", {}, "Now")),
          ...res.map(r => h("tr", { style: r.old.respond !== r.new.respond ? "background:var(--accent-2)" : "" },
            h("td", {}, h("b", {}, r.old.trigger.sender + ": "), r.old.trigger.text),
            h("td", { class: "small" }, outcomeBadge(r.old.outcome), " ", r.old.reason, h("div", {}, (r.old.parts || []).join(" | "))),
            h("td", { class: "small" }, outcomeBadge(r.new.error ? "error" : r.new.respond ? "respond" : "silent"), " ", r.new.reason || r.new.error || "",
              h("div", {}, (r.new.parts || []).join(" | ")))))));
    } finally { replayBtn.disabled = false; }
  }
  reload();
};

// Unsaved prompt drafts (from the Prompts tab) are used by playground/replay when the toggle is on.
function draftPayload(channel) {
  if (!state.useDrafts) return {};
  const p = {};
  if (state.drafts.system !== undefined) p.system_draft = state.drafts.system;
  if (channel && state.drafts[channel] !== undefined) p.channel_draft = state.drafts[channel];
  return p;
}
function draftsToggle() {
  const has = Object.keys(state.drafts).length > 0;
  const cb = h("input", { type: "checkbox", checked: !!state.useDrafts, disabled: !has, onchange: e => { state.useDrafts = e.target.checked; } });
  return h("label", { class: "row small", title: has ? "" : "Edit a prompt on the Prompts tab without saving to create a draft" },
    cb, has ? "Use unsaved prompt drafts" : "No unsaved prompt drafts");
}

// ----- Playground -----
function openInPlayground(channel, thread) {
  state.pg = { channel, thread: thread.map(m => `${m.own ? state.settings.values.bot_name : m.sender}: ${m.text}`).join("\n") };
  showTab("playground");
}
TABS.playground = async main => {
  await loadSettings();
  if (!state.schema) state.schema = await api("GET", "/api/schema");
  const pg = state.pg || { channel: channelNames()[0] || "#testbot", thread: "Alice: anyone know how far LoRa can reach?" };
  await loadDMs();
  const chan = channelSelect(pg.channel, { dm: true, extra: ["@Tester"] });
  const thread = h("textarea", { rows: 10 }); thread.value = pg.thread;
  thread.oninput = () => { state.pg = { channel: chan.value, thread: thread.value }; };
  const ov = {};
  const quick = ["model", "fallback_model", "eagerness", "max_parts", "packet_text_bytes", "web_search", "weather"].map(k => {
    const f = state.schema.channel_fields.find(x => x.key === k);
    const c = fieldControl(f, null, { inherit: true, inheritValue: "channel" });
    c.input.style.width = "150px";
    ov[k] = c; return h("label", { class: "small" }, f.label, h("br"), c.el);
  });
  const out = h("div", { class: "muted" }, "Ask the model to see whether it would reply, what it would send packet by packet, and the exact prompt it got.");
  const run = h("button", { class: "primary", onclick: async () => {
    const lines = thread.value.split("\n").map(s => s.trim()).filter(Boolean);
    const bot = state.settings.values.bot_name.toLowerCase();
    const msgs = lines.map(l => { const i = l.indexOf(":"); const sender = i > 0 ? l.slice(0, i).trim() : "Someone";
      const text = i > 0 ? l.slice(i + 1).trim() : l; return { sender, text, own: sender.toLowerCase() === bot }; });
    const overrides = Object.fromEntries(Object.entries(ov).map(([k, c]) => [k, c.get()]).filter(([, v]) => v !== null));
    run.disabled = true; run.textContent = "Thinking...";
    out.replaceChildren(h("div", { class: "muted" }, "Waiting for the model..."));
    try { const r = await api("POST", "/api/playground", { channel: chan.value, thread: msgs, overrides, ...draftPayload(chan.value) });
      out.replaceChildren(resultView(r));
    } catch (e) { out.replaceChildren(h("div", { style: "color:var(--bad)" }, e.message)); }
    finally { run.disabled = false; run.textContent = "Ask the model"; }
  } }, "Ask the model");
  main.append(h("div", { class: "split" },
    h("div", { class: "card" }, h("h2", {}, "Playground"),
      h("div", { class: "muted small", style: "margin-bottom:8px" },
        "Try a conversation without transmitting anything. One message per line as \"Sender: text\", newest last. ",
        `Lines from "${state.settings.values.bot_name}" count as the bot's own earlier replies.`),
      h("div", { class: "row", style: "margin-bottom:8px" }, h("label", { class: "small" }, "Channel", h("br"), chan), ...quick),
      thread,
      h("div", { class: "row", style: "margin-top:8px" }, draftsToggle(), h("span", { class: "spacer" }),
        h("button", { onclick: async () => {
          const rows = await api("GET", `/api/messages?limit=15&channel=${encodeURIComponent(chan.value)}`);
          thread.value = rows.reverse().map(m => `${m.sender}: ${m.text}`).join("\n") || thread.value;
          state.pg = { channel: chan.value, thread: thread.value }; } }, "Load recent traffic"), run)),
    h("div", { class: "card" }, h("h2", {}, "Result"), out)));
};

function laterPages(pages) {
  if (!pages || !pages.length) return "";
  return h("details", { style: "margin-top:8px" },
    h("summary", {}, `${pages.length} more batch${pages.length > 1 ? "es" : ""}, each sent when someone replies "more"`),
    ...pages.map((p, i) => h("div", {}, h("div", { class: "muted small" }, `After "more" #${i + 1}`), packetsView(p))));
}

function resultView(r) {
  if (r.error) return h("div", {}, h("div", { style: "color:var(--bad)" }, r.error), h("div", { class: "small" }, modelLine(r)));
  return h("div", {},
    h("div", { class: "row", style: "margin-bottom:8px" }, outcomeBadge(r.respond ? "respond" : "silent"),
      h("span", {}, r.reason), h("span", { class: "spacer" })),
    h("div", { class: "small", style: "margin-bottom:8px" }, modelLine(r)),
    searchesView(r.searches),
    r.respond ? packetsView(r.parts || []) : h("div", { class: "muted" }, "The bot would stay quiet."),
    r.shortened ? h("div", { class: "muted small" }, "Too long at first; the model shortened it.") : "",
    r.truncated ? h("div", { class: "muted small" }, "Still too long; it was cut off.") : "",
    laterPages(r.later_pages),
    h("details", {}, h("summary", {}, "Raw model output"), h("pre", { class: "mono" }, r.raw || "")),
    r.system ? h("details", {}, h("summary", {}, "System prompt sent"), h("pre", { class: "mono" }, r.system)) : "",
    r.user ? h("details", {}, h("summary", {}, "User message sent"), h("pre", { class: "mono" }, r.user)) : "");
}

// ----- Prompts -----
TABS.prompts = async main => {
  await loadSettings();
  const saved = await api("GET", "/api/prompts");
  const opts = ["system", ...Object.keys(saved.channels)];
  const pick = h("select", {}, ...opts.map(o => h("option", { value: o }, o === "system" ? "System prompt (all channels)" : o.startsWith("@") ? "Notes for direct messages (all DMs)" : `Notes for ${o}`)));
  pick.value = state.promptPick && opts.includes(state.promptPick) ? state.promptPick : "system";
  const ta = h("textarea", { style: "min-height:420px" });
  const info = h("div", { class: "muted small" });
  const savedText = w => w === "system" ? saved.system : saved.channels[w];
  const status = h("span", { class: "small" });
  function show() {
    state.promptPick = pick.value;
    ta.value = state.drafts[pick.value] ?? savedText(pick.value);
    info.textContent = pick.value === "system"
      ? "Sent to the model for every channel. {bot_name} is replaced with the bot's name. The output-format rules, time and byte limits are added automatically after this."
      : `Added after the system prompt, for ${pick.value} only. Leave empty for none.`;
    mark();
  }
  function mark() {
    const dirty = ta.value !== savedText(pick.value);
    if (dirty) state.drafts[pick.value] = ta.value; else delete state.drafts[pick.value];
    status.textContent = dirty ? "Unsaved draft (usable in Playground / replay)" : "Saved";
    status.style.color = dirty ? "var(--warn)" : "var(--muted)";
  }
  pick.onchange = show; ta.oninput = mark;
  main.append(h("div", { class: "card" },
    h("h2", {}, h("span", {}, "Prompts"), h("span", { class: "row" }, pick)), info,
    h("div", { style: "margin-top:10px" }, ta),
    h("div", { class: "row", style: "margin-top:8px" }, status, h("span", { class: "spacer" }),
      h("button", { onclick: () => { delete state.drafts[pick.value]; show(); } }, "Revert"),
      h("button", { onclick: () => { state.useDrafts = true; showTab("playground"); } }, "Test draft in playground"),
      h("button", { class: "primary", onclick: async () => {
        await api("PUT", "/api/prompts", { which: pick.value, text: ta.value });
        if (pick.value === "system") saved.system = ta.value; else saved.channels[pick.value] = ta.value;
        delete state.drafts[pick.value]; mark(); toast("Prompt saved; it applies to the next message"); } }, "Save"))));
  show();
};

// ----- Channels -----
TABS.channels = async main => {
  await loadSettings();
  if (!state.schema) state.schema = await api("GET", "/api/schema");
  const cf = state.schema.channel_fields, vals = state.settings.values;
  let chans = state.settings.channels.map(c => ({ ...c }));
  const box = h("div");
  main.append(h("div", { class: "card" }, h("h2", {}, h("span", {}, "Channels"),
      h("span", { class: "row" }, h("button", { onclick: () => { chans.push({ name: "#", slot: nextSlot(), _new: true }); draw(); } }, "Add channel"))),
    h("div", { class: "muted small", style: "margin-bottom:10px" },
      "Hashtag channels (#name) get their key from the name, so anyone can join by typing it. Any other name is a private channel and needs a 16-byte secret. ",
      "Blank per-channel settings inherit the defaults from the Settings tab. Saving reconnects the companion if names, slots or secrets changed."),
    box));
  const nextSlot = () => { const used = new Set(chans.map(c => Number(c.slot))); for (let i = 1; i < 40; i++) if (!used.has(i)) return i; return 0; };
  function draw() {
    box.replaceChildren(...chans.map((c, i) => {
      const name = h("input", { type: "text", value: c.name, oninput: e => { c.name = e.target.value; } });
      const slot = h("input", { type: "number", min: 0, max: 39, value: c.slot, oninput: e => { c.slot = Number(e.target.value); } });
      const ctrls = cf.map(f => { const ctl = fieldControl(f, c[f.key], { inherit: true, inheritValue: inheritOf(f, vals) || "none" });
        ctl.input.addEventListener("change", () => { c[f.key] = ctl.get(); }); return [f, ctl]; });
      const priv = !String(c.name).startsWith("#");
      const secretRow = priv && !c._new ? h("div", { class: "row small", style: "margin-top:6px" },
        h("span", {}, "Secret: "), c.secret_set ? h("b", { style: "color:var(--ok)" }, "set") : h("b", { style: "color:var(--bad)" }, "not set"),
        h("button", { onclick: async () => { const v = prompt("Paste the channel secret (32 hex characters):"); if (!v) return;
          state.settings = await api("POST", "/api/channels/secret", { name: c.name, secret: v }); toast("Secret saved"); refresh(); } }, "Set"),
        h("button", { onclick: async () => { if (c.secret_set && !confirm("Replace the existing secret with a new random one?")) return;
          const r = await api("POST", "/api/channels/secret", { name: c.name, secret: "generate" }); state.settings = r;
          qrModal(c.name, r, `New secret for ${c.name}`); refresh(); } }, "Generate"),
        c.secret_set ? h("button", { class: "danger", onclick: async () => { if (!confirm("Clear this channel's secret?")) return;
          state.settings = await api("POST", "/api/channels/secret", { name: c.name, secret: null }); refresh(); } }, "Clear") : "",
        c.secret_set ? qrButton(c.name) : "",
        c.secret_env ? h("span", { class: "muted" }, ` (also read from env ${c.secret_env})`) : "")
        : priv ? h("div", { class: "small muted", style: "margin-top:6px" }, "Save first, then set the secret.")
        : !c._new ? h("div", { class: "row small", style: "margin-top:6px" }, qrButton(c.name)) : "";
      return h("div", { class: "card", style: "margin-bottom:10px" },
        h("div", { class: "row" }, h("label", { class: "small" }, "Name", h("br"), name), h("label", { class: "small" }, "Slot", h("br"), slot),
          h("span", { class: "spacer" }),
          h("button", { class: "danger", onclick: () => { if (confirm(`Remove ${c.name}? (Save to apply)`)) { chans.splice(i, 1); draw(); } } }, "Remove")),
        secretRow,
        h("details", { open: c._new || cf.some(f => c[f.key] != null) }, h("summary", {}, "Per-channel settings"),
          ...ctrls.map(([f, ctl]) => h("label", { class: "field" }, h("div", {}, f.label, h("div", { class: "help" }, f.help)), ctl.el))));
    }), h("div", { class: "row end" }, h("button", { onclick: () => refresh() }, "Discard changes"),
      h("button", { class: "primary", onclick: save }, "Save channels")));
  }
  async function save() {
    const payload = chans.map(c => { const o = { name: c.name.trim(), slot: c.slot };
      if (c.secret_env) o.secret_env = c.secret_env; cf.forEach(f => { if (c[f.key] != null) o[f.key] = c[f.key]; }); return o; });
    state.settings = await api("PUT", "/api/channels", { channels: payload });
    toast("Channels saved"); refresh();
  }
  function refresh() { chans = state.settings.channels.map(c => ({ ...c })); draw(); }
  draw();
};

// ----- channel join QR (meshcore://channel/add link, scanned by the MeshCore app) -----
function qrButton(name) {
  return h("button", { onclick: async () => {
    if (!name.startsWith("#") && !confirm(`Show the join QR for ${name}? It contains the channel key: anyone who sees or photographs it can join.`)) return;
    qrModal(name, await api("POST", "/api/channels/qr", { name }));
  } }, "QR code");
}

function qrModal(name, r, title) {
  const box = h("div", { class: "qr" }); box.innerHTML = r.svg;  // server-made SVG (segno), no user markup
  const priv = !name.startsWith("#");
  modal(h("div", {}, h("h2", {}, title || `Join ${name}`),
    h("p", { class: "small" }, "In the MeshCore app: add a channel, choose scan QR, and point the camera here.",
      priv ? " This QR holds the channel key, so only show it to people who should be on the channel." : ""),
    box,
    h("div", { class: "small muted", style: "margin-top:8px" }, "Or share the link / key:"),
    h("pre", { class: "mono", style: "white-space:pre-wrap;word-break:break-all;padding:8px;background:var(--bg)" }, r.url),
    h("div", { class: "row" },
      h("button", { onclick: () => { navigator.clipboard?.writeText(r.url); toast("Link copied"); } }, "Copy link"),
      priv ? h("button", { onclick: () => { navigator.clipboard?.writeText(r.url.split("secret=")[1]); toast("Key copied"); } }, "Copy key") : "")));
}

// ----- Settings -----
TABS.settings = async main => {
  await loadSettings();
  if (!state.schema) state.schema = await api("GET", "/api/schema");
  const vals = state.settings.values, sec = state.settings.secrets;
  // Secrets first: they're what people come here for on day one.
  const key = h("input", { type: "password", placeholder: sec.llm_api_key ? "(set; type to replace)" : "sk-...", autocomplete: "off", style: "width:320px" });
  const ohKey = h("input", { type: "password", placeholder: sec.openhop_api_key ? "(set; type to replace)" : "API token", autocomplete: "off", style: "width:320px" });
  const qrzUser = h("input", { type: "text", placeholder: sec.qrz ? "(set) username" : "QRZ username", autocomplete: "off", style: "width:160px" });
  const qrzPass = h("input", { type: "password", placeholder: "QRZ password", autocomplete: "off", style: "width:160px" });
  const guestPw = h("textarea", { placeholder: "IN.ALL.Example.Rptr=hello", autocomplete: "off", style: "min-height:60px;width:420px" });
  const pwCur = h("input", { type: "password", placeholder: "current password", autocomplete: "current-password" });
  const pwNew = h("input", { type: "password", placeholder: "new password (8+ chars)", autocomplete: "new-password" });
  main.append(h("div", { class: "card" }, h("h2", {}, "Secrets"),
    h("div", { class: "muted small", style: "margin-bottom:6px" }, "Stored on the server in data/secrets.json (owner-only). They're never shown again after saving."),
    h("label", { class: "field" }, h("div", {}, "LiteLLM API key", h("div", { class: "help" },
        sec.llm_api_key ? "Set from this page." : sec.llm_api_key_env ? "Not set here; using LITELLM_API_KEY from .env." : "Not set.")),
      h("div", { class: "row" }, key,
        h("button", { class: "primary", onclick: async () => { if (!key.value) return;
          state.settings = await api("POST", "/api/secrets", { llm_api_key: key.value }); toast("API key saved"); showTab("settings"); } }, "Save"),
        sec.llm_api_key ? h("button", { class: "danger", onclick: async () => { if (!confirm("Clear the API key?")) return;
          state.settings = await api("POST", "/api/secrets", { llm_api_key: null }); showTab("settings"); } }, "Clear") : "")),
    h("label", { class: "field" }, h("div", {}, "openHop API key", h("div", { class: "help" },
        "For our repeater's own stats (create one in openHop's web UI). meshbot only calls read-only endpoints with it. ",
        sec.openhop_api_key ? "Set from this page." : sec.openhop_api_key_env ? "Not set here; using OPENHOP_API_KEY from .env." : "Not set.")),
      h("div", { class: "row" }, ohKey,
        h("button", { class: "primary", onclick: async () => { if (!ohKey.value) return;
          state.settings = await api("POST", "/api/secrets", { openhop_api_key: ohKey.value }); toast("openHop key saved"); showTab("settings"); } }, "Save"),
        sec.openhop_api_key ? h("button", { class: "danger", onclick: async () => { if (!confirm("Clear the openHop API key?")) return;
          state.settings = await api("POST", "/api/secrets", { openhop_api_key: null }); showTab("settings"); } }, "Clear") : "")),
    h("label", { class: "field" }, h("div", {}, "QRZ.com login", h("div", { class: "help" },
        "Optional, for worldwide callsign lookups (QRZ XML data subscription). Without it, US calls use free FCC data. Only first name, last initial and city/state are ever returned. ",
        sec.qrz ? "Set." : "Not set.")),
      h("div", { class: "row" }, qrzUser, qrzPass,
        h("button", { class: "primary", onclick: async () => { if (!qrzUser.value || !qrzPass.value) return toast("Enter both", true);
          state.settings = await api("POST", "/api/secrets", { qrz_username: qrzUser.value, qrz_password: qrzPass.value }); toast("QRZ login saved"); showTab("settings"); } }, "Save"),
        sec.qrz ? h("button", { class: "danger", onclick: async () => { if (!confirm("Clear the QRZ login?")) return;
          state.settings = await api("POST", "/api/secrets", { qrz_username: null, qrz_password: null }); showTab("settings"); } }, "Clear") : "",
        h("button", { onclick: async () => { const c = prompt("Callsign to test:", "W1AW"); if (!c) return;
          const r = await api("POST", "/api/test_callsign", { callsign: c }); r.ok ? toast(`${r.text} (${r.source || "no record"})`) : toast(r.error, true); } }, "Test lookup"))),
    h("label", { class: "field" }, h("div", {}, "Repeater guest passwords", h("div", { class: "help" },
        "One per line: Repeater name=password. Used by radio queries; others use the default guest password in Mesh data. ",
        (sec.guest_passwords || []).length ? `Set for: ${sec.guest_passwords.join(", ")}. Saving replaces the whole list.` : "None set.")),
      h("div", {}, guestPw, h("div", { class: "row" },
        h("button", { class: "primary", onclick: async () => {
          state.settings = await api("POST", "/api/secrets", { guest_passwords: guestPw.value }); toast("Guest passwords saved"); showTab("settings"); } }, "Save"),
        (sec.guest_passwords || []).length ? h("button", { class: "danger", onclick: async () => { if (!confirm("Clear all repeater guest passwords?")) return;
          state.settings = await api("POST", "/api/secrets", { guest_passwords: "" }); showTab("settings"); } }, "Clear") : ""))),
    h("label", { class: "field" }, h("div", {}, "Web password", h("div", { class: "help" }, "Login is user admin.")),
      h("div", { class: "row" }, pwCur, pwNew, h("button", { onclick: async () => {
        await api("POST", "/api/password", { current: pwCur.value, new: pwNew.value });
        toast("Password changed; your browser will ask you to log in again"); setTimeout(() => location.reload(), 1500); } }, "Change")))));
  for (const s of state.schema.sections) {
    const ctrls = s.fields.map(f => { const path = (s.prefix ? s.prefix + "." : "") + f.key; return [path, f, fieldControl(f, vals[path])]; });
    main.append(h("div", { class: "card" }, h("h2", {}, s.title), s.id === "defaults" ? h("div", { class: "muted small" }, "Each channel uses these unless it overrides them on the Channels tab.") : "",
      ...ctrls.map(([, f, c]) => h("label", { class: "field" }, h("div", {}, f.label, h("div", { class: "help" }, f.help)), c.el)),
      s.id === "home_assistant" ? hassCheck() : "",
      h("div", { class: "row end" }, h("button", { class: "primary", onclick: async () => {
        const values = Object.fromEntries(ctrls.map(([p, , c]) => [p, c.get()]));
        state.settings = await api("POST", "/api/settings", { values }); toast(`${s.title} saved`); } }, "Save"))));
  }
};

// Home Assistant: show the server LiteLLM gives us and how each tool is treated (read / control / blocked).
function hassCheck() {
  const out = h("div", { class: "small" });
  return h("div", { style: "margin:8px 0" }, h("div", { class: "row" }, h("button", { onclick: async e => {
    e.target.disabled = true;
    try {
      const r = await api("GET", "/api/hass");
      if (!r.ok) { out.replaceChildren(h("div", { style: "color:var(--bad)" }, r.error)); return; }
      out.replaceChildren(h("div", { class: "muted" }, `Server ${r.server_id}: ${r.tools.length} tools. Save first if you changed the lists.`),
        ...["read", "control", "blocked", "hidden"].flatMap(st => {
          const ts = r.tools.filter(t => t.status === st);
          if (!ts.length) return [];
          const color = { read: "var(--ok)", control: "var(--warn)", blocked: "var(--bad)", hidden: "var(--muted)" }[st];
          const label = { read: "Read (read + control mode)", control: "Control (control mode only)", blocked: "Blocked", hidden: "Hidden (never offered)" }[st];
          const rows = ts.map(t => h("div", {}, h("b", {}, t.name), h("span", { class: "muted" }, " " + t.description.slice(0, 100))));
          return [h("div", { style: `margin-top:8px;color:${color};font-weight:600` }, `${label}: ${ts.length}`),
            ...(st === "hidden" ? [h("details", {}, h("summary", { class: "muted" }, "show"), ...rows)] : rows)];
        }));
    } finally { e.target.disabled = false; } } }, "Check Home Assistant tools"),
    h("span", { class: "muted small" }, "Lists the tools; calls nothing in your house.")), out);
}

// ----- Advanced -----
TABS.advanced = async main => {
  const { text } = await api("GET", "/api/raw_config");
  const ta = h("textarea", { style: "min-height:420px" }); ta.value = text;
  const logs = h("div", { class: "logs" });
  main.append(h("div", { class: "card" }, h("h2", {}, "config.yaml"),
    h("div", { class: "muted small", style: "margin-bottom:8px" }, "The raw file behind the other tabs. It's validated before saving; a bad edit is rejected and the old file is kept."),
    ta, h("div", { class: "row end", style: "margin-top:8px" },
      h("button", { onclick: async () => { ta.value = (await api("GET", "/api/raw_config")).text; } }, "Reload"),
      h("button", { class: "primary", onclick: async () => { await api("PUT", "/api/raw_config", { text: ta.value }); toast("config.yaml saved"); } }, "Save"))),
    h("div", { class: "card" }, h("h2", {}, "Log"), logs));
  let after = 0;
  every(3000, async () => {
    const lines = await api("GET", `/api/logs?after=${after}`);
    if (!lines.length) return;
    after = lines[lines.length - 1].seq;
    const atBottom = logs.scrollTop + logs.clientHeight >= logs.scrollHeight - 20;
    logs.append(...lines.map(l => h("div", { class: l.level }, l.text)));
    while (logs.children.length > 1000) logs.firstChild.remove();
    if (atBottom) logs.scrollTop = logs.scrollHeight;
  });
};

// ---------- routing ----------
async function showTab(name) {
  if (!TABS[name]) name = "dashboard";
  state.timers.forEach(clearInterval); state.timers = [];
  state.tab = name;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  const main = $("#main"); main.replaceChildren();
  try { await TABS[name](main); } catch (e) { main.append(h("div", { class: "card", style: "color:var(--bad)" }, "Error: " + e.message)); }
}
document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (t !== state.tab) showTab(t); });
api("GET", "/api/status").then(updateHeader).catch(() => {});
showTab(location.hash.slice(1) || "dashboard");
