// The page: one script, no build step and nothing fetched from elsewhere. Every text the server sends is set as
// text (never as HTML), so a prompt, an answer or a device name cannot add markup to the page.
"use strict";

const APPROVERS = ["approver", "tenant-admin"];
const ADMINS = ["tenant-admin"];
const FINISHED = ["VERIFIED", "ROLLED_BACK", "FAILED", "SUPERSEDED", "REJECTED", "EXPIRED"];
let me = null; // /api/me once signed in
let turns = []; // this page's conversation: {prompt, answer, state, request}
let ticket = 0; // the newest screen being drawn; an older one that finishes late is dropped
let timer = null;

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k in el && k !== "list") el[k] = v; // an input's list is read-only: it is set as an attribute
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false).map(
    (k) => (k instanceof Node ? k : String(k))));
  return el;
}

function logo() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#logo");
  svg.append(use);
  return svg;
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && me && !["/api/mfa", "/api/sign-in"].includes(path)) {
    me = null;
    start();
  }
  if (!res.ok) throw Object.assign(new Error(data.error || `The server answered ${res.status}.`), { status: res.status });
  return data;
}

const store = {
  get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private window: the field is just not remembered */ } },
};

// the server sends "2026-10-26 07:00:00.123456+00:00"; not every browser parses a space or six decimals
const when = (s) => new Date(String(s).replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));
const stamp = (s) => (s ? when(s).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never");
const clock = (s) => when(s).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function ago(s) {
  if (!s) return "never";
  const minutes = Math.round((Date.now() - when(s)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return stamp(s);
}
const count = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);
const pill = (state, text) => h("span", { class: "pill", "data-state": state }, text || state.replace("_", " "));
const target = (c) => (c.args.devices ? c.args.devices.join(", ") : `${c.args.module}/${c.args.workspace}`);
const title = (c) => `${c.capability} on ${target(c)}`;
const approved = (c) => c.decisions.filter((d) => d.decision === "approve").length;
const isApprover = () => APPROVERS.includes(me.role);
const isAdmin = () => ADMINS.includes(me.role);

function say(text, bad) {
  const flash = document.getElementById("flash");
  if (!flash) return;
  flash.textContent = text || "";
  flash.className = bad ? "flash bad" : "flash";
  flash.hidden = !text;
  if (text) flash.scrollIntoView({ block: "nearest" });
}

// run an action from a button: busy while it works, the server's refusal shown, the screen drawn again after
async function act(button, work, done) {
  const label = button && button.textContent;
  if (button) { button.disabled = true; button.textContent = "Working…"; }
  try {
    const result = await work();
    if (done !== false) say(typeof done === "string" ? done : "");
    if (done !== false) await route();
    return result;
  } catch (e) {
    say(e.message, true);
  } finally {
    if (button && button.isConnected) { button.disabled = false; button.textContent = label; }
  }
}

// a button that asks to be pressed a second time, instead of a browser dialog
function sure(label, armed, work, done, cls = "danger small") {
  const b = h("button", { type: "button", class: cls, onblur: () => { b.classList.remove("armed"); b.textContent = label; } }, label);
  b.addEventListener("click", () => {
    if (!b.classList.contains("armed")) { b.classList.add("armed"); b.textContent = armed; return; }
    b.classList.remove("armed");
    act(b, work, done);
  });
  return b;
}

function field(label, input, hint) {
  input.id = input.id || `f-${Math.random().toString(36).slice(2, 9)}`;
  return h("div", { class: "field" }, h("label", { htmlFor: input.id }, label), input, hint && h("span", { class: "muted" }, hint));
}

function json(data) {
  return h("pre", { class: "json" }, JSON.stringify(data, null, 1));
}

function facts(pairs) {
  return h("dl", { class: "facts" }, pairs.filter(Boolean).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

// Markdown the model writes, drawn as elements: paragraphs, headings, lists, tables, code, bold and http(s) links
function markdown(text) {
  const lines = String(text || "").split("\n");
  const out = [];
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isItem = (l) => /^\s*([-*]|\d+\.)\s+/.test(l);
  const starts = /^(```|#{1,4}\s)/;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code = [];
      for (i++; i < lines.length && !lines[i].startsWith("```"); i++) code.push(lines[i]);
      i++;
      out.push(h("pre", {}, h("code", {}, code.join("\n"))));
    } else if (/^#{1,4}\s/.test(line)) {
      const [, marks, rest] = line.match(/^(#{1,4})\s+(.*)/);
      out.push(h(`h${Math.min(marks.length + 2, 6)}`, {}, inline(rest)));
      i++;
    } else if (isRow(line)) {
      const rows = [];
      for (; i < lines.length && isRow(lines[i]); i++) rows.push(lines[i].trim().slice(1, -1).split("|").map((c) => c.trim()));
      const head = rows.length > 1 && rows[1].every((c) => /^:?-+:?$/.test(c)) ? rows.splice(0, 2)[0] : null;
      out.push(h("div", { class: "scroll" }, h("table", {}, head && h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inline(c))))),
        h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, inline(c)))))))));
    } else if (isItem(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      for (; i < lines.length && isItem(lines[i]); i++) items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""));
      out.push(h(ordered ? "ol" : "ul", {}, items.map((t) => h("li", {}, inline(t)))));
    } else if (!line.trim()) {
      i++;
    } else {
      const para = [];
      do para.push(lines[i++]);
      while (i < lines.length && lines[i].trim() && !starts.test(lines[i]) && !isRow(lines[i]) && !isItem(lines[i]));
      out.push(h("p", {}, inline(para.join("\n"))));
    }
  }
  return h("div", { class: "md" }, out);
}

function inline(text) {
  const parts = [];
  const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(pattern)) {
    parts.push(text.slice(last, m.index));
    if (m[1] !== undefined) parts.push(h("code", {}, m[1]));
    else if (m[2] !== undefined) parts.push(h("strong", {}, m[2]));
    else parts.push(h("a", { href: m[4], rel: "noopener noreferrer", target: "_blank" }, m[3]));
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}

function diff(text) {
  return h("pre", { class: "diff" }, String(text).split("\n").map((l) => h("span", {
    class: l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "at" : null,
  }, `${l}\n`)));
}

// ---------- signing in ----------

