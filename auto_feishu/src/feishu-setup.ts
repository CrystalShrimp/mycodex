import { access, mkdir, open, readFile, writeFile, rm } from "node:fs/promises";
import { execFileSync, spawn } from "node:child_process";
import path from "node:path";
import { stdin as input, stdout as output } from "node:process";
import readline from "node:readline/promises";
import { chromium, type Browser, type BrowserContext, type Locator, type Page } from "playwright";

type LogLevel = "info" | "debug";

interface RawConfig {
  appName: string;
  appDescription: string;
  appIconPath?: string;
  botName: string;
  reuseStartedApp?: boolean;
  permissionsImportJsonPath: string;
  enableGroupMessagePermission: boolean;
  enableEventSubscription: boolean;
  eventName?: string;
  eventNames?: string[];
  publishAfterSetup: boolean;
  resultPath: string;
  envPath?: string;
  localServiceUrl?: string;
  localServiceRootDir?: string;
  startLocalService?: boolean;
  localServiceWaitMs?: number;
  loginTimeoutMs?: number;
  screenshotsDir: string;
  htmlDumpDir: string;
  storageStatePath?: string;
  userDataDirPath?: string;
  timeoutMs: number;
}

interface LoadedConfig extends RawConfig {
  rootDir: string;
  appIconPath?: string;
  permissionsImportJsonPath: string;
  resultPath: string;
  eventNames: string[];
  envPath: string;
  localServiceUrl: string;
  localServiceRootDir: string;
  startLocalService: boolean;
  localServiceWaitMs: number;
  loginTimeoutMs: number;
  screenshotsDir: string;
  htmlDumpDir: string;
  storageStatePath: string;
  userDataDirPath: string;
}

interface ResultState {
  appName: string;
  appDescription: string;
  createdAt: string;
  updatedAt: string;
  appId: string | null;
  maskedSecret: string | null;
  existingAppReused: boolean;
  existingStartedAppChosen: boolean;
  reusedAppName: string | null;
  loginCompleted: boolean;
  permissionsImported: boolean;
  botEnabled: boolean;
  eventSubscriptionConfigured: boolean;
  published: boolean;
  lastCompletedStep: string | null;
  status: "pending" | "partial" | "completed" | "failed";
  nextSteps: string[];
  warnings: string[];
  artifacts: Array<{
    step: string;
    screenshotPath: string | null;
    htmlPath: string | null;
    createdAt: string;
  }>;
  failure: null | {
    step: string;
    message: string;
    recoverable: boolean;
  };
}

interface Logger {
  info(message: string): void;
  debug(message: string): void;
  warn(message: string): void;
  error(message: string): void;
}

interface StepContext {
  browser: Browser | null;
  context: BrowserContext;
  page: Page;
  config: LoadedConfig;
  result: ResultState;
  runtimeAppSecret: string | null;
  logger: Logger;
  prompt: readline.Interface;
}

class AutomationStepError extends Error {
  readonly step: string;
  readonly recoverable: boolean;
  readonly screenshotPath: string | null;
  readonly htmlPath: string | null;

  constructor(options: {
    step: string;
    message: string;
    recoverable: boolean;
    screenshotPath: string | null;
    htmlPath: string | null;
  }) {
    super(options.message);
    this.name = "AutomationStepError";
    this.step = options.step;
    this.recoverable = options.recoverable;
    this.screenshotPath = options.screenshotPath;
    this.htmlPath = options.htmlPath;
  }
}

const DEBUG_ENABLED = process.argv.includes("--debug");
const DEAD_PROXY_PATTERN = /^(?:https?:\/\/)?127\.0\.0\.1:6984\/?$/i;

function clearDeadProxyEnvironment(logger: Logger): void {
  const proxyKeys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"];
  const cleared: string[] = [];

  for (const key of proxyKeys) {
    const value = process.env[key]?.trim();
    if (value && DEAD_PROXY_PATTERN.test(value)) {
      delete process.env[key];
      cleared.push(key);
    }
  }

  if (cleared.length > 0) {
    logger.info("已忽略失效本地代理 127.0.0.1:6984：" + cleared.join(", "));
  }
}
const LOGIN_SIGNALS = [
  "控制台",
  "开发者后台",
  "开发者平台",
  "应用管理",
  "创建应用",
  "企业自建应用"
];
const APP_BACKEND_SIGNALS = [
  "凭证与基础信息",
  "权限管理",
  "应用能力",
  "版本管理与发布",
  "机器人",
  "事件订阅"
];

function createLogger(level: LogLevel): Logger {
  const write = (label: string, message: string): void => {
    const ts = new Date().toISOString().replace("T", " ").replace("Z", "");
    output.write(`[${ts}] [${label}] ${message}\n`);
  };

  return {
    info(message: string) {
      write("INFO", message);
    },
    debug(message: string) {
      if (level === "debug") {
        write("DEBUG", message);
      }
    },
    warn(message: string) {
      write("WARN", message);
    },
    error(message: string) {
      write("ERROR", message);
    }
  };
}

