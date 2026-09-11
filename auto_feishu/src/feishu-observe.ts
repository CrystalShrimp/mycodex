import { appendFile, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { stdout as output } from "node:process";
import { chromium, type Page } from "playwright";

interface RawConfig {
  screenshotsDir: string;
  htmlDumpDir: string;
  userDataDirPath?: string;
  timeoutMs: number;
}

interface LoadedConfig {
  rootDir: string;
  screenshotsDir: string;
  htmlDumpDir: string;
  userDataDirPath: string;
  timeoutMs: number;
  observeDir: string;
  observeLogPath: string;
}

interface ObserveTarget {
  tag: string;
  id: string | null;
  classes: string[];
  text: string | null;
  role: string | null;
  name: string | null;
  placeholder: string | null;
}

type ObserveEvent =
  | {
      type: "page-attached" | "navigated" | "load" | "domcontentloaded";
      url: string;
      title: string;
      at: string;
      pageId: string;
      dialogs?: string[];
      screenshotPath?: string | null;
      htmlPath?: string | null;
    }
  | {
      type: "click" | "input" | "change";
      at: string;
      pageId: string;
      url: string;
      title: string;
      target: {
        tag: string;
        id: string | null;
        classes: string[];
        text: string | null;
        role: string | null;
        name: string | null;
        placeholder: string | null;
      };
      dialogs?: string[];
      screenshotPath?: string | null;
      htmlPath?: string | null;
    }
  | {
      type: "dialog";
      at: string;
      pageId: string;
      url: string;
      title: string;
      message: string;
    }
  | {
      type: "requestfailed";
      at: string;
      pageId: string;
      url: string;
      title: string;
      requestUrl: string;
      method: string;
      failure: string | null;
    }
  | {
      type: "console";
      at: string;
      pageId: string;
      url: string;
      title: string;
      level: string;
      text: string;
    }
  | {
      type: "pageerror";
      at: string;
      pageId: string;
      url: string;
      title: string;
      message: string;
    };

function nowIso(): string {
  return new Date().toISOString();
}

function logLine(message: string): void {
  const ts = new Date().toISOString().replace("T", " ").replace("Z", "");
  output.write(`[${ts}] ${message}\n`);
}

function sanitizeFilePart(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 80) || "step";
}

async function ensureDir(dirPath: string): Promise<void> {
  await mkdir(dirPath, { recursive: true });
}

function resolveFromRoot(rootDir: string, candidate?: string): string | undefined {
  if (!candidate) {
    return undefined;
  }

  return path.isAbsolute(candidate) ? candidate : path.resolve(rootDir, candidate);
}

async function loadConfig(rootDir: string): Promise<LoadedConfig> {
  const configPath = path.resolve(rootDir, "config.json");
  const raw = JSON.parse(await readFile(configPath, "utf8")) as RawConfig;
  const observeDir = path.resolve(rootDir, "artifacts", "observe");
  const runId = new Date().toISOString().replace(/[:.]/g, "-");

  return {
    rootDir,
    screenshotsDir: resolveFromRoot(rootDir, raw.screenshotsDir) ?? path.resolve(rootDir, "artifacts", "screenshots"),
    htmlDumpDir: resolveFromRoot(rootDir, raw.htmlDumpDir) ?? path.resolve(rootDir, "artifacts", "html"),
    userDataDirPath:
      resolveFromRoot(rootDir, raw.userDataDirPath) ?? path.resolve(rootDir, "artifacts", "feishu-user-data"),
    timeoutMs: raw.timeoutMs ?? 20_000,
    observeDir,
    observeLogPath: path.join(observeDir, `events-${runId}.jsonl`)
  };
}

async function appendEvent(config: LoadedConfig, event: ObserveEvent): Promise<void> {
  await appendFile(config.observeLogPath, `${JSON.stringify(event)}\n`, "utf8");
}

