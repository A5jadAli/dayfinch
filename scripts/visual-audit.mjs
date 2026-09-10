import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const baseUrl = (process.env.DAYFINCH_AUDIT_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const email = process.env.DAYFINCH_AUDIT_EMAIL || "";
const password = process.env.DAYFINCH_AUDIT_PASSWORD || "";
const outputDir = resolve(process.env.DAYFINCH_AUDIT_OUTPUT || ".visual-audit");
const chrome = process.env.CHROME_BIN || [
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].find(existsSync);

if (!chrome) throw new Error("Set CHROME_BIN to a Chrome or Chromium executable");
if (!email || !password) {
  throw new Error("Set DAYFINCH_AUDIT_EMAIL and DAYFINCH_AUDIT_PASSWORD");
}

mkdirSync(outputDir, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "dayfinch-visual-"));
const browser = spawn(chrome, [
  "--headless=new",
  "--no-sandbox",
  "--disable-background-networking",
  "--disable-default-apps",
  "--disable-extensions",
  "--disable-sync",
  "--hide-scrollbars",
  "--metrics-recording-only",
  "--no-first-run",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
const browserExited = new Promise(resolveExit => browser.once("exit", resolveExit));

const browserWsUrl = await new Promise((resolveUrl, reject) => {
  const timeout = setTimeout(() => reject(new Error("Chrome DevTools did not start")), 15000);
  browser.stderr.setEncoding("utf8");
  browser.stderr.on("data", chunk => {
    const match = chunk.match(/DevTools listening on (ws:\/\/\S+)/);
    if (match) {
      clearTimeout(timeout);
      resolveUrl(match[1]);
    }
  });
  browser.once("exit", code => reject(new Error(`Chrome exited before DevTools started (${code})`)));
});

const socket = new WebSocket(browserWsUrl);
await new Promise((resolveOpen, reject) => {
  socket.addEventListener("open", resolveOpen, { once: true });
  socket.addEventListener("error", () => reject(new Error("Could not connect to Chrome DevTools")), { once: true });
});

let commandId = 0;
const pending = new Map();
const eventWaiters = new Map();
socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve: resolveCommand, reject, timeout } = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(timeout);
    if (message.error) reject(new Error(message.error.message));
    else resolveCommand(message.result || {});
    return;
  }
  const key = `${message.sessionId || "browser"}:${message.method}`;
  const waiters = eventWaiters.get(key) || [];
  eventWaiters.delete(key);
  waiters.forEach(resolveEvent => resolveEvent(message.params || {}));
});

function send(method, params = {}, sessionId) {
  const id = ++commandId;
  socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  return new Promise((resolveCommand, reject) => {
    const timeout = setTimeout(() => {
      if (!pending.has(id)) return;
      pending.delete(id);
      reject(new Error(`CDP command timed out: ${method}`));
    }, 15000);
    pending.set(id, { resolve: resolveCommand, reject, timeout });
  });
}

function waitFor(method, sessionId, timeoutMs = 15000) {
  const key = `${sessionId || "browser"}:${method}`;
  return new Promise((resolveEvent, reject) => {
    let timeout;
    const resolveAndClear = event => {
      clearTimeout(timeout);
      resolveEvent(event);
    };
    const waiters = eventWaiters.get(key) || [];
    waiters.push(resolveAndClear);
    eventWaiters.set(key, waiters);
    timeout = setTimeout(() => {
      const current = eventWaiters.get(key) || [];
      const index = current.indexOf(resolveAndClear);
      if (index >= 0) current.splice(index, 1);
      if (current.length) eventWaiters.set(key, current);
      else eventWaiters.delete(key);
      reject(new Error(`CDP event timed out: ${method}`));
    }, timeoutMs);
  });
}

const { targetId } = await send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
await send("Page.enable", {}, sessionId);
await send("Runtime.enable", {}, sessionId);

async function navigate(path) {
  const loaded = waitFor("Page.loadEventFired", sessionId);
  await send("Page.navigate", { url: `${baseUrl}${path}` }, sessionId);
  await loaded;
}

async function evaluate(expression, awaitPromise = false) {
  const result = await send("Runtime.evaluate", {
    expression,
    awaitPromise,
    returnByValue: true,
  }, sessionId);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Browser evaluation failed");
  return result.result?.value;
}

