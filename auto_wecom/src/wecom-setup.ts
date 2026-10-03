import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { stdin as input, stdout as output } from "node:process";
import readline from "node:readline/promises";
import { chromium, type Browser, type BrowserContext, type Locator, type Page, type Response } from "playwright";

interface RawConfig {
  botName: string;
  botDescription?: string;
  envPath?: string;
  resultPath?: string;
  screenshotsDir?: string;
  htmlDumpDir?: string;
  userDataDirPath?: string;
  timeoutMs?: number;
  loginTimeoutMs?: number;
}

interface LoadedConfig extends Required<Omit<RawConfig, "botDescription">> {
  rootDir: string;
  botDescription: string;
}

interface ResultState {
  botName: string | null;
  botId: string | null;
  creatorUserId: string | null;
  deployMode: "personal" | "public";
  maskedSecret: string | null;
  reused: boolean;
  createdAt: string;
  updatedAt: string;
  status: "pending" | "partial" | "completed" | "failed";
  lastCompletedStep: string | null;
  warnings: string[];
  nextSteps: string[];
  artifacts: Array<{ step: string; screenshotPath: string | null; htmlPath: string | null; createdAt: string }>;
  failure: null | { step: string; message: string; recoverable: boolean };
}

interface Logger {
  info(message: string): void;
  debug(message: string): void;
  warn(message: string): void;
  error(message: string): void;
}

const DEBUG_ENABLED = process.argv.includes("--debug");
const DEPLOY_MODE: "personal" | "public" = (() => {
  if (process.argv.includes("--public") || (process.env.WECOM_DEPLOY_MODE || "").trim().toLowerCase() === "public") {
    return "public";
  }
  return "personal";
})();
// CDP 附加模式（联调用）：WECOM_CDP_URL=http://127.0.0.1:9333 npm run wecom:setup
const CDP_URL = (() => {
  const i = process.argv.indexOf("--cdp");
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return (process.env.WECOM_CDP_URL || "").trim() || "";
})();
const BOT_NAME_ARG = (() => {
  const i = process.argv.indexOf("--bot-name");
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1].trim();
  const pref = process.argv.find((a) => a.startsWith("--bot-name=")) || "";
  if (pref) return pref.slice("--bot-name=".length).trim();
  return (process.env.WECOM_BOT_NAME || "").trim();
})();

const RESERVED_COMMANDS = new Set([
  "reset", "help", "stop", "clear", "cd", "session", "mode", "model", "effort", "status", "diff"
]);

