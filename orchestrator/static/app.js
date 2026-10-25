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
  (signedIn ? $("prompt") : $("tenant")).focus();
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
  show();
});

$("kill").addEventListener("click", async () => {
  const stop = !$("kill").dataset.stopped;
  const reason = stop ? window.prompt("Why are you stopping all agent work?") : null;
  if (stop && !reason) return;
  await api("/api/kill-switch", { stop, reason });
  show();
});

$("ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  const button = e.submitter;
  say(prompt, "you");
  $("prompt").value = "";
  button.disabled = true;
  button.textContent = "Working…";
  try {
    const res = await api("/api/ask", { prompt });
    if (res.status === 401) return show();
    say(res.data.answer ?? res.data.error, res.data.state === "DONE" ? "" : "failed");
  } finally {
    button.disabled = false;
    button.textContent = "Ask";
  }
});

show();
