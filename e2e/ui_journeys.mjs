// On-screen journeys: what a person does with their fingers, checked in
// the real UI of the test household (not only through the API).
//
//   cd e2e && node ui_journeys.mjs          → report/ui_journeys.md + shots/ui-*.png
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const H = JSON.parse(fs.readFileSync(path.join(HERE, ".run", "household.json"), "utf8"));
const BASE = H.base_url;
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE) || BASE.endsWith(":8000")) throw new Error("test household only");
const OUT = path.join(HERE, "report");
fs.mkdirSync(path.join(OUT, "shots"), { recursive: true });
const results = [];
const who = (k) => H.people.find((p) => p.key === k);

async function newPage(browser, device) {
  const ctx = await browser.newContext(device === "phone"
    ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, locale: "de-DE", timezoneId: "Europe/Berlin" }
    : { viewport: { width: 1440, height: 900 }, locale: "de-DE", timezoneId: "Europe/Berlin" });
  // Only the test household; the made-up sites in test mails answer
  // with a stub page so a link that opens can be seen opening.
  await ctx.route("**/*", (r) => {
    const u = new URL(r.request().url());
    if (u.origin === BASE) return r.continue();
    if (u.hostname.endsWith(".example.test")) return r.fulfill({ status: 200, contentType: "text/html", body: "<p>stub</p>" });
    return r.abort();
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e.message)));
  page.on("response", (r) => { if (r.url().startsWith(BASE) && r.status() >= 500) errors.push(`${r.status()} ${new URL(r.url()).pathname}`); });
  return { ctx, page, errors };
}

async function login(page, key) {
  await page.goto(BASE + "/r/home");
  await page.fill("input[type=email]", who(key).email);
  await page.fill("input[type=password]", H.password);
  await page.click("button[type=submit]");
  await page.waitForLoadState("networkidle");
}

