// Read-only: open the published version detail and dump availability +
// external-group option states. Usage: node _check_version_detail.mjs <appId>
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const APP = process.argv[2];
if (!APP) {
  console.error("usage: node _check_version_detail.mjs <appId>");
  process.exit(1);
}
const ROOT = process.cwd();
const userDataDir = path.join(ROOT, "artifacts", "feishu-user-data");
const outDir = path.join(ROOT, "artifacts", "appcheck");
mkdirSync(outDir, { recursive: true });
const outName = `verdetail-${APP}.txt`;

const ctx = await chromium.launchPersistentContext(userDataDir, {
  headless: true,
  viewport: { width: 1440, height: 1200 },
});
const page = ctx.pages()[0] ?? (await ctx.newPage());

const dump = async (tag) => {
  const title = await page.title().catch(() => "");
  let text = "";
  try {
    text = await page.locator("#app, body").first().innerText({ timeout: 8000 });
  } catch {
    text = await page.locator("body").innerText().catch(() => "");
  }
  text = text.replace(/\n{3,}/g, "\n\n").slice(0, 15000);
  writeFileSync(path.join(outDir, outName), `[${tag}] URL: ${page.url()}\nTITLE: ${title}\n----\n${text}`, "utf8");
  await page.screenshot({ path: path.join(outDir, `verdetail-${APP}.png`), fullPage: true }).catch(() => {});
  console.log(`[${tag}] saved ->`, outName);
};

await page.goto(`https://open.feishu.cn/app/${APP}/version`, {
  waitUntil: "domcontentloaded", timeout: 30000,
});
await page.waitForTimeout(6000);

// Try every plausible entry into the published version's detail.
for (const candidate of [
  () => page.getByText("查看版本详情", { exact: false }).first().click({ timeout: 4000 }),
  () => page.getByText("1.0.0", { exact: true }).first().click({ timeout: 4000 }),
  () => page.getByText("版本详情", { exact: false }).first().click({ timeout: 4000 }),
]) {
  try {
    await candidate();
    await page.waitForTimeout(4000);
    break;
  } catch {
    /* try next */
  }
}
await dump("after-click");
await ctx.close();
console.log("done");