async function collectVisibleDialogs(page: Page): Promise<string[]> {
  try {
    return await page.evaluate(`
      (() => {
        const selectors = [
          '[role="dialog"]',
          '.arco-modal',
          '.semi-modal',
          '.ud-modal',
          '.arco-drawer',
          ".semi-portal [aria-modal='true']"
        ];
        const texts = new Set();
        for (const selector of selectors) {
          for (const element of Array.from(document.querySelectorAll(selector))) {
            const style = window.getComputedStyle(element);
            if (style.display === 'none' || style.visibility === 'hidden') {
              continue;
            }
            const text = (element.innerText || '').replace(/\\s+/g, ' ').trim();
            if (text) {
              texts.add(text.slice(0, 500));
            }
          }
        }
        return Array.from(texts);
      })()
    `);
  } catch {
    return [];
  }
}

async function snapshot(page: Page, config: LoadedConfig, pageId: string, reason: string): Promise<{
  screenshotPath: string | null;
  htmlPath: string | null;
}> {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const prefix = `${stamp}-${sanitizeFilePart(pageId)}-${sanitizeFilePart(reason)}`;
  const screenshotPath = path.join(config.observeDir, `${prefix}.png`);
  const htmlPath = path.join(config.observeDir, `${prefix}.html`);

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

  return {
    screenshotPath: savedScreenshot,
    htmlPath: savedHtml
  };
}

async function bindPage(page: Page, config: LoadedConfig, pageId: string): Promise<void> {
  const emitState = async (type: "page-attached" | "navigated" | "load" | "domcontentloaded", reason: string) => {
    const title = await page.title().catch(() => "");
    const dialogs = await collectVisibleDialogs(page);
    const artifacts = await snapshot(page, config, pageId, reason);
    await appendEvent(config, {
      type,
      at: nowIso(),
      pageId,
      url: page.url(),
      title,
      dialogs,
      screenshotPath: artifacts.screenshotPath,
      htmlPath: artifacts.htmlPath
    });
    logLine(`${type}: ${title || "(no-title)"} -> ${page.url()}`);
  };

  await page.exposeBinding("__codexObserveEvent", async (_source, payload: ObserveTarget & { eventType: "click" | "input" | "change" }) => {
    const title = await page.title().catch(() => "");
    const dialogs = await collectVisibleDialogs(page);
    const shouldSnapshot = payload.eventType === "click" || (payload.eventType === "change" && payload.tag === "TEXTAREA");
    const artifacts = shouldSnapshot ? await snapshot(page, config, pageId, payload.eventType) : { screenshotPath: null, htmlPath: null };
    await appendEvent(config, {
      type: payload.eventType,
      at: nowIso(),
      pageId,
      url: page.url(),
      title,
      target: {
        tag: payload.tag,
        id: payload.id,
        classes: payload.classes,
        text: payload.text,
        role: payload.role,
        name: payload.name,
        placeholder: payload.placeholder
      },
      dialogs,
      screenshotPath: artifacts.screenshotPath,
      htmlPath: artifacts.htmlPath
    });
    logLine(`${payload.eventType}: ${payload.tag} ${payload.text ?? payload.name ?? ""}`.trim());
  });

  await page.addInitScript({
    content: `
      (() => {
        const describeElement = (element) => {
          if (!(element instanceof HTMLElement)) {
            return null;
          }
          const text = (element.innerText || '').replace(/\\s+/g, ' ').trim() || null;
          return {
            tag: element.tagName,
            id: element.id || null,
            classes: Array.from(element.classList),
            text: text ? text.slice(0, 160) : null,
            role: element.getAttribute('role'),
            name: element.getAttribute('name'),
            placeholder: element.getAttribute('placeholder')
          };
        };

        const report = (eventType, target) => {
          const base = target instanceof Element
            ? (target.closest("button, a, input, textarea, [role='button'], [role='tab'], [role='option'], .monaco-editor, .app-card") || target)
            : null;
          const described = describeElement(base);
          if (!described) {
            return;
          }
          window.setTimeout(() => {
            const notify = window.__codexObserveEvent;
            if (typeof notify === 'function') {
              notify({
                eventType,
                ...described
              });
            }
          }, 0);
        };

        document.addEventListener('click', (event) => {
          report('click', event.target);
        }, true);

        document.addEventListener('input', (event) => {
          const element = event.target;
          if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
            report('input', element);
          }
        }, true);

        document.addEventListener('change', (event) => {
          report('change', event.target);
        }, true);
      })();
    `
  });

  page.on("dialog", async (dialog) => {
    const title = await page.title().catch(() => "");
    await appendEvent(config, {
      type: "dialog",
      at: nowIso(),
      pageId,
      url: page.url(),
      title,
      message: dialog.message()
    });
    logLine(`dialog: ${dialog.message()}`);
  });

  page.on("requestfailed", async (request) => {
    const title = await page.title().catch(() => "");
    await appendEvent(config, {
      type: "requestfailed",
      at: nowIso(),
      pageId,
      url: page.url(),
      title,
      requestUrl: request.url(),
      method: request.method(),
      failure: request.failure()?.errorText ?? null
    });
  });

  page.on("console", async (message) => {
    if (message.type() === "error" || message.type() === "warning") {
      const title = await page.title().catch(() => "");
      await appendEvent(config, {
        type: "console",
        at: nowIso(),
        pageId,
        url: page.url(),
        title,
        level: message.type(),
        text: message.text()
      });
    }
  });

  page.on("pageerror", async (error) => {
    const title = await page.title().catch(() => "");
    await appendEvent(config, {
      type: "pageerror",
      at: nowIso(),
      pageId,
      url: page.url(),
      title,
      message: error.message
    });
  });

  page.on("domcontentloaded", () => {
    void emitState("domcontentloaded", "domcontentloaded");
  });

  page.on("load", () => {
    void emitState("load", "load");
  });

  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) {
      void emitState("navigated", "navigated");
    }
  });

  await emitState("page-attached", "attached");
}

