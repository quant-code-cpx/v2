import { defineConfig } from "@playwright/test";

const browserChannel = (globalThis as { process?: { env?: Record<string, string | undefined> } })
  .process?.env?.PLAYWRIGHT_BROWSER_CHANNEL;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:4173",
    channel: browserChannel,
    trace: "on-first-retry",
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: {
    command: "vp preview --host 127.0.0.1 --port 4173",
    reuseExistingServer: true,
  },
});
