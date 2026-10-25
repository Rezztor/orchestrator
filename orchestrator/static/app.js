const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return { status: res.status, data: await res.json() };
}

async function show() {
  const me = await api("/api/me");
  const signedIn = me.status === 200;
  $("sign-in").hidden = signedIn;
  $("chat").hidden = !signedIn;
  $("sign-out").hidden = !signedIn;
  $("who").textContent = signedIn ? `${me.data.user} (${me.data.role})` : "";
  const stopped = signedIn && me.data.stopped;
  $("stopped").hidden = !stopped;
  $("stopped").textContent = stopped ? `Agent work is stopped: ${stopped}` : "";
  $("kill").hidden = !(signedIn && me.data.role === "tenant-admin");
  $("kill").textContent = stopped ? "Resume agent work" : "Stop all agent work";
  $("kill").dataset.stopped = stopped ? "1" : "";
  current = signedIn ? me.data : null;
  if (signedIn) loadChanges();
  if (signedIn) loadLibrary();
  (signedIn ? $("prompt") : $("tenant")).focus();
}

const APPROVERS = ["approver", "tenant-admin"];
let current = null; // the signed-in user, from /api/me
let polling = null;

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function button(label, onClick, cls) {
  const b = el("button", label, cls);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

async function act(path, body) {
  const res = await api(path, body);
  $("change-error").textContent = res.status < 300 ? "" : res.data.error;
  loadChanges();
}

function card(c) {
  const li = el("li");
  li.append(el("h3", `${c.capability} on ${(c.args.devices || []).join(", ")}`));
  const decided = c.decisions.filter((d) => d.decision === "approve").length;
  const expires = new Date(c.expires_at).toLocaleString();
  li.append(el("p", `${c.state} · risk ${c.risk} · approved by ${decided} of ${c.approvals_needed} · ` +
    `asked by ${c.requested_by} · ${c.plan.rollback ? "can be rolled back" : "no rollback"} · expires ${expires}`, "muted"));
  if (c.review) li.append(el("p", `Reviewer: ${c.review}`, "review"));
  for (const d of c.decisions) li.append(el("p", `${d.user}: ${d.decision}${d.reason ? ` (${d.reason})` : ""}`));
  const plan = el("details");
  plan.append(el("summary", "Plan (dry run)"), el("pre", JSON.stringify(c.args, null, 2)));
  for (const [host, h] of Object.entries(c.plan.hosts)) {
    for (const t of h.tasks.filter((t) => t.changed)) {
      plan.append(el("pre", `${host} · ${t.task}\n${t.diff || "(changes, no diff)"}`));
    }
  }
  li.append(plan);
  if (c.result) {
    const result = el("details");
    result.append(el("summary", `Result: ${c.result.outcome}`), el("pre", JSON.stringify(c.result, null, 2)));
    li.append(result);
  }
  const me = current;
  const actions = el("p", undefined, "actions");
  const approver = APPROVERS.includes(me.role);
  if (c.state === "PLANNED" && approver && c.requested_by !== me.user && !c.decisions.some((d) => d.user === me.user)) {
    const decide = (approve, reason) => act("/api/changes/decide", { change: c.id, digest: c.digest, approve, reason });
    actions.append(button("Approve this plan", () => decide(true)),
      button("Reject", () => {
        const reason = window.prompt("Why are you rejecting this change?");
        if (reason) decide(false, reason);
      }, "secondary"));
  }
  if (c.state === "APPROVED" && (approver || c.requested_by === me.user)) {
    actions.append(button("Run the change", () => act("/api/changes/run", { change: c.id })));
  }
  if (actions.childElementCount) li.append(actions);
  li.className = { VERIFIED: "ok", FAILED: "failed", ROLLED_BACK: "failed", SUPERSEDED: "failed" }[c.state] || "";
  return li;
}

async function loadChanges() {
  const res = await api("/api/changes");
  if (res.status !== 200) return;
  $("changes").replaceChildren(...res.data.changes.map(card));
  if (!res.data.changes.length) $("changes").append(el("li", "No changes proposed yet.", "muted"));
  clearTimeout(polling);
  if (res.data.changes.some((c) => c.state === "EXECUTING")) polling = setTimeout(loadChanges, 3000);
}

function say(text, cls) {
  const li = document.createElement("li");
  li.textContent = text;
  if (cls) li.className = cls;
  $("log").append(li);
  li.scrollIntoView({ block: "end" });
}

$("sign-in").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await api("/api/sign-in", { tenant: $("tenant").value, user: $("user").value, password: $("password").value });
  $("password").value = "";
  $("sign-in-error").textContent = res.status === 200 ? "" : res.data.error;
  if (res.status === 200) show();
});

