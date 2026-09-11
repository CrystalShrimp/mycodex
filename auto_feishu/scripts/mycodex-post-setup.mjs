#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const args = { reportFile: null };

  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--report-file") {
      args.reportFile = argv[index + 1] ?? null;
      index += 1;
      continue;
    }
  }

  return args;
}

function resolvePackageRoot() {
  const packageRoot = process.env.MYCODEX_PACKAGE_ROOT?.trim();
  if (!packageRoot) {
    throw new Error("缺少 MYCODEX_PACKAGE_ROOT，无法定位 mycodex 安装目录。");
  }

  return path.resolve(packageRoot);
}

function toModuleUrl(filePath) {
  return pathToFileURL(filePath).href;
}

async function importDistModule(packageRoot, relativePath) {
  const targetPath = path.join(packageRoot, "dist", ...relativePath.split("/"));
  return import(toModuleUrl(targetPath));
}

function collectHookReasons(hook) {
  const reasons = [];

  if (hook.disabled) {
    reasons.push("已在配置中禁用");
  }

  const bins = Array.isArray(hook.missing?.bins) ? hook.missing.bins : [];
  const env = Array.isArray(hook.missing?.env) ? hook.missing.env : [];
  const config = Array.isArray(hook.missing?.config) ? hook.missing.config : [];

  if (bins.length > 0) {
    reasons.push(`缺少命令：${bins.join(", ")}`);
  }

  if (env.length > 0) {
    reasons.push(`缺少环境变量：${env.join(", ")}`);
  }

  if (config.length > 0) {
    reasons.push(`缺少配置项：${config.join(", ")}`);
  }

  if (hook.managedByPlugin) {
    reasons.push("由插件管理");
  }

  if (reasons.length === 0) {
    reasons.push("当前不符合启用条件");
  }

  return reasons;
}