async function start() {
  clearTimeout(timer);
  const app = document.getElementById("app");
  let step;
  try {
    step = await fetch("/api/me").then(async (r) => (r.ok ? r.json() : null));
  } catch {
    return app.replaceChildren(h("p", { class: "loading" }, "The orchestrator cannot be reached. Try again in a moment."));
  }
  if (!step) return app.replaceChildren(signIn());
  if (step.mfa) return app.replaceChildren(code(step));
  me = step;
  app.replaceChildren(shell());
  window.onhashchange = route;
  route();
}

function welcome(form) {
  return h("div", { class: "welcome" },
    h("section", { class: "story" },
      h("div", { class: "brand" }, logo(), "Rezztor Orchestrator"),
      h("h1", {}, "One request in. Facts, a plan, and people in charge."),
      h("ol", {}, [
        "The agent reads your systems and answers from what it found.",
        "For a change it writes a dry-run plan. People approve that exact plan.",
        "The executor applies it, checks it, and backs it out if it fails.",
      ].map((t, n) => h("li", {}, h("span", {}, `0${n + 1}`), t))),
      h("small", {}, "AGPL-3.0 · Rezztor Technologies (Private) Limited")),
    h("section", { class: "form" }, form));
}

function signIn() {
  const org = h("input", { autocomplete: "organization", required: true, value: store.get("orch.org") });
  const user = h("input", { autocomplete: "username", required: true });
  const password = h("input", { type: "password", autocomplete: "current-password", required: true });
  const error = h("p", { class: "error", role: "alert" });
  const go = h("button", { type: "submit" }, "Continue");
  const directory = h("button", { type: "button", class: "directory", onclick: () => {
    if (!org.value.trim()) { error.textContent = "Type your organisation first."; return org.focus(); }
    store.set("orch.org", org.value.trim());
    window.location.assign(`/oidc/start?tenant=${encodeURIComponent(org.value.trim())}`);
  } }, "Sign in with your company directory");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    go.disabled = true;
    try {
      await api("/api/sign-in", { tenant: org.value.trim(), user: user.value.trim(), password: password.value });
      store.set("orch.org", org.value.trim());
      start();
    } catch (err) {
      error.textContent = err.message;
      password.value = "";
      password.focus();
    } finally {
      go.disabled = false;
    }
  } },
  h("div", { class: "field" }, h("h1", {}, "Sign in"),
    h("p", { class: "muted" }, "Use your company account where your organisation has set one up.")),
  field("Organisation", org), directory, h("div", { class: "or" }, "or with a password"),
  field("Username", user), field("Password", password), go, error,
  h("p", { class: "muted" }, "Approvers and administrators enter a code from their authenticator app next. After five wrong tries sign-in pauses for five minutes."));
  queueMicrotask(() => (org.value ? user : org).focus());
  return welcome(form);
}

function code(step) {
  const input = h("input", { class: "otp", inputmode: "numeric", autocomplete: "one-time-code", maxLength: 6,
    pattern: "[0-9]{6}", required: true });
  const error = h("p", { class: "error", role: "alert" });
  const enrol = step.mfa === "enroll";
  const uri = enrol && String(step.uri).startsWith("otpauth://") ? step.uri : null;
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    try {
      await api("/api/mfa", { code: input.value.trim() });
      start();
    } catch (err) {
      error.textContent = err.message;
      input.value = "";
      input.focus();
    }
  } },
  h("span", { class: "k" }, `Step 2 of 2 · ${step.user}, ${step.role}`),
  h("h1", {}, enrol ? "Add your authenticator" : "Enter your code"),
  enrol
    ? [h("p", {}, "Approvers and administrators confirm every sign-in with an authenticator app (Microsoft Authenticator, Google Authenticator, 1Password and the like). Add this account to yours with the key below, then type the code it shows."),
      h("p", { class: "secret" }, step.secret.match(/.{1,4}/g).join(" ")),
      uri && h("a", { href: uri }, "Open in an authenticator app on this device")]
    : h("p", { class: "muted" }, "Open your authenticator app and type the 6-digit code for Rezztor Orchestrator."),
  field("6-digit code", input), h("button", { type: "submit" }, "Verify and continue"), error,
  h("div", { class: "actions" },
    h("button", { type: "button", class: "secondary small", onclick: async () => { await api("/api/sign-out", {}); start(); } }, "Use a different account"),
    h("span", { class: "muted" }, "Lost your phone? Ask an administrator.")));
  queueMicrotask(() => input.focus());
  return welcome(form);
}

// ---------- the signed-in shell ----------

const NAV = [
  ["Work", [["overview", "Overview"], ["ask", "Ask"], ["changes", "Changes"], ["activity", "Activity", APPROVERS], ["recipes", "Recipes"]]],
  ["Estate", [["devices", "Devices"], ["skills", "Skills"]]],
  ["Administration", [["people", "People and policy", ADMINS], ["integrations", "Integrations", ADMINS]]],
];

function shell() {
  const nav = h("nav", { "aria-label": "Screens" }, NAV.map(([group, items]) => {
    const shown = items.filter(([, , roles]) => !roles || roles.includes(me.role));
    return shown.length && h("div", { class: "group" }, h("span", {}, group), shown.map(([id, label]) =>
      h("a", { href: `#/${id}`, "data-screen": id }, label, id === "changes" && h("span", { class: "badge", id: "badge", hidden: true }))));
  }));
  const org = store.get("orch.org");
  return h("div", { class: "shell" },
    h("aside", { class: "side" }, h("div", { class: "brand" }, logo(), "Orchestrator"), nav,
      h("div", { class: "foot" },
        h("span", { class: `state${me.stopped ? " stopped" : ""}`, id: "agent" }, me.stopped ? "Agent work stopped" : "Agent running"),
        h("span", {}, h("strong", {}, me.user), ` · ${me.role}${org ? ` · ${org}` : ""}`),
        h("button", { type: "button", class: "small", onclick: async () => {
          await api("/api/sign-out", {}).catch(() => {});
          me = null;
          turns = [];
          start();
        } }, "Sign out"))),
    h("div", { class: "page" },
      h("p", { class: "banner", id: "stopped", role: "status", hidden: !me.stopped }, me.stopped && `Agent work is stopped: ${me.stopped}`),
      h("p", { class: "flash", id: "flash", role: "status", hidden: true }),
      h("main", { id: "main" })));
}

