// Read-only: dump key pages of the Feishu app console using the persisted login.
// Usage: node _check_app.mjs  (run inside auto_feishu/)
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const APP = "cli_aa23fc213e389bc6";
const ROOT = process.cwd();
const userDataDir = path.join(ROOT, "artifacts", "feishu-user-data");
const outDir = path.join(ROOT, "artifacts", "appcheck");
mkdirSync(outDir, { recursive: true });

const PAGES = [
  ["event", `https://open.feishu.cn/app/${APP}/event`],
  ["version", `https://open.feishu.cn/app/${APP}/version`],
  ["bot", `https://open.feishu.cn/app/${APP}/bot`],
  ["baseinfo", `https://open.feishu.cn/app/${APP}/baseinfo`],
  ["security", `https://open.feishu.cn/app/${APP}/security`],
  ["availability", `https://open.feishu.cn/app/${APP}/availability`],
];

const ctx = await chromium.launchPersistentContext(userDataDir, {
  headless: true,
  viewport: { width: 1440, height: 960 },
});
const page = ctx.pages()[0] ?? (await ctx.newPage());

for (const [name, url] of PAGES) {
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(6000);
    const title = await page.title().catch(() => "");
    let text = "";
    try {
      text = await page.locator("#app, body").first().innerText({ timeout: 8000 });
    } catch {
      text = await page.locator("body").innerText().catch(() => "");
    }
    text = text.replace(/\n{3,}/g, "\n\n").slice(0, 6000);
    writeFileSync(path.join(outDir, `${name}.txt`), `URL: ${url}\nTITLE: ${title}\n----\n${text}`, "utf8");
    await page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: false }).catch(() => {});
    console.log(`[${name}] saved, title=${title}`);
  } catch (e) {
    writeFileSync(path.join(outDir, `${name}.txt`), `URL: ${url}\nERROR: ${e}\n`, "utf8");
    console.log(`[${name}] ERROR ${e}`);
  }
}

await ctx.close();
console.log("done ->", outDir);
