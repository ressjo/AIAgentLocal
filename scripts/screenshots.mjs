// README-Screenshots im Demo-Modus neu erzeugen (kein echtes Modell nötig).
//
//   ORBWISE_FAKE_LLM=1 orbwise serve   (mit einer Config: language: en, voice.enabled: false)
//   node scripts/screenshots.mjs [http://localhost:8765]
//
// Braucht Playwright (npm i -g playwright). CHROMIUM=/pfad/zu/chrome setzt den Browser.
import { execSync } from "node:child_process";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

let pw;
try {
  pw = await import("playwright");
} catch {  // global installiert (npm i -g) – ESM ignoriert NODE_PATH
  const root = execSync("npm root -g").toString().trim();
  pw = await import(pathToFileURL(join(root, "playwright", "index.mjs")).href);
}
const { chromium } = pw;

const base = process.argv[2] || "http://localhost:8765";
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 1440, height: 860 } });

async function ask(text) {
  await page.fill("#input", text);
  await page.press("#input", "Enter");
}

await page.goto(base);
const boot = page.locator("#boot-btn");
if (await boot.isVisible().catch(() => false)) await boot.click();
await page.waitForTimeout(4500); // Startsequenz

// 1) Übersicht: Unterhaltung mit Werkzeug-Aufruf, Aktivität, Orb mit Kontext-Ring
await ask("Good evening! What can you help me with?");
await page.waitForTimeout(3000);
await ask("Search the web for Linux 6.18 release notes");
await page.waitForTimeout(700); // Werkzeug-Symbol (Cloud) + Strahl am Orb sichtbar
await page.screenshot({ path: "docs/screenshot.png" });

// 2) Rückfrage vor einer Änderung am System
await page.waitForTimeout(6000);
await page.click("#btn-reset"); // neuer Chat
await page.waitForTimeout(800);
await ask("Please update my system");
await page.locator("#confirm").waitFor({ state: "visible" });
await page.waitForTimeout(900);
await page.screenshot({ path: "docs/screenshot-confirm.png" });
await page.click("#confirm-no");

await browser.close();