function collectSkillReasons(skill) {
  const reasons = [];

  if (skill.disabled) {
    reasons.push("已在配置中禁用");
  }

  if (skill.blockedByAllowlist) {
    reasons.push("被 allowlist 阻止");
  }

  const bins = Array.isArray(skill.missing?.bins) ? skill.missing.bins : [];
  const env = Array.isArray(skill.missing?.env) ? skill.missing.env : [];
  const config = Array.isArray(skill.missing?.config) ? skill.missing.config : [];

  if (bins.length > 0) {
    reasons.push(`缺少命令：${bins.join(", ")}`);
  }

  if (env.length > 0) {
    reasons.push(`缺少环境变量：${env.join(", ")}`);
  }

  if (config.length > 0) {
    reasons.push(`缺少配置项：${config.join(", ")}`);
  }

  if (reasons.length === 0) {
    reasons.push("仍需人工确认");
  }

  return reasons;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const packageRoot = resolvePackageRoot();

  const [{ loadConfig, writeConfigFile }, { resolveAgentWorkspaceDir, resolveDefaultAgentId }, { buildWorkspaceHookStatus }, { buildWorkspaceSkillStatus }, { installSkill }, { getRemoteSkillEligibility }] = await Promise.all([
    importDistModule(packageRoot, "config/config.js"),
    importDistModule(packageRoot, "agents/agent-scope.js"),
    importDistModule(packageRoot, "hooks/hooks-status.js"),
    importDistModule(packageRoot, "agents/skills-status.js"),
    importDistModule(packageRoot, "agents/skills-install.js"),
    importDistModule(packageRoot, "infra/skills-remote.js"),
  ]);

  let config = loadConfig();
  const agentId = resolveDefaultAgentId(config);
  const workspaceDir = resolveAgentWorkspaceDir(config, agentId);

  console.log(`[mycodex 自动补全] 工作区：${workspaceDir}`);

  const hookReport = buildWorkspaceHookStatus(workspaceDir, { config });
  const eligibleHooks = hookReport.hooks.filter((hook) => hook.eligible);
  const skippedHooks = hookReport.hooks
    .filter((hook) => !hook.eligible)
    .map((hook) => ({
      name: hook.name,
      hookKey: hook.hookKey,
      reasons: collectHookReasons(hook),
    }));

  const enabledHooks = [];

  if (eligibleHooks.length > 0) {
    console.log(`[mycodex 自动补全] 正在启用 ${eligibleHooks.length} 个可用 hooks...`);

    const entries = { ...(config.hooks?.internal?.entries ?? {}) };
    for (const hook of eligibleHooks) {
      entries[hook.hookKey] = {
        ...(entries[hook.hookKey] ?? {}),
        enabled: true,
      };
      enabledHooks.push({
        name: hook.name,
        hookKey: hook.hookKey,
      });
      console.log(`[mycodex 自动补全] Hook 已启用：${hook.name}`);
    }

    config = {
      ...config,
      hooks: {
        ...config.hooks,
        internal: {
          ...config.hooks?.internal,
          enabled: true,
          entries,
        },
      },
    };

    await writeConfigFile(config);
  } else {
    console.log("[mycodex 自动补全] 未发现可自动启用的 hooks。");
  }

  const initialSkillReport = buildWorkspaceSkillStatus(workspaceDir, {
    config,
    eligibility: { remote: getRemoteSkillEligibility() },
  });

  const missingSkills = initialSkillReport.skills.filter(
    (skill) => !skill.eligible && !skill.disabled && !skill.blockedByAllowlist,
  );

  const installableSkills = missingSkills.filter(
    (skill) => Array.isArray(skill.install) && skill.install.length > 0 && Array.isArray(skill.missing?.bins) && skill.missing.bins.length > 0,
  );

  const skillInstallResults = [];

  if (installableSkills.length > 0) {
    console.log(`[mycodex 自动补全] 正在安装 ${installableSkills.length} 个可自动安装的 skills 依赖...`);
  } else {
    console.log("[mycodex 自动补全] 没有需要自动安装的 skills 依赖。");
  }

  for (const skill of installableSkills) {
    const installSpec = skill.install[0];
    const installId = installSpec?.id;
    const installLabel = installSpec?.label ?? installId ?? "默认安装方式";

    if (!installId) {
      skillInstallResults.push({
        name: skill.name,
        installLabel,
        ok: false,
        message: "缺少 installId，无法自动安装",
      });
      console.log(`[mycodex 自动补全] Skill 跳过：${skill.name}（缺少 installId）`);
      continue;
    }

    console.log(`[mycodex 自动补全] 安装 Skill 依赖：${skill.name} -> ${installLabel}`);

    try {
      const result = await installSkill({
        workspaceDir,
        skillName: skill.name,
        installId,
        config,
      });

      skillInstallResults.push({
        name: skill.name,
        installLabel,
        ok: result.ok === true,
        message: result.message,
        code: result.code ?? null,
        warnings: result.warnings ?? [],
      });

      if (result.ok) {
        console.log(`[mycodex 自动补全] Skill 已安装：${skill.name}`);
      } else {
        console.log(`[mycodex 自动补全] Skill 安装失败：${skill.name} -> ${result.message}`);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      skillInstallResults.push({
        name: skill.name,
        installLabel,
        ok: false,
        message,
        code: null,
        warnings: [],
      });
      console.log(`[mycodex 自动补全] Skill 安装失败：${skill.name} -> ${message}`);
    }
  }

  const finalSkillReport = buildWorkspaceSkillStatus(workspaceDir, {
    config,
    eligibility: { remote: getRemoteSkillEligibility() },
  });

  const manualSkills = finalSkillReport.skills
    .filter((skill) => !skill.eligible && !skill.disabled && !skill.blockedByAllowlist)
    .map((skill) => ({
      name: skill.name,
      skillKey: skill.skillKey,
      reasons: collectSkillReasons(skill),
    }));

  const report = {
    status: "completed",
    generatedAt: new Date().toISOString(),
    workspaceDir,
    enabledHooks,
    skippedHooks,
    skillInstallResults,
    manualSkills,
  };

  if (args.reportFile) {
    const reportPath = path.resolve(args.reportFile);
    await fs.mkdir(path.dirname(reportPath), { recursive: true });
    await fs.writeFile(reportPath, JSON.stringify(report, null, 2), "utf8");
  }

  const installedCount = skillInstallResults.filter((item) => item.ok).length;
  const failedCount = skillInstallResults.filter((item) => !item.ok).length;

  console.log(`[mycodex 自动补全] Hooks 已启用：${enabledHooks.length} 个。`);
  console.log(`[mycodex 自动补全] Skills 自动安装成功：${installedCount} 个，失败：${failedCount} 个，仍需人工处理：${manualSkills.length} 个。`);
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`[mycodex 自动补全] 执行失败：${message}`);
  process.exitCode = 1;
});
