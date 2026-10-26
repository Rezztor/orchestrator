// The page in a real browser against stand_in.py: every screen at desktop and phone width, the admin actions and a
// change approved and run. Prints "no problems" or what went wrong; screenshots go to uat/browser/shots/.
// UAT_PORT (default 8099) must match stand_in.py's.
const { chromium } = require("playwright");
const BASE = `http://localhost:${process.env.UAT_PORT || 8099}`;
const crypto = require("crypto");
const OUT = __dirname + "/shots/";
require("fs").mkdirSync(OUT, { recursive: true });
function totp(secret) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of secret.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g).map((b) => parseInt(b, 2)));
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const mac = crypto.createHmac("sha1", key).update(buf).digest();
  const o = mac[19] & 15;
  return String(((mac.readUInt32BE(o) & 0x7fffffff) % 1e6)).padStart(6, "0");
}
const problems = [];
async function signIn(page, user, password) {
  await page.goto(BASE + "/");
  await page.getByLabel("Organisation").fill("acme");
  await page.getByLabel("Username").fill(user);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByLabel("6-digit code").waitFor();
  const secret = await page.locator(".secret").textContent();
  await page.screenshot({ path: OUT + `mfa-${user}.png` });
  await page.getByLabel("6-digit code").fill(totp(secret));
  await page.getByRole("button", { name: "Verify and continue" }).click();
  await page.locator(".side").waitFor();
}
(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("console", (m) => { if (["error", "warning"].includes(m.type()) && !m.text().includes("401")) problems.push(`console: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  await page.goto(BASE + "/");
  await page.getByLabel("Organisation").waitFor();
  await page.screenshot({ path: OUT + "sign-in.png" });
  await signIn(page, "alice", "correct horse battery");

  // Ask: a read, then a change
  await page.getByLabel("Ask the orchestrator").fill("which devices are in colombo?");
  await page.keyboard.press("Control+Enter");
  await page.locator(".answer table").waitFor();
  if (await page.locator(".answer b").count()) problems.push("markdown let HTML through");
  await page.getByLabel("Ask the orchestrator").fill("set the login banner on core-sw1");
  await page.getByRole("button", { name: "Ask" }).click();
  await page.locator(".answer .plan").waitFor();
  await page.locator(".steps summary").last().click();
  await page.locator(".steps ol").last().waitFor();
  await page.screenshot({ path: OUT + "ask.png", fullPage: true });

  for (const screen of ["overview", "changes", "activity", "recipes", "devices", "skills", "people", "integrations"]) {
    await page.goto(`${BASE}/#/${screen}`);
    await page.locator("main h1").waitFor();
    await page.waitForTimeout(300);
    if (await page.locator("main > .empty").count() && !(await page.locator("main h1").count())) problems.push(`${screen}: error`);
    await page.screenshot({ path: OUT + `${screen}.png`, fullPage: true });
  }
  // admin actions
  await page.goto(BASE + "/#/devices");
  await page.getByLabel("Name").fill("dist-sw9"); await page.getByLabel("Address").fill("10.9.9.9"); await page.getByLabel("Platform").fill("nxos");
  await page.getByRole("button", { name: "Add device" }).click();
  await page.locator("td", { hasText: "dist-sw9" }).waitFor();
  await page.getByLabel("core-sw1 is critical").check();
  await page.locator(".flash", { hasText: "core-sw1 saved" }).waitFor();
  await page.goto(BASE + "/#/integrations");
  await page.getByRole("button", { name: "New trigger" }).click();
  await page.getByLabel("Trigger address").waitFor();
  const address = await page.getByLabel("Trigger address").inputValue();
  if (!address.startsWith(BASE + "/hooks/")) problems.push(`trigger address ${address}`);
  await page.getByRole("cell", { name: "health" }).first().waitFor({ timeout: 3000 }).catch(() => problems.push("new trigger not listed"));
  if (await page.getByText("No triggers yet.").count()) problems.push("still says no triggers");
  await page.screenshot({ path: OUT + "integrations-trigger.png", fullPage: true });
  await page.goto(BASE + "/#/people");
  await page.getByLabel("Policy document").fill("approvals: {medium: 1}\nchange_window: {days: [sat, sun], start: \"22:00\", end: \"04:00\"}\n");
  await page.getByRole("button", { name: "Save policy" }).click();
  await page.locator(".flash", { hasText: "Policy saved" }).waitFor();
  await page.locator("dd", { hasText: "sat, sun 22:00–04:00 UTC" }).waitFor();
  await page.getByLabel("Policy document").fill("approvals: {medium: 1}\n");
  await page.getByRole("button", { name: "Save policy" }).click();
  await page.locator("dd", { hasText: "any time" }).waitFor();
  // sign out; bob approves and runs on a phone-sized screen
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByLabel("Organisation").waitFor();
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const p2 = await phone.newPage();
  p2.on("console", (m) => { if (["error", "warning"].includes(m.type()) && !m.text().includes("401")) problems.push(`phone console: ${m.text()}`); });
  p2.on("pageerror", (e) => problems.push(`phone pageerror: ${e.message}`));
  await signIn(p2, "bob", "bob password 123");
  if (/(^|\n)0(\n|$)/.test(await p2.locator("nav").innerText())) problems.push("stray 0 in the sidebar");
  await p2.goto(BASE + "/#/changes");
  await p2.getByRole("button", { name: "Approve this plan" }).waitFor();
  await p2.screenshot({ path: OUT + "phone-change.png", fullPage: true });
  await p2.getByRole("button", { name: "Reject" }).click();
  await p2.locator(".flash.bad", { hasText: "Say why" }).waitFor();
  await p2.getByRole("button", { name: "Approve this plan" }).click();
  await p2.getByRole("button", { name: "Run the change" }).waitFor();
  await p2.getByRole("button", { name: "Run the change" }).click();
  await p2.locator(".pill", { hasText: "VERIFIED" }).first().waitFor({ timeout: 15000 });
  await p2.screenshot({ path: OUT + "phone-verified.png", fullPage: true });
  await p2.goto(BASE + "/#/overview");
  await p2.locator("main h1").waitFor();
  await p2.screenshot({ path: OUT + "phone-overview.png", fullPage: true });
  const wide = await p2.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  if (wide) problems.push("phone: page scrolls sideways");
  await browser.close();
  console.log(problems.length ? problems.join("\n") : "no problems");
  process.exitCode = problems.length ? 1 : 0;
})().catch((e) => { console.log("FAILED", e.message, "\n", problems.join("\n")); process.exit(1); });
