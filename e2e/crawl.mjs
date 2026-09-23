// The crawler: every page, every clickable thing, as every kind of family
// member, on a desktop and on a phone — against the test household only.
//
//   venv/bin/python e2e/household.py up
//   cd e2e && npm run crawl            (or: node crawl.mjs anna phone /r/tasks)
//
// It does not judge whether something is *right*; it reports what is
// visibly broken: a page that crashes, a server error, a blank screen, a
// console error, a phone page wider than the phone. The report lands in
// e2e/report/ (SUMMARY.md to read, crawl.json for tools, screenshots).

import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOUSEHOLD = JSON.parse(fs.readFileSync(path.join(HERE, ".run", "household.json"), "utf8"));
const BASE = HOUSEHOLD.base_url;
const REPORT = path.join(HERE, "report");

if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE) || BASE.endsWith(":8000")) {
  throw new Error(`refusing to crawl ${BASE}: only the test household on 127.0.0.1 is allowed`);
}

const ROUTES = [
  "/r/home", "/r/chat", "/r/tasks", "/r/calendar", "/r/briefing", "/r/email", "/r/whatsapp",
  "/r/contacts", "/r/documents", "/r/photos", "/r/recordings", "/r/compose", "/r/write",
  "/r/board", "/r/ambient", "/r/settings",
];
const VIEWPORTS = {
  desktop: { viewport: { width: 1440, height: 900 } },
  phone: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
};
// Anna runs the box, Ben is the other parent, Clara a child.
const PEOPLE = ["anna", "ben", "clara"];
const MAX_CLICKS_PER_PAGE = Number(process.env.CRAWL_MAX_CLICKS || 60);

// Never clicked: they would end the session or throw the seeded family
// away mid-run. Deleting and sending get their own scripted tests.
const SKIP = /abmelden|log ?out|sign ?out|lösch|delete|entfern|remove|zurücksetz|reset|wipe|uninstall|deinstall|restart|neu ?start|notfall|emergency|revoke|widerruf|disconnect|trennen|backup|rebuild|neu aufbauen|alle leeren|clear all|pair|koppeln|senden|send|verschick|abschließen|finalis|festschreib|demo/i;

const CLICKABLE = [
  "button", "[role=button]", "[role=tab]", "[role=switch]", "[role=checkbox]", "[role=menuitem]",
  "input[type=checkbox]", "input[type=radio]", "summary", "a[href^='/r/']", "select",
].join(",");

// ─── one page ───────────────────────────────────────────────────────

function watch(page, sink) {
  const where = () => sink.context;
  page.on("pageerror", (e) => sink.add("crash", String(e.message || e).slice(0, 400), where()));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    if (/\[ErrorBoundary\]/.test(text)) sink.add("crash", text.slice(0, 400), where());
    else if (!/Failed to load resource/.test(text)) sink.add("console", text.slice(0, 300), where());
  });
  page.on("response", (r) => {
    const url = r.url();
    if (!url.startsWith(BASE)) return;
    const u = new URL(url);
    if (r.status() >= 500) sink.add("server", `${r.status()} ${r.request().method()} ${u.pathname}`, where());
    else if (r.status() >= 400 && r.status() !== 401 && u.pathname.startsWith("/api/"))
      sink.add("client", `${r.status()} ${r.request().method()} ${u.pathname}`, where());
  });
  page.on("dialog", (d) => d.dismiss().catch(() => {}));
}

async function looks(page, sink, viewport) {
  const facts = await page.evaluate(() => ({
    text: (document.body?.innerText || "").trim().length,
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  })).catch(() => null);
  if (!facts) return;
  if (facts.text < 15) sink.add("blank", `the page shows almost nothing (${facts.text} characters)`, sink.context);
  if (viewport === "phone" && facts.scroll > facts.inner + 2)
    sink.add("overflow", `page is ${facts.scroll}px wide on a ${facts.inner}px phone`, sink.context);
}

async function candidates(page) {
  return page.$$eval(CLICKABLE, (els) => els.map((el, i) => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const label = (el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText
      || el.getAttribute("name") || el.getAttribute("placeholder") || "").replace(/\s+/g, " ").trim().slice(0, 60);
    return {
      i, label, tag: el.tagName.toLowerCase(),
      visible: r.width > 0 && r.height > 0 && style.visibility !== "hidden" && style.display !== "none",
      disabled: el.disabled || el.getAttribute("aria-disabled") === "true",
      external: el.tagName === "A" && (el.target === "_blank"),
      file: el.tagName === "INPUT" && el.type === "file",
    };
  }));
}