export function validateBotName(name: string): { valid: boolean; reason?: string } {
  if (!name || !name.trim()) {
    return { valid: false, reason: "机器人名称不能为空" };
  }
  const trimmed = name.trim();

  // 1. 检查任何空白字符（半角空格、全角空格、制表符、换行符）
  if (/\s/.test(name) || name.includes("\u3000")) {
    return { valid: false, reason: "机器人名称严禁包含任何空格或换行（会导致群聊 @ 识别失效）" };
  }

  // 2. 检查长度限制（建议 2~20 个字符）
  if (trimmed.length < 2) {
    return { valid: false, reason: "机器人名称过短，至少需要 2 个字符" };
  }
  if (trimmed.length > 20) {
    return { valid: false, reason: "机器人名称过长，建议在 20 个字符以内（避免移动端群聊 @ 被截断）" };
  }

  // 3. 检查 @ 符号
  if (trimmed.includes("@") || trimmed.includes("＠")) {
    return { valid: false, reason: "机器人名称不能包含「@」符号" };
  }

  // 4. 检查斜杠与反斜杠
  if (trimmed.includes("/") || trimmed.includes("\\")) {
    return { valid: false, reason: "机器人名称不能包含斜杠「/」或反斜杠「\\」（与系统指令冲突）" };
  }

  // 5. 检查敏感标点与特殊符号
  const illegalCharsPattern = /[:;,'"`|<>&\uff01-\uff0f\uff1a-\uff20]/;
  if (illegalCharsPattern.test(trimmed)) {
    return { valid: false, reason: "机器人名称不能包含特殊标点符号（如冒号、逗号、引号、尖括号等）" };
  }

  // 6. 检查系统保留指令
  if (RESERVED_COMMANDS.has(trimmed.toLowerCase())) {
    return { valid: false, reason: `「${trimmed}」为系统内置保留指令，不能作为机器人名称` };
  }

  // 7. 合法字符集合验证：允许中文、英文字母、数字、下划线、短横线
  if (!/^[\w\u4e00-\u9fa5-]+$/.test(trimmed)) {
    return { valid: false, reason: "机器人名称仅支持中文、英文字母、数字、下划线「_」或短横线「-」" };
  }

  return { valid: true };
}

const LOGIN_URL = "https://work.weixin.qq.com/wework_admin/loginpage_wx";
const LIST_MANAGE_URL = "https://work.weixin.qq.com/wework_admin/frame#/aiHelper/list?from=manage_tools&tab=manage";
const LIST_CREATE_URL = "https://work.weixin.qq.com/wework_admin/frame#/aiHelper/list?from=manage_tools";
const LOGIN_SIGNALS = ["应用管理", "我的企业", "管理工具", "微信插件", "安全与管理", "通讯录"];

function createLogger(): Logger {
  const write = (label: string, message: string): void => {
    const ts = new Date().toISOString().replace("T", " ").replace("Z", "");
    output.write(`[${ts}] [${label}] ${message}\n`);
  };
  return {
    info: (m) => write("INFO", m),
    debug: (m) => { if (DEBUG_ENABLED) write("DEBUG", m); },
    warn: (m) => write("WARN", m),
    error: (m) => write("ERROR", m),
  };
}

const logger = createLogger();

function nowIso(): string { return new Date().toISOString(); }

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function pathExists(candidate?: string): Promise<boolean> {
  if (!candidate) return false;
  try { await access(candidate); return true; } catch { return false; }
}

function maskSecret(secret: string | null): string | null {
  if (!secret) return null;
  if (secret.length <= 8) return "*".repeat(secret.length);
  return `${secret.slice(0, 4)}****${secret.slice(-4)}`;
}

function isNonInteractive(): boolean {
  return !input.isTTY || !output.isTTY;
}

function resolveFromRoot(rootDir: string, candidate?: string): string | undefined {
  if (!candidate) return undefined;
  if (path.isAbsolute(candidate)) return candidate;
  return path.resolve(rootDir, candidate);
}

async function loadConfig(rootDir: string): Promise<LoadedConfig> {
  const raw = JSON.parse(await readFile(path.join(rootDir, "config.json"), "utf8")) as RawConfig;

  let activeBotName = raw.botName;
  if (BOT_NAME_ARG) {
    const checkArg = validateBotName(BOT_NAME_ARG);
    if (!checkArg.valid) {
      throw new Error(`--bot-name 参数「${BOT_NAME_ARG}」不合法: ${checkArg.reason}`);
    }
    activeBotName = BOT_NAME_ARG;
  } else if (raw.botName) {
    const checkBot = validateBotName(raw.botName);
    if (!checkBot.valid) {
      throw new Error(`config.json 中的 botName (「${raw.botName}」) 不合法: ${checkBot.reason}。请修改配置文件。`);
    }
  }

  return {
    ...raw,
    botName: activeBotName,
    rootDir,
    botDescription: raw.botDescription ?? "",
    envPath: resolveFromRoot(rootDir, raw.envPath) ?? path.resolve(rootDir, "..", ".env"),
    resultPath: resolveFromRoot(rootDir, raw.resultPath) ?? path.join(rootDir, "wecom-bot-result.json"),
    screenshotsDir: resolveFromRoot(rootDir, raw.screenshotsDir) ?? path.join(rootDir, "artifacts", "screenshots"),
    htmlDumpDir: resolveFromRoot(rootDir, raw.htmlDumpDir) ?? path.join(rootDir, "artifacts", "html"),
    userDataDirPath: resolveFromRoot(rootDir, raw.userDataDirPath) ?? path.join(rootDir, "artifacts", "wecom-user-data"),
    timeoutMs: raw.timeoutMs ?? 20000,
    loginTimeoutMs: raw.loginTimeoutMs ?? 600000,
  };
}

async function createInitialResult(config: LoadedConfig): Promise<ResultState> {
  const initial: ResultState = {
    botName: null, botId: null, creatorUserId: null, deployMode: DEPLOY_MODE,
    maskedSecret: null, reused: false,
    createdAt: nowIso(), updatedAt: nowIso(),
    status: "pending", lastCompletedStep: null,
    warnings: [], nextSteps: [], artifacts: [], failure: null,
  };
  try {
    const previous = JSON.parse(await readFile(config.resultPath, "utf8")) as Partial<ResultState>;
    return { ...initial, ...previous, deployMode: DEPLOY_MODE, updatedAt: nowIso(), status: "pending", failure: null };
  } catch {
    return initial;
  }
}

async function persistResult(config: LoadedConfig, result: ResultState): Promise<void> {
  result.updatedAt = nowIso();
  const safe = { ...result } as ResultState & { secret?: unknown };
  delete safe.secret;
  await writeFile(config.resultPath, JSON.stringify(safe, null, 2), "utf8");
}

async function saveArtifacts(page: Page, config: LoadedConfig, step: string): Promise<{ screenshotPath: string | null; htmlPath: string | null }> {
  await mkdir(config.screenshotsDir, { recursive: true }).catch(() => undefined);
  await mkdir(config.htmlDumpDir, { recursive: true }).catch(() => undefined);
  const stamp = nowIso().replace(/[:.]/g, "-");
  const slug = step.replace(/[^\w一-鿿]+/g, "-").replace(/^-+|-+$/g, "") || "step";
  const base = `${stamp}-${slug}`;
  const screenshotPath = path.join(config.screenshotsDir, `${base}.png`);
  const htmlPath = path.join(config.htmlDumpDir, `${base}.html`);
  let savedShot: string | null = null;
  let savedHtml: string | null = null;
  try { await page.screenshot({ path: screenshotPath, fullPage: true }); savedShot = screenshotPath; } catch { /* ignore */ }
  try { await writeFile(htmlPath, await page.content(), "utf8"); savedHtml = htmlPath; } catch { /* ignore */ }
  return { screenshotPath: savedShot, htmlPath: savedHtml };
}

async function waitForLogin(page: Page, config: LoadedConfig): Promise<void> {
  const checkSignals = async (): Promise<string | null> => {
    for (const frame of page.frames()) {
      const url = frame.url();
      if (!url.includes("wework_admin")) continue;
      if (url.includes("loginpage") || url.includes("login_qrcode")) continue;
      try {
        const text = await frame.locator("body").innerText({ timeout: 2000 });
        const hit = LOGIN_SIGNALS.find((s) => text.includes(s));
        if (hit) return hit;
      } catch { /* ignore */ }
    }
    return null;
  };

  const direct = await checkSignals();
  if (direct) {
    logger.info("检测到已登录的管理后台会话：" + direct);
    return;
  }

  await page.goto(LOGIN_URL, { waitUntil: "domcontentloaded", timeout: config.timeoutMs });
  logger.info("请在打开的浏览器中用企业微信 App 扫码登录管理后台；登录成功后脚本自动继续。");
  const deadline = Date.now() + config.loginTimeoutMs;
  let signal: string | null = null;
  while (!signal && Date.now() < deadline) {
    await page.waitForTimeout(3000);
    signal = await checkSignals();
  }
  if (!signal) {
    throw new Error(`等待企业微信扫码登录超时（${Math.round(config.loginTimeoutMs / 60000)} 分钟）。重跑本脚本可再试。`);
  }
  logger.info("扫码登录成功，页面特征：" + signal);
}

/** 机器人管理页签：解析「企业创建」区域下的机器人名单。 */
async function listExistingBots(page: Page, config: LoadedConfig): Promise<string[]> {
  await page.goto(LIST_MANAGE_URL, { waitUntil: "domcontentloaded", timeout: config.timeoutMs });
  await page.waitForTimeout(5000);
  const text = await page.locator("body").innerText({ timeout: config.timeoutMs });
  const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);

  const names: string[] = [];
  let inCorp = false;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i] === "企业创建") { inCorp = true; continue; }
    if (lines[i] === "成员创建") break;
    if (!inCorp) continue;
    // 卡片结构（实测）：名称行，下一行是「API 模式」标签，随后使用统计
    if (i + 1 < lines.length && /^API\s*模式$/.test(lines[i + 1]) && lines[i] !== "API 模式") {
      names.push(lines[i]);
    }
  }
  return names;
}