function nowIso(): string {
  return new Date().toISOString();
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function toRegex(value: string): RegExp {
  return new RegExp(escapeRegExp(value), "i");
}

function resolveFromRoot(rootDir: string, candidate?: string): string | undefined {
  if (!candidate) {
    return undefined;
  }

  if (path.isAbsolute(candidate)) {
    return candidate;
  }

  return path.resolve(rootDir, candidate);
}

async function pathExists(candidate?: string): Promise<boolean> {
  if (!candidate) {
    return false;
  }

  try {
    await access(candidate);
    return true;
  } catch {
    return false;
  }
}

async function ensureDir(dirPath: string): Promise<void> {
  await mkdir(dirPath, { recursive: true });
}

function maskSecret(secret: string | null): string | null {
  if (!secret) {
    return null;
  }

  if (secret.length <= 8) {
    return "*".repeat(secret.length);
  }

  return `${secret.slice(0, 4)}****${secret.slice(-4)}`;
}

function buildNextSteps(result: ResultState): string[] {
  const nextSteps = new Set<string>();

  if (result.appId) {
    nextSteps.add("飞书凭据已写入本地根目录 .env。");
  } else {
    nextSteps.add("回到飞书开放平台补全并记录 App ID / App Secret。");
  }

  if (!result.eventSubscriptionConfigured) {
    nextSteps.add("先在 myclaw 中添加 Feishu 渠道并启动网关，再返回飞书重试事件订阅。");
  } else {
    nextSteps.add("启动 myclaw 网关。");
  }

  if (!result.published) {
    nextSteps.add("回到飞书开放平台完成版本发布或组织审批。");
  }

  nextSteps.add("在飞书里测试机器人。");
  return [...nextSteps];
}

async function persistResult(config: LoadedConfig, result: ResultState): Promise<void> {
  result.updatedAt = nowIso();
  result.nextSteps = buildNextSteps(result);
  const safeResult = { ...result } as ResultState & { appSecret?: unknown };
  delete safeResult.appSecret;
  await writeFile(config.resultPath, JSON.stringify(safeResult, null, 2), "utf8");
}

async function createInitialResult(config: LoadedConfig): Promise<ResultState> {
  const initial: ResultState = {
    appName: config.appName,
    appDescription: config.appDescription,
    createdAt: nowIso(),
    updatedAt: nowIso(),
    appId: null,
    maskedSecret: null,
    existingAppReused: false,
    existingStartedAppChosen: false,
    reusedAppName: null,
    loginCompleted: false,
    permissionsImported: false,
    botEnabled: false,
    eventSubscriptionConfigured: false,
    published: false,
    lastCompletedStep: null,
    status: "pending",
    nextSteps: [],
    warnings: [],
    artifacts: [],
    failure: null
  };

  try {
    const previous = JSON.parse(await readFile(config.resultPath, "utf8")) as Partial<ResultState> & { appSecret?: unknown };
    if (!previous?.appId) {
      return initial;
    }

    const { appSecret: _legacySecret, ...safePrevious } = previous;
    return {
      ...initial,
      ...safePrevious,
      updatedAt: nowIso(),
      status: "pending",
      failure: null,
      nextSteps: previous.nextSteps ?? [],
      warnings: previous.warnings ?? [],
      artifacts: previous.artifacts ?? []
    };
  } catch {
    return initial;
  }
}

async function loadConfig(rootDir: string): Promise<LoadedConfig> {
  const configPath = path.join(rootDir, "config.json");
  const raw = JSON.parse(await readFile(configPath, "utf8")) as RawConfig;

  return {
    ...raw,
    rootDir,
    reuseStartedApp: raw.reuseStartedApp ?? true,
    appIconPath: resolveFromRoot(rootDir, raw.appIconPath),
    permissionsImportJsonPath: resolveFromRoot(rootDir, raw.permissionsImportJsonPath) ?? "",
    resultPath: resolveFromRoot(rootDir, raw.resultPath) ?? path.join(rootDir, "feishu-app-result.json"),
    envPath: resolveFromRoot(rootDir, raw.envPath) ?? path.resolve(rootDir, "..", ".env"),
    localServiceUrl: raw.localServiceUrl ?? "http://127.0.0.1:8080/health",
    localServiceRootDir: resolveFromRoot(rootDir, raw.localServiceRootDir) ?? path.resolve(rootDir, ".."),
    eventNames: raw.eventNames?.length ? raw.eventNames : [raw.eventName ?? "im.message.receive_v1", "card.action.trigger"],
    startLocalService: raw.startLocalService ?? true,
    localServiceWaitMs: raw.localServiceWaitMs ?? 30000,
    loginTimeoutMs: raw.loginTimeoutMs ?? 600000,
    screenshotsDir: resolveFromRoot(rootDir, raw.screenshotsDir) ?? path.join(rootDir, "artifacts", "screenshots"),
    htmlDumpDir: resolveFromRoot(rootDir, raw.htmlDumpDir) ?? path.join(rootDir, "artifacts", "html"),
    storageStatePath:
      resolveFromRoot(rootDir, raw.storageStatePath) ?? path.join(rootDir, "artifacts", "feishu-storage-state.json"),
    userDataDirPath:
      resolveFromRoot(rootDir, raw.userDataDirPath) ?? path.join(rootDir, "artifacts", "feishu-user-data")
  };
}

function slugify(value: string): string {
  const slug = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");

  return slug || "step";
}

async function saveArtifacts(page: Page, config: LoadedConfig, step: string): Promise<{ screenshotPath: string | null; htmlPath: string | null }> {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const fileBase = `${stamp}-${slugify(step)}`;
  const screenshotPath = path.join(config.screenshotsDir, `${fileBase}.png`);
  const htmlPath = path.join(config.htmlDumpDir, `${fileBase}.html`);

  let savedScreenshot: string | null = null;
  let savedHtml: string | null = null;

  try {
    await page.screenshot({ path: screenshotPath, fullPage: true });
    savedScreenshot = screenshotPath;
  } catch {
    savedScreenshot = null;
  }

  try {
    await writeFile(htmlPath, await page.content(), "utf8");
    savedHtml = htmlPath;
  } catch {
    savedHtml = null;
  }

  return { screenshotPath: savedScreenshot, htmlPath: savedHtml };
}

async function captureAndWrapError(
  ctx: StepContext,
  step: string,
  error: unknown,
  recoverable: boolean
): Promise<AutomationStepError> {
  if (error instanceof AutomationStepError) {
    return error;
  }

  const message = error instanceof Error ? error.message : String(error);
  const artifacts = await saveArtifacts(ctx.page, ctx.config, step);

  return new AutomationStepError({
    step,
    message,
    recoverable,
    screenshotPath: artifacts.screenshotPath,
    htmlPath: artifacts.htmlPath
  });
}

async function waitForEnter(prompt: readline.Interface, message: string): Promise<void> {
  if (!input.isTTY || !output.isTTY) {
    throw new Error("当前为非交互环境，无法等待终端输入。");
  }
  await prompt.question(`${message}\n`);
}

async function promptText(prompt: readline.Interface, message: string): Promise<string> {
  if (!input.isTTY || !output.isTTY) {
    throw new Error("当前为非交互环境，无法读取终端输入。");
  }
  const answer = await prompt.question(`${message}\n`);
  return answer.trim();
}

async function promptYesNo(prompt: readline.Interface, message: string, defaultYes = true): Promise<boolean> {
  const suffix = defaultYes ? "[Y/n]" : "[y/N]";
  const answer = (await promptText(prompt, `${message} ${suffix}`)).toLowerCase();
  if (!answer) {
    return defaultYes;
  }

  return ["y", "yes", "1"].includes(answer);
}

async function listClickableTexts(page: Page): Promise<string[]> {
  const items = await page.evaluate(() => {
    const selectors = [
      "button",
      "[role='button']",
      "a",
      "[role='link']",
      "[role='tab']",
      "[role='menuitem']",
      "input[type='button']",
      "input[type='submit']"
    ].join(",");

    const visible = (element: Element): boolean => {
      const htmlElement = element as HTMLElement;
      const rect = htmlElement.getBoundingClientRect();
      const style = window.getComputedStyle(htmlElement);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };

    const texts = new Set<string>();
    for (const element of document.querySelectorAll(selectors)) {
      if (!visible(element)) {
        continue;
      }

      const htmlElement = element as HTMLElement;
      const raw = (
        htmlElement.innerText ||
        htmlElement.getAttribute("aria-label") ||
        htmlElement.getAttribute("title") ||
        (htmlElement as HTMLInputElement).value ||
        ""
      ).trim();

      if (raw) {
        texts.add(raw.replace(/\s+/g, " "));
      }
    }

    return Array.from(texts).slice(0, 40);
  });

  return items;
}

async function listClickableTextsV2(page: Page): Promise<string[]> {
  const selectors = [
    "button",
    "[role='button']",
    "a",
    "[role='link']",
    "[role='tab']",
    "[role='menuitem']",
    "input[type='button']",
    "input[type='submit']"
  ].join(", ");
  const locators = page.locator(selectors);
  const count = Math.min(await locators.count(), 80);
  const texts = new Set<string>();

  for (let index = 0; index < count; index += 1) {
    const locator = locators.nth(index);

    try {
      if (!(await locator.isVisible())) {
        continue;
      }

      const raw = (
        (await locator.innerText().catch(() => "")) ||
        (await locator.getAttribute("aria-label")) ||
        (await locator.getAttribute("title")) ||
        (await locator.inputValue().catch(() => "")) ||
        ""
      ).trim();

      if (raw) {
        texts.add(raw.replace(/\s+/g, " "));
      }
    } catch {
      continue;
    }
  }

  return Array.from(texts).slice(0, 40);
}

async function manualTakeover(ctx: StepContext, title: string, actions: string[]): Promise<void> {
  const detail = actions.join(" ");
  throw new Error(
    title + " 无法自动完成。" + detail + " 请检查组织审批、验证码、页面权限或页面版本；排错资料会保存到 artifacts。"
  );
}
async function firstVisibleLocator(locators: Locator[], timeoutMs: number): Promise<Locator | null> {
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    for (const locator of locators) {
      try {
        if (await locator.count()) {
          const candidate = locator.first();
          if (await candidate.isVisible({ timeout: 500 })) {
            return candidate;
          }
        }
      } catch {
        continue;
      }
    }

    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  return null;
}

async function findVisibleModal(page: Page, timeoutMs = 3000): Promise<Locator | null> {
  const candidates = [
    page.locator(".ud__modal"),
    page.locator("[role='dialog']"),
    page.locator(".semi-modal"),
    page.locator(".arco-modal"),
    page.locator(".modal")
  ];

  return firstVisibleLocator(candidates, timeoutMs);
}

function buildActionLocators(root: Page | Locator, text: string): Locator[] {
  const regex = toRegex(text);
  return [
    root.getByRole("button", { name: regex }),
    root.getByRole("link", { name: regex }),
    root.getByRole("tab", { name: regex }),
    root.getByRole("menuitem", { name: regex }),
    root.getByRole("option", { name: regex }),
    root.getByRole("radio", { name: regex }),
    root.getByRole("switch", { name: regex }),
    root.getByLabel(regex),
    root.getByText(regex)
  ];
}

async function clickByCandidates(root: Page | Locator, texts: string[], timeoutMs: number, logger: Logger): Promise<string | null> {
  await dismissKnownBlockingPopups(getPageFromRoot(root), logger);
  for (const text of texts) {
    const locator = await firstVisibleLocator(buildActionLocators(root, text), Math.min(timeoutMs, 2500));
    if (!locator) {
      continue;
    }

    const disabledAttr = await locator.getAttribute("disabled").catch(() => null);
    const ariaDisabled = await locator.getAttribute("aria-disabled").catch(() => null);
    let disabled = disabledAttr !== null || (ariaDisabled || "").toLowerCase() === "true";
    if (!disabled) {
      disabled = await locator.isDisabled().catch(() => false);
    }
    if (disabled) {
      logger.debug(`跳过不可点击项：${text}`);
      continue;
    }

    try {
      await locator.scrollIntoViewIfNeeded();
      await locator.click({ timeout: timeoutMs });
      logger.debug(`点击成功：${text}`);
      return text;
    } catch {
      continue;
    }
  }

  return null;
}

async function waitForEnabledAction(
  root: Page | Locator,
  texts: string[],
  timeoutMs: number
): Promise<{ locator: Locator; text: string } | null> {
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    for (const text of texts) {
      const locator = await firstVisibleLocator(buildActionLocators(root, text), 800);
      if (!locator) {
        continue;
      }

      const disabledAttr = await locator.getAttribute("disabled").catch(() => null);
      const ariaDisabled = await locator.getAttribute("aria-disabled").catch(() => null);
      let disabled = disabledAttr !== null || (ariaDisabled || "").toLowerCase() === "true";
      if (!disabled) {
        disabled = await locator.isDisabled().catch(() => false);
      }

      if (!disabled) {
        return { locator, text };
      }
    }

    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  return null;
}

async function readPermissionEditorPayload(root: Page | Locator): Promise<string | null> {
  const monacoLines = root.locator(".monaco-editor .view-line");
  const monacoLineCount = Math.min(await monacoLines.count().catch(() => 0), 400);
  if (monacoLineCount > 0) {
    const chunks: string[] = [];
    for (let index = 0; index < monacoLineCount; index += 1) {
      const line = monacoLines.nth(index);
      try {
        if (!(await line.isVisible({ timeout: 200 }))) {
          continue;
        }
        const text = ((await line.innerText().catch(() => "")) || (await line.textContent().catch(() => "")) || "").trimEnd();
        if (text) {
          chunks.push(text);
        }
      } catch {
        continue;
      }
    }

    const joined = chunks.join("\n").trim();
    if (joined.includes('"scopes"') || joined.includes('"tenant"') || joined.includes('"user"')) {
      return joined;
    }
  }

  const selectors = [
    ".monaco-editor textarea.inputarea",
    "textarea.inputarea",
    "[role='textbox'][aria-roledescription='editor']",
    "textarea",
    ".monaco-editor .view-lines",
    ".monaco-editor .view-line",
    ".monaco-editor",
    "[contenteditable='true']"
  ];

  for (const selector of selectors) {
    const locator = root.locator(selector);
    let count = 0;
    try {
      count = await locator.count();
    } catch {
      count = 0;
    }

    for (let index = 0; index < count; index += 1) {
      const item = locator.nth(index);
      try {
        if (!(await item.isVisible({ timeout: 500 }))) {
          continue;
        }

        const value =
          (await item.inputValue().catch(() => "")) ||
          (await item.innerText().catch(() => "")) ||
          (await item.textContent().catch(() => "")) ||
          "";

        const normalized = value.trim();
        if (normalized.includes('"scopes"') || normalized.includes('"tenant"') || normalized.includes('"user"')) {
          return normalized;
        }
      } catch {
        continue;
      }
    }
  }

  return null;
}

function getPageFromRoot(root: Page | Locator): Page {
  return typeof (root as Locator).page === "function" ? (root as Locator).page() : (root as Page);
}

function writeSystemClipboardText(text: string): boolean {
  try {
    if (process.platform === "win32") {
      execFileSync("powershell.exe", ["-NoProfile", "-Command", "$input | Set-Clipboard"], {
        input: text,
        encoding: "utf8",
        stdio: ["pipe", "ignore", "ignore"]
      });
      return true;
    }
  } catch {
    return false;
  }

  return false;
}

async function dismissKnownBlockingPopups(page: Page, logger: Logger): Promise<boolean> {
  const acknowledgeTexts = ["我知道了", "知道了", "明白了"];
  const popupSignals = ["权限升级引导说明", "权限升级", "升级引导"];
  const roots: Array<Page | Locator> = [page];

  const modal = await findVisibleModal(page, 800).catch(() => null);
  if (modal) {
    roots.unshift(modal);
  }

  for (const root of roots) {
    let popupVisible: string | null = null;
    for (const signal of popupSignals) {
      const signalLocator = await firstVisibleLocator(buildActionLocators(root, signal), 500);
      if (signalLocator) {
        popupVisible = signal;
        break;
      }
    }
    if (!popupVisible && root !== page) {
      continue;
    }

    for (const text of acknowledgeTexts) {
      const locator = await firstVisibleLocator(buildActionLocators(root, text), 800);
      if (!locator) {
        continue;
      }

      try {
        await locator.scrollIntoViewIfNeeded();
        await locator.click({ timeout: 1500 });
        await page.waitForTimeout(500);
        logger.info(`已自动关闭弹窗：${text}`);
        return true;
      } catch {
        continue;
      }
    }
  }

  return false;
}

async function waitForAnyText(root: Page | Locator, texts: string[], timeoutMs: number): Promise<string | null> {
  await dismissKnownBlockingPopups(getPageFromRoot(root), createLogger(DEBUG_ENABLED ? "debug" : "info"));
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    for (const text of texts) {
      const locator = await firstVisibleLocator(buildActionLocators(root, text), 800);
      if (locator) {
        return text;
      }
    }

    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  return null;
}

async function fillTextbox(
  root: Page | Locator,
  labels: string[],
  value: string,
  timeoutMs: number,
  options: { genericSelectors?: string[] } = {}
): Promise<string | null> {
  await dismissKnownBlockingPopups(getPageFromRoot(root), createLogger(DEBUG_ENABLED ? "debug" : "info"));
  for (const label of labels) {
    const regex = toRegex(label);
    const locators = [
      root.getByLabel(regex),
      root.getByPlaceholder(regex),
      root.getByRole("textbox", { name: regex })
    ];

    const locator = await firstVisibleLocator(locators, Math.min(timeoutMs, 2000));
    if (!locator) {
      continue;
    }

    try {
      await locator.fill(value, { timeout: timeoutMs });
      return label;
    } catch {
      continue;
    }
  }

  const genericSelectors = options.genericSelectors ?? ["textarea, input[type='text']"];
  for (const selector of genericSelectors) {
    const genericTextboxes = root.locator(selector);
    const textboxCount = await genericTextboxes.count();
    for (let index = 0; index < textboxCount; index += 1) {
      const locator = genericTextboxes.nth(index);
      try {
        if (await locator.isVisible({ timeout: 500 })) {
          await locator.fill(value, { timeout: timeoutMs });
          return `generic:${selector}`;
        }
      } catch {
        continue;
      }
    }
  }

  return null;
}

async function setFileIfPresent(root: Page | Locator, filePath: string, logger: Logger): Promise<boolean> {
  const fileInputs = root.locator("input[type='file']");
  const count = await fileInputs.count();

  for (let index = 0; index < count; index += 1) {
    const locator = fileInputs.nth(index);
    try {
      if (await locator.isVisible({ timeout: 1000 })) {
        await locator.setInputFiles(filePath);
        logger.info(`已上传图标文件：${filePath}`);
        return true;
      }
    } catch {
      continue;
    }
  }

  return false;
}

async function extractValueNearLabels(page: Page, labels: string[]): Promise<string | null> {
  return page.evaluate((labelCandidates) => {
    const normalizedLabels = labelCandidates.map((label) => label.toLowerCase());

    const visible = (element: Element | null): boolean => {
      if (!element) {
        return false;
      }

      const htmlElement = element as HTMLElement;
      const rect = htmlElement.getBoundingClientRect();
      const style = window.getComputedStyle(htmlElement);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };

    const tokenPattern = /[A-Za-z0-9._:-]{6,}/;

    const tryReadElement = (element: Element | null): string | null => {
      if (!element || !visible(element)) {
        return null;
      }

      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        const value = element.value.trim();
        return tokenPattern.test(value) ? value : null;
      }

      const text = (element.textContent || "").trim().replace(/\s+/g, " ");
      if (!text || normalizedLabels.some((label) => text.toLowerCase() === label)) {
        return null;
      }

      return tokenPattern.test(text) ? text : null;
    };

    const allElements = Array.from(document.querySelectorAll("label, span, div, td, th, p, strong, h1, h2, h3"));
    for (const element of allElements) {
      if (!visible(element)) {
        continue;
      }

      const labelText = (element.textContent || "").trim().toLowerCase();
      if (!normalizedLabels.some((label) => labelText.includes(label))) {
        continue;
      }

      const related: Array<Element | null> = [
        element.nextElementSibling,
        element.previousElementSibling,
        element.parentElement,
        element.parentElement?.nextElementSibling ?? null
      ];

      for (const candidate of related) {
        const direct = tryReadElement(candidate);
        if (direct) {
          return direct;
        }

        for (const nested of candidate?.querySelectorAll("input, textarea, code, pre, span, div") || []) {
          const nestedValue = tryReadElement(nested);
          if (nestedValue) {
            return nestedValue;
          }
        }
      }
    }

    const pageText = document.body.innerText || "";
    for (const label of normalizedLabels) {
      const regex = new RegExp(`${label}\\s*[:：]?\\s*([A-Za-z0-9._:-]{6,})`, "i");
      const match = pageText.match(regex);
      if (match?.[1]) {
        return match[1];
      }
    }

    return null;
  }, labels);
}

async function extractValueNearLabelsV2(page: Page, labels: string[]): Promise<string | null> {
  const normalizedLabels = labels.map((label) => label.toLowerCase());
  const tokenPattern = /[A-Za-z0-9._:-]{6,}/;
  const targets = page.locator("label, span, div, td, th, p, strong, h1, h2, h3");
  const count = Math.min(await targets.count(), 200);

  for (let index = 0; index < count; index += 1) {
    const target = targets.nth(index);
    let labelText = "";

    try {
      if (!(await target.isVisible())) {
        continue;
      }
      labelText = ((await target.innerText()) || "").trim().toLowerCase();
    } catch {
      continue;
    }

    if (!labelText || !normalizedLabels.some((label) => labelText.includes(label))) {
      continue;
    }

    const candidates = [
      target.locator("xpath=following-sibling::*[1]"),
      target.locator("xpath=ancestor::*[1]//*[self::input or self::textarea][1]"),
      target.locator("xpath=ancestor::*[1]//*[contains(@class,'value')][1]"),
      target.locator("xpath=ancestor::*[1]//*[contains(@data-testid,'value')][1]"),
      target.locator("xpath=ancestor::*[1]/following-sibling::*[1]")
    ];

    for (const candidateGroup of candidates) {
      const innerCount = Math.min(await candidateGroup.count().catch(() => 0), 3);
      for (let innerIndex = 0; innerIndex < innerCount; innerIndex += 1) {
        const candidate = candidateGroup.nth(innerIndex);
        try {
          if (!(await candidate.isVisible())) {
            continue;
          }

          const text =
            ((await candidate.inputValue().catch(() => "")) || (await candidate.innerText().catch(() => "")) || "").trim().replace(/\s+/g, " ");
          if (text && !normalizedLabels.some((label) => text.toLowerCase() === label) && tokenPattern.test(text)) {
            return text;
          }
        } catch {
          continue;
        }
      }
    }
  }

  return null;
}

function isMaskedSecret(value: string | null): boolean {
  if (!value) {
    return true;
  }

  const normalized = value.replace(/\s+/g, "");
  return normalized.length > 0 && /^[*＊∗•·]+$/.test(normalized);
}

async function readClipboardText(ctx: StepContext): Promise<string | null> {
  try {
    await ctx.context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: "https://open.feishu.cn" });
  } catch {
    return null;
  }

  try {
    const value = await ctx.page.evaluate(async () => {
      return navigator.clipboard ? await navigator.clipboard.readText() : "";
    });
    const normalized = value.trim();
    return normalized || null;
  } catch {
    return null;
  }
}

async function tryReadAppIdFromCredentialRow(ctx: StepContext): Promise<string | null> {
  const appIdRow = ctx.page.locator(".auth-info__appid").first();
  if ((await appIdRow.count().catch(() => 0)) === 0) {
    return null;
  }

  const appIdCode = appIdRow.locator(".secret-code__code").first();
  const readVisibleAppId = async (): Promise<string | null> => {
    const text = await appIdCode.innerText().then((value) => value.trim()).catch(() => "");
    if (!text || /secret/i.test(text)) {
      return null;
    }
    return text;
  };

  const directVisible = await readVisibleAppId();
  if (directVisible) {
    return directVisible;
  }

  const copyButton = appIdRow
    .locator("[data-icon='CopyOutlined']")
    .locator("xpath=ancestor::*[contains(@class,'secret-code__btn') or self::button or self::span][1]")
    .first();

  if ((await copyButton.count().catch(() => 0)) > 0) {
    try {
      await copyButton.click({ timeout: 3000 });
      await ctx.page.waitForTimeout(500);
      const clipboardAppId = await readClipboardText(ctx);
      if (clipboardAppId && !/secret/i.test(clipboardAppId)) {
        ctx.logger.debug("已通过复制按钮读取 App ID。");
        return clipboardAppId;
      }
    } catch {
      ctx.logger.debug("点击 App ID 复制按钮失败。");
    }
  }

  return null;
}