async function crawlRoute(page, sink, person, viewport, route) {
  sink.context = { person, viewport, route, action: "open" };
  try {
    await page.goto(BASE + route, { waitUntil: "networkidle", timeout: 20000 });
  } catch (e) {
    sink.add("server", `page did not settle: ${String(e.message).split("\n")[0]}`, sink.context);
  }
  await page.waitForTimeout(400);
  await looks(page, sink, viewport);
  const shot = path.join(REPORT, "shots", `${person}-${viewport}-${route.replaceAll("/", "_")}.png`);
  await page.screenshot({ path: shot, fullPage: false }).catch(() => {});

  const tried = new Set();
  let clicks = 0;
  for (let round = 0; round < MAX_CLICKS_PER_PAGE * 2 && clicks < MAX_CLICKS_PER_PAGE; round++) {
    const list = (await candidates(page).catch(() => []))
      .filter((c) => c.visible && !c.disabled && !c.external && !c.file && !SKIP.test(c.label));
    const next = list.find((c) => !tried.has(`${c.tag}|${c.label}|${c.label ? "" : c.i}`));
    if (!next) break;
    const key = `${next.tag}|${next.label}|${next.label ? "" : next.i}`;
    tried.add(key);
    clicks++;
    sink.context = { person, viewport, route, action: `click ${next.tag} "${next.label || "#" + next.i}"` };
    const handle = (await page.$$(CLICKABLE))[next.i];
    if (!handle) continue;
    try {
      if (next.tag === "select") {
        const values = await handle.$$eval("option", (os) => os.map((o) => o.value));
        if (values.length > 1) await handle.selectOption(values[values.length - 1]);
      } else {
        await handle.click({ timeout: 2000 });
      }
    } catch {
      continue;        // covered by an overlay or gone: not an error in itself
    }
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(250);
    await looks(page, sink, viewport);
    const here = new URL(page.url());
    if (here.origin !== BASE || here.pathname !== route) {
      await page.goto(BASE + route, { waitUntil: "networkidle", timeout: 20000 }).catch(() => {});
    } else {
      await page.keyboard.press("Escape").catch(() => {});
      await page.keyboard.press("Escape").catch(() => {});
    }
  }
  return clicks;
}

// ─── the run ────────────────────────────────────────────────────────

class Sink {
  constructor() { this.findings = []; this.context = {}; }
  add(kind, message, context) { this.findings.push({ kind, message, ...context }); }
}

async function crawlAs(browser, person, viewport, routes) {
  const who = HOUSEHOLD.people.find((p) => p.key === person);
  const context = await browser.newContext({ ...VIEWPORTS[viewport], baseURL: BASE, locale: "de-DE",
                                             timezoneId: "Europe/Berlin" });
  // The browser talks to the test household and nothing else: the
  // photo app would otherwise frame the real Immich on this machine.
  await context.route("**/*", (r) => (new URL(r.request().url()).origin === BASE ? r.continue() : r.abort()));
  const login = await context.request.post("/api/auth/login", { data: { email: who.email, password: HOUSEHOLD.password } });
  if (!login.ok()) throw new Error(`login ${person} failed: ${login.status()}`);
  const page = await context.newPage();
  const sink = new Sink();
  watch(page, sink);
  let clicks = 0;
  for (const route of routes) clicks += await crawlRoute(page, sink, person, viewport, route);
  await context.close();
  return { person, viewport, routes, clicks, findings: sink.findings };
}

const LABEL = {
  crash: "Page crashed (error screen or script error)",
  server: "Server error (5xx) or page did not load",
  blank: "Blank page",
  overflow: "Phone: page wider than the screen",
  console: "Error in the browser console",
  client: "Refused or missing API call (4xx)",
};

function summary(results, seconds) {
  const all = results.flatMap((r) => r.findings);
  const clicks = results.reduce((n, r) => n + r.clicks, 0);
  const lines = [
    `# Crawl of the test household`,
    ``,
    `${new Date().toLocaleString("de-DE")} · ${results.length} runs (person × device) · ` +
      `${new Set(results.flatMap((r) => r.routes)).size} pages · ${clicks} clicks · ${Math.round(seconds)} s`,
    ``,
  ];
  for (const kind of Object.keys(LABEL)) {
    const hits = all.filter((f) => f.kind === kind);
    lines.push(`## ${LABEL[kind]}: ${hits.length}`);
    // One line per distinct message and page; who and where it showed.
    const groups = new Map();
    for (const f of hits) {
      const key = `${f.route} · ${f.message}`;
      if (!groups.has(key)) groups.set(key, { f, who: new Set(), actions: new Set() });
      const g = groups.get(key);
      g.who.add(`${f.person}/${f.viewport}`);
      g.actions.add(f.action);
    }
    for (const { f, who, actions } of groups.values()) {
      const acts = [...actions].slice(0, 3).join("; ") + (actions.size > 3 ? ` (+${actions.size - 3})` : "");
      lines.push(`- **${f.route}** — ${f.message}  \n  _${[...who].join(", ")}_ · after: ${acts}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

async function main() {
  const [onlyPerson, onlyViewport, onlyRoute] = process.argv.slice(2);
  fs.rmSync(REPORT, { recursive: true, force: true });
  fs.mkdirSync(path.join(REPORT, "shots"), { recursive: true });
  const started = Date.now();
  const browser = await chromium.launch({
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
  });
  const runs = [];
  for (const person of PEOPLE) for (const viewport of Object.keys(VIEWPORTS)) {
    if (onlyPerson && onlyPerson !== person) continue;
    if (onlyViewport && onlyViewport !== viewport) continue;
    runs.push(crawlAs(browser, person, viewport, onlyRoute ? [onlyRoute] : ROUTES));
  }
  const results = await Promise.all(runs);
  await browser.close();
  const seconds = (Date.now() - started) / 1000;
  fs.writeFileSync(path.join(REPORT, "crawl.json"), JSON.stringify(results, null, 2));
  const text = summary(results, seconds);
  fs.writeFileSync(path.join(REPORT, "SUMMARY.md"), text);
  console.log(text);
  const bad = results.flatMap((r) => r.findings).filter((f) => ["crash", "server", "blank"].includes(f.kind));
  process.exit(bad.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