async function openBotDetail(page: Page, config: LoadedConfig, botName: string): Promise<void> {
  await page.goto(LIST_MANAGE_URL, { waitUntil: "domcontentloaded", timeout: config.timeoutMs });
  await page.waitForTimeout(5000);
  const locator = page.getByText(new RegExp(`^${escapeRegExp(botName)}$`)).first();
  if (!(await locator.isVisible({ timeout: 5000 }).catch(() => false))) {
    throw new Error(`机器人管理列表中未找到「${botName}」。`);
  }
  await locator.click({ timeout: config.timeoutMs });
  const deadline = Date.now() + config.timeoutMs;
  while (Date.now() < deadline) {
    if (page.url().includes("aibotid=")) {
      try {
        const text = await page.locator("body").innerText({ timeout: 3000 });
        if (text.includes("Bot ID")) return;
      } catch { /* ignore */ }
    }
    await page.waitForTimeout(1000);
  }
  throw new Error(`点击机器人「${botName}」后未进入详情页。`);
}

interface ExtractedCredential {
  botId: string;
  secret: string;
  longConnection: boolean;
  rawName: string | null;
}

async function extractCredentials(page: Page): Promise<ExtractedCredential> {
  const text = await page.locator("body").innerText({ timeout: 10000 });
  const botIdMatch = text.match(/Bot\s*ID\s*\n\s*([A-Za-z0-9][A-Za-z0-9_-]{10,})/);
  const secretMatch = text.match(/Secret\s*\n\s*([A-Za-z0-9_-]{20,})/);
  if (!botIdMatch) throw new Error("详情页未解析到 Bot ID。");
  if (!secretMatch) throw new Error("详情页未解析到 Secret。");
  const longConnection = text.includes("长连接");
  const nameMatch = text.match(/智能机器人详情\s*\n\s*(.+)\s*\n/);
  return {
    botId: botIdMatch[1],
    secret: secretMatch[1],
    longConnection,
    rawName: nameMatch?.[1]?.trim() ?? null,
  };
}

async function writeWecomEnv(envPath: string, botId: string, secret: string): Promise<void> {
  let content = "";
  try { content = await readFile(envPath, "utf8"); } catch { content = ""; }
  const upsert = (source: string, key: string, value: string): string => {
    const line = `${key}=${value}`;
    const pattern = new RegExp("^" + escapeRegExp(key) + "=.*$", "m");
    return pattern.test(source)
      ? source.replace(pattern, line)
      : source.replace(/\s*$/, "") + (source ? "\n" : "") + line + "\n";
  };
  content = upsert(content, "WECOM_BOT_ID", botId);
  content = upsert(content, "WECOM_SECRET", secret);
  await writeFile(envPath, content, "utf8");
}

async function promptText(prompt: readline.Interface, message: string): Promise<string> {
  if (isNonInteractive()) throw new Error("当前为非交互环境，无法读取终端输入。");
  return (await prompt.question(`${message}\n`)).trim();
}

/**
 * 创建新机器人（API 模式 + 长连接）。
 * 注意：本流程基于 2026-09 的公开教程与管理后台页面结构编写，未在真机上完整验证；
 * 任一步骤失败会保存排错资料（截图/HTML）并以可恢复方式继续。
 */