const SCREENS = {
  overview: { draw: overview },
  ask: { draw: ask },
  changes: { draw: changesScreen },
  activity: { draw: activity, roles: APPROVERS },
  recipes: { draw: recipesScreen },
  devices: { draw: devicesScreen },
  skills: { draw: skillsScreen },
  people: { draw: peopleScreen, roles: ADMINS },
  integrations: { draw: integrations, roles: ADMINS },
};

let lastScreen = "";
async function route() {
  if (!me) return;
  clearTimeout(timer);
  const mine = ++ticket;
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const [name, id] = path.split("/");
  const screen = SCREENS[name] && (!SCREENS[name].roles || SCREENS[name].roles.includes(me.role)) ? name : "ask";
  if (screen !== lastScreen) say("");
  lastScreen = screen;
  for (const a of document.querySelectorAll(".side nav a")) {
    if (a.dataset.screen === screen) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  refreshShell();
  try {
    const node = await SCREENS[screen].draw(id ? decodeURIComponent(id) : null, new URLSearchParams(query || ""));
    if (mine !== ticket || !me) return;
    document.getElementById("main").replaceChildren(node);
  } catch (e) {
    if (mine !== ticket || !me) return;
    document.getElementById("main").replaceChildren(h("p", { class: "empty" }, e.message));
  }
}

async function refreshShell() {
  try {
    const [now, list] = await Promise.all([api("/api/me"), api("/api/changes")]);
    me = now;
    const badge = document.getElementById("badge");
    const waiting = list.changes.filter((c) => c.state === "PLANNED").length;
    if (badge) { badge.textContent = waiting; badge.hidden = !waiting; }
    const banner = document.getElementById("stopped");
    banner.hidden = !me.stopped;
    banner.textContent = me.stopped ? `Agent work is stopped: ${me.stopped}` : "";
    const agent = document.getElementById("agent");
    agent.textContent = me.stopped ? "Agent work stopped" : "Agent running";
    agent.className = `state${me.stopped ? " stopped" : ""}`;
  } catch { /* the screen shows its own error */ }
}

function head(text, sub, ...extra) {
  return h("div", { class: "head" }, h("div", {}, h("h1", {}, text), sub && h("p", {}, sub)), extra);
}

// ---------- Ask ----------

async function ask() {
  const [history, list] = await Promise.all([api("/api/requests"), api("/api/changes")]);
  const prompt = h("textarea", { rows: 3, maxLength: 8000, required: true,
    placeholder: "Ask about your infrastructure, for example: which devices in Colombo are down?" });
  const send = h("button", { type: "submit" }, "Ask");
  const log = h("ol", { class: "log", "aria-live": "polite" }, turns.map((t) => turn(t, list.changes)));
  if (!turns.length) {
    log.append(h("li", { class: "empty" }, "Ask about your devices, monitoring or a change you need. Answers come from what the agent read. A change comes back as a plan for people to approve; nothing changes until they do."));
  }
  const submit = async () => {
    const text = prompt.value.trim();
    if (!text) return;
    if (!turns.length) log.replaceChildren();
    const pending = h("li", { class: "turn" }, h("div", { class: "you" }, text), h("div", { class: "answer muted" }, "Working… the agent is reading your systems."));
    log.append(pending);
    prompt.value = "";
    const res = await act(send, () => api("/api/ask", { prompt: text }), false);
    if (!res) { pending.remove(); prompt.value = text; return; }
    turns.push({ prompt: text, answer: res.answer, state: res.state, request: res.request });
    route();
  };
  prompt.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } });
  const open = async (id) => {
    const r = await api(`/api/requests/${id}`).catch((e) => say(e.message, true));
    if (!r) return;
    turns = [{ prompt: r.prompt, answer: r.answer, state: r.state, request: r.id }];
    route();
  };
  const shownIds = turns.map((t) => t.request);
  return h("div", { class: "ask" },
    h("section", { class: "card" }, h("div", { class: "actions" }, h("h2", {}, "Your requests"),
      turns.length > 0 && h("button", { type: "button", class: "secondary small", onclick: () => { turns = []; route(); } }, "New")),
    history.requests.length
      ? h("ol", { class: "history" }, history.requests.map((r) => h("li", {}, h("button", {
        type: "button", class: shownIds.includes(r.id) ? "on" : "", onclick: () => open(r.id) },
      h("span", { class: "line" }, r.prompt), h("span", { class: "meta" }, `${r.state} · ${ago(r.created_at)}`)))))
      : h("p", { class: "muted" }, "Nothing asked yet.")),
    h("section", { class: "talk" }, log,
      h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); submit(); } },
        h("label", { class: "visually-hidden", htmlFor: "prompt" }, "Ask the orchestrator"), Object.assign(prompt, { id: "prompt" }),
        h("div", { class: "actions" }, h("span", {}, "Never type passwords here. Ctrl+Enter sends."), send))));
}

function turn(t, changes) {
  const plans = changes.filter((c) => c.request_id === t.request);
  const failed = t.state === "FAILED";
  return h("li", { class: "turn" }, h("div", { class: "you" }, t.prompt),
    h("div", { class: `answer${t.state === "DONE" ? "" : " failed"}` },
      failed ? h("p", {}, `This request could not be completed. Reference ${t.request}.`)
        : t.answer ? markdown(t.answer) : h("p", { class: "muted" }, `Still ${String(t.state).toLowerCase()}…`),
      plans.map(planLink), t.request && steps(t.request)));
}

function planLink(c) {
  return h("a", { class: "plan", href: `#/changes/${c.id}` }, pill(c.state), h("strong", {}, title(c)),
    h("span", { class: "muted" }, `risk ${c.risk} · approved ${approved(c)} of ${c.approvals_needed}`));
}

function steps(request) {
  const box = h("details", { class: "steps" }, h("summary", {}, "Steps"));
  box.addEventListener("toggle", async () => {
    if (!box.open || box.dataset.loaded) return;
    box.dataset.loaded = "1";
    try {
      const r = await api(`/api/requests/${request}`);
      box.append(h("ol", {}, r.steps.map((s) => h("li", {}, h("span", { class: "mono" }, `${clock(s.at)} · ${s.actor} · ${s.kind}`),
        Object.keys(s.data || {}).length > 0 && json(s.data)))));
    } catch (e) {
      box.append(h("p", { class: "error" }, e.message));
    }
  });
  return box;
}

// ---------- Changes ----------

const FILTERS = { waiting: ["Waiting", ["PLANNED"]], approved: ["Approved", ["APPROVED", "EXECUTING"]], finished: ["Finished", FINISHED], all: ["All", null] };