async function tryReadSecretFromCredentialRow(ctx: StepContext): Promise<string | null> {
  const secretRow = ctx.page.locator(".auth-info__secret").first();
  if ((await secretRow.count().catch(() => 0)) === 0) {
    return null;
  }

  const secretCode = secretRow.locator(".secret-code__code").first();
  const readVisibleSecret = async (): Promise<string | null> => {
    const text = await secretCode.innerText().then((value) => value.trim()).catch(() => "");
    if (!text || isMaskedSecret(text)) {
      return null;
    }
    return text;
  };

  const directVisible = await readVisibleSecret();
  if (directVisible) {
    return directVisible;
  }

  const visibleButton = secretRow
    .locator("[data-icon='VisibleOutlined']")
    .locator("xpath=ancestor::*[contains(@class,'secret-code__btn') or self::button or self::span][1]")
    .first();

  if ((await visibleButton.count().catch(() => 0)) > 0) {
    try {
      await visibleButton.click({ timeout: 3000 });
      await ctx.page.waitForTimeout(800);
      const revealed = await readVisibleSecret();
      if (revealed) {
        ctx.logger.debug("已通过显示按钮读取 App Secret。");
        return revealed;
      }
    } catch {
      ctx.logger.debug("点击 App Secret 显示按钮失败。");
    }
  }

  const copyButton = secretRow
    .locator("[data-icon='CopyOutlined']")
    .locator("xpath=ancestor::*[contains(@class,'secret-code__btn') or self::button or self::span][1]")
    .first();

  if ((await copyButton.count().catch(() => 0)) > 0) {
    try {
      await copyButton.click({ timeout: 3000 });
      await ctx.page.waitForTimeout(500);
      const clipboardSecret = await readClipboardText(ctx);
      if (clipboardSecret && !isMaskedSecret(clipboardSecret)) {
        ctx.logger.debug("已通过复制按钮读取 App Secret。");
        return clipboardSecret;
      }
    } catch {
      ctx.logger.debug("点击 App Secret 复制按钮失败。");
    }
  }

  return null;
}

async function gotoWithConfirmation(ctx: StepContext, urls: string[], confirmationTexts: string[], description: string): Promise<void> {
  for (const url of urls) {
    try {
      ctx.logger.debug(`尝试打开：${url}`);
      await ctx.page.goto(url, { waitUntil: "domcontentloaded", timeout: ctx.config.timeoutMs });
      const signal = await waitForAnyText(ctx.page, confirmationTexts, Math.max(4000, ctx.config.timeoutMs));
      if (signal) {
        ctx.logger.debug(`页面确认成功：${description} -> ${signal}`);
        return;
      }
    } catch {
      continue;
    }
  }

  await manualTakeover(ctx, `${description} 页面未自动确认`, [
    `请在浏览器中手动打开或切换到：${description}。`,
    "确认页面中能看到对应入口后，再返回终端。"
  ]);
}

async function executeStep<T>(
  ctx: StepContext,
  step: string,
  fn: () => Promise<T>,
  options: { recoverable?: boolean } = {}
): Promise<T | undefined> {
  const recoverable = options.recoverable ?? false;
  ctx.logger.info(`正在执行：${step}`);

  try {
    const result = await fn();
    ctx.result.lastCompletedStep = step;
    if (ctx.result.status === "pending") {
      ctx.result.status = "partial";
    }
    ctx.result.failure = null;
    await persistResult(ctx.config, ctx.result);
    ctx.logger.info(`已完成：${step}`);
    return result;
  } catch (error) {
    const wrapped = await captureAndWrapError(ctx, step, error, recoverable);
    ctx.result.failure = {
      step: wrapped.step,
      message: wrapped.message,
      recoverable: wrapped.recoverable
    };
    ctx.result.artifacts.push({
      step,
      screenshotPath: wrapped.screenshotPath,
      htmlPath: wrapped.htmlPath,
      createdAt: nowIso()
    });

    if (recoverable) {
      ctx.result.status = "partial";
      ctx.result.warnings.push(`${step}：${wrapped.message}`);
      await persistResult(ctx.config, ctx.result);
      ctx.logger.warn(`${step} 失败，但流程继续：${wrapped.message}`);
      if (wrapped.screenshotPath || wrapped.htmlPath) {
        ctx.logger.warn(`已保存排错资料：${wrapped.screenshotPath ?? "无截图"} | ${wrapped.htmlPath ?? "无HTML"}`);
      }
      return undefined;
    }

    ctx.result.status = "failed";
    await persistResult(ctx.config, ctx.result);
    throw wrapped;
  }
}

async function waitForLogin(ctx: StepContext): Promise<void> {
  await ctx.page.goto("https://open.feishu.cn/app", {
    waitUntil: "domcontentloaded",
    timeout: ctx.config.timeoutMs
  });

  let signal = await waitForAnyText(ctx.page, LOGIN_SIGNALS, Math.min(ctx.config.timeoutMs, 5000));
  if (!signal) {
    ctx.logger.info(
      "请在已打开的浏览器中完成唯一一次飞书登录（包括验证码、2FA 或组织确认）；登录成功后脚本会自动继续。"
    );
    const deadline = Date.now() + ctx.config.loginTimeoutMs;
    while (!signal && Date.now() < deadline) {
      await ctx.page.waitForTimeout(1000);
      signal = await waitForAnyText(ctx.page, LOGIN_SIGNALS, 1500);
    }
  }

  if (!signal) {
    throw new Error(
      "等待飞书登录超时。重新运行脚本会复用当前浏览器登录状态，并从结果文件中的步骤继续。"
    );
  }

  ctx.logger.info("已检测到登录后的页面特征：" + signal);
  ctx.result.loginCompleted = true;
  await ctx.context.storageState({ path: ctx.config.storageStatePath }).catch(() => undefined);
}
async function openAppWorkbench(ctx: StepContext): Promise<void> {
  await gotoWithConfirmation(
    ctx,
    [
      "https://open.feishu.cn/app",
      "https://open.feishu.cn/app?lang=zh-CN",
      "https://open.feishu.cn/"
    ],
    [...LOGIN_SIGNALS, ctx.config.appName],
    "应用管理"
  );
}

interface StartedAppCandidate {
  name: string;
  status: string;
  containerText: string;
}

function isNonInteractivePrompt(): boolean {
  return !process.stdin.isTTY || !process.stdout.isTTY;
}

async function hasExactStartedTag(root: Locator): Promise<boolean> {
  const acceptedStatuses = ["已启动", "已启用"];
  const locators = [
    ...acceptedStatuses.map((status) => root.getByText(new RegExp(`^${escapeRegExp(status)}$`))),
    ...acceptedStatuses.map((status) =>
      root.locator("[class*='tag'], [class*='status'], .ud__tag").getByText(new RegExp(`^${escapeRegExp(status)}$`))
    )
  ];

  for (const locator of locators) {
    try {
      if ((await locator.count()) > 0 && (await locator.first().isVisible({ timeout: 500 }))) {
        return true;
      }
    } catch {
      continue;
    }
  }

  try {
    const text = await root.innerText();
    return acceptedStatuses.some((status) => text.includes(status));
  } catch {
    return false;
  }
}

async function countVisibleAppCardsByName(page: Page, appName: string): Promise<number> {
  const cards = page.locator(".app-card, [class*='app-card']");
  const count = Math.min(await cards.count(), 20);
  let visibleCount = 0;

  for (let index = 0; index < count; index += 1) {
    try {
      const card = cards.nth(index);
      if (!(await card.isVisible())) {
        continue;
      }

      const title = await card
        .locator(".app-card__title, [class*='app-card__title']")
        .first()
        .innerText()
        .then((value) => value.trim())
        .catch(() => "");

      if (title === appName) {
        visibleCount += 1;
      }
    } catch {
      continue;
    }
  }

  return visibleCount;
}

async function clickSelfBuiltAppsTab(ctx: StepContext): Promise<boolean> {
  const locators = [
    ctx.page.getByRole("tab", { name: /^企业自建应用$/ }),
    ctx.page.getByRole("button", { name: /^企业自建应用$/ }),
    ctx.page.getByRole("link", { name: /^企业自建应用$/ }),
    ctx.page.getByText(/^企业自建应用$/)
  ];

  const locator = await firstVisibleLocator(locators, 5000);
  if (!locator) {
    return false;
  }

  await locator.click({ timeout: ctx.config.timeoutMs });
  await ctx.page.waitForTimeout(1500);
  ctx.logger.debug("已切换到“企业自建应用”列表。");
  return true;
}

async function openCreateSelfBuiltAppEntry(ctx: StepContext): Promise<boolean> {
  const locators = [
    ctx.page.getByRole("button", { name: /创建企业自建应用|创建应用|创建自建应用|创建/ }),
    ctx.page.getByRole("link", { name: /创建企业自建应用|创建应用|创建自建应用|创建/ }),
    ctx.page.getByText(/创建企业自建应用|创建应用|创建自建应用|创建/),
    ctx.page.locator("[aria-label*='创建企业自建应用'], [title*='创建企业自建应用']"),
    ctx.page.locator("[aria-label*='创建应用'], [title*='创建应用']"),
    ctx.page.locator("[aria-label='创建'], [title='创建']"),
    ctx.page.locator("[class*='create']").getByText(/创建企业自建应用|创建应用|创建自建应用|创建/)
  ];

  const locator = await firstVisibleLocator(locators, 5000);
  if (!locator) {
    return false;
  }

  const disabledAttr = await locator.getAttribute("disabled").catch(() => null);
  const ariaDisabled = await locator.getAttribute("aria-disabled").catch(() => null);
  let disabled = disabledAttr !== null || (ariaDisabled || "").toLowerCase() === "true";
  if (!disabled) {
    disabled = await locator.isDisabled().catch(() => false);
  }
  if (disabled) {
    return false;
  }

  await locator.scrollIntoViewIfNeeded().catch(() => undefined);
  await locator.click({ timeout: ctx.config.timeoutMs });
  await ctx.page.waitForTimeout(1500);
  return true;
}

async function listStartedAppCandidates(page: Page): Promise<StartedAppCandidate[]> {
  const items = await page.evaluate(() => {
    const selectors = ["a", "[role='link']", "button", "[role='button']", "[role='row']", "tr", "li"].join(",");
    const normalize = (value: string): string => value.replace(/\s+/g, " ").trim();
    const isVisible = (element: Element | null): element is HTMLElement => {
      if (!(element instanceof HTMLElement)) {
        return false;
      }

      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };

    const results: Array<{ name: string; status: string; containerText: string }> = [];
    const seen = new Set<string>();

    for (const element of document.querySelectorAll(selectors)) {
      if (!isVisible(element)) {
        continue;
      }

      const container =
        element.closest("tr,[role='row'],li,[class*='row'],[class*='item'],[class*='card'],[class*='list'],div") ?? element;

      if (!isVisible(container)) {
        continue;
      }

      const containerText = normalize(container.innerText || "");
      if (!containerText.includes("已启动")) {
        continue;
      }

      const triggerText = normalize(
        element instanceof HTMLInputElement
          ? element.value || ""
          : element.innerText || element.getAttribute("aria-label") || element.getAttribute("title") || ""
      );
      const fallbackName =
        containerText
          .split(/\n| {2,}/)
          .map((part) => normalize(part))
          .find((part) => part && !part.includes("已启动") && part.length >= 2 && part.length <= 80) || "";
      const candidateName = triggerText || fallbackName;

      if (!candidateName) {
        continue;
      }

      const key = `${candidateName}__${containerText}`;
      if (seen.has(key)) {
        continue;
      }

      seen.add(key);
      results.push({
        name: candidateName,
        status: "已启动",
        containerText
      });
    }

    return results.slice(0, 10);
  });

  return items;
}

async function listStartedAppCandidatesV2(page: Page): Promise<StartedAppCandidate[]> {
  const scanPromise = (async () => {
    const normalize = (value: string): string => value.replace(/\s+/g, " ").trim();
    const acceptedStatuses = ["已启动", "已启用"];
    const candidates: StartedAppCandidate[] = [];
    const seen = new Set<string>();

    const appCards = page.locator(".app-card, [class*='app-card']");
    const cardCount = Math.min(await appCards.count().catch(() => 0), 50);

    for (let index = 0; index < cardCount; index += 1) {
      const card = appCards.nth(index);
      let cardText = "";

      try {
        if (!(await card.isVisible())) {
          continue;
        }
        cardText = normalize(await card.innerText());
      } catch {
        continue;
      }

      if (!cardText || !(await hasExactStartedTag(card))) {
        continue;
      }

      const title = await card
        .locator(".app-card__title, [class*='app-card__title']")
        .first()
        .innerText()
        .then((value) => normalize(value))
        .catch(() => "");
      const status = await card
        .locator(".ud__tag__content, [class*='tag__content']")
        .first()
        .innerText()
        .then((value) => normalize(value))
        .catch(() => "");
      if (!acceptedStatuses.includes(status)) {
        continue;
      }
      const candidateName =
        title ||
        cardText
          .split(/\n| {2,}/)
          .map((part) => normalize(part))
          .find(
            (part) =>
              part !== "已启动" &&
              !["创建应用", "创建企业自建应用", "企业自建应用", "开发者后台"].includes(part) &&
              part.length >= 2 &&
              part.length <= 80
          ) ||
        "";

      if (!candidateName) {
        continue;
      }

      const key = `${candidateName}__${cardText}`;
      if (seen.has(key)) {
        continue;
      }

      seen.add(key);
      candidates.push({
        name: candidateName,
        status,
        containerText: cardText
      });
    }

    return candidates.slice(0, 10);
  })();

  const timeoutPromise = new Promise<StartedAppCandidate[]>((resolve) =>
    setTimeout(() => resolve([]), 1500)
  );

  return Promise.race([scanPromise, timeoutPromise]);
}

