import { randomUUID } from "node:crypto";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const email = process.env.DAYFINCH_BROWSER_EMAIL;
const password = process.env.DAYFINCH_BROWSER_PASSWORD;

async function signIn(page) {
  await page.goto("/login");
  await page.getByLabel("Work email").fill(email);
  await page.getByLabel("Password").fill(password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.endsWith("/login")),
    page.getByRole("button", { name: "Sign in securely" }).click(),
  ]);
}

async function auditPage(page, testInfo, label) {
  await expect(page.locator("body")).toBeVisible();
  await page.evaluate(() => document.fonts?.ready);

  const overflow = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(
    overflow.scrollWidth,
    `${label} has horizontal page overflow: ${JSON.stringify(overflow)}`,
  ).toBeLessThanOrEqual(overflow.clientWidth + 1);

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const blocking = results.violations.filter((item) =>
    ["critical", "serious"].includes(item.impact),
  );
  const advisory = results.violations.filter((item) =>
    ["moderate", "minor"].includes(item.impact),
  );

  await testInfo.attach(`${label}-axe.json`, {
    body: JSON.stringify({ blocking, advisory }, null, 2),
    contentType: "application/json",
  });
  if (advisory.length) {
    console.log(
      `AXE_ADVISORY ${testInfo.project.name} ${label} ${JSON.stringify(
        advisory.map(({ id, impact, nodes }) => ({
          id,
          impact,
          occurrences: nodes.length,
        })),
      )}`,
    );
  }
  expect(blocking, `${label} has critical/serious axe violations`).toEqual([]);
}

test.describe("Dayfinch browser acceptance", () => {
  test.describe.configure({ mode: "serial" });

  let projectPath;

  test.beforeAll(() => {
    if (!email || !password) {
      throw new Error(
        "Set DAYFINCH_BROWSER_EMAIL and DAYFINCH_BROWSER_PASSWORD for a disposable local admin.",
      );
    }
  });

  test("login", async ({ page }, testInfo) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
    await auditPage(page, testInfo, "login");
  });

  test("dashboard and project creation", async ({ page }, testInfo) => {
    await signIn(page);
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await auditPage(page, testInfo, "dashboard");

    const projectName = `Browser ${testInfo.project.name} ${randomUUID().slice(0, 8)}`;
    const form = page.locator('form[action="/projects"]');
    await form.locator('input[name="name"]').fill(projectName);
    await form.locator('input[name="description"]').fill("Playwright acceptance project");
    const team = form.locator('select[name="team_id"]');
    if (await team.count()) {
      await team.selectOption({ index: 1 });
    }
    await Promise.all([
      page.waitForURL(/\/projects\/[0-9a-f-]+$/),
      form.getByRole("button", { name: "Create project" }).click(),
    ]);
    projectPath = new URL(page.url()).pathname;
  });

  test("project enrollment form", async ({ page }, testInfo) => {
    await signIn(page);
    await page.goto(projectPath);
    await expect(page.getByRole("heading", { name: /^Browser / })).toBeVisible();
    await expect(page.locator('form[action="/devices"]')).toBeVisible();
    await auditPage(page, testInfo, "project");

    const form = page.locator('form[action="/devices"]');
    await form.locator('input[name="name"]').fill(`Playwright ${testInfo.project.name}`);
    await form.locator('select[name="tracker_kind"]').selectOption("desktop");
    await Promise.all([
      page.waitForURL(/\/devices$/),
      form.getByRole("button", { name: "Create enrollment" }).click(),
    ]);
    await expect(page.locator("#agentConfig")).toContainText("device_token");
  });

  for (const [label, path, heading] of [
    ["timesheets", "/timesheets", "Timesheets"],
    ["reports", "/reports", "Reports & exports"],
    ["screenshots", "/activity?tab=screenshots", "Activity"],
    ["settings", "/settings", "Settings"],
  ]) {
    test(label, async ({ page }, testInfo) => {
      await signIn(page);
      await page.goto(path);
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
      if (label === "screenshots") {
        await expect(page.getByRole("link", { name: "Screenshots" })).toHaveClass(/active/);
      }
      await auditPage(page, testInfo, label);
    });
  }
});