async function changesScreen(id, query) {
  const [{ changes }, pol] = await Promise.all([api("/api/changes"), api("/api/policy")]);
  const show = FILTERS[query.get("show")] ? query.get("show") : (id ? "all" : "waiting");
  const shown = changes.filter((c) => !FILTERS[show][1] || FILTERS[show][1].includes(c.state));
  const chosen = changes.find((c) => c.id === id) || shown[0];
  // name the shown change in the address, so it stays on screen after a decision moves it to another tab
  if (chosen && !id) history.replaceState(null, "", `#/changes/${chosen.id}?show=${show}`);
  if (chosen && chosen.state === "EXECUTING") timer = setTimeout(route, 3000);
  return h("div", { class: "talk" },
    head("Changes", "The agent only proposes. A change runs after the people its risk needs have approved the plan they were shown."),
    h("nav", { class: "tabs", "aria-label": "Show" }, Object.entries(FILTERS).map(([key, [label, states]]) =>
      h("a", { href: `#/changes?show=${key}`, "aria-current": key === show ? "page" : null },
        `${label} ${changes.filter((c) => !states || states.includes(c.state)).length}`))),
    h("div", { class: "board" },
      shown.length
        ? h("ol", { class: "list" }, shown.map((c) => h("li", {}, h("a", {
          href: `#/changes/${c.id}?show=${show}`, "aria-current": chosen && c.id === chosen.id ? "true" : null },
        h("div", { class: "actions" }, pill(c.state), pill(c.risk, c.risk.toUpperCase())),
        h("strong", {}, title(c)),
        h("span", { class: "meta" }, `${c.requested_by} · ${ago(c.created_at)} · approved ${approved(c)} of ${c.approvals_needed}`)))))
        : h("p", { class: "empty" }, "No changes here."),
      chosen ? change(chosen, pol.effective) : h("p", { class: "empty" }, "Pick a change to see its plan.")));
}

function change(c, pol) {
  const mine = c.requested_by === me.user;
  const decided = c.decisions.some((d) => d.user === me.user);
  const open = ["PLANNED", "APPROVED"].includes(c.state);
  const hosts = Object.entries(c.plan.hosts || {});
  const now = new Date();
  const freezes = (pol.freeze || []).filter((f) => when(f.end) > now);
  const needsRecord = pol.change_record.includes(c.risk);
  const card = h("article", { class: "card" },
    h("div", { class: "actions" }, pill(c.state), pill(c.risk, c.risk.toUpperCase()), c.break_glass && pill("bad", "BREAK-GLASS")),
    h("h2", {}, title(c)),
    h("p", { class: "muted" }, `Asked by ${c.requested_by} at ${stamp(c.created_at)}.${open ? ` Expires ${stamp(c.expires_at)}.` : ""}`),
    h("div", { class: "stats" },
      h("div", {}, h("span", { class: "k" }, "Risk"), c.risk),
      h("div", {}, h("span", { class: "k" }, "Approvals"), `${approved(c)} of ${c.approvals_needed}`),
      h("div", {}, h("span", { class: "k" }, "Back-out"), c.plan.rollback ? "Ready" : "None"),
      h("div", {}, h("span", { class: "k" }, "Digest"), h("span", { class: "mono", title: c.digest }, `${c.digest.slice(0, 4)}…${c.digest.slice(-4)}`))),
    c.break_glass && h("p", { class: "alert" }, `Approved alone in an emergency: ${c.break_glass}`),
    c.change_record && h("p", {}, `Change record ${c.change_record}`),
    h("h3", {}, "What the plan changes (dry run)"),
    hosts.map(([host, plan]) => {
      const changed = (plan.tasks || []).filter((t) => t.changed);
      return h("div", { class: "field" }, h("span", { class: "mono" }, host),
        changed.length ? changed.map((t) => [h("span", { class: "muted" }, t.task), diff(t.diff || "(changes, no diff)")])
          : h("p", { class: "muted" }, "No change on this host."));
    }),
    h("details", {}, h("summary", {}, "Arguments the agent chose"), json(c.args)),
    c.review && [h("h3", {}, "Reviewer"), h("p", { class: "note" }, c.review)],
    c.decisions.length > 0 && [h("h3", {}, "Decisions"), h("ul", { class: "checks" }, c.decisions.map((d) =>
      h("li", { class: d.decision === "approve" ? "done" : "" }, `${d.user} ${d.decision === "approve" ? "approved" : "rejected"}${d.reason ? `: ${d.reason}` : ""}`)))],
    c.result && [h("h3", {}, `Result: ${c.result.outcome || c.state}`), h("details", {}, h("summary", {}, "What the executor reported"), json(c.result))],
    open && [h("h3", {}, "Before it can run"), h("ul", { class: "checks" },
      h("li", { class: approved(c) >= c.approvals_needed ? "done" : "" },
        approved(c) >= c.approvals_needed ? "Approved by enough people" : `${c.approvals_needed - approved(c)} more approval${c.approvals_needed - approved(c) === 1 ? "" : "s"}, never from ${c.requested_by}`),
      needsRecord && h("li", { class: c.change_record ? "done" : "" }, c.change_record ? `Change record ${c.change_record} attached` : `An approved change record (${c.risk} risk needs one)`),
      pol.windows.map((w) => h("li", {}, `Runs only ${w.days.join(", ")} ${w.start}–${w.end} UTC`)),
      freezes.map((f) => h("li", {}, `No change runs ${stamp(f.start)} to ${stamp(f.end)}: ${f.reason}`)),
      !pol.windows.length && !freezes.length && h("li", { class: "done" }, "No change window or freeze applies"))]);

  const actions = h("div", { class: "field" });
  const may = isApprover() || mine;
  if (open && may) {
    const number = h("input", { value: c.change_record || "", placeholder: "CHG0012345", maxLength: 40 });
    actions.append(h("div", { class: "row" }, field("Change record", number),
      h("button", { type: "button", class: "secondary", onclick: (e) => number.value.trim() &&
        act(e.currentTarget, () => api("/api/changes/record", { change: c.id, number: number.value.trim() }), "Change record saved.") },
      c.change_record ? "Change the record" : "Attach")));
  }
  if (c.state === "PLANNED" && isApprover() && !mine && !decided) {
    const note = h("textarea", { rows: 2, maxLength: 500, placeholder: "Optional when approving; say why when rejecting" });
    const decide = (btn, yes) => {
      if (!yes && !note.value.trim()) { say("Say why you are rejecting the change.", true); return note.focus(); }
      act(btn, () => api("/api/changes/decide", { change: c.id, digest: c.digest, approve: yes, reason: note.value.trim() || undefined }),
        yes ? "You approved this plan." : "You rejected this change.");
    };
    actions.append(field("Your note", note), h("div", { class: "actions" },
      h("button", { type: "button", onclick: (e) => decide(e.currentTarget, true) }, "Approve this plan"),
      h("button", { type: "button", class: "danger", onclick: (e) => decide(e.currentTarget, false) }, "Reject")));
    if (isAdmin() && c.risk !== "destructive") {
      const why = h("textarea", { rows: 2, maxLength: 500, placeholder: "What is the emergency?" });
      actions.append(h("details", {}, h("summary", {}, "Break glass"),
        h("div", { class: "field" }, h("p", { class: "muted" }, "Approve on your own, outside the change window. Every webhook is told and the ledger flags it. A freeze still holds."),
          field("Reason", why), h("div", { class: "actions" }, sure("Approve alone", "Press again to break glass", () => {
            if (!why.value.trim()) throw new Error("Say what the emergency is.");
            return api("/api/changes/break-glass", { change: c.id, digest: c.digest, reason: why.value.trim() });
          }, "Approved alone. Every webhook was told and the ledger flags it.", "danger")))));
    }
  } else if (c.state === "PLANNED" && isApprover()) {
    actions.append(h("p", { class: "muted" }, mine ? "You asked for this change, so other people approve it." : "You have decided on this change."));
  }
  if (c.state === "APPROVED" && may) {
    actions.append(h("div", { class: "actions" }, h("button", { type: "button", onclick: (e) =>
      act(e.currentTarget, () => api("/api/changes/run", { change: c.id }), "The change is running. This page follows it.") }, "Run the change")));
  }
  if (c.state === "EXECUTING") actions.append(h("p", { class: "muted" }, "Running now. This page checks every few seconds."));
  if (FINISHED.includes(c.state) && may) {
    actions.append(h("div", { class: "actions" },
      h("a", { class: "button secondary", href: `/api/changes/evidence/${c.id}`, download: `change-${c.id}.json` }, "Export evidence"),
      isApprover() && h("a", { href: `#/activity?ref=${c.id}` }, "Ledger rows")));
  }
  if (actions.childElementCount) card.append(h("div", { class: "decide" }, actions));
  return card;
}

