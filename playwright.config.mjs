import { defineConfig } from "@playwright/test";

const baseURL = process.env.DAYFINCH_BROWSER_URL || "http://127.0.0.1:8000";

const engines = ["chromium", "firefox", "webkit"];
const viewports = [
  ["desktop", { width: 1440, height: 1000 }],
  ["mobile", { width: 390, height: 844 }],
];

export default defineConfig({
  testDir: "./browser-tests",
  outputDir: "test-results/artifacts",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ["line"],
    ["json", { outputFile: "test-results/playwright-report.json" }],
  ],
  use: {
    baseURL,
    reducedMotion: "reduce",
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: engines.flatMap((browserName) =>
    viewports.map(([size, viewport]) => ({
      name: `${browserName}-${size}`,
      use: { browserName, viewport },
    })),
  ),
});
