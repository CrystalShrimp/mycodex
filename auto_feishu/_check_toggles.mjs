// Read-only: dump every checkbox/switch state with its label text on the
// published version detail page. Usage: node _check_toggles.mjs <appId>
import { chromium } from "playwright";
import path from "node:path";

const APP = process.argv[2];
if (!APP) process.exit(1);
const ROOT = process.cwd();
const userDataDir = path.join(ROOT, "artifacts", "feishu-user-data");

const ctx = await chromium.launchPersistentContext(userDataDir, {
  headless: true,
  viewport: { width: 1440, height: 1200 },
});
const page = ctx.pages()[0] ?? (await ctx.newPage());

await page.goto(`https://open.feishu.cn/app/${APP}/version`, {
  waitUntil: "domcontentloaded", timeout: 30000,
});
await page.waitForTimeout(6000);

for (const open of [
  () => page.getByText("查看版本详情", { exact: false }).first().click({ timeout: 4000 }),
  () => page.getByText("版本详情", { exact: false }).first().click({ timeout: 4000 }),
]) {
  try { await open(); await page.waitForTimeout(4000); break; } catch { /* next */ }
}

const info = await page.evaluate(() => {
  const out = [];
  // checkboxes with labels
  for (const cb of document.querySelectorAll("input[type=checkbox]")) {
    const wrap = cb.closest(".ud__checkbox, .ud__form__item, label") || cb.parentElement;
    out.push({
      kind: "checkbox",
      label: (wrap?.innerText || "").replace(/\s+/g, " ").slice(0, 60),
      checked: cb.checked,
    });
  }
  // switches (Feishu uses ud__switch)
  for (const sw of document.querySelectorAll(".ud__switch")) {
    const wrap = sw.closest(".ud__form__item, .ud__modal, .ud__card") || sw.parentElement?.parentElement;
    const ariaChecked = sw.getAttribute("aria-checked");
    const cls = sw.className;
    out.push({
      kind: "switch",
      label: (wrap?.innerText || "").replace(/\s+/g, " ").slice(0, 60),
      ariaChecked,
      cls: String(cls).slice(0, 80),
    });
  }
  // radios with labels
  for (const rd of document.querySelectorAll("input[type=radio]")) {
    const wrap = rd.closest("label, .ud__radio, .ud__form__item") || rd.parentElement;
    out.push({
      kind: "radio",
      label: (wrap?.innerText || "").replace(/\s+/g, " ").slice(0, 60),
      checked: rd.checked,
    });
  }
  return out;
});

console.log(JSON.stringify({ appId: APP, url: page.url(), controls: info }, null, 2));
await ctx.close();