// ---------- Overview ----------

async function overview() {
  const [o, links] = await Promise.all([api("/api/overview"), isAdmin() ? api("/api/connections") : null]);
  const bars = o.per_hour;
  const top = Math.max(1, ...bars);
  const chart = h("div", { class: "bars", role: "img",
    "aria-label": `Requests per hour over the last 24 hours, oldest first: ${bars.join(", ")}` },
  bars.map((n) => {
    const bar = h("span", { title: `${n} request${n === 1 ? "" : "s"}` });
    bar.style.setProperty("--v", (n / top) * 100);
    return bar;
  }));
  const tokens = h("meter", { min: 0, max: o.tokens_limit, value: o.tokens_24h, optimum: 0, high: o.tokens_limit * 0.8 });
  const reason = h("input", { maxLength: 500, placeholder: "Why are you stopping all agent work?" });
  return h("div", { class: "talk" },
    head("Overview", "What the agent did and what is waiting for people."),
    h("div", { class: "tiles" },
      h("div", { class: "tile" }, h("span", { class: "k" }, "Requests, 24 h"), h("strong", {}, count(o.requests_24h)),
        h("span", { class: "muted" }, `${o.requests_24h.FAILED || 0} failed · ${o.requests_24h.REFUSED || 0} refused`)),
      h("a", { class: "tile", href: "#/changes" }, h("span", { class: "k" }, "Waiting for approval"), h("strong", {}, o.waiting),
        h("span", { class: "muted" }, "Open the changes")),
      h("div", { class: "tile" }, h("span", { class: "k" }, "Changes, 7 days"), h("strong", {}, count(o.changes_7d)),
        h("span", { class: "muted" }, `${o.changes_7d.VERIFIED || 0} verified · ${o.changes_7d.ROLLED_BACK || 0} backed out`)),
      h("div", { class: "tile" }, h("span", { class: "k" }, "Model tokens, 24 h"), h("strong", {}, o.tokens_24h.toLocaleString()), tokens,
        h("span", { class: "muted" }, `of ${o.tokens_limit.toLocaleString()} a day`))),
    h("div", { class: "split" },
      h("section", { class: "card" }, h("h2", {}, "Requests per hour"), chart, h("div", { class: "axis" }, h("span", {}, "24 h ago"), h("span", {}, "now"))),
      h("section", { class: "card" }, h("h2", {}, "Latest changes"),
        o.recent_changes.length ? o.recent_changes.map(planLink) : h("p", { class: "muted" }, "No changes proposed yet."))),
    h("div", { class: "split" },
      h("section", { class: "card" }, h("h2", {}, "Audit ledger"),
        facts([["Rows", o.ledger.rows || 0], ["Last row", ago(o.ledger.last_at)], ["Last anchor", ago(o.ledger.last_anchor_at)]]),
        isApprover() && h("a", { href: "#/activity" }, "Open the ledger")),
      isAdmin() && h("section", { class: "card" }, h("h2", {}, "Stop all agent work"),
        o.stopped
          ? [h("p", {}, `Stopped: ${o.stopped}`), h("div", { class: "actions" }, h("button", { type: "button", onclick: (e) =>
            act(e.currentTarget, () => api("/api/kill-switch", { stop: false }), "Agent work resumed.") }, "Resume agent work"))]
          : [h("p", { class: "muted" }, "Stops every request and change run for this organisation until an administrator resumes it."),
            field("Reason", reason), h("div", { class: "actions" }, sure("Stop all agent work", "Press again to stop", () => {
              if (!reason.value.trim()) throw new Error("Say why you are stopping agent work.");
              return api("/api/kill-switch", { stop: true, reason: reason.value.trim() });
            }, "All agent work is stopped.", "danger"))])),
    links && h("section", { class: "card" }, h("div", { class: "actions" }, h("h2", {}, "Connected systems"), h("a", { href: "#/integrations" }, "Integrations")),
      h("div", { class: "actions" }, [["Model", links.model], ["Ansible", links.ansible], ["OpenTofu", links.tofu],
        ["Directory", links.directory], ["ServiceNow", links.servicenow], ["SolarWinds", links.solarwinds]].map(([name, on]) =>
        pill(on ? "ok" : "off", `${name} ${on ? "connected" : "not set"}`)), links.mcp.map((s) => pill("ok", `MCP ${s.name}`)))));
}