async function chooseStartedApp(ctx: StepContext): Promise<StartedAppCandidate | null> {
  await ctx.page.waitForLoadState("domcontentloaded").catch(() => undefined);
  await ctx.page.waitForTimeout(1000);

  const candidates = await listStartedAppCandidatesV2(ctx.page);
  if (candidates.length === 0) {
    const pageText = await ctx.page
      .locator("body")
      .innerText()
      .then((value) => value.replace(/\s+/g, " ").trim().slice(0, 500))
      .catch(() => "");
    if (pageText) {
      ctx.logger.debug(`应用管理页文本片段：${pageText}`);
    }
    return null;
  }

  ctx.logger.info(`检测到 ${candidates.length} 个“已启动”的飞书实例。`);
  candidates.forEach((candidate, index) => {
    ctx.logger.info(`${index + 1}. ${candidate.name} | ${candidate.status} | ${candidate.containerText}`);
  });

  if (isNonInteractivePrompt()) {
    ctx.logger.info(`当前为非交互环境，自动复用第 1 个“已启动”实例：${candidates[0].name}`);
    return candidates[0];
  }

  ctx.logger.info(`检测到页面上有 ${candidates.length} 个已启动的飞书实例，请在下方确认是否复用：`);
  const shouldReuse = await promptYesNo(ctx.prompt, "是否复用一个已启动的飞书实例？", true);
  if (!shouldReuse) {
    return null;
  }

  while (true) {
    const answer = await promptText(ctx.prompt, `请输入要复用的实例序号（1-${candidates.length}），直接回车则取消复用`);
    if (!answer) {
      return null;
    }

    const selectedIndex = Number(answer);
    if (Number.isInteger(selectedIndex) && selectedIndex >= 1 && selectedIndex <= candidates.length) {
      return candidates[selectedIndex - 1];
    }

    ctx.logger.warn("输入无效，请输入正确的序号。");
  }
}

async function openAppByName(ctx: StepContext, appName: string, preferStarted = false, expectedStatus?: string): Promise<boolean> {
  if (preferStarted) {
    const cards = ctx.page.locator(".app-card, [class*='app-card']").filter({ hasText: appName });
    const count = Math.min(await cards.count(), 20);

    for (let index = 0; index < count; index += 1) {
      const card = cards.nth(index);
      try {
        if (!(await card.isVisible())) {
          continue;
        }
      } catch {
        continue;
      }

      if (!(await hasExactStartedTag(card))) {
        continue;
      }
      if (expectedStatus) {
        const actualStatus = await card
          .locator(".ud__tag__content, [class*='tag__content']")
          .first()
          .innerText()
          .then((value) => value.trim())
          .catch(() => "");
        if (actualStatus !== expectedStatus) {
          continue;
        }
      }

      const locator = await firstVisibleLocator(
        [
          card.locator(".app-card__title, [class*='app-card__title']"),
          card.getByText(toRegex(appName)),
          card
        ],
        3000
      );
      if (!locator) {
        continue;
      }

      await locator.click({ timeout: ctx.config.timeoutMs });
      return true;
    }
  }

  const locator = await firstVisibleLocator(
    [
      ctx.page.getByRole("link", { name: toRegex(appName) }),
      ctx.page.getByRole("button", { name: toRegex(appName) }),
      ctx.page.getByText(toRegex(appName))
    ],
    5000
  );

  if (!locator) {
    return false;
  }

  await locator.click({ timeout: ctx.config.timeoutMs });
  return true;
}

async function createOrOpenApp(ctx: StepContext): Promise<void> {
  if (ctx.result.appId) {
    const reuseHistorical = isNonInteractivePrompt()
      ? true
      : await promptYesNo(
        ctx.prompt,
        `检测到历史应用记录 (${ctx.result.appId})，是否确认复用该历史应用？`,
        true
      );

    if (reuseHistorical) {
      const appUrl = "https://open.feishu.cn/app/" + ctx.result.appId + "/baseinfo";
      ctx.logger.info("确认复用历史应用续跑：" + ctx.result.appId);
      await ctx.page.goto(appUrl, { waitUntil: "domcontentloaded" });

      const inaccessible = await waitForAnyText(
        ctx.page,
        ["无权访问", "应用不存在", "页面不存在", "抱歉，您无权访问此页面"],
        Math.min(ctx.config.timeoutMs, 4000)
      );
      const backendSignal = inaccessible
        ? null
        : await waitForAnyText(ctx.page, APP_BACKEND_SIGNALS, ctx.config.timeoutMs);

      if (backendSignal) {
        return;
      }

      ctx.logger.warn(
        "历史 App ID 对当前登录账号不可用，已放弃该状态并返回应用列表：" + ctx.result.appId
      );
    } else {
      ctx.logger.info(`用户放弃复用历史应用 ${ctx.result.appId}，重置记录并返回应用列表...`);
    }

    ctx.result.appId = null;
    ctx.result.maskedSecret = null;
    ctx.result.existingAppReused = false;
    ctx.result.existingStartedAppChosen = false;
    ctx.result.reusedAppName = null;
    ctx.result.permissionsImported = false;
    ctx.result.botEnabled = false;
    ctx.result.eventSubscriptionConfigured = false;
    ctx.result.published = false;
    await persistResult(ctx.config, ctx.result);
  }
  ctx.logger.info("正在打开飞书开放平台应用列表页...");
  await openAppWorkbench(ctx);

  ctx.logger.info("正在切换到“企业自建应用”页签...");
  await clickSelfBuiltAppsTab(ctx).catch(() => false);

  ctx.logger.info("正在检测是否有已启动状态的应用实例...");
  const startedApp = ctx.config.reuseStartedApp ? await chooseStartedApp(ctx) : null;
  if (startedApp) {
    const opened = await openAppByName(ctx, startedApp.name, true, startedApp.status);
    if (!opened) {
      await manualTakeover(ctx, "进入已启动飞书实例后台", [
        `请在页面中手动打开已启动实例“${startedApp.name}”。`,
        "确认页面左侧已出现应用后台菜单后，再回到终端。"
      ]);
    }

    ctx.result.appName = startedApp.name;
    ctx.result.existingAppReused = true;
    ctx.result.existingStartedAppChosen = true;
    ctx.result.reusedAppName = startedApp.name;
    const appSignal = await waitForAnyText(ctx.page, APP_BACKEND_SIGNALS, ctx.config.timeoutMs);
    if (!appSignal) {
      await manualTakeover(ctx, "进入已启动飞书实例后台", [
        `请在页面中手动打开应用“${startedApp.name}”。`,
        "确认页面左侧出现应用后台菜单后，再回到终端。"
      ]);
    }
    return;
  }

  const sameNameCardCount = await countVisibleAppCardsByName(ctx.page, ctx.config.appName);
  if (ctx.config.reuseStartedApp && sameNameCardCount > 1) {
    throw new Error(`检测到 ${sameNameCardCount} 个同名应用“${ctx.config.appName}”，但没有锁定到“已启动”标签，已停止自动选择。`);
  }

  ctx.logger.info(`正在扫描“${ctx.config.appName}”已有应用卡片...`);
  const existingApp = await firstVisibleLocator([ctx.page.getByText(toRegex(ctx.config.appName))], 3000);
  if (existingApp) {
    ctx.logger.info(`检测到同名已有应用“${ctx.config.appName}”，请在下方确认是否复用：`);
    const shouldReuse = isNonInteractivePrompt()
      ? true
      : await promptYesNo(
        ctx.prompt,
        `检测到账号下已存在应用“${ctx.config.appName}”，是否复用该应用？`,
        true
      );

    if (shouldReuse) {
      ctx.logger.info(`确认复用已有应用：${ctx.config.appName}`);
      await existingApp.click({ timeout: ctx.config.timeoutMs });
      ctx.result.existingAppReused = true;
      ctx.result.reusedAppName = ctx.config.appName;
      const appSignal = await waitForAnyText(ctx.page, APP_BACKEND_SIGNALS, ctx.config.timeoutMs);
      if (!appSignal) {
        await manualTakeover(ctx, "进入已有应用后台", [
          `请在页面中手动打开应用“${ctx.config.appName}”。`,
          "确认页面左侧出现应用后台菜单后，再回到终端。"
        ]);
      }
      return;
    }

    ctx.logger.info(`用户选择不复用已有应用“${ctx.config.appName}”，将继续进入创建新应用流程...`);
  } else {
    ctx.logger.info(`未在当前账号下扫描到已有应用“${ctx.config.appName}”，即将进入创建新应用表单...`);
  }

  const createClicked = (await openCreateSelfBuiltAppEntry(ctx)) || (await clickByCandidates(
    ctx.page,
    ["创建企业自建应用", "创建应用", "企业自建应用", "创建自建应用"],
    ctx.config.timeoutMs,
    ctx.logger
  ));

  if (!createClicked) {
    await manualTakeover(ctx, "进入创建应用入口", [
      "请在飞书开放平台中打开“创建企业自建应用”页面。",
      "如果页面有“创建应用”或“企业自建应用”按钮，请手动点击进去。"
    ]);
  }

  await fillAppBasicInfo(ctx);
}

async function fillAppBasicInfo(ctx: StepContext): Promise<void> {
  const modal = await findVisibleModal(ctx.page, Math.min(ctx.config.timeoutMs, 5000));
  const formRoot = modal ?? ctx.page;
  if (modal) {
    ctx.logger.info("已检测到创建应用弹窗，后续仅在弹窗内定位输入框和按钮。");
  }

  const signal = await waitForAnyText(
    formRoot,
    ["应用名称", "应用描述", "企业自建应用", "创建应用", "应用介绍"],
    Math.min(ctx.config.timeoutMs, 5000)
  );
  const visibleField = await firstVisibleLocator(
    [
      formRoot.locator("input[type='text']"),
      formRoot.locator("input:not([type])"),
      formRoot.locator("textarea")
    ],
    2000
  );

  if (!signal && !visibleField) {
    await manualTakeover(ctx, "定位应用信息表单", [
      "请确保当前页面已经进入创建应用表单。",
      "如果需要先选择“企业自建应用”，请手动完成。"
    ]);
  }

  const nameFilled = await fillTextbox(formRoot, ["应用名称", "名称", "App Name"], ctx.config.appName, ctx.config.timeoutMs, {
    genericSelectors: [
      "input[type='text']",
      "input:not([type])",
      ".semi-input input",
      ".arco-input input"
    ]
  });
  if (!nameFilled) {
    await manualTakeover(ctx, "填写应用名称", [
      `请在当前页面手动填写应用名称：${ctx.config.appName}`,
      "填写后不要关闭浏览器。"
    ]);
  }

  const descriptionFilled = await fillTextbox(
    formRoot,
    ["应用描述", "描述", "应用介绍", "Description"],
    ctx.config.appDescription,
    ctx.config.timeoutMs,
    {
      genericSelectors: ["textarea", ".semi-input-textarea textarea", ".arco-textarea textarea"]
    }
  );
  if (!descriptionFilled) {
    await manualTakeover(ctx, "填写应用描述", [
      `请在当前页面手动填写应用描述：${ctx.config.appDescription}`,
      "填写完成后再继续。"
    ]);
  }

  if (ctx.config.appIconPath && (await pathExists(ctx.config.appIconPath))) {
    const uploaded = await setFileIfPresent(formRoot, ctx.config.appIconPath, ctx.logger);
    if (!uploaded) {
      ctx.logger.warn(`未自动找到图标上传控件，请手动上传：${ctx.config.appIconPath}`);
    }
  } else if (ctx.config.appIconPath) {
    ctx.logger.warn(`图标文件不存在，跳过上传：${ctx.config.appIconPath}`);
  }

  let submitClicked = await clickByCandidates(formRoot, ["创建", "确认", "提交", "完成"], ctx.config.timeoutMs, ctx.logger);
  if (!submitClicked && modal) {
    submitClicked = await clickByCandidates(ctx.page, ["创建", "确认", "提交", "完成"], ctx.config.timeoutMs, ctx.logger);
  }
  if (!submitClicked) {
    await manualTakeover(ctx, "提交应用创建表单", [
      "请在浏览器中手动点击创建或确认按钮。",
      "创建成功后，保持在应用后台页面。"
    ]);
  }

  const backendSignal = await waitForAnyText(ctx.page, [...APP_BACKEND_SIGNALS, ctx.config.appName], ctx.config.timeoutMs * 2);
  if (!backendSignal) {
    await manualTakeover(ctx, "确认已进入应用后台", [
      "请确认当前页面已经切换到应用后台。",
      "左侧通常会出现“凭证与基础信息 / 权限管理 / 应用能力 / 版本管理与发布”等菜单。"
    ]);
  }
}

async function writeFeishuCredentials(envPath: string, appId: string, appSecret: string): Promise<void> {
  let content = "";
  try {
    content = await readFile(envPath, "utf8");
  } catch {
    content = "";
  }

  const upsert = (source: string, key: string, value: string): string => {
    const line = key + "=" + value;
    const pattern = new RegExp("^" + escapeRegExp(key) + "=.*$", "m");
    return pattern.test(source) ? source.replace(pattern, line) : source.replace(/\s*$/, "") + (source ? "\n" : "") + line + "\n";
  };

  content = upsert(content, "FEISHU_APP_ID", appId);
  content = upsert(content, "FEISHU_APP_SECRET", appSecret);
  await writeFile(envPath, content, "utf8");
}

async function readFeishuCredentials(envPath: string): Promise<{ appId: string; appSecret: string } | null> {
  try {
    const content = await readFile(envPath, "utf8");
    const readValue = (key: string): string => {
      const match = content.match(new RegExp("^" + escapeRegExp(key) + "=(.*)$", "m"));
      return match?.[1]?.trim() ?? "";
    };
    const appId = readValue("FEISHU_APP_ID");
    const appSecret = readValue("FEISHU_APP_SECRET");
    return appId && appSecret ? { appId, appSecret } : null;
  } catch {
    return null;
  }
}
async function stopLocalClawService(ctx: StepContext): Promise<void> {
  if (process.platform !== "win32") return;
  try {
    const out = execFileSync("cmd", ["/c", "netstat -ano | findstr :8080"], { encoding: "utf8" }).toString();
    const pids = new Set<string>();
    for (const line of out.split("\n")) {
      const parts = line.trim().split(/\s+/);
      if (parts.length >= 5 && parts[3] === "LISTENING" && /^\d+$/.test(parts[4])) {
        pids.add(parts[4]);
      }
    }
    for (const pid of pids) {
      try {
        execFileSync("taskkill", ["/F", "/T", "/PID", pid], { stdio: "ignore" });
        ctx.logger.info("已停止占用 8080 端口的本地服务进程：" + pid);
      } catch {
        // 进程可能已退出
      }
    }
  } catch {
    // netstat 无输出 = 服务本就没在跑
  }
}

