// Dump permission page: navigate via left menu click (URL unknown), read-only.
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";
import path from "node:path";

const APP = "cli_aad73a05bfb99d05";
const ROOT = process.cwd();
const outDir = path.join(ROOT, "artifacts", "appcheck");

const ctx = await chromium.launchPersistentContext(path.join(ROOT, "artifacts", "feishu-user-data"), {
  headless: true,
  viewport: { width: 1440, height: 960 },
});
const page = ctx.pages()[0] ?? (await ctx.newPage());
await page.goto(`https://open.feishu.cn/app/${APP}/baseinfo`, { waitUntil: "domcontentloaded", timeout: 30000 });
await page.waitForTimeout(5000);

// 点击左侧"权限管理"
const menu = page.getByText("权限管理", { exact: true }).first();
try {
  await menu.click({ timeout: 8000 });
  await page.waitForTimeout(6000);
} catch (e) {
  console.log("menu click failed:", e);
}
console.log("URL now:", page.url());
const title = await page.title().catch(() => "");
let text = "";
try { text = await page.locator("body").innerText({ timeout: 8000 }); } catch {}
text = text.replace(/\n{3,}/g, "\n\n");
writeFileSync(path.join(outDir, "permission.txt"), `URL: ${page.url()}\nTITLE: ${title}\n----\n${text.slice(0, 12000)}`, "utf8");
await page.screenshot({ path: path.join(outDir, "permission.png"), fullPage: true }).catch(() => {});
await ctx.close();
console.log("saved permission.txt");