// ---------- Activity ----------

async function activity(_, query) {
  const keep = new URLSearchParams([...query].filter(([k, v]) => ["kind", "actor", "ref", "before"].includes(k) && v));
  const page = await api(`/api/audit?${keep}`);
  const kind = h("input", { name: "kind", value: query.get("kind") || "", placeholder: "change.", list: "kinds" });
  const actor = h("input", { name: "actor", value: query.get("actor") || "", placeholder: "alice" });
  const ref = h("input", { name: "ref", value: query.get("ref") || "", placeholder: "a request or change id" });
  const chain = page.chain;
  const older = new URLSearchParams(keep);
  if (page.rows.length) older.set("before", page.rows[page.rows.length - 1].seq);
  return h("div", { class: "talk" },
    head("Activity", "Every step anyone or the agent took, in a hash-chained ledger."),
    h("section", { class: "card" }, h("p", { class: `chain${chain.holds ? "" : " bad"}` },
      chain.holds ? "✓ The chain holds: no row was changed or removed." : `✗ The chain is broken at row ${chain.broken_at}.`),
    facts([["Rows", chain.rows], ["Anchors", chain.anchors], ["Last anchor", ago(chain.last_anchor_at)]])),
    h("form", { class: "card", onsubmit: (e) => {
      e.preventDefault();
      const q = new URLSearchParams([["kind", kind.value.trim()], ["actor", actor.value.trim()], ["ref", ref.value.trim()]].filter(([, v]) => v));
      location.hash = `#/activity?${q}`;
    } }, h("div", { class: "row" }, field("Kind starts with", kind), field("Actor", actor), field("Request or change", ref),
      h("button", { type: "submit" }, "Filter"), keep.toString() && h("a", { class: "button secondary", href: "#/activity" }, "Clear")),
    h("datalist", { id: "kinds" }, ["auth.", "request.", "tool.", "change.", "policy.", "user.", "skill.", "recipe.", "device.", "webhook.", "trigger.", "audit."].map((k) => h("option", { value: k })))),
    page.rows.length
      ? h("section", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Row", "When", "Actor", "Kind", "Details"].map((t) => h("th", {}, t)))),
        h("tbody", {}, page.rows.map((r) => {
          const text = JSON.stringify(r.data || {});
          return h("tr", {}, h("td", { class: "mono" }, r.seq), h("td", {}, stamp(r.at)), h("td", {}, r.actor), h("td", { class: "mono" }, r.kind),
            h("td", {}, text.length > 90 ? h("details", {}, h("summary", {}, `${text.slice(0, 80)}…`), json(r.data)) : h("span", { class: "mono" }, text)));
        }))), page.rows.length === 50 && h("a", { href: `#/activity?${older}` }, "Older rows"))
      : h("p", { class: "empty" }, "No rows match."));
}

// ---------- Recipes ----------

async function recipesScreen() {
  const lib = await api("/api/library");
  return h("div", { class: "talk" },
    head("Recipes", "Questions your organisation asks often, with blanks to fill in. A recipe runs as you, under your role."),
    lib.recipes.length ? h("div", { class: "cards" }, lib.recipes.map((r) => {
      const inputs = r.params.map((p) => h("input", { name: p, required: true, maxLength: 200 }));
      const go = h("button", { type: "submit" }, "Run recipe");
      return h("form", { class: "card", onsubmit: async (e) => {
        e.preventDefault();
        const values = Object.fromEntries(inputs.map((i) => [i.name, i.value.trim()]));
        const res = await act(go, () => api("/api/recipes/run", { name: r.name, values }), false);
        if (!res) return;
        turns.push({ prompt: `Recipe ${r.name}${r.params.length ? `: ${r.params.map((p) => `${p} = ${values[p]}`).join(", ")}` : ""}`, answer: res.answer, state: res.state, request: res.request });
        location.hash = "#/ask";
      } }, h("h2", {}, r.name), h("p", { class: "muted" }, r.description),
      inputs.map((i) => field(i.name.replaceAll("_", " "), i)),
      h("details", {}, h("summary", {}, "What it asks"), h("pre", { class: "json" }, r.prompt)), h("div", { class: "actions" }, go));
    })) : h("p", { class: "empty" }, "No recipes yet. An administrator adds them on the command line with recipe-add."));
}

// ---------- Devices ----------

async function devicesScreen() {
  const { devices } = await api("/api/devices");
  const add = ["name", "address", "platform", "site"].map((k) => h("input", { name: k, required: k !== "site", maxLength: 200,
    placeholder: { name: "core-sw1", address: "10.0.0.1", platform: "ios", site: "colombo" }[k] }));
  return h("div", { class: "talk" },
    head("Devices", "What the agent may read and plan changes on. A critical device raises a change's risk."),
    devices.length ? h("section", { class: "card scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "Address", "Platform", "Site", "Tags", "Critical"].map((t) => h("th", {}, t)))),
      h("tbody", {}, devices.map((d) => h("tr", {}, h("td", { class: "mono" }, d.name), h("td", { class: "mono" }, d.address), h("td", {}, d.platform),
        h("td", {}, d.site || "—"), h("td", {}, (d.tags || []).join(", ") || "—"),
        h("td", {}, isAdmin() ? h("input", { type: "checkbox", checked: d.critical, "aria-label": `${d.name} is critical`, onchange: (e) =>
          act(null, () => api("/api/devices/critical", { name: d.name, critical: e.currentTarget.checked }), `${d.name} saved.`) })
          : d.critical ? "Yes" : "No"))))))
      : h("p", { class: "empty" }, "No devices yet."),
    isAdmin() && h("form", { class: "card", onsubmit: (e) => {
      e.preventDefault();
      const body = Object.fromEntries(add.map((i) => [i.name, i.value.trim()]));
      act(e.submitter, () => api("/api/devices/add", body), `${body.name} added.`);
    } }, h("h2", {}, "Add a device"), h("div", { class: "row" }, add.map((i) => field(i.name[0].toUpperCase() + i.name.slice(1), i)),
      h("button", { type: "submit" }, "Add device"))));
}