async function journey(name, device, fn, browser) {
  const { ctx, page, errors } = await newPage(browser, device);
  let ok = false, detail = "";
  try {
    const out = await fn(page);
    ok = out === true || (Array.isArray(out) && out[0]);
    detail = Array.isArray(out) ? out[1] : "";
  } catch (e) {
    detail = String(e.message).split("\n")[0];
  }
  if (errors.length) detail += ` | errors: ${[...new Set(errors)].slice(0, 3).join("; ")}`;
  await page.screenshot({ path: path.join(OUT, "shots", `ui-${name.replace(/[^a-z0-9]+/gi, "-").slice(0, 60)}.png`) }).catch(() => {});
  results.push({ name, device, ok, detail });
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name} (${device}) ${detail}`);
  await ctx.close();
}

const browser = await chromium.launch();

await journey("wrong password shows a message and stays on the login", "phone", async (page) => {
  await page.goto(BASE + "/r/home");
  await page.fill("input[type=email]", who("ben").email);
  await page.fill("input[type=password]", "falsch-falsch");
  await page.click("button[type=submit]");
  await page.waitForTimeout(1500);
  const text = await page.locator("body").innerText();
  return [(await page.locator("input[type=password]").count()) > 0 && /invalid|falsch|wrong|incorrect|ungültig|fehl/i.test(text),
          text.replace(/\s+/g, " ").slice(0, 160)];
}, browser);

await journey("Ben signs in on his phone and lands on Home", "phone", async (page) => {
  await login(page, "ben");
  const text = await page.locator("body").innerText();
  return [/Ben/.test(text) && (await page.locator("input[type=password]").count()) === 0, text.replace(/\s+/g, " ").slice(0, 120)];
}, browser);

await journey("Anna adds a task by typing it into Tasks", "desktop", async (page) => {
  await login(page, "anna");
  await page.goto(BASE + "/r/tasks");
  await page.waitForLoadState("networkidle");
  await page.fill("input[placeholder^='Add a task'], textarea[placeholder^='Add a task']", "Fenster putzen");
  await page.getByRole("button", { name: /^Add/ }).first().click();
  await page.waitForTimeout(1500);
  const api = await page.request.get(BASE + "/api/tasks?role=platform_admin");
  const found = (await api.json()).some((t) => t.title === "Fenster putzen");
  return [found, found ? "task saved" : "not in /api/tasks"];
}, browser);

await journey("Clara ticks her chore on her phone (Tasks)", "phone", async (page) => {
  await login(page, "clara");
  await page.goto(BASE + "/r/tasks");
  await page.waitForLoadState("networkidle");
  const all = page.getByRole("button", { name: /^All/ });
  if (await all.count()) await all.first().click();
  const row = page.locator("text=Buch zurückgeben").first();
  await row.waitFor({ timeout: 5000 });
  const card = row.locator("xpath=ancestor::*[.//input[@type='checkbox'] or .//button[contains(@aria-label,'done') or contains(@aria-label,'erledigt') or contains(@aria-label,'Mark')]][1]");
  const box = card.locator("input[type=checkbox], button[aria-label*='done' i], button[aria-label*='erledigt' i], button[aria-label*='Mark' i]").first();
  await box.click();
  await page.waitForTimeout(1500);
  const t = (await (await page.request.get(BASE + "/api/tasks?role=restricted")).json()).find((x) => x.title === "Buch zurückgeben");
  return [!!t?.done, `done=${t?.done}`];
}, browser);

if (H.real_llm) await journey("Anna asks the chat on her phone and gets an answer", "phone", async (page) => {
  await login(page, "anna");
  await page.goto(BASE + "/r/chat");
  await page.waitForLoadState("networkidle");
  const box = page.locator("textarea[placeholder^='Ask me'], input[placeholder^='Ask me']").first();
  await box.fill("Welche Termine habe ich morgen?");
  await box.press("Enter");
  await page.waitForTimeout(12000);
  const text = await page.locator("body").innerText();
  const answer = text.split("Welche Termine habe ich morgen?").pop() || "";
  // Four appointments tomorrow (seeded); "keine Termine" is the wrong answer.
  return [/Elternabend|Kundentermin|vier|four|\b4\b/i.test(answer) && !/keine Termine|no (appointments|events)/i.test(answer),
          (text.split("Welche Termine habe ich morgen?").pop() || "").replace(/\s+/g, " ").slice(0, 200)];
}, browser);

async function openMail(page, subject) {
  await page.goto(BASE + "/r/email");
  await page.waitForLoadState("networkidle");
  await page.getByText(subject, { exact: true }).first().click();
  await page.waitForTimeout(1200);
}

await journey("a mail's activation button opens its page (target=_self, like Trustpilot)", "phone", async (page) => {
  await login(page, "anna");
  await openMail(page, "Aktivieren Sie Ihr Konto");
  const [popup] = await Promise.all([
    page.context().waitForEvent("page", { timeout: 8000 }),
    page.frameLocator("iframe[title='email body']").getByText("Konto aktivieren").click(),
  ]);
  await popup.waitForLoadState("domcontentloaded").catch(() => {});
  const url = popup.url();
  return [url.includes("bewertungen.example.test/activate"), `new tab: ${url}`];
}, browser);

await journey("a link in a plain-text mail opens its page", "desktop", async (page) => {
  await login(page, "anna");
  await openMail(page, "Anmeldung Sommerfest");
  const [popup] = await Promise.all([
    page.context().waitForEvent("page", { timeout: 8000 }),
    page.getByRole("link", { name: /verein\.example\.test\/sommerfest/ }).click(),
  ]);
  await popup.waitForLoadState("domcontentloaded").catch(() => {});
  const url = popup.url();
  return [url.startsWith("https://verein.example.test/sommerfest?id=e2e-text") && !url.endsWith("."), `new tab: ${url}`];
}, browser);

await journey("Clara opens the family board on the wall page", "desktop", async (page) => {
  await login(page, "clara");
  await page.goto(BASE + "/r/board");
  await page.waitForLoadState("networkidle");
  const text = await page.locator("body").innerText();
  return [/Clara|Mich anzeigen/.test(text), text.replace(/\s+/g, " ").slice(0, 200)];
}, browser);

await browser.close();
const lines = ["# On-screen journeys", "", "| | Journey | Device | Detail |", "| --- | --- | --- | --- |",
  ...results.map((r) => `| ${r.ok ? "✅" : "❌"} | ${r.name} | ${r.device} | ${r.detail.replace(/\|/g, "/")} |`)];
fs.writeFileSync(path.join(OUT, "ui_journeys.md"), lines.join("\n") + "\n");
fs.writeFileSync(path.join(OUT, "ui_journeys.json"), JSON.stringify(results, null, 2));
process.exit(results.every((r) => r.ok) ? 0 : 1);