async function main(): Promise<void> {
  const rootDir = process.cwd();
  const config = await loadConfig(rootDir);
  await ensureDir(config.observeDir);
  await ensureDir(config.userDataDirPath);
  await ensureDir(config.screenshotsDir);
  await ensureDir(config.htmlDumpDir);

  const latestPath = path.join(config.observeDir, "latest-run.json");
  await writeFile(
    latestPath,
    JSON.stringify(
      {
        startedAt: nowIso(),
        observeLogPath: config.observeLogPath,
        userDataDirPath: config.userDataDirPath
      },
      null,
      2
    ),
    "utf8"
  );

  logLine(`观察日志: ${config.observeLogPath}`);
  logLine(`持久登录目录: ${config.userDataDirPath}`);

  const context = await chromium.launchPersistentContext(config.userDataDirPath, {
    headless: false,
    viewport: { width: 1440, height: 960 }
  });

  const seenPages = new WeakSet<Page>();
  let pageCounter = 0;

  const attach = async (page: Page) => {
    if (seenPages.has(page)) {
      return;
    }
    seenPages.add(page);
    pageCounter += 1;
    await bindPage(page, config, `page-${pageCounter}`);
  };

  for (const page of context.pages()) {
    await attach(page);
  }

  context.on("page", (page) => {
    void attach(page);
  });

  const firstPage = context.pages()[0] ?? (await context.newPage());
  const currentUrl = firstPage.url();
  if (!currentUrl || currentUrl === "about:blank") {
    await firstPage.goto("https://open.feishu.cn/app", {
      waitUntil: "domcontentloaded",
      timeout: config.timeoutMs
    });
  }

  logLine("观察模式已启动。你现在可以手动演示，关闭浏览器窗口即可结束。");

  await new Promise<void>((resolve) => {
    context.on("close", () => resolve());
  });

  logLine("观察模式已结束。");
}

void main().catch((error) => {
  logLine(`观察模式失败: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
  process.exitCode = 1;
});
