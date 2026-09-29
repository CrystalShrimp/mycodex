import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { stdin as input, stdout as output } from "node:process";
import readline from "node:readline/promises";
import { chromium, type Browser, type BrowserContext, type Page } from "playwright";

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
// CDP 附加模式（联调用）：WECOM_CDP_URL=http://127.0.0.1:9333 npm run wecom:setup
const CDP_URL = (() => {
  const i = process.argv.indexOf("--cdp");
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return (process.env.WECOM_CDP_URL || "").trim() || "";
})();

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
  return {
    ...raw,
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
    botName: null, botId: null, maskedSecret: null, reused: false,
    createdAt: nowIso(), updatedAt: nowIso(),
    status: "pending", lastCompletedStep: null,
    warnings: [], nextSteps: [], artifacts: [], failure: null,
  };
  try {
    const previous = JSON.parse(await readFile(config.resultPath, "utf8")) as Partial<ResultState>;
    return { ...initial, ...previous, updatedAt: nowIso(), status: "pending", failure: null };
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
  const base = `${stamp}-${step.replace(/[^a-zA-Z0-9]+/g, "-")}`;
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
async function createNewBot(page: Page, config: LoadedConfig, prompt: readline.Interface, result: ResultState): Promise<string> {
  await page.goto(LIST_CREATE_URL, { waitUntil: "domcontentloaded", timeout: config.timeoutMs });
  await page.waitForTimeout(5000);

  const createBtn = page.getByText("新建机器人", { exact: true }).first();
  if (!(await createBtn.isVisible({ timeout: 5000 }).catch(() => false))) {
    throw new Error("创建页未找到「新建机器人」按钮（可能无创建权限，见「权限管理」页签）。");
  }
  await createBtn.click({ timeout: config.timeoutMs });
  await page.waitForTimeout(2000);

  // 名称：优先带 label 的输入框，兜底第一个可见文本输入
  const modal = page.locator("[class*='dialog'], [class*='modal'], [role='dialog']").first();
  const root = (await modal.isVisible({ timeout: 3000 }).catch(() => false)) ? modal : page;
  const nameInput = root.locator("input[type='text']").first();
  if (!(await nameInput.isVisible({ timeout: 5000 }).catch(() => false))) {
    throw new Error("创建弹窗未找到名称输入框。");
  }
  await nameInput.fill(config.botName, { timeout: config.timeoutMs });

  // 接入模式：选「API 模式」（候选文案多种灰度写法）
  for (const t of ["API 模式", "API模式", "API 接入", "API"]) {
    const opt = root.getByText(new RegExp(`^${escapeRegExp(t)}$`)).first();
    if (await opt.isVisible({ timeout: 1200 }).catch(() => false)) {
      await opt.click({ timeout: 5000 }).catch(() => undefined);
      break;
    }
  }
  // 连接方式：确保长连接（如出现选择项）
  for (const t of ["使用 SDK 启动长连接", "长连接"]) {
    const opt = root.getByText(new RegExp(escapeRegExp(t))).first();
    if (await opt.isVisible({ timeout: 1200 }).catch(() => false)) {
      await opt.click({ timeout: 5000 }).catch(() => undefined);
      break;
    }
  }
  // 可见范围：教程要求必须添加（添加本企业/自己），出现选择框时尽力选第一项
  const scopeAdd = root.getByText(/添加|选择.*范围|可见范围/).first();
  if (await scopeAdd.isVisible({ timeout: 2000 }).catch(() => false)) {
    await scopeAdd.click({ timeout: 5000 }).catch(() => undefined);
    await page.waitForTimeout(1500);
    const firstOrg = page.getByText(/本企业|全企业|我的企业|所有人/).first();
    if (await firstOrg.isVisible({ timeout: 2000 }).catch(() => false)) {
      await firstOrg.click({ timeout: 5000 }).catch(() => undefined);
    }
    const confirmScope = page.getByText("确定", { exact: true }).last();
    if (await confirmScope.isVisible({ timeout: 2000 }).catch(() => false)) {
      await confirmScope.click({ timeout: 5000 }).catch(() => undefined);
    }
  }

  // 提交创建
  const submit = root.getByText(/创建|确\s*定/).last();
  if (!(await submit.isVisible({ timeout: 3000 }).catch(() => false))) {
    throw new Error("创建弹窗未找到提交按钮。");
  }
  await submit.click({ timeout: config.timeoutMs });
  await page.waitForTimeout(6000);

  // 创建后可能直达详情，也可能回列表
  if (page.url().includes("aibotid=")) return config.botName;
  const names = await listExistingBots(page, config);
  const created = names.find((n) => n.toLowerCase() === config.botName.toLowerCase()) ?? names[0];
  if (!created) {
    const takeover = isNonInteractive()
      ? "创建提交后未在列表中发现新机器人（非交互环境）。"
      : await promptText(prompt, `创建提交后未自动确认，若列表已出现新机器人请输入其名称（候选：${names.join("、") || "无"}），否则直接回车：`);
    if (!takeover) throw new Error("创建流程未确认成功，请人工在浏览器中完成创建后重跑本脚本（已有机器人会自动复用）。");
    return takeover;
  }
  result.warnings.push("创建流程为防御式实现（未真机全量验证），请核对详情页配置。");
  return created;
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
    logger.info("正在确认管理后台登录状态...");
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

    // Step 4: 详情页读取凭据
    logger.info(`正在打开机器人「${chosen}」详情页...`);
    try {
      await openBotDetail(page, config, chosen);
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

main().catch((e) => {
  logger.error(String(e));
  process.exitCode = 1;
});