$("sign-out").addEventListener("click", async () => {
  await api("/api/sign-out", {});
  $("log").replaceChildren();
  $("changes").replaceChildren();
  $("skills").replaceChildren();
  show();
});

$("kill").addEventListener("click", async () => {
  const stop = !$("kill").dataset.stopped;
  const reason = stop ? window.prompt("Why are you stopping all agent work?") : null;
  if (stop && !reason) return;
  await api("/api/kill-switch", { stop, reason });
  show();
});

async function converse(button, label, path, body) {
  button.disabled = true;
  button.textContent = "Working…";
  try {
    const res = await api(path, body);
    if (res.status === 401) return show();
    say(res.data.answer ?? res.data.error, res.data.state === "DONE" ? "" : "failed");
    loadChanges();
    loadLibrary();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

$("ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  say(prompt, "you");
  $("prompt").value = "";
  await converse(e.submitter, "Ask", "/api/ask", { prompt });
});

let recipes = [];

function showRecipe() {
  const r = recipes.find((x) => x.name === $("recipe-name").value);
  $("recipe-description").textContent = r ? r.description : "";
  $("recipe-values").replaceChildren(...(r ? r.params : []).flatMap((p) => {
    const label = el("label", p.replaceAll("_", " "));
    const input = el("input");
    label.htmlFor = input.id = `recipe-${p}`;
    input.name = p;
    input.required = true;
    input.maxLength = 200;
    return [label, input];
  }));
}

$("recipe-name").addEventListener("change", showRecipe);
$("recipe").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("recipe-name").value;
  const values = Object.fromEntries([...$("recipe-values").querySelectorAll("input")].map((i) => [i.name, i.value.trim()]));
  say(`Recipe ${name}: ${Object.entries(values).map(([k, v]) => `${k} = ${v}`).join(", ") || "no values"}`, "you");
  await converse(e.submitter, "Run recipe", "/api/recipes/run", { name, values });
});

async function decideSkill(name, approve) {
  const res = await api("/api/skills/decide", { name, approve });
  $("skill-error").textContent = res.status === 200 ? "" : res.data.error;
  loadLibrary();
}

async function loadLibrary() {
  const res = await api("/api/library");
  if (res.status !== 200) return;
  const chosen = $("recipe-name").value;
  recipes = res.data.recipes;
  $("recipe").hidden = !recipes.length;
  $("no-recipes").hidden = !!recipes.length;
  $("recipe-name").replaceChildren(...recipes.map((r) => el("option", r.name)));
  if (recipes.some((r) => r.name === chosen)) $("recipe-name").value = chosen;
  showRecipe();
  $("shipped-skills").textContent = `Shipped with the product: ${res.data.shipped_skills.join(", ")}.`;
  $("skills").replaceChildren(...res.data.skills.map((s) => {
    const li = el("li", undefined, s.state === "proposed" ? "pending" : "");
    li.append(el("h3", s.name), el("p", `${s.state} · ${s.source === "induced" ? "proposed by the agent" : "added by " + s.proposed_by}` +
      ` · uses ${s.capabilities.join(", ") || "no tools"}`, "muted"), el("p", s.description));
    const body = el("details");
    body.append(el("summary", "Procedure"), el("pre", s.body));
    li.append(body);
    if (current.role === "tenant-admin" && s.state !== "retired") {
      const actions = el("p", undefined, "actions");
      if (s.state === "proposed") actions.append(button("Approve skill", () => decideSkill(s.name, true)));
      actions.append(button(s.state === "proposed" ? "Reject" : "Retire", () => decideSkill(s.name, false), "secondary"));
      li.append(actions);
    }
    return li;
  }));
}

show();