await navigate("/login");
const loggedIn = waitFor("Page.loadEventFired", sessionId);
await evaluate(`(() => {
  const form = document.querySelector('form[action="/login"]');
  form.elements.email.value = ${JSON.stringify(email)};
  form.elements.password.value = ${JSON.stringify(password)};
  form.requestSubmit();
})()`);
await loggedIn;
if ((await evaluate("location.pathname")) === "/login") {
  throw new Error("Visual audit login failed; verify the audit credentials");
}

const pages = [
  ["dashboard", "/"],
  ["activity", "/activity"],
  ["time-entries", "/time-entries"],
  ["timesheets", "/timesheets"],
  ["schedules", "/schedules"],
  ["locations", "/locations"],
  ["field", "/field"],
  ["people", "/people"],
  ["financials", "/financials"],
  ["reports", "/reports"],
  ["audit-log", "/reports/audit"],
  ["settings", "/settings"],
  ["member-tracking", "/settings/member-tracking"],
];
const viewports = [
  ["desktop", 1440, 1000, false],
  ["mobile", 390, 844, true],
];
const failures = [];
const report = [];

for (const [viewportName, width, height, mobile] of viewports) {
  await send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile,
  }, sessionId);
  for (const theme of ["light", "dark"]) {
    await evaluate(`localStorage.setItem("df-theme", ${JSON.stringify(theme)})`);
    for (const [name, path] of pages) {
      await navigate(path);
      await evaluate("new Promise(resolve => setTimeout(resolve, 1200))", true);
      const audit = await evaluate(`(() => {
        const visible = node => {
          const style = getComputedStyle(node);
          return style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length > 0;
        };
        const unresolvedIcons = [...document.querySelectorAll('svg use')]
          .map(node => node.getAttribute('href'))
          .filter(href => href?.startsWith('#') && !document.querySelector(href));
        const unlabeledControls = [...document.querySelectorAll('button, a')]
          .filter(visible)
          .filter(node => !node.textContent.trim() && !node.getAttribute('aria-label') && !node.getAttribute('title'))
          .map(node => node.outerHTML.slice(0, 180));
        const unlabeledInputs = [...document.querySelectorAll('input:not([type="hidden"]), select, textarea')]
          .filter(visible)
          .filter(node => !node.closest('label') && !node.getAttribute('aria-label') && !(node.id && document.querySelector('label[for="' + CSS.escape(node.id) + '"]')))
          .map(node => node.outerHTML.slice(0, 180));
        return {
          path: location.pathname,
          title: document.title,
          bodyOverflow: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
          unresolvedIcons,
          unlabeledControls,
          unlabeledInputs,
          theme: document.documentElement.dataset.theme,
        };
      })()`);
      const screenshot = await send("Page.captureScreenshot", {
        format: "png",
        fromSurface: true,
        captureBeyondViewport: false,
      }, sessionId);
      const filename = `${viewportName}-${theme}-${name}.png`;
      writeFileSync(join(outputDir, filename), Buffer.from(screenshot.data, "base64"));
      const issues = [];
      if (audit.path !== path) issues.push(`redirected to ${audit.path}`);
      if (audit.theme !== theme) issues.push(`theme is ${audit.theme}`);
      if (audit.bodyOverflow > 1) issues.push(`body overflows by ${audit.bodyOverflow}px`);
      if (audit.unresolvedIcons.length) issues.push(`${audit.unresolvedIcons.length} unresolved icons`);
      if (audit.unlabeledControls.length) issues.push(`${audit.unlabeledControls.length} unlabeled controls`);
      if (audit.unlabeledInputs.length) issues.push(`${audit.unlabeledInputs.length} unlabeled fields`);
      report.push({ viewport: viewportName, theme, page: name, screenshot: filename, issues, audit });
      if (issues.length) failures.push(`${viewportName}/${theme}/${name}: ${issues.join(", ")}`);
    }
  }
}

writeFileSync(join(outputDir, "report.json"), JSON.stringify(report, null, 2));
socket.close();
browser.kill("SIGTERM");
await Promise.race([
  browserExited,
  new Promise(resolveTimeout => setTimeout(resolveTimeout, 3000)),
]);
rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });

if (failures.length) {
  throw new Error(`Visual audit failed:\n${failures.join("\n")}`);
}
console.log(`Visual audit passed: ${report.length} renders written to ${outputDir}`);