// ---------- Skills ----------

async function skillsScreen() {
  const lib = await api("/api/library");
  return h("div", { class: "talk" },
    head("Skills", "Procedures the agent follows. One it proposes from its own work waits for an administrator."),
    h("p", { class: "muted" }, `Shipped with the product: ${lib.shipped_skills.join(", ") || "none"}.`),
    lib.skills.length ? h("div", { class: "cards" }, lib.skills.map((s) => h("article", { class: "card" },
      h("div", { class: "actions" }, pill(s.state), h("h2", {}, s.name)),
      h("p", {}, s.description),
      h("p", { class: "muted" }, `${s.source === "induced" ? "Proposed by the agent" : `Added by ${s.proposed_by}`} · uses ${s.capabilities.join(", ") || "no tools"}`),
      h("details", {}, h("summary", {}, "Procedure"), markdown(s.body)),
      isAdmin() && s.state !== "retired" && h("div", { class: "actions" },
        s.state === "proposed" && h("button", { type: "button", onclick: (e) => act(e.currentTarget, () => api("/api/skills/decide", { name: s.name, approve: true }), `${s.name} approved.`) }, "Approve skill"),
        sure(s.state === "proposed" ? "Reject" : "Retire", "Press again to confirm", () => api("/api/skills/decide", { name: s.name, approve: false }),
          `${s.name} ${s.state === "proposed" ? "rejected" : "retired"}.`))))) : h("p", { class: "empty" }, "No skills of your own yet."));
}

// ---------- People and policy ----------

const ROLES = ["viewer", "operator", "approver", "tenant-admin"];

async function peopleScreen() {
  const [{ directory, people }, pol] = await Promise.all([api("/api/people"), api("/api/policy")]);
  const name = h("input", { required: true, maxLength: 64, pattern: "[a-z0-9][a-z0-9._\\-]{0,63}", placeholder: "ava" });
  const role = h("select", {}, ROLES.map((r) => h("option", { value: r }, r)));
  const password = h("input", { type: "password", autocomplete: "new-password", minLength: 12 });
  const doc = h("textarea", { class: "code", rows: 12, value: pol.document || "# This organisation's own policy. It can only narrow the shipped one.\n" });
  const eff = pol.effective;
  const shipped = pol.shipped;
  return h("div", { class: "talk" },
    head("People and policy", "Who may do what here. A role change signs the person out, so a new approver signs in again with an authenticator."),
    h("section", { class: "card scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "Role", "Signs in with", "Authenticator", "Last sign-in", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, people.map((p) => h("tr", {}, h("td", {}, h("strong", {}, p.name)),
        h("td", {}, p.name === me.user ? p.role : h("select", { "aria-label": `Role of ${p.name}`, onchange: (e) =>
          act(null, () => api("/api/people/role", { name: p.name, role: e.currentTarget.value }), `${p.name} is now ${e.currentTarget.value} and signs in again.`) },
        ROLES.map((r) => h("option", { value: r, selected: r === p.role }, r)))),
        h("td", {}, p.password ? "Password" : directory ? "Company directory" : "Nothing yet"),
        h("td", {}, p.authenticator ? "Set" : APPROVERS.includes(p.role) ? "Not set: added at next sign-in" : "Not needed"),
        h("td", {}, ago(p.last_seen)),
        h("td", {}, p.authenticator && p.name !== me.user && sure("Reset authenticator", "Press again to reset", () =>
          api("/api/people/mfa-reset", { name: p.name }), `${p.name} adds a new authenticator at the next sign-in.`)))))),
    directory && h("p", { class: "muted" }, "Your company directory signs people in; someone who signs in from it for the first time is added as a viewer.")),
    h("form", { class: "card", onsubmit: (e) => {
      e.preventDefault();
      act(e.submitter, () => api("/api/people/add", { name: name.value.trim(), role: role.value, password: password.value || undefined }), `${name.value.trim()} added.`);
    } }, h("h2", {}, "Add a person"), h("div", { class: "row" }, field("Username", name), field("Role", role),
      field("Password", password, directory ? "Leave empty for directory sign-in" : "At least 12 characters"), h("button", { type: "submit" }, "Add person"))),
    h("div", { class: "split" },
      h("section", { class: "card scroll" }, h("h2", {}, "Approvals a change needs"), h("table", {},
        h("thead", {}, h("tr", {}, ["Risk", "Here", "Shipped"].map((t) => h("th", {}, t)))),
        h("tbody", {}, Object.entries(eff.approvals).map(([risk, n]) => h("tr", {}, h("td", {}, risk), h("td", {}, h("strong", {}, n)), h("td", { class: "muted" }, shipped.approvals[risk]))))),
      facts([["Change record for", eff.change_record.join(", ") || "no risk class"],
        ["Change window", eff.windows.map((w) => `${w.days.join(", ")} ${w.start}–${w.end} UTC`).join("; ") || "any time"],
        ["Freezes", eff.freeze.map((f) => `${stamp(f.start)} to ${stamp(f.end)}: ${f.reason}`).join("; ") || "none"],
        ["Denied", eff.deny.join(", ") || "nothing"]])),
      h("section", { class: "card scroll" }, h("h2", {}, "Limits"), h("table", {},
        h("thead", {}, h("tr", {}, ["Limit", "Here", "Shipped"].map((t) => h("th", {}, t)))),
        h("tbody", {}, Object.entries(eff.limits).map(([k, v]) => h("tr", {}, h("td", { class: "mono" }, k), h("td", {}, h("strong", {}, v.toLocaleString())),
          h("td", { class: "muted" }, shipped.limits[k].toLocaleString()))))))),
    h("section", { class: "card scroll" }, h("h2", {}, "What each role may use"), h("table", {},
      h("thead", {}, h("tr", {}, ["Role", "Shipped", "Narrowed here to"].map((t) => h("th", {}, t)))),
      h("tbody", {}, ROLES.map((r) => h("tr", {}, h("td", {}, r), h("td", { class: "mono" }, (eff.layers[0][r] || []).join(" ")),
        h("td", { class: "mono" }, eff.layers[1][r] ? eff.layers[1][r].join(" ") || "nothing" : "not narrowed")))))),
    h("form", { class: "card", onsubmit: (e) => {
      e.preventDefault();
      act(e.submitter, () => api("/api/policy/set", { document: doc.value }), "Policy saved.");
    } }, h("h2", {}, "This organisation's policy"),
    h("p", { class: "muted" }, pol.set_by ? `Last set by ${pol.set_by}, ${stamp(pol.set_at)}.` : "Not set: the shipped policy applies."),
    h("p", { class: "muted" }, "YAML with roles, deny, limits, approvals, change_record, change_window and freeze. It can only narrow the shipped policy: fewer capabilities, lower limits, more approvals."),
    field("Policy document", doc), h("div", { class: "actions" }, h("button", { type: "submit" }, "Save policy"))));
}