/** 抓取当前页面所有可见的 toast / 报错 / 弹窗文案（用于保存在第一现场消失前取证）。 */
async function harvestVisibleNotices(page: Page): Promise<string> {
  return page
    .evaluate(() => {
      const parts: string[] = [];
      for (const el of Array.from(
        document.querySelectorAll("[class*='toast'], [class*='message'], [class*='error'], [class*='warn'], [class*='tip'], [class*='dialog'], [class*='modal']")
      )) {
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        if (r.width > 0 && r.height > 0 && s.visibility !== "hidden") {
          const t = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120);
          if (t) parts.push(t);
        }
      }
      return [...new Set(parts)].slice(0, 5).join(" | ");
    })
    .catch(() => "");
}

/**
 * 创建新机器人（2026-09-29 真机逐步驱动验证的完整流程）：
 * 1. 名称/简介：placeholder 精准定位 input 本体（placeholder 同时挂在包装 div 上，
 *    用 input[placeholder=…] 避免定位歧义）
 * 2. 点「API 模式」文本切换模式——默认选中普通模式！点击后表单重渲染出连接方式
 *    radio（长连接默认选中）。文本带缩进换行，只能子串匹配。
 * 3. 【关键】Secret 必须点「点击获取」生成——不生成时保存被客户端校验拦截
 *    （toast「生成Secret后方可保存机器人」，且不发任何请求）
 * 4. 保存 → createAiBot 接口 → 页面自动跳转详情页
 */