async function waitForLocalClawOnline(ctx: StepContext): Promise<void> {
  const url = ctx.config.localServiceUrl;
  const deadline = Date.now() + ctx.config.localServiceWaitMs;
  let lastError = "服务未响应";
  let started = false;
  let restarted = false;

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      const body = (await response.json()) as { status?: string; ws_connected?: boolean };
      if (response.ok && body.status === "ok" && body.ws_connected === true) {
        ctx.logger.info("本地 claw 服务在线，WebSocket 已连接：" + url);
        return;
      }
      lastError = "服务已响应，但 WebSocket 未连接（" + JSON.stringify(body) + "）";
      // 服务多半是带着旧/空凭据启动的：停掉它，下一轮 fetch 失败时会被重新拉起并加载最新 .env
      if (!restarted && ctx.config.startLocalService) {
        ctx.logger.warn("WebSocket 未连接，重启本地服务以加载刚写入的 .env 凭据...");
        await stopLocalClawService(ctx);
        started = false;
        restarted = true;
      }
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      if (!started && ctx.config.startLocalService) {
        const python = process.platform === "win32"
          ? path.join(ctx.config.localServiceRootDir, ".venv", "Scripts", "python.exe")
          : path.join(ctx.config.localServiceRootDir, ".venv", "bin", "python");
        if (!(await pathExists(python))) {
          throw new Error(
            "本地 claw 服务无法启动：未找到 " + python +
            "。请先在项目根目录运行 MyClaw-Setup.bat（或执行 uv sync）完成 Python 环境安装，再重跑 setup.cmd。"
          );
        }
        const bootLogPath = path.join(ctx.config.localServiceRootDir, "logs", "auto_feishu_service_boot.log");
        await mkdir(path.dirname(bootLogPath), { recursive: true }).catch(() => undefined);
        const bootLog = await open(bootLogPath, "w").catch(() => null);
        const child = spawn(python, ["-m", "app.main"], {
          cwd: ctx.config.localServiceRootDir,
          detached: true,
          stdio: bootLog ? ["ignore", bootLog.fd, bootLog.fd] : "ignore",
          windowsHide: true
        });
        child.unref();
        started = true;
        ctx.logger.info("本地 claw 服务未运行，已尝试启动：" + ctx.config.localServiceRootDir);
        ctx.logger.info("服务启动日志（若一直未上线请查看）：" + bootLogPath);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  let bootTail = "";
  try {
    const bootLogPath = path.join(ctx.config.localServiceRootDir, "logs", "auto_feishu_service_boot.log");
    if (await pathExists(bootLogPath)) {
      const content = await readFile(bootLogPath, "utf8");
      bootTail = content.split("\n").slice(-15).join("\n").trim();
    }
  } catch {
    // 读不到启动日志就只报原始错误
  }

  throw new Error(
    "本地 claw 服务未达到可订阅状态：" + lastError
    + "。请确认根目录 .env 已写入 FEISHU_APP_ID/FEISHU_APP_SECRET，然后检查 "
    + url + " 和 myclaw.log。"
    + (bootTail ? "\n服务启动日志最后几行：\n" + bootTail : "")
  );
}

async function fetchCredentials(ctx: StepContext): Promise<void> {
  const clicked = await clickByCandidates(ctx.page, ["凭证与基础信息", "基础信息", "凭证"], ctx.config.timeoutMs, ctx.logger);
  if (!clicked) {
    await manualTakeover(ctx, "进入凭证与基础信息页面", [
      "请在左侧菜单中打开“凭证与基础信息”。",
      "打开后保持页面停留在凭证信息区域。"
    ]);
  }

  let appId = await tryReadAppIdFromCredentialRow(ctx);
  if (!appId) {
    appId =
      (await extractValueNearLabelsV2(ctx.page, ["App ID", "AppId", "APP ID"])) ??
      (await extractValueNearLabels(ctx.page, ["App ID", "AppId", "APP ID"]));
  }
  if (!appId) {
    await manualTakeover(ctx, "读取 App ID", [
      "请确保当前页面已经显示 App ID。",
      "如果 App ID 在折叠区域，请手动展开。"
    ]);
    appId =
      (await tryReadAppIdFromCredentialRow(ctx)) ??
      (await extractValueNearLabelsV2(ctx.page, ["App ID", "AppId", "APP ID"])) ??
      (await extractValueNearLabels(ctx.page, ["App ID", "AppId", "APP ID"]));
  }

  let secret = await tryReadSecretFromCredentialRow(ctx);
  if (!secret) {
    const revealSecret = await clickByCandidates(ctx.page, ["显示", "查看", "显示 Secret", "查看 Secret"], 3000, ctx.logger);
    if (revealSecret) {
      ctx.logger.debug(`已尝试展开 Secret：${revealSecret}`);
    }
  }

  if (!secret) {
    secret =
      (await extractValueNearLabelsV2(ctx.page, ["App Secret", "AppSecret", "APP Secret"])) ??
      (await extractValueNearLabels(ctx.page, ["App Secret", "AppSecret", "APP Secret"]));
  }
  if (!secret) {
    await manualTakeover(ctx, "读取 App Secret", [
      "请手动点击 App Secret 旁边的“显示”或“复制”图标按钮。",
      "如果组织策略要求确认，请手动完成。"
    ]);
    secret =
      (await tryReadSecretFromCredentialRow(ctx)) ??
      (await extractValueNearLabelsV2(ctx.page, ["App Secret", "AppSecret", "APP Secret"])) ??
      (await extractValueNearLabels(ctx.page, ["App Secret", "AppSecret", "APP Secret"]));
  }

  const finalAppId =
    appId ??
    (await tryReadAppIdFromCredentialRow(ctx)) ??
    (await extractValueNearLabelsV2(ctx.page, ["App ID", "AppId", "APP ID"])) ??
    (await extractValueNearLabels(ctx.page, ["App ID", "AppId", "APP ID"]));
  if (!finalAppId || !secret) {
    throw new Error("未能读取到 App ID 或 App Secret，请检查页面是否已完整展示凭证。");
  }

  ctx.result.appId = finalAppId;
  ctx.runtimeAppSecret = secret;
  ctx.result.maskedSecret = maskSecret(secret);
  ctx.logger.info(`App ID 已抓取：${finalAppId}`);
  ctx.logger.info(`App Secret 已抓取：${ctx.result.maskedSecret} (将在发布成功后统一同步至 .env)`);
  await persistResult(ctx.config, ctx.result);
}

async function importPermissions(ctx: StepContext): Promise<void> {
  const clicked = await clickByCandidates(ctx.page, ["权限管理", "权限"], ctx.config.timeoutMs, ctx.logger);
  if (!clicked) {
    await manualTakeover(ctx, "进入权限管理页面", [
      "请在左侧菜单中手动打开“权限管理”。",
      "打开后保持页面停留在权限列表区域。"
    ]);
  }

  const importButton = await waitForEnabledAction(
    ctx.page,
    ["批量导入/导出权限", "批量导入", "导入权限", "导入"],
    ctx.config.timeoutMs
  );
  if (!importButton) {
    await manualTakeover(ctx, "打开批量导入弹窗", [
      "请手动点击“批量导入”按钮，打开权限导入弹窗。",
      "弹窗出现后不要关闭浏览器。"
    ]);
  } else {
    await importButton.locator.scrollIntoViewIfNeeded().catch(() => undefined);
    await importButton.locator.click({ timeout: ctx.config.timeoutMs, force: true });
    ctx.logger.debug(`已点击权限导入入口：${importButton.text}`);
  }

  const modal = await findVisibleModal(ctx.page, Math.min(ctx.config.timeoutMs, 5000));
  const formRoot = modal ?? ctx.page;

  const rawPermissions = JSON.parse(await readFile(ctx.config.permissionsImportJsonPath, "utf8")) as {
    scopes?: {
      tenant?: string[];
      user?: string[];
    };
  };

  if (!ctx.config.enableGroupMessagePermission) {
    rawPermissions.scopes = rawPermissions.scopes ?? {};
    rawPermissions.scopes.tenant = (rawPermissions.scopes.tenant ?? []).filter(
      (scope) => scope !== "im:message.group_msg"
    );
  }

  const payload = JSON.stringify(rawPermissions, null, 2);
  // 页面存在隐藏的只读 Monaco（common-monaco-editor--readOnly，DOM 顺序在前），
  // .first() 会瞄准幽灵编辑器：粘贴落空/JSON 损坏/提交按钮禁用。必须按可见性过滤。
  const monacoEditor = formRoot.locator(".monaco-editor:visible").first();
  const monacoInput = formRoot
    .locator(".monaco-editor:visible textarea.inputarea, textarea.inputarea:visible, [role='textbox'][aria-roledescription='editor']:visible")
    .first();
  const editor = await firstVisibleLocator(
    [monacoInput, formRoot.locator("[contenteditable='true']"), formRoot.locator("textarea")],
    ctx.config.timeoutMs
  );

  if (!editor) {
    await manualTakeover(ctx, "粘贴权限 JSON", [
      `请把文件内容手动粘贴到导入框：${ctx.config.permissionsImportJsonPath}`,
      "粘贴后再继续。"
    ]);
  } else {
    const hasMonaco = (await monacoEditor.count().catch(() => 0)) > 0;
    if (hasMonaco) {
      await monacoEditor.click({ timeout: ctx.config.timeoutMs, force: true, position: { x: 120, y: 40 } }).catch(() => undefined);
      await monacoInput.focus().catch(() => undefined);
      await ctx.page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
      await ctx.page.keyboard.press("Backspace");
      const pasted = writeSystemClipboardText(payload);
      if (pasted) {
        await ctx.page.keyboard.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
      } else {
        await ctx.page.keyboard.insertText(payload);
      }
    } else {
      await editor.click({ timeout: ctx.config.timeoutMs });
      try {
        await editor.fill(payload, { timeout: ctx.config.timeoutMs });
      } catch {
        await ctx.page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
        await ctx.page.keyboard.press("Backspace");
        await ctx.page.keyboard.insertText(payload);
      }
    }
  }

  await ctx.page.keyboard.press("Escape").catch(() => undefined);
  const submit = await clickByCandidates(
    formRoot,
    ["下一步，确认新增权限", "确认新增权限", "下一步", "导入", "确认", "确定"],
    ctx.config.timeoutMs,
    ctx.logger
  );
  if (!submit) {
    await manualTakeover(ctx, "确认权限导入", [
      "请手动点击导入确认按钮。",
      "等待页面出现导入成功提示后再继续。"
    ]);
  }

  let success = await waitForAnyText(ctx.page, ["导入成功", "已导入", "权限已更新", "导入完成"], ctx.config.timeoutMs);
  if (!success) {
    await ctx.page.keyboard.press("Escape").catch(() => undefined);
    const applySubmit = await clickByCandidates(formRoot, ["申请开通"], Math.min(ctx.config.timeoutMs, 8000), ctx.logger);
    if (applySubmit) {
      await clickByCandidates(ctx.page, ["确认开启", "开启"], Math.min(ctx.config.timeoutMs, 5000), ctx.logger);
      success = await waitForAnyText(
        ctx.page,
        ["确认开启成功", "开启成功", "申请开通成功", "申请已提交", "提交成功", "导入成功", "已导入", "权限已更新", "导入完成"],
        ctx.config.timeoutMs
      );
    }
  }
  if (!success) {
    ctx.logger.warn("未检测到明确的权限导入成功提示，请人工确认。");
  }

  ctx.result.permissionsImported = true;
}

async function importPermissionsV2(ctx: StepContext): Promise<void> {
  if (ctx.result.appId) {
    const authUrl = `https://open.feishu.cn/app/${ctx.result.appId}/auth`;
    ctx.logger.debug(`\u76f4\u63a5\u6253\u5f00\u6743\u9650\u7ba1\u7406\u9875\uff1a${authUrl}`);
    await ctx.page.goto(authUrl, { waitUntil: "domcontentloaded" });
  }

  const direct = await clickByCandidates(
    ctx.page,
    ["\u6743\u9650\u7ba1\u7406", "\u5e94\u7528\u80fd\u529b"],
    ctx.config.timeoutMs,
    ctx.logger
  );
  if (!direct) {
    await manualTakeover(ctx, "\u8fdb\u5165\u6743\u9650\u7ba1\u7406\u9875\u9762", [
      "\u8bf7\u5728\u5de6\u4fa7\u83dc\u5355\u4e2d\u6253\u5f00\u201c\u6743\u9650\u7ba1\u7406\u201d\u3002",
      "\u6253\u5f00\u540e\u4fdd\u6301\u9875\u9762\u505c\u7559\u5728\u6743\u9650\u5217\u8868\u533a\u57df\u3002"
    ]);
  } else if (direct === "\u5e94\u7528\u80fd\u529b") {
    await clickByCandidates(ctx.page, ["\u6743\u9650\u7ba1\u7406"], ctx.config.timeoutMs, ctx.logger);
  }

  await waitForAnyText(ctx.page, ["\u6279\u91cf\u5bfc\u5165", "\u6743\u9650\u7ba1\u7406", "\u5bfc\u5165\u6743\u9650"], ctx.config.timeoutMs);

  const importButton = await waitForEnabledAction(
    ctx.page,
    ["\u6279\u91cf\u5bfc\u5165/\u5bfc\u51fa\u6743\u9650", "\u6279\u91cf\u5bfc\u5165", "\u5bfc\u5165\u6743\u9650", "\u5bfc\u5165"],
    ctx.config.timeoutMs
  );
  if (!importButton) {
    await manualTakeover(ctx, "\u6253\u5f00\u6279\u91cf\u5bfc\u5165\u5f39\u7a97", [
      "\u8bf7\u624b\u52a8\u70b9\u51fb\u201c\u6279\u91cf\u5bfc\u5165\u201d\u6309\u94ae\uff0c\u6253\u5f00\u6743\u9650\u5bfc\u5165\u5f39\u7a97\u3002",
      "\u5f39\u7a97\u51fa\u73b0\u540e\u4e0d\u8981\u5173\u95ed\u6d4f\u89c8\u5668\u3002"
    ]);
  } else {
    await importButton.locator.scrollIntoViewIfNeeded().catch(() => undefined);
    await importButton.locator.click({ timeout: ctx.config.timeoutMs, force: true });
    ctx.logger.debug(`\u5df2\u70b9\u51fb\u6743\u9650\u5bfc\u5165\u5165\u53e3\uff1a${importButton.text}`);
  }

  const modal = await findVisibleModal(ctx.page, Math.min(ctx.config.timeoutMs, 5000));
  const formRoot = modal ?? ctx.page;

  const rawPermissions = JSON.parse(await readFile(ctx.config.permissionsImportJsonPath, "utf8")) as {
    scopes?: {
      tenant?: string[];
      user?: string[];
    };
  };

  if (!ctx.config.enableGroupMessagePermission) {
    rawPermissions.scopes = rawPermissions.scopes ?? {};
    rawPermissions.scopes.tenant = (rawPermissions.scopes.tenant ?? []).filter(
      (scope) => scope !== "im:message.group_msg"
    );
  }

  const payload = JSON.stringify(rawPermissions, null, 2);
  // 页面存在隐藏的只读 Monaco（common-monaco-editor--readOnly，DOM 顺序在前），
  // .first() 会瞄准幽灵编辑器：粘贴落空/JSON 损坏/提交按钮禁用。必须按可见性过滤。
  const monacoEditor = formRoot.locator(".monaco-editor:visible").first();
  const monacoInput = formRoot
    .locator(".monaco-editor:visible textarea.inputarea, textarea.inputarea:visible, [role='textbox'][aria-roledescription='editor']:visible")
    .first();
  const editor = await firstVisibleLocator(
    [monacoInput, formRoot.locator("[contenteditable='true']"), formRoot.locator("textarea")],
    ctx.config.timeoutMs
  );

  if (!editor) {
    await manualTakeover(ctx, "\u7c98\u8d34\u6743\u9650 JSON", [
      `\u8bf7\u628a\u6587\u4ef6\u5185\u5bb9\u624b\u52a8\u7c98\u8d34\u5230\u5bfc\u5165\u6846\uff1a${ctx.config.permissionsImportJsonPath}`,
      "\u7c98\u8d34\u540e\u7ee7\u7eed\u3002"
    ]);
  } else {
    const hasMonaco = (await monacoEditor.count().catch(() => 0)) > 0;
    if (hasMonaco) {
      await monacoEditor.click({ timeout: ctx.config.timeoutMs, position: { x: 120, y: 40 } });
      const selectAll = process.platform === "darwin" ? "Meta+A" : "Control+A";
      await ctx.page.keyboard.press(selectAll);
      await ctx.page.keyboard.press("Backspace");
      if (!writeSystemClipboardText(payload)) {
        throw new Error("无法写入系统剪贴板，已停止权限导入。");
      }
      await ctx.page.keyboard.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
      await ctx.page.waitForTimeout(300);
      const formatted = await clickByCandidates(formRoot, ["格式化 JSON"], Math.min(ctx.config.timeoutMs, 5000), ctx.logger);
      if (!formatted) throw new Error("权限 JSON 粘贴后未找到格式化按钮。");
      ctx.logger.debug("权限 JSON 已通过剪贴板粘贴，避免 Monaco 自动补全额外右括号。");
    } else {
      await editor.click({ timeout: ctx.config.timeoutMs });
      try {
        await editor.fill(payload, { timeout: ctx.config.timeoutMs });
      } catch {
        await ctx.page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
        await ctx.page.keyboard.press("Backspace");
        await ctx.page.keyboard.insertText(payload);
      }
    }
  }


  // \u65b0\u7248\u98de\u4e66\u5bfc\u5165\u4e3a\u4e09\u6bb5\u5f0f\uff1a\u2460\u7c98\u8d34\u2192\u786e\u8ba4\u65b0\u589e\u6743\u9650 \u2461\u914d\u7f6e\u53ef\u8bbf\u95ee\u6570\u636e\u8303\u56f4 \u2462\u786e\u5b9a\u5f00\u901a\u3002
  // \u7b2c\u4e09\u6bb5\u7684\u5bb9\u5668\u4e0d\u518d\u662f .ud__modal\uff08formRoot \u63a2\u6d4b\u5931\u6548\uff09\uff0c\u5fc5\u987b\u9875\u9762\u7ea7\u641c\u7d22\u6309\u94ae\u3002
  // \u53e6\uff1a\u7c98\u8d34\u540e\u9700\u8ba9\u7f16\u8f91\u5668\u5931\u7126\u89e6\u53d1\u6821\u9a8c\uff0c\u5426\u5219\u201c\u4e0b\u4e00\u6b65\u201d\u4fdd\u6301\u7981\u7528\u3002
  const vp = ctx.page.viewportSize();
  if (vp) {
    await ctx.page.mouse.click(Math.round(vp.width / 2), 250).catch(() => undefined);
    await ctx.page.waitForTimeout(1200);
  }

  const clickStageButton = async (texts: string[], scope: "modal" | "page"): Promise<string | null> => {
    const root = scope === "modal" ? formRoot : ctx.page;
    for (const t of texts) {
      const act = await waitForEnabledAction(root, [t], Math.min(ctx.config.timeoutMs, 15000));
      if (act) {
        await act.locator.scrollIntoViewIfNeeded().catch(() => undefined);
        await act.locator.click({ timeout: ctx.config.timeoutMs, force: true });
        ctx.logger.info(`\u6743\u9650\u5bfc\u5165\u6d41\u7a0b\u5df2\u70b9\u51fb\uff1a${act.text}`);
        return act.text;
      }
    }
    return null;
  };

  const stage1 = await clickStageButton(["\u4e0b\u4e00\u6b65\uff0c\u786e\u8ba4\u65b0\u589e\u6743\u9650", "\u4e0b\u4e00\u6b65"], "modal");
  if (!stage1) {
    await manualTakeover(ctx, "\u786e\u8ba4\u6743\u9650\u5bfc\u5165", [
      "\u8bf7\u624b\u52a8\u70b9\u51fb\u201c\u4e0b\u4e00\u6b65\uff0c\u786e\u8ba4\u65b0\u589e\u6743\u9650\u201d\u3002",
      "\u51fa\u73b0\u540e\u7eed\u6b65\u9aa4\u540e\u518d\u7ee7\u7eed\u3002"
    ]);
  }
  await ctx.page.waitForTimeout(1500);

  // \u7b2c\u4e8c\u6bb5\u53ef\u80fd\u4e0d\u5b58\u5728\uff08\u65e7\u7248 UI \u76f4\u63a5\u8fdb\u5165\u672b\u6bb5\uff09\uff0c\u70b9\u4e0d\u5230\u5c31\u8df3\u8fc7
  await clickStageButton(["\u4e0b\u4e00\u6b65\uff0c\u914d\u7f6e\u53ef\u8bbf\u95ee\u6570\u636e\u8303\u56f4"], "modal");
  await ctx.page.waitForTimeout(1500);

  // \u7b2c\u4e09\u6bb5\uff1a\u786e\u5b9a\u5f00\u901a\uff08\u5bb9\u5668\u5df2\u53d8\uff0c\u9875\u9762\u7ea7\u641c\u7d22\u53ef\u89c1\u6309\u94ae\uff09
  const finalClick = await clickStageButton(["\u786e\u5b9a\u5f00\u901a", "\u786e\u8ba4\u5f00\u901a", "\u7533\u8bf7\u5f00\u901a", "\u786e\u8ba4", "\u786e\u5b9a"], "page");
  if (!finalClick) {
    ctx.logger.warn("\u672a\u627e\u5230\u201c\u786e\u5b9a\u5f00\u901a\u201d\u7c7b\u6309\u94ae\uff0c\u6743\u9650\u53ef\u80fd\u5df2\u5728\u6b64\u524d\u751f\u6548\u3002");
  }
  await ctx.page.waitForTimeout(2500);

  let success = await waitForAnyText(
    ctx.page,
    ["\u5f00\u901a\u6210\u529f", "\u5bfc\u5165\u6210\u529f", "\u5df2\u5bfc\u5165", "\u6743\u9650\u5df2\u66f4\u65b0", "\u5bfc\u5165\u5b8c\u6210"],
    Math.min(ctx.config.timeoutMs, 10000)
  );
  if (!success) {
    // \u6743\u5a01\u6821\u9a8c\uff1a\u7a7a\u5217\u8868\u63d0\u793a\u6d88\u5931\u5373\u89c6\u4e3a\u5bfc\u5165\u751f\u6548\uff08\u6bd4\u6210\u529f toast \u66f4\u53ef\u9760\uff09
    const stillEmpty = await waitForAnyText(ctx.page, ["\u6682\u672a\u5f00\u901a\u4efb\u4f55\u6743\u9650"], 3000);
    if (!stillEmpty) {
      success = "\u6743\u9650\u5217\u8868\u5df2\u66f4\u65b0";
      ctx.logger.info("\u68c0\u6d4b\u5230\u6743\u9650\u5217\u8868\u5df2\u975e\u7a7a\uff0c\u5bfc\u5165\u89c6\u4e3a\u6210\u529f\u3002");
    }
  }

  if (!success) {
    ctx.logger.warn("\u672a\u68c0\u6d4b\u5230\u660e\u786e\u7684\u6743\u9650\u5bfc\u5165\u6210\u529f\u63d0\u793a\uff0c\u8bf7\u4eba\u5de5\u786e\u8ba4\u3002");
  }

  ctx.result.permissionsImported = true;
}
async function enableBotCapability(ctx: StepContext): Promise<void> {
  if (!ctx.result.appId) throw new Error("缺少 App ID，无法配置机器人能力。");

  const capabilityUrl = `https://open.feishu.cn/app/${ctx.result.appId}/capability`;
  ctx.logger.debug(`直接打开应用能力页：${capabilityUrl}`);
  await ctx.page.goto(capabilityUrl, { waitUntil: "domcontentloaded", timeout: ctx.config.timeoutMs });
  await waitForAnyText(ctx.page, ["添加应用能力", "机器人"], ctx.config.timeoutMs);

  const robotTitle = ctx.page.getByText("机器人", { exact: true }).first();
  const robotCard = robotTitle.locator("xpath=ancestor::*[contains(@class, 'ability-card')][1]");
  const addRobot = robotCard.getByRole("button", { name: "添加", exact: true }).first();
  if (await addRobot.isVisible().catch(() => false)) {
    await addRobot.click({ timeout: ctx.config.timeoutMs });
    ctx.logger.debug("已在机器人能力卡片内点击：添加");
    await ctx.page.waitForTimeout(1500);
  }

  await ctx.page.reload({ waitUntil: "domcontentloaded", timeout: ctx.config.timeoutMs });
  await waitForAnyText(ctx.page, ["添加应用能力", "机器人"], ctx.config.timeoutMs);
  const refreshedTitle = ctx.page.getByText("机器人", { exact: true }).first();
  const refreshedCard = refreshedTitle.locator("xpath=ancestor::*[contains(@class, 'ability-card')][1]");
  const stillAddable = refreshedCard.getByRole("button", { name: "添加", exact: true }).first();
  if (await stillAddable.isVisible().catch(() => false)) {
    throw new Error("机器人能力添加后仍显示可添加，飞书未保存该能力。");
  }

  ctx.logger.info("机器人能力已启用，名称继承应用名称。");
  ctx.result.botEnabled = true;
}

// ===== 线上活体校验 =====
// result.json 里的 permissionsImported / eventSubscriptionConfigured 只是历史快照，
// 后台任何手动改动都不会使其失效。跳过决策必须基于当前线上状态，而不是缓存标志。

async function verifyPermissionsLive(ctx: StepContext): Promise<{ ok: boolean; detail: string }> {
  if (!ctx.result.appId) return { ok: false, detail: "无 App ID" };
  try {
    const url = `https://open.feishu.cn/app/${ctx.result.appId}/auth`;
    await ctx.page.goto(url, { waitUntil: "domcontentloaded", timeout: ctx.config.timeoutMs });
    await ctx.page.waitForTimeout(4000);
    const body = await ctx.page.locator("body").innerText().catch(() => "");
    const empty = body.includes("暂未开通任何权限");
    return { ok: !empty, detail: empty ? "权限列表为空（暂未开通任何权限）" : "" };
  } catch (e) {
    return { ok: false, detail: `权限页无法打开：${e}` };
  }
}

async function verifyPublishLive(ctx: StepContext): Promise<{ ok: boolean; detail: string }> {
  // ok=true 表示"无待发布修改"可跳过发版；ok=false 表示后台挂着
  // "版本发布后，当前修改方可生效"提示（如重新添加的事件还没随版本生效）。
  if (!ctx.result.appId) return { ok: false, detail: "无 App ID" };
  try {
    const url = `https://open.feishu.cn/app/${ctx.result.appId}/baseinfo`;
    await ctx.page.goto(url, { waitUntil: "domcontentloaded", timeout: ctx.config.timeoutMs });
    await ctx.page.waitForTimeout(4000);
    const body = await ctx.page.locator("body").innerText().catch(() => "");
    if (body.includes("版本发布后，当前修改方可生效")) {
      return { ok: false, detail: "存在未发布的修改（版本发布后方可生效）" };
    }
    return { ok: true, detail: "" };
  } catch (e) {
    // 页面打不开时不冒险跳过——交由发版流程自行处理
    return { ok: false, detail: `基础信息页无法打开：${e}` };
  }
}

async function verifyEventSubscriptionLive(ctx: StepContext): Promise<{ ok: boolean; detail: string }> {
  if (!ctx.result.appId) return { ok: false, detail: "无 App ID" };
  try {
    const url = `https://open.feishu.cn/app/${ctx.result.appId}/event`;
    await ctx.page.goto(url, { waitUntil: "domcontentloaded", timeout: ctx.config.timeoutMs });
    await ctx.page.waitForTimeout(4000);
    const body = await ctx.page.locator("body").innerText().catch(() => "");
    const failed: string[] = [];
    if (!body.includes("长连接")) failed.push("订阅方式不是长连接");
    if (!body.includes("im.message.receive_v1")) failed.push("缺事件 im.message.receive_v1");
    if (body.includes("请开通以下任一权限")) failed.push("消息事件所需权限未开通");

    // 回调配置页签单独校验 card.action.trigger（不同页签，需点击切换）
    let callbackOk = false;
    try {
      const tab = ctx.page.getByText("回调配置", { exact: true }).first();
      if (await tab.isVisible({ timeout: 3000 })) {
        await tab.click({ timeout: 5000 });
        await ctx.page.waitForTimeout(2500);
        const b2 = await ctx.page.locator("body").innerText().catch(() => "");
        callbackOk = b2.includes("card.action.trigger");
      }
    } catch {
      // 页签点不到视为未配置
    }
    if (!callbackOk) failed.push("缺回调 card.action.trigger");

    // 校验完切回“事件配置”页签：configureEventSubscription 的默认起始页签是它，
    // 停留在回调页签会让后续“添加事件”找不到按钮。
    try {
      const backTab = ctx.page.getByText("事件配置", { exact: true }).first();
      if (await backTab.isVisible({ timeout: 2000 })) {
        await backTab.click({ timeout: 4000 }).catch(() => undefined);
        await ctx.page.waitForTimeout(1500);
      }
    } catch {
      // 切不回则由后续步骤自行处理
    }
    return { ok: failed.length === 0, detail: failed.join("；") };
  } catch (e) {
    return { ok: false, detail: `事件页无法打开：${e}` };
  }
}

async function configureEventSubscription(ctx: StepContext): Promise<void> {
  await waitForLocalClawOnline(ctx);

  ctx.logger.info("正在点击页面左侧“事件与回调 / 事件订阅”菜单...");
  const clicked = await clickByCandidates(ctx.page, ["事件与回调", "事件订阅"], ctx.config.timeoutMs, ctx.logger);
  if (!clicked) {
    await manualTakeover(ctx, "进入事件订阅页面", ["请检查是否有权限访问事件订阅页面。"]);
  }

  ctx.logger.info("正在设置订阅方式为 WebSocket 长连接模式...");
  const subscriptionMode = await clickByCandidates(
    ctx.page,
    ["订阅方式"],
    ctx.config.timeoutMs,
    ctx.logger
  );
  if (!subscriptionMode) {
    const label = ctx.page.getByText("订阅方式", { exact: true }).first();
    const formItem = label.locator("xpath=ancestor::*[contains(@class, 'ud__form__item')][1]");
    const editButton = formItem.getByRole("button").first();
    if (!(await editButton.isVisible().catch(() => false))) {
      throw new Error("未找到订阅方式编辑按钮。");
    }
    await editButton.click({ timeout: ctx.config.timeoutMs });
  }

  const websocketSelected = await clickByCandidates(
    ctx.page,
    ["使用长连接接收事件（WebSocket）", "使用长连接接收事件", "WebSocket", "长连接"],
    ctx.config.timeoutMs,
    ctx.logger
  );
  if (!websocketSelected) {
    await manualTakeover(ctx, "选择 WebSocket 长连接模式", ["请检查页面是否允许启用 WebSocket 长连接。"]);
  }

  ctx.logger.info("正在点击“验证”并保存 WebSocket 连通状态...");
  const modeDialog = (await findVisibleModal(ctx.page, Math.min(ctx.config.timeoutMs, 5000))) ?? ctx.page;
  const verifyMode = await clickByCandidates(modeDialog, ["验证"], ctx.config.timeoutMs, ctx.logger);
  if (verifyMode) {
    await waitForAnyText(ctx.page, ["验证成功", "连接成功", "可用"], Math.min(ctx.config.timeoutMs, 8000));
  }
  const saveMode = await clickByCandidates(modeDialog, ["保存"], ctx.config.timeoutMs, ctx.logger);
  if (!saveMode) {
    throw new Error("WebSocket 订阅方式验证后未找到保存按钮。");
  }
  await ctx.page.waitForTimeout(1500);

  ctx.logger.info("正在检查并添加需要的消息事件 (im.message.receive_v1 等)...");

  const missingEvents: string[] = [];
  for (const eventName of ctx.config.eventNames.filter((name) => !name.startsWith("card."))) {
    const alreadySubscribed = await waitForAnyText(ctx.page, [eventName], 1200);
    if (alreadySubscribed) {
      ctx.logger.info("事件已订阅，跳过：" + eventName);
    } else {
      missingEvents.push(eventName);
    }
  }

  // 注意：不能在消息事件齐全时提前 return —— 后面的“回调配置”（card.action.trigger）
  // 段必须始终执行，否则审批卡片回调可能漏配却把标志置成 true。
  if (missingEvents.length > 0) {
  const addEvent = await clickByCandidates(ctx.page, ["添加事件"], ctx.config.timeoutMs, ctx.logger);
  if (!addEvent) throw new Error("未找到可用的添加事件按钮。");
  const eventDialog = (await findVisibleModal(ctx.page, ctx.config.timeoutMs)) ?? ctx.page;
  const search = eventDialog.getByPlaceholder("搜索").first();

  for (const eventName of missingEvents) {
    await search.fill(eventName, { timeout: ctx.config.timeoutMs });
    const eventText = eventDialog.getByText(eventName, { exact: true }).first();
    await eventText.waitFor({ state: "visible", timeout: ctx.config.timeoutMs });
    const eventRow = eventText.locator("xpath=ancestor::*[.//input[@type='checkbox']][1]");
    const checkbox = eventRow.locator("input[type='checkbox']").first();
    if (!(await checkbox.isVisible().catch(() => false))) {
      throw new Error("未找到事件复选框：" + eventName);
    }
    await eventDialog.locator(".ud__loading__container-blur").waitFor({ state: "hidden", timeout: ctx.config.timeoutMs }).catch(() => undefined);
    await checkbox.check({ timeout: ctx.config.timeoutMs });
    if ((await checkbox.getAttribute("aria-checked")) !== "true") throw new Error("事件复选框未选中：" + eventName);
    ctx.logger.debug("已勾选事件：" + eventName);
  }

  const confirmAdd = await waitForEnabledAction(eventDialog, ["添加"], ctx.config.timeoutMs);
  if (!confirmAdd) throw new Error("勾选事件后未找到添加确认按钮。");
  await confirmAdd.locator.click({ timeout: ctx.config.timeoutMs });

  // 新版飞书在添加事件后会弹“推荐开通以下权限”二级确认框，必须先确认，否则
  // 主对话框不会关闭，“回调配置”页签也会被弹窗遮挡导致点击超时。
  const permissionConfirm = await waitForEnabledAction(
    ctx.page,
    ["申请权限", "确认开通", "开通权限", "确认", "确定"],
    Math.min(ctx.config.timeoutMs, 8000)
  );
  if (permissionConfirm) {
    ctx.logger.info("检测到“推荐开通以下权限”弹窗，已确认开通。");
    await permissionConfirm.locator.click({ timeout: ctx.config.timeoutMs }).catch(() => undefined);
  }

  // 关闭仍打开的“添加事件”主对话框（点了“添加”但没自动关的场景）。
  const cancelAdd = await waitForEnabledAction(eventDialog, ["取消"], 3000);
  if (cancelAdd) {
    await cancelAdd.locator.click({ timeout: 3000 }).catch(() => undefined);
  } else {
    await ctx.page.keyboard.press("Escape").catch(() => undefined);
  }
  await ctx.page.waitForTimeout(1000);

  for (const eventName of missingEvents) {
    const visible = await waitForAnyText(ctx.page, [eventName], ctx.config.timeoutMs);
    if (!visible) throw new Error("添加后未在事件列表中找到：" + eventName);
  }
  } // end if (missingEvents.length > 0)

  const callbackNames = ctx.config.eventNames.filter((name) => name.startsWith("card."));
  if (callbackNames.length > 0) {
    const callbackTab = await clickByCandidates(ctx.page, ["回调配置"], ctx.config.timeoutMs, ctx.logger);
    if (!callbackTab) throw new Error("未找到回调配置页签。");

    const missingCallbacks: string[] = [];
    for (const callbackName of callbackNames) {
      if (await waitForAnyText(ctx.page, [callbackName], 1200)) {
        ctx.logger.info("回调已订阅，跳过：" + callbackName);
      } else {
        missingCallbacks.push(callbackName);
      }
    }

    if (missingCallbacks.length > 0) {
      const callbackMode = await clickByCandidates(ctx.page, ["订阅方式"], ctx.config.timeoutMs, ctx.logger);
      if (callbackMode) {
        await clickByCandidates(ctx.page, ["使用长连接接收回调", "使用长连接接收事件", "WebSocket", "长连接"], ctx.config.timeoutMs, ctx.logger);
        const callbackModeDialog = (await findVisibleModal(ctx.page, 3000)) ?? ctx.page;
        await clickByCandidates(callbackModeDialog, ["验证"], Math.min(ctx.config.timeoutMs, 8000), ctx.logger);
        const callbackModeSaved = await clickByCandidates(callbackModeDialog, ["保存"], ctx.config.timeoutMs, ctx.logger);
        if (!callbackModeSaved) throw new Error("未能保存回调订阅方式。");
        await ctx.page.waitForTimeout(1200);
      }

      const addCallback = await clickByCandidates(ctx.page, ["添加回调"], ctx.config.timeoutMs, ctx.logger);
      if (!addCallback) throw new Error("未找到添加回调按钮。");
      const callbackDialog = (await findVisibleModal(ctx.page, ctx.config.timeoutMs)) ?? ctx.page;
      const callbackSearch = callbackDialog.getByPlaceholder("搜索").first();
      for (const callbackName of missingCallbacks) {
        await callbackSearch.fill(callbackName, { timeout: ctx.config.timeoutMs });
        const callbackText = callbackDialog.getByText(callbackName, { exact: true }).first();
        await callbackText.waitFor({ state: "visible", timeout: ctx.config.timeoutMs });
        const callbackRow = callbackText.locator("xpath=ancestor::*[.//input[@type='checkbox']][1]");
        const checkbox = callbackRow.locator("input[type='checkbox']").first();
        await checkbox.check({ timeout: ctx.config.timeoutMs });
        ctx.logger.debug("已勾选回调：" + callbackName);
      }
      const confirmCallback = await waitForEnabledAction(callbackDialog, ["添加"], ctx.config.timeoutMs);
      if (!confirmCallback) throw new Error("勾选回调后未找到添加确认按钮。");
      await confirmCallback.locator.click({ timeout: ctx.config.timeoutMs });
      for (const callbackName of missingCallbacks) {
        if (!(await waitForAnyText(ctx.page, [callbackName], ctx.config.timeoutMs))) {
          throw new Error("添加后未在回调列表中找到：" + callbackName);
        }
      }
    }
  }

  ctx.result.eventSubscriptionConfigured = true;
}

async function publishApp(ctx: StepContext): Promise<void> {
  const clicked = await clickByCandidates(ctx.page, ["版本管理与发布", "版本管理", "发布"], ctx.config.timeoutMs, ctx.logger);
  if (!clicked) {
    await manualTakeover(ctx, "进入版本管理与发布页面", [
      "请在左侧菜单中打开“版本管理与发布”。",
      "打开后保持在版本管理区域。"
    ]);
  }

  // 发版必要性判断：不做"已发布就跳过"的提前返回——旧版本发布时不一定带上了
  // 表单内的最新选项（如"允许机器人被添加到外部群中使用"），已发布状态下
  // "创建版本"按钮通常仍可点击，重发一版即可让选项生效。
  const hasPendingVersion = await waitForAnyText(ctx.page, ["待申请"], 2000);

  // 已存在"待申请"版本时，"创建版本"按钮会被禁用，发布表单在"查看版本详情"里。
  let enteredForm: string | null = null;
  if (hasPendingVersion) {
    ctx.logger.info("检测到待申请版本，优先通过“查看版本详情”进入发布表单。");
    enteredForm = await clickByCandidates(ctx.page, ["查看版本详情"], ctx.config.timeoutMs, ctx.logger);
  }
  if (!enteredForm) {
    enteredForm = await clickByCandidates(
      ctx.page,
      ["创建版本", "新建版本", "发布版本", "创建并发布"],
      ctx.config.timeoutMs,
      ctx.logger
    );
  }
  if (!enteredForm) {
    // 两条入口都进不去（已全部发布且创建按钮真被禁用）。外部群开关等表单内
    // 选项在此状态下无法自动修改——仅提醒，不中断整个流程。
    ctx.logger.warn(
      "无法进入发版表单（已全部发布且“创建版本”不可用）。若“允许机器人被添加到外部群中使用”尚未开启，请检查账号是否实名验证，或手动创建版本启用。"
    );
    ctx.result.published = true;
    return;
  }

  const versionDescription = "自动化初始化配置";
  const filled = await fillTextbox(ctx.page, ["版本说明", "更新说明", "发布说明", "说明"], versionDescription, ctx.config.timeoutMs);
  if (!filled) {
    ctx.logger.warn("未自动定位到版本说明输入框，可能需要人工填写。");
  }

  const availabilityLabel = ctx.page.getByText("可用范围", { exact: true }).first();
  await availabilityLabel.waitFor({ state: "visible", timeout: ctx.config.timeoutMs });
  const availabilityItem = availabilityLabel.locator("xpath=ancestor::*[contains(@class, 'ud__form__item')][1]");
  const allMembersConfigured = await waitForAnyText(ctx.page, ["全部成员", "所有员工"], 1200);
  if (!allMembersConfigured) {
    const editAvailability = ctx.page.getByRole("button", { name: "编辑", exact: true }).last();
    if (!(await editAvailability.isVisible().catch(() => false))) {
      throw new Error("未找到应用可用范围的编辑按钮。");
    }
    await editAvailability.click({ timeout: ctx.config.timeoutMs });
    const availabilityDialog = (await findVisibleModal(ctx.page, ctx.config.timeoutMs)) ?? ctx.page;
    const allMembersOption = await firstVisibleLocator(
      [
        availabilityDialog.getByRole("radio", { name: "全部成员" }),
        availabilityDialog.getByText("全部成员", { exact: true })
      ],
      ctx.config.timeoutMs
    );
    if (!allMembersOption) throw new Error("可用范围弹窗中未找到“全部成员”选项。");
    await allMembersOption.click({ timeout: ctx.config.timeoutMs });
    const confirmAvailability = await waitForEnabledAction(availabilityDialog, ["确定", "确认"], ctx.config.timeoutMs);
    if (!confirmAvailability) throw new Error("选择全部成员后未找到可用范围确认按钮。");
    await confirmAvailability.locator.click({ timeout: ctx.config.timeoutMs });
    await ctx.page.waitForTimeout(800);
  }
  const finalAvailability = await waitForAnyText(ctx.page, ["全部成员", "所有员工"], 3000);
  if (!finalAvailability) {
    throw new Error("应用可用范围未成功设置为全部成员，已停止发布。");
  }
  ctx.logger.info("应用可用范围已设置为全员：" + finalAvailability);

  // 尝试勾选“允许机器人被添加到外部群中使用”（群聊使用的前提之一）。
  // 该选项依赖账号实名状态：勾不动/不存在时仅提醒，绝不阻断发布流程。
  try {
    const extGroupLabel = ctx.page
      .locator(".ud__checkbox__label-content", { hasText: "允许机器人被添加到外部群中使用" })
      .first();
    const labelVisible = await extGroupLabel.isVisible({ timeout: 3000 }).catch(() => false);
    if (!labelVisible) {
      ctx.logger.info("未找到“允许机器人被添加到外部群中使用”选项（随账号/版本状态隐藏），跳过。");
    } else {
      const box = extGroupLabel.locator("xpath=ancestor::*[contains(@class,'ud__checkbox')][1]");
      const input = box.locator("input[type='checkbox']").first();
      const already = await input.isChecked().catch(() => false);
      if (already) {
        ctx.logger.info("“允许机器人被添加到外部群中使用”已勾选。");
      } else {
        await input.check({ timeout: 3000 }).catch(async () => {
          await extGroupLabel.click({ timeout: 3000 }).catch(() => undefined);
        });
        const now = await input.isChecked().catch(() => false);
        if (now) {
          ctx.logger.info("已勾选“允许机器人被添加到外部群中使用”。");
        } else {
          ctx.logger.warn("“允许机器人被添加到外部群中使用”无法勾选，请检查账号是否实名验证。");
        }
      }
    }
  } catch {
    ctx.logger.warn("“允许机器人被添加到外部群中使用”处理异常，请检查账号是否实名验证。");
  }

  // "保存"只存在于"创建新版本"表单；待申请版本的详情页直接提供"确认发布"。
  const saveVersion = await waitForEnabledAction(ctx.page, ["保存"], Math.min(ctx.config.timeoutMs, 8000));
  if (saveVersion) {
    await saveVersion.locator.click({ timeout: ctx.config.timeoutMs });
    ctx.logger.info("已点击“保存”。");
  } else {
    ctx.logger.info("未找到“保存”按钮（待申请版本详情页），直接进入“确认发布”。");
  }

  // 保存后飞书可能弹出确认对话框（ud__dialog），不处理会拦截后续“确认发布”的点击。
  await ctx.page.waitForTimeout(1000);
  const postSaveDialog = await findVisibleModal(ctx.page, 4000);
  if (postSaveDialog) {
    ctx.logger.info("保存后检测到弹窗，优先处理弹窗内的确认动作...");
    const dialogConfirm = await waitForEnabledAction(
      postSaveDialog,
      ["确认发布", "确认", "确定", "知道了", "继续发布"],
      Math.min(ctx.config.timeoutMs, 8000)
    );
    if (dialogConfirm) {
      await dialogConfirm.locator.click({ timeout: Math.min(ctx.config.timeoutMs, 10000) }).catch(() => undefined);
      ctx.logger.info("已点击弹窗内的确认按钮：" + dialogConfirm.text);
    } else {
      // 信息型弹窗：关闭后再继续页面上的“确认发布”
      const closer = await waitForEnabledAction(postSaveDialog, ["取消", "关闭"], 3000);
      if (closer) {
        await closer.locator.click({ timeout: 3000 }).catch(() => undefined);
      } else {
        await ctx.page.keyboard.press("Escape").catch(() => undefined);
      }
    }
    await ctx.page.waitForTimeout(1200);
  }

  const shouldPublish = isNonInteractivePrompt()
    ? true
    : await promptYesNo(ctx.prompt, "版本配置已完成，是否确认提交发布该应用版本？", true);

  if (!shouldPublish) {
    ctx.logger.info("用户暂停提交发布应用。后续您可在飞书开放平台后台手动点击发布。");
    ctx.result.published = false;
    return;
  }

  // 弹窗内的确认可能已直接触发发布 → 先查成功状态，未成功才点页面上的“确认发布”。
  const alreadyPublished = await waitForAnyText(ctx.page, ["已发布", "发布成功", "审核中", "已提交"], 3000);
  if (!alreadyPublished) {
    const confirmPublish = await waitForEnabledAction(ctx.page, ["确认发布"], ctx.config.timeoutMs);
    if (!confirmPublish) throw new Error("保存版本后未找到确认发布按钮。");
    await confirmPublish.locator.click({ timeout: ctx.config.timeoutMs });
  }

  const success = await waitForAnyText(ctx.page, ["已发布", "发布成功", "审核中", "已提交"], ctx.config.timeoutMs);
  if (!success) throw new Error("版本提交后未检测到发布成功状态。");

  ctx.result.published = true;
}

async function main(): Promise<void> {
  const rootDir = process.cwd();
  const logger = createLogger(DEBUG_ENABLED ? "debug" : "info");
  clearDeadProxyEnvironment(logger);
  const prompt = readline.createInterface({ input, output });

  let browser: Browser | null = null;
  let context: BrowserContext | null = null;

  try {
    const config = await loadConfig(rootDir);
    await ensureDir(config.screenshotsDir);
    await ensureDir(config.htmlDumpDir);

    const result = await createInitialResult(config);
    await persistResult(config, result);

    logger.info("项目文件结构：");
    logger.info(". / myclaw_Installer.bat / myclaw_Installer.ps1 / config.json / feishu-permissions.json / package.json / README.md / tsconfig.json / src/feishu-setup.ts");

    const hasStorageState = await pathExists(config.storageStatePath);
    const hasUserData = await pathExists(config.userDataDirPath);

    if ((hasStorageState || hasUserData) && !isNonInteractivePrompt()) {
      const reuseUserData = await promptYesNo(
        prompt,
        "检测到本地已存在飞书登录用户数据，是否沿用当前用户数据？",
        true
      );
      if (!reuseUserData) {
        logger.info("用户选择不沿用，正在清理历史登录数据与 Session 缓存...");
        if (hasStorageState) {
          await rm(config.storageStatePath, { force: true }).catch(() => undefined);
        }
        if (hasUserData) {
          await rm(config.userDataDirPath, { recursive: true, force: true }).catch(() => undefined);
        }
      } else {
        logger.info("确认沿用当前飞书登录用户数据。");
      }
    }

    await ensureDir(config.userDataDirPath);
    const storageState = (await pathExists(config.storageStatePath)) ? config.storageStatePath : undefined;
    context = await chromium.launchPersistentContext(config.userDataDirPath, {
      headless: false,
      args: ["--no-proxy-server"],
      ...(storageState ? { storageState } : {})
    });
    browser = context.browser();

    const page = context.pages()[0] ?? (await context.newPage());
    page.setDefaultTimeout(config.timeoutMs);

    const ctx: StepContext = {
      browser,
      context,
      page,
      config,
      result,
      runtimeAppSecret: null,
      logger,
      prompt
    };

    await executeStep(ctx, "打开飞书开放平台并等待用户登录", async () => {
      await waitForLogin(ctx);
    });

    await executeStep(ctx, "创建或复用飞书应用", async () => {
      await createOrOpenApp(ctx);
    });

    await executeStep(ctx, "获取 App ID 和 App Secret", async () => {
      await fetchCredentials(ctx);
    });

    await executeStep(ctx, "导入飞书权限配置", async () => {
      await importPermissionsV2(ctx);
    });

    await executeStep(ctx, "启用机器人能力并设置机器人名称", async () => {
      await enableBotCapability(ctx);
    });

    if (config.enableEventSubscription) {
      await executeStep(
        ctx,
        "配置事件订阅",
        async () => {
          await configureEventSubscription(ctx);
        },
        { recoverable: true }
      );
    }

    if (config.publishAfterSetup) {
      await executeStep(
        ctx,
        "创建并发布应用版本",
        async () => {
          await publishApp(ctx);
        },
        { recoverable: true }
      );
    }

    ctx.result.status = "completed";
    await persistResult(config, ctx.result);
    logger.info(`结果文件已写入：${config.resultPath}`);
    logger.info(`App Secret 已掩码显示：${ctx.result.maskedSecret ?? "未获取"}`);
    logger.info("交付完成：.env 已写入飞书凭据，结果 JSON 未保存 Secret，可启动本地 claw 并在飞书测试机器人。");
  } catch (error) {
    if (error instanceof AutomationStepError) {
      output.write(`\n失败步骤：${error.step}\n`);
      output.write(`错误信息：${error.message}\n`);
      output.write(`截图：${error.screenshotPath ?? "未保存"}\n`);
      output.write(`HTML：${error.htmlPath ?? "未保存"}\n`);
    } else if (error instanceof Error) {
      output.write(`\n执行失败：${error.message}\n`);
    } else {
      output.write(`\n执行失败：${String(error)}\n`);
    }

    throw error;
  } finally {
    if (context) {
      await context.close();
    }
    if (browser) {
      await browser.close();
    }
    prompt.close();
  }
}

async function mainV2(): Promise<void> {
  const rootDir = process.cwd();
  const logger = createLogger(DEBUG_ENABLED ? "debug" : "info");
  clearDeadProxyEnvironment(logger);
  const prompt = readline.createInterface({ input, output });

  let browser: Browser | null = null;
  let context: BrowserContext | null = null;

  try {
    const config = await loadConfig(rootDir);
    await ensureDir(config.screenshotsDir);
    await ensureDir(config.htmlDumpDir);

    const result = await createInitialResult(config);
    await persistResult(config, result);

    logger.info("项目文件：");
    logger.info(". / myclaw_Installer.bat / myclaw_Installer.ps1 / config.json / feishu-permissions.json / package.json / README.md / tsconfig.json / src/feishu-setup.ts");

    const hasStorageState = await pathExists(config.storageStatePath);
    const hasUserData = await pathExists(config.userDataDirPath);

    if (hasStorageState || hasUserData) {
      const reuseUserData = isNonInteractivePrompt()
        ? true
        : await promptYesNo(
          prompt,
          "检测到本地已存在飞书登录用户数据，是否沿用当前用户数据？",
          true
        );
      if (!reuseUserData) {
        logger.info("用户选择不沿用，正在清理历史登录数据与 Session 缓存...");
        if (hasStorageState) {
          await rm(config.storageStatePath, { force: true }).catch(() => undefined);
        }
        if (hasUserData) {
          await rm(config.userDataDirPath, { recursive: true, force: true }).catch(() => undefined);
        }
      } else {
        logger.info("确认沿用当前飞书登录用户数据。");
      }
    }

    await ensureDir(config.userDataDirPath);
    const storageState = (await pathExists(config.storageStatePath)) ? config.storageStatePath : undefined;
    context = await chromium.launchPersistentContext(config.userDataDirPath, {
      headless: false,
      args: ["--no-proxy-server"],
      ...(storageState ? { storageState } : {})
    });
    browser = context.browser();

    const page = context.pages()[0] ?? (await context.newPage());
    page.setDefaultTimeout(config.timeoutMs);

    const ctx: StepContext = {
      browser,
      context,
      page,
      config,
      result,
      runtimeAppSecret: null,
      logger,
      prompt
    };

    await executeStep(ctx, "打开飞书开放平台并等待登录", async () => {
      await waitForLogin(ctx);
    });

    await executeStep(ctx, "创建应用或复用已有应用", async () => {
      await createOrOpenApp(ctx);
    });

    await executeStep(ctx, "读取飞书 App ID 和 App Secret 凭据", async () => {
      const existingCredentials = await readFeishuCredentials(config.envPath);
      if (
        existingCredentials
        && ctx.result.appId
        && existingCredentials.appId === ctx.result.appId
      ) {
        ctx.result.appId = existingCredentials.appId;
        ctx.runtimeAppSecret = existingCredentials.appSecret;
        ctx.result.maskedSecret = maskSecret(existingCredentials.appSecret);
        logger.info("已读取根目录 .env 中的现有飞书凭据。");
      } else {
        await fetchCredentials(ctx);
      }
      // 立即写入 .env：事件订阅步骤要求本地服务带着有效凭据建立 WS 长连接，
      // 不能等到发布成功后才同步（全新部署时 .env 是空的，服务会起成"无凭据"状态）。
      if (ctx.result.appId && ctx.runtimeAppSecret) {
        await writeFeishuCredentials(config.envPath, ctx.result.appId, ctx.runtimeAppSecret);
        logger.info("已将最新凭据写入根目录 .env（供本地服务 WebSocket 连接使用）。");
      }
    });

    // 跳过决策基于线上活体校验，不信任 result.json 的历史标志：
    // 后台手动改动（权限关闭/事件退订）不会使缓存标志失效。
    const permLive = await verifyPermissionsLive(ctx);
    if (!permLive.ok) {
      logger.info(`线上校验：飞书权限异常（${permLive.detail || "未通过"}），执行导入...`);
      await executeStep(ctx, "导入飞书权限", async () => {
        await importPermissionsV2(ctx);
      });
    } else {
      logger.info("线上校验：飞书权限已开通，跳过导入。");
    }

    if (!ctx.result.botEnabled) {
      await executeStep(ctx, "启用机器人能力", async () => {
        await enableBotCapability(ctx);
      });
    } else {
      logger.info("续跑：机器人能力已启用，跳过。");
    }

    if (config.enableEventSubscription) {
      const evLive = await verifyEventSubscriptionLive(ctx);
      if (!evLive.ok) {
        logger.info(`线上校验：事件订阅异常（${evLive.detail || "未全部通过"}），执行配置...`);
        await executeStep(ctx, "配置 WebSocket 事件订阅", async () => {
          await configureEventSubscription(ctx);
        });
      } else {
        logger.info("线上校验：事件订阅与回调均在线，跳过配置。");
      }
    }

    if (config.publishAfterSetup) {
      const pubLive = await verifyPublishLive(ctx);
      if (!pubLive.ok) {
        logger.info(`线上校验：${pubLive.detail}，执行发版...`);
        await executeStep(ctx, "创建并发布飞书应用版本", async () => {
          await publishApp(ctx);
        });
      } else {
        ctx.result.published = true;
        logger.info("线上校验：无待发布修改，跳过发版。");
      }
    }

    if (ctx.result.published && ctx.result.appId && ctx.runtimeAppSecret) {
      await executeStep(ctx, "同步 App ID 和 App Secret 到根目录 .env", async () => {
        await writeFeishuCredentials(config.envPath, ctx.result.appId!, ctx.runtimeAppSecret!);
        logger.info("已成功同步验证发布无误的飞书凭据到根目录 .env。");
      });
    } else {
      logger.info("提示：应用未提交发布，已跳过根目录 .env 的凭据写入，保持原有配置不被覆盖。");
    }

    ctx.result.status = "completed";
    await persistResult(config, ctx.result);
    logger.info(`结果文件已写入：${config.resultPath}`);
    logger.info(`App Secret 脱敏预览：${ctx.result.maskedSecret ?? "未抓取"}`);
    logger.info("交付完成：结果 JSON 未保存 Secret，可在飞书测试机器人。");
  } catch (error) {
    if (error instanceof AutomationStepError) {
      output.write(`\n步骤失败：${error.step}\n`);
      output.write(`错误信息：${error.message}\n`);
      output.write(`截图：${error.screenshotPath ?? "未保存"}\n`);
      output.write(`HTML：${error.htmlPath ?? "未保存"}\n`);
    } else if (error instanceof Error) {
      output.write(`\n执行失败：${error.message}\n`);
    } else {
      output.write(`\n执行失败：${String(error)}\n`);
    }

    throw error;
  } finally {
    if (context) {
      await context.close();
    }
    if (browser) {
      await browser.close();
    }
    prompt.close();
  }
}

mainV2().catch(() => {
  process.exitCode = 1;
});