// ---------- Integrations ----------

async function integrations() {
  const [c, lib, { people }] = await Promise.all([api("/api/connections"), api("/api/library"), api("/api/people")]);
  const owner = h("p", { class: "muted" }, "Set by the installation owner on the command line.");
  const endpoint = (name, e, extra) => h("section", { class: "card" }, h("div", { class: "actions" }, h("h2", {}, name), pill(e ? "ok" : "off", e ? "Connected" : "Not set")),
    e ? facts([["Address", e.url], ["Route", e.own ? "This organisation's own" : "The installation's"], ["Secret", e.secret_set ? "Set" : "Missing"], ...(extra || [])]) : null, owner.cloneNode(true));
  const kind = h("select", {}, ["teams", "slack", "json"].map((k) => h("option", { value: k }, k)));
  const url = h("input", { type: "url", required: true, placeholder: "https://…", maxLength: 2000 });
  const recipe = h("select", { required: true }, lib.recipes.map((r) => h("option", { value: r.name }, r.name)));
  const runAs = h("select", { required: true }, people.map((p) => h("option", { value: p.name, selected: p.name === me.user }, `${p.name} (${p.role})`)));
  const address = h("div", { class: "field", hidden: true });
  return h("div", { class: "talk" },
    head("Integrations", "What this organisation is connected to. Secrets are never shown here, only whether one is set."),
    h("div", { class: "cards" },
      endpoint("Model", c.model, c.model && [["Model", c.model.model || "—"], ["Reviewer", c.model.reviewer || "the same model"]]),
      endpoint("Ansible executor", c.ansible), endpoint("OpenTofu executor", c.tofu),
      h("section", { class: "card" }, h("div", { class: "actions" }, h("h2", {}, "Company directory"), pill(c.directory ? "ok" : "off", c.directory ? "Connected" : "Not set")),
        c.directory && facts([["Issuer", c.directory.issuer], ["Client", c.directory.client_id], ["Username from", c.directory.username_claim]]), owner.cloneNode(true)),
      h("section", { class: "card" }, h("div", { class: "actions" }, h("h2", {}, "ServiceNow"), pill(c.servicenow ? "ok" : "off", c.servicenow ? "Connected" : "Not set")),
        c.servicenow && facts([["Address", c.servicenow], ["Record needed for", c.change_record.join(", ") || "no risk class"]]), owner.cloneNode(true)),
      h("section", { class: "card" }, h("div", { class: "actions" }, h("h2", {}, "SolarWinds"), pill(c.solarwinds ? "ok" : "off", c.solarwinds ? "Connected" : "Not set")),
        c.solarwinds && facts([["Address", c.solarwinds]]), owner.cloneNode(true)),
      h("section", { class: "card" }, h("h2", {}, "MCP servers"), c.mcp.length
        ? facts(c.mcp.map((s) => [s.name, s.tools.join(", ")])) : h("p", { class: "muted" }, "None for this organisation."), owner.cloneNode(true))),
    h("section", { class: "card" }, h("h2", {}, "Notifications"),
      h("p", { class: "muted" }, "Teams, Slack or any JSON receiver is told when a change waits for approval, runs, or is approved alone. The address carries its own secret, so only its host is shown."),
      c.webhooks.length ? h("table", {}, h("tbody", {}, c.webhooks.map((w) => h("tr", {}, h("td", {}, w.kind), h("td", { class: "mono" }, w.host),
        h("td", {}, sure("Remove", "Press again to remove", () => api("/api/webhooks/remove", { id: w.id }), "Webhook removed.")))))) : h("p", {}, "No webhooks yet."),
      h("form", { onsubmit: (e) => {
        e.preventDefault();
        act(e.submitter, () => api("/api/webhooks/add", { kind: kind.value, url: url.value.trim() }), "Webhook added.");
      } }, h("div", { class: "row" }, field("Kind", kind), field("Address", url), h("button", { type: "submit" }, "Add webhook")))),
    h("section", { class: "card" }, h("h2", {}, "Triggers"),
      h("p", { class: "muted" }, "Another system (an alert, a ticket) starts a recipe by sending JSON to a secret address. The recipe runs as the person named, under their role."),
      c.triggers.length ? h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Recipe", "Runs as", "Made by", "Made", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, c.triggers.map((t) => h("tr", {}, h("td", {}, t.recipe), h("td", {}, t.run_as), h("td", {}, t.created_by), h("td", {}, stamp(t.created_at)),
          h("td", {}, sure("Revoke", "Press again to revoke", () => api("/api/triggers/remove", { id: t.id }), "Trigger revoked: its address no longer works."))))))) : h("p", {}, "No triggers yet."),
      lib.recipes.length ? h("form", { onsubmit: async (e) => {
        e.preventDefault();
        const res = await act(e.submitter, () => api("/api/triggers/add", { recipe: recipe.value, run_as: runAs.value }), false);
        if (!res) return;
        const box = h("input", { readOnly: true, value: res.address, "aria-label": "Trigger address" });
        address.replaceChildren(h("p", { class: "alert" }, "Copy this address now. It is shown once; the orchestrator keeps only its hash."),
          h("div", { class: "address" }, box, h("button", { type: "button", class: "secondary", onclick: async (ev) => {
            box.select();
            try { await navigator.clipboard.writeText(res.address); ev.currentTarget.textContent = "Copied"; } catch { say("Select the address and copy it yourself.", true); }
          } }, "Copy")));
        address.hidden = false;
      } }, h("div", { class: "row" }, field("Recipe", recipe), field("Runs as", runAs), h("button", { type: "submit" }, "New trigger")), address)
        : h("p", { class: "muted" }, "Add a recipe first.")),
    h("p", { class: "muted" }, `Last ledger anchor: ${ago(c.last_anchor_at)}.`));
}

start();