async function createNewBot(page: Page, config: LoadedConfig, prompt: readline.Interface, result: ResultState): Promise<string> {
  await page.goto(LIST_CREATE_URL, { waitUntil: "domcontentloaded", timeout: config.timeoutMs });
  await page.waitForTimeout(5000);

  const createBtn = page.getByText("新建机器人", { exact: true }).first();
  if (!(await createBtn.isVisible({ timeout: 5000 }).catch(() => false))) {
    throw new Error("创建页未找到「新建机器人」按钮（可能无创建权限，见「权限管理」页签）。");
  }
  await createBtn.click({ timeout: config.timeoutMs });
  await page.waitForTimeout(4000);

  // 1. 名称 + 简介
  const nameInput = page.locator("input[placeholder*='名称'], input[placeholder*='机器人名']").first();
  if (!(await nameInput.isVisible({ timeout: 5000 }).catch(() => false))) {
    throw new Error("创建页未找到名称输入框。");
  }
  await nameInput.fill(config.botName, { timeout: config.timeoutMs });
  const descBox = page.locator("textarea[placeholder*='简介']").first();
  if (await descBox.isVisible({ timeout: 2000 }).catch(() => false)) {
    await descBox.fill(config.botDescription || `${config.botName} 的企业微信智能机器人`, { timeout: config.timeoutMs }).catch(() => undefined);
  }

  // 2. 切换到 API 模式（默认普通模式；radio 索引 1 = API）
  const radioStates = () =>
    page
      .evaluate<Array<boolean>>("Array.from(document.querySelectorAll(\"input[type='radio']\")).map(r => r.checked)")
      .catch(() => [] as boolean[]);
  const apiOpt = page.getByText("API 模式").first();
  if (await apiOpt.isVisible({ timeout: 3000 }).catch(() => false)) {
    await apiOpt.click({ timeout: config.timeoutMs }).catch(() => undefined);
    await page.waitForTimeout(1500);
    let radios = await radioStates();
    if (radios.length >= 2 && !radios[1]) {
      logger.warn("首次点击未切到 API 模式，重试一次...");
      await apiOpt.click({ timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(1500);
      radios = await radioStates();
    }
    if (radios.length >= 2 && !radios[1]) {
      const art = await saveArtifacts(page, config, "创建-模式未切换");
      throw new Error(`点击「API 模式」后 radio 仍为普通模式（${radios.join(",")}）。截图：${art.screenshotPath ?? "无"}`);
    }
    logger.info("已切换到 API 模式。");
  } else {
    logger.warn("未找到「API 模式」选项文本（可能已在该模式），继续。");
  }

  // 3. 连接方式：确保长连接（API 模式重渲染后默认选中，点选幂等）
  for (const t of ["使用长连接", "使用 SDK 启动长连接", "长连接"]) {
    const opt = page.getByText(t).first();
    if (await opt.isVisible({ timeout: 1200 }).catch(() => false)) {
      await opt.click({ timeout: 5000 }).catch(() => undefined);
      break;
    }
  }

  // 4. 【关键】生成 Secret：不生成保存必被拦截（toast：生成Secret后方可保存机器人）
  const secretAreaText = () =>
    page
      .evaluate<string>(`(() => {
        const spans = Array.from(document.querySelectorAll('*')).filter(el => el.childElementCount === 0 && (el.textContent || '').trim() === 'Secret');
        if (!spans.length) return '';
        let cur = spans[0];
        for (let i = 0; i < 3 && cur; i++) {
          cur = cur.parentElement;
          const t = (cur.innerText || '').replace(/\\s+/g, ' ').trim();
          if (t.length > 6 && t.length < 150) return t;
        }
        return '';
      })()`)
      .catch(() => "");
  if (!/[A-Za-z0-9_-]{30,}/.test(await secretAreaText())) {
    let genClicked = false;
    for (const sel of ["button:has-text('点击获取')", "a:has-text('点击获取')", "button:has-text('生成')"]) {
      const c = page.locator(sel);
      const n = Math.min(await c.count().catch(() => 0), 5);
      for (let i = 0; i < n; i++) {
        const el = c.nth(i);
        if (await el.isVisible({ timeout: 500 }).catch(() => false)) {
          await el.click({ timeout: config.timeoutMs }).catch(() => undefined);
          genClicked = true;
          break;
        }
      }
      if (genClicked) break;
    }
    if (!genClicked) {
      const fb = page.getByText(/点击获取|生成\s*Secret/).first();
      if (await fb.isVisible({ timeout: 2000 }).catch(() => false)) {
        await fb.click({ timeout: 5000 }).catch(() => undefined);
        genClicked = true;
      }
    }
    if (genClicked) {
      logger.info("已点击 Secret「点击获取」，等待生成...");
      await page.waitForTimeout(1500);
      // 可能弹确认框（只在弹窗容器内找按钮，绝不点页面级元素——左侧导航的「退出」是登出）
      const dlg = page.locator("[class*='dialog']:visible, [class*='modal']:visible, [role='dialog']:visible").first();
      if (await dlg.isVisible({ timeout: 2000 }).catch(() => false)) {
        for (const t of ["确定", "生成", "确认"]) {
          const btn = dlg.getByText(t, { exact: true }).first();
          if (await btn.isVisible({ timeout: 800 }).catch(() => false)) {
            await btn.click({ timeout: 5000 }).catch(() => undefined);
            break;
          }
        }
      }
      await page.waitForTimeout(2000);
    }
    const secretNow = await secretAreaText();
    if (!/[A-Za-z0-9_-]{30,}/.test(secretNow)) {
      const toast = await harvestVisibleNotices(page);
      const art = await saveArtifacts(page, config, "创建-Secret未生成");
      throw new Error(`未能生成 Secret（保存会被拦截）。区域文本：「${secretNow}」${toast ? `；页面提示：${toast}` : ""}。截图：${art.screenshotPath ?? "无"}`);
    }
    logger.info("Secret 已生成（保存拦截解除）。");
  }

  // 4.5 按部署模式设置「使用方式」（仅个人使用 vs 多人使用）
  await applyUseModeInForm(page, config, DEPLOY_MODE);

  // 5. 保存：挂接口采集器（真因只存在于 POST 响应里）+ 第一现场抓取
  const xhrLog: string[] = [];
  const onResponse = async (resp: Response): Promise<void> => {
    try {
      if (resp.request().method() !== "POST") return;
      if (!/aihelper|aibot|robot|smart/i.test(resp.url())) return;
      let body = "";
      try {
        body = (await resp.text()).replace(/\s+/g, " ").slice(0, 200);
      } catch {
        /* 响应体读不到就只记状态码 */
      }
      xhrLog.push(`[HTTP ${resp.status()}] …${resp.url().slice(-90)} ${body}`);
    } catch {
      /* ignore */
    }
  };
  page.on("response", onResponse);

  try {
    const clickSave = async (): Promise<boolean> => {
      const groups = [
        page.locator("button.navi_button:has-text('保存')"),
        page.locator("button.t-button--theme-primary:has-text('保存')"),
        page.locator("button:has-text('保存'), button:has-text('创建'), button:has-text('确定')"),
        page.getByRole("button", { name: /保\s*存|创\s*建|确\s*定/ }),
      ];
      for (const group of groups) {
        const count = Math.min(await group.count().catch(() => 0), 10);
        for (let i = 0; i < count; i++) {
          const opt = group.nth(i);
          if (!(await opt.isVisible({ timeout: 600 }).catch(() => false))) continue;
          if (await opt.isDisabled().catch(() => false)) continue;
          await opt.scrollIntoViewIfNeeded().catch(() => undefined);
          const clicked = await opt
            .click({ timeout: config.timeoutMs })
            .then(() => true)
            .catch(() => opt.click({ timeout: 5000, force: true }).then(() => true).catch(() => false));
          if (clicked) {
            const label = (await opt.innerText().catch(() => "")).trim().replace(/\s+/g, " ");
            logger.info(`已点击提交按钮：「${label || "(无文本)"}」`);
            return true;
          }
        }
      }
      return false;
    };
    if (!(await clickSave())) {
      throw new Error("创建页未找到提交按钮（保存/创建/确定）。");
    }

    await page.waitForTimeout(1200);
    const earlyToast = await harvestVisibleNotices(page);
    logger.info(`保存点击后 URL：${page.url()}${earlyToast ? `；页面提示：${earlyToast}` : ""}`);
    await page.waitForTimeout(7000);

    // 成功特征：页面跳到详情页（aibotid=）
    if (page.url().includes("aibotid=")) {
      result.warnings.push("新建机器人已完成，请核对详情页配置方式为「API 模式 + 长连接」。");
      return config.botName;
    }

    // 失败诊断：保存按钮还在 = 未提交成功
    let postSaveNote = "";
    const saveStill = page.locator("button:has-text('保存')").first();
    if (await saveStill.isVisible({ timeout: 2000 }).catch(() => false)) {
      postSaveNote = (await harvestVisibleNotices(page)) || "无可见报错提示";
      await saveArtifacts(page, config, "创建-保存后现场");
    }

    // 列表核验带重试（异步出列兜底），严格精确匹配
    let names: string[] = [];
    for (let i = 0; i < 3 && names.length === 0; i++) {
      if (i > 0) {
        await page.waitForTimeout(5000);
      }
      names = await listExistingBots(page, config);
    }
    const created = names.find((n) => n.toLowerCase() === config.botName.toLowerCase());
    if (created) {
      result.warnings.push("新建机器人已完成，请核对详情页配置方式为「API 模式 + 长连接」。");
      return created;
    }
    const takeover = isNonInteractive()
      ? ""
      : await promptText(prompt, `保存后未在列表中确认到「${config.botName}」。若列表已出现请输入其名称（候选：${names.join("、") || "无"}），否则直接回车退出：`);
    if (!takeover) {
      const xhrTail = xhrLog.slice(-5).join(" ⟂ ");
      const reason = postSaveNote || earlyToast || "无可见报错提示";
      throw new Error(
        `保存后未在机器人列表中确认到「${config.botName}」（当前列表：${names.join("、") || "空"}）。${reason}` +
          (xhrTail ? `。保存相关接口响应：${xhrTail}` : "（未捕获到保存接口请求——保存点击未触发表单提交）")
      );
    }
    return takeover;
  } finally {
    page.off("response", onResponse);
  }
}

/**
 * 在创建页或编辑页中，将「使用方式」下拉框切换为目标模式：
 * - personal -> 「仅个人使用」
 * - public   -> 「多人使用」，并自动点击「添加」勾选全企业根部门可见范围
 */
async function applyUseModeInForm(page: Page, config: LoadedConfig, mode: "personal" | "public"): Promise<void> {
  const targetOptionText = mode === "public" ? "多人使用" : "仅个人使用";
  try {
    const selectWrap = page.locator(".use-mode__select, .wd-dropdown.use-mode__select").first();
    if (!(await selectWrap.isVisible({ timeout: 3000 }).catch(() => false))) {
      logger.debug(`页面未找到 .use-mode__select 下拉框，跳过使用方式切换（目标：${targetOptionText}）。`);
      return;
    }
    await selectWrap.scrollIntoViewIfNeeded().catch(() => undefined);
    await selectWrap.click({ timeout: 5000 }).catch(() => undefined);
    await page.waitForTimeout(600);

    const opt = page.locator(`li.t-select-option[title='${targetOptionText}'], li.t-select-option:has-text('${targetOptionText}')`).first();
    if (await opt.isVisible({ timeout: 3000 }).catch(() => false)) {
      await opt.click({ timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(800);
      logger.info(`已将机器人「使用方式」设置为：${targetOptionText}`);
    } else {
      logger.warn(`下拉列表中未找到「${targetOptionText}」选项，保持当前默认状态。`);
    }

    // 若为公用模式（多人使用），确保可见范围已添加（若未添加，点击「添加」勾选顶层企业节点）
    if (mode === "public") {
      const addScopeBtn = page.locator(".use-mode button:has-text('添加'), .visible_selector button:has-text('添加')").first();
      if (await addScopeBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
        // 检查是否已有已选标签
        const hasTags = await page.locator(".use-mode .range_tag_list > *, .visible_selector .range_tag_list > *").count().catch(() => 0);
        if (hasTags === 0) {
          logger.info("公用模式：正在点击可见范围「添加」选择全企业范围...");
          await addScopeBtn.click({ timeout: 5000 }).catch(() => undefined);
          await page.waitForTimeout(1200);

          const dlg = page.locator(".t-dialog:visible, [role='dialog']:visible, .ww_dialog:visible").first();
          if (await dlg.isVisible({ timeout: 3000 }).catch(() => false)) {
            // 尝试点击弹窗左侧树中的第一个顶层部门节点或复选框
            const rootSelectors = [
              ".jstree-anchor",
              ".ww_treeSelector_item",
              ".t-tree__item .t-checkbox",
              ".t-tree__item",
              "input[type='checkbox']",
            ];
            for (const sel of rootSelectors) {
              const firstNode = dlg.locator(sel).first();
              if (await firstNode.isVisible({ timeout: 800 }).catch(() => false)) {
                await firstNode.click({ timeout: 3000 }).catch(() => undefined);
                await page.waitForTimeout(500);
                break;
              }
            }
            // 点击弹窗内的「确定」
            const confirmBtn = dlg.locator("button:has-text('确定'), a:has-text('确定')").first();
            if (await confirmBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
              await confirmBtn.click({ timeout: 5000 }).catch(() => undefined);
              await page.waitForTimeout(800);
            }
          }
        }
      }
    }
  } catch (e) {
    logger.warn(`设置使用方式（${targetOptionText}）时遇到非致命异常：${e instanceof Error ? e.message : String(e)}`);
  }
}

/**
 * 复用已有机器人时，在详情页检查「可使用方式」是否与目标模式一致；
 * 若不一致（例如个人用模式变成公用模式，或反之），自动点击右上角「编辑」进行切换并保存。
 */
async function ensureBotUseModeOnDetail(page: Page, config: LoadedConfig, mode: "personal" | "public", currentName: string): Promise<void> {
  try {
    const bodyText = await page.locator("body").innerText({ timeout: 5000 });
    const isCurrentlyPersonal = bodyText.includes("仅个人使用");
    const modeMatched = (mode === "personal" && isCurrentlyPersonal) || (mode === "public" && !isCurrentlyPersonal);
    const nameMatched = currentName.trim() === config.botName.trim();
    if (modeMatched && nameMatched) {
      logger.info(`当前机器人名称（${config.botName}）与使用方式（${mode === "personal" ? "仅个人使用" : "多人/公用"}）均已符合目标。`);
      return;
    }

    const editBtn = page.locator("button:has-text('编辑')").first();
    if (!(await editBtn.isVisible({ timeout: 3000 }).catch(() => false))) {
      logger.warn("详情页未找到「编辑」按钮，跳过名称/使用方式更新。");
      return;
    }

    logger.info(`正在进入编辑页同步机器人名称（-> ${config.botName}）与使用方式（-> ${mode === "personal" ? "仅个人使用" : "多人使用"}）...`);
    await editBtn.click({ timeout: 5000 });
    await page.waitForTimeout(2500);

    if (!nameMatched) {
      const nameInput = page.locator("input[placeholder*='名称'], input[placeholder*='机器人名']").first();
      if (await nameInput.isVisible({ timeout: 3000 }).catch(() => false)) {
        await nameInput.fill(config.botName, { timeout: config.timeoutMs }).catch(() => undefined);
        logger.info(`已将机器人名称从「${currentName}」改为「${config.botName}」`);
      }
    }

    await applyUseModeInForm(page, config, mode);

    const saveBtn = page.locator("button.navi_button:has-text('保存'), button.t-button--theme-primary:has-text('保存'), button:has-text('保存')").first();
    if (await saveBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await saveBtn.click({ timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(4000);
      logger.info("已保存机器人编辑变更。");
    }
  } catch (e) {
    logger.warn(`详情页核对/切换使用方式异常：${e instanceof Error ? e.message : String(e)}`);
  }
}

async function main(): Promise<void> {
  const rootDir = process.cwd();
  const config = await loadConfig(rootDir);
  await mkdir(config.screenshotsDir, { recursive: true }).catch(() => undefined);
  await mkdir(config.htmlDumpDir, { recursive: true }).catch(() => undefined);

  const result = await createInitialResult(config);
  await persistResult(config, result);

  const prompt = readline.createInterface({ input, output });
  let browser: Browser | null = null;
  let context: BrowserContext | null = null;
  let page: Page;

  if (CDP_URL) {
    logger.info(`CDP 附加模式：${CDP_URL}`);
    browser = await chromium.connectOverCDP(CDP_URL);
    context = browser.contexts()[0];
    for (const p of context.pages()) {
      if (!p.url().includes("wework_admin")) await p.close().catch(() => undefined);
    }
    page = context.pages().find((p) => p.url().includes("wework_admin")) ?? (await context.newPage());
  } else {
    await mkdir(path.dirname(config.userDataDirPath), { recursive: true }).catch(() => undefined);
    context = await chromium.launchPersistentContext(config.userDataDirPath, {
      headless: false,
      args: ["--no-proxy-server"],
      viewport: { width: 1440, height: 900 },
    });
    browser = context.browser();
    page = context.pages()[0] ?? (await context.newPage());
  }
  page.setDefaultTimeout(config.timeoutMs);

  // 监听 getAIRobotDetail 接口，自动捕获创建者本人的企业微信 userid (acctid)
  page.on("response", async (resp: Response) => {
    try {
      if (!resp.url().includes("getAIRobotDetail")) return;
      const data = await resp.json().catch(() => null);
      const vids = data?.data?.aibot_profile?.robot_config?.visiable_range?.visible_vids;
      if (Array.isArray(vids)) {
        for (const item of vids) {
          if (item && typeof item === "object" && typeof item.acctid === "string" && item.acctid.trim()) {
            const acctid = item.acctid.trim();
            if (result.creatorUserId !== acctid) {
              result.creatorUserId = acctid;
              logger.info(`已从后台接口自动捕获创建者 userid：${acctid}`);
            }
            break;
          }
        }
      }
    } catch {
      /* ignore */
    }
  });

  const fail = async (step: string, message: string): Promise<never> => {
    const artifacts = await saveArtifacts(page, config, step);
    result.failure = { step, message, recoverable: false };
    result.status = "failed";
    result.artifacts.push({ step, ...artifacts, createdAt: nowIso() });
    await persistResult(config, result);
    logger.error(`${step} 失败：${message}`);
    logger.error(`排错资料：${artifacts.screenshotPath ?? "无截图"} | ${artifacts.htmlPath ?? "无HTML"}`);
    throw new Error(`${step}：${message}`);
  };

  try {
    // Step 1: 登录
    logger.info(`正在确认管理后台登录状态（目标模式：${DEPLOY_MODE === "personal" ? "个人用" : "公用"}）...`);
    try {
      await waitForLogin(page, config);
      result.lastCompletedStep = "登录管理后台";
    } catch (e) {
      await fail("登录管理后台", e instanceof Error ? e.message : String(e));
    }

    // Step 2: 盘点已有机器人并选择
    logger.info("正在读取「机器人管理」列表...");
    let names: string[] = [];
    try {
      names = await listExistingBots(page, config);
    } catch (e) {
      await fail("读取机器人列表", e instanceof Error ? e.message : String(e));
    }

    let chosen: string | null = null;
    if (names.length > 0) {
      const historical = result.botName;
      const base = config.botName.split(/\s+/)[0].toLowerCase();
      // 匹配规则：与配置名精确/首词匹配的机器人 → 复用；上次配置过的 → 复用；
      // 都没有 → 默认创建（多项目同租户场景：myclaw/mycodex 各用各的机器人，
      // 智能机器人是"单机器人单连接"，复用别人的 bot 会互踢长连接）。
      const matched =
        names.find((n) => n.toLowerCase() === config.botName.toLowerCase()) ??
        names.find((n) => base && n.toLowerCase().includes(base)) ??
        (historical ? names.find((n) => n === historical) : undefined);
      const preferred: string | null = matched ?? null;
      if (isNonInteractive()) {
        chosen = preferred;
        logger.info(
          preferred
            ? `检测到 ${names.length} 个企业创建的机器人，自动选用：${preferred}`
            : `检测到 ${names.length} 个机器人（${names.join("、")}）但没有匹配「${config.botName}」的，默认创建新机器人。`
        );
      } else {
        logger.info(`检测到当前企业下有 ${names.length} 个机器人：`);
        names.forEach((n, i) => logger.info(`  ${i + 1}. ${n}`));
        logger.info(`  0. 创建全新机器人（默认名称：${config.botName}）`);
        const answer = await promptText(prompt, `请选择序号 [0-${names.length}]（直接回车 = ${preferred ?? "创建新机器人"}）：`);
        if (answer === "0") {
          chosen = null;
        } else if (answer && /^[1-9]\d*$/.test(answer) && Number(answer) <= names.length) {
          chosen = names[Number(answer) - 1];
        } else {
          chosen = preferred;
        }
      }
    }

    // Step 3: 创建（如需要）
    if (!chosen) {
      logger.info("未选择已有机器人，进入创建流程...");
      if (!isNonInteractive()) {
        logger.info(">> 命名规则：严禁包含任何空格（否则群聊 @ 无法识别），仅支持 2-20 位中文、英文、数字、下划线或短横线。");
        while (true) {
          const inputName = (await promptText(
            prompt,
            `请输入新机器人的名称 (不可含空格，直接回车默认 "${config.botName}"): `
          )).trim();
          if (!inputName) {
            const check = validateBotName(config.botName);
            if (!check.valid) {
              logger.error(`默认机器人名称「${config.botName}」不合法: ${check.reason}，请手动输入合法名称。`);
              continue;
            }
            logger.info(`采用默认机器人名称：“${config.botName}”`);
            break;
          }
          const check = validateBotName(inputName);
          if (!check.valid) {
            logger.warn(`[!] 输入的名称不符合规范: ${check.reason}，请重新输入。`);
            continue;
          }
          config.botName = inputName;
          logger.info(`已设置新机器人名称为：“${config.botName}”`);
          break;
        }
      }
      try {
        chosen = await createNewBot(page, config, prompt, result);
        result.reused = false;
      } catch (e) {
        await fail("创建机器人", e instanceof Error ? e.message : String(e));
      }
    } else {
      result.reused = true;
    }
    if (!chosen) {
      await fail("选择机器人", "未能确定目标机器人。");
      throw new Error("未能确定目标机器人。");
    }

    // Step 4: 详情页读取凭据并核对使用方式
    logger.info(`正在打开机器人「${chosen}」详情页...`);
    try {
      await openBotDetail(page, config, chosen);
      if (result.reused) {
        await ensureBotUseModeOnDetail(page, config, DEPLOY_MODE, chosen);
      }
      const cred = await extractCredentials(page);
      if (!cred.longConnection) {
        result.warnings.push(`「${chosen}」详情页未检测到“长连接”字样，请人工确认配置方式为长连接（SDK）。`);
        logger.warn("详情页未检测到长连接配置提示，已记录警告。");
      }
      result.botName = cred.rawName ?? chosen;
      result.botId = cred.botId;
      result.maskedSecret = maskSecret(cred.secret);
      await writeWecomEnv(config.envPath, cred.botId, cred.secret);
      logger.info(`Bot ID：${cred.botId}`);
      logger.info(`Secret：${result.maskedSecret}（已明文写入 ${config.envPath}）`);
      if (result.creatorUserId) {
        logger.info(`创建者 userid：${result.creatorUserId}`);
      }
      result.lastCompletedStep = "读取凭据并写入 .env";
    } catch (e) {
      await fail("读取凭据", e instanceof Error ? e.message : String(e));
    }

    result.status = "completed";
    result.nextSteps = [
      "企业微信凭据已写入根目录 .env（WECOM_BOT_ID / WECOM_SECRET）。",
      "若服务正在运行，请重启服务使配置生效。",
      "在企业微信通讯录中找到该机器人，发送 /help 验证。",
    ];
    await persistResult(config, result);
    logger.info(`结果文件已写入：${config.resultPath}`);
    logger.info("配置完成：请在企业微信中测试机器人。");
  } finally {
    prompt.close();
    if (!CDP_URL) {
      // 自起浏览器模式才关闭；CDP 附加模式保持宿主浏览器在线
      await context?.close().catch(() => undefined);
      await browser?.close().catch(() => undefined);
    }
  }
}

if (typeof require !== "undefined" && require.main === module) {
  main().catch((e) => {
    logger.error(String(e));
    process.exitCode = 1;
  });
}
