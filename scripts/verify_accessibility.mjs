#!/usr/bin/env node
/**
 * Accessibility audit for ResiliNet pages (Task 24).
 *
 * Boots the real FastAPI app (replay mode, throwaway seeded SQLite file),
 * then runs axe (WCAG 2.1 AA tags), a keyboard walk, a 200%-equivalent
 * narrow-viewport check, and Lighthouse accessibility on the public pages.
 * Tool verdicts decide pass/fail; violations are printed in full, never
 * suppressed. A deliberately inaccessible fixture page proves the checker
 * can fail.
 *
 * Usage: pnpm audit   (or: node scripts/verify_accessibility.mjs)
 */
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";
import * as chromeLauncher from "chrome-launcher";
import lighthouse from "lighthouse";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const PORT = 8791;
const BASE = `http://127.0.0.1:${PORT}`;
const AXE_SOURCE = readFileSync(
  join(ROOT, "node_modules/axe-core/axe.min.js"),
  "utf8"
);

const failures = [];
const notes = [];

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: ROOT,
    encoding: "utf8",
    ...options,
  });
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(" ")} failed (${result.status}):\n${result.stdout}\n${result.stderr}`
    );
  }
  return result.stdout;
}

async function waitForHealth(deadlineMs = 60_000) {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${BASE}/api/v1/health`);
      if (response.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("server did not become healthy in time");
}

async function axeAudit(page, label, { expectViolations = false } = {}) {
  await page.addScriptTag({ content: AXE_SOURCE });
  const results = await page.evaluate(async () => {
    return await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
    });
  });
  const serious = results.violations.filter((v) =>
    ["serious", "critical"].includes(v.impact)
  );
  for (const violation of results.violations) {
    notes.push(
      `axe[${label}] ${violation.impact}: ${violation.id} — ${violation.help} ` +
        `(${violation.nodes.length} node(s), e.g. ${violation.nodes[0]?.target?.join(" ")})`
    );
  }
  if (expectViolations) {
    if (results.violations.length === 0) {
      failures.push(`${label}: checker found no violations on a bad fixture`);
    } else {
      notes.push(
        `${label}: checker correctly found ${results.violations.length} violation(s)`
      );
    }
    return results;
  }
  if (serious.length > 0) {
    failures.push(
      `${label}: ${serious.length} serious/critical axe violation(s): ` +
        serious.map((v) => v.id).join(", ")
    );
  }
  return results;
}

async function keyboardWalk(page) {
  await page.goto(`${BASE}/replay`, { waitUntil: "networkidle" });
  const seen = [];
  let markerReached = false;
  let markerActivated = false;
  for (let i = 0; i < 40; i += 1) {
    await page.keyboard.press("Tab");
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el) return null;
      return {
        tag: el.tagName.toLowerCase(),
        id: el.id || "",
        label:
          el.getAttribute("aria-label") ||
          el.textContent?.trim().slice(0, 40) ||
          "",
        href: el.getAttribute("href") || "",
        inMarkers: Boolean(el.closest?.("#markers")),
      };
    });
    if (!info) break;
    seen.push(`${info.tag}${info.id ? `#${info.id}` : ""}:${info.label}`);
    if (info.inMarkers) {
      markerReached = true;
      await page.keyboard.press("Enter");
      const hash = await page.evaluate(() => window.location.hash);
      markerActivated = hash.startsWith("#need-") || hash.startsWith("#supply-") || hash === "#tables-heading";
      break;
    }
  }
  const joined = seen.join(" | ");
  if (!seen.some((entry) => entry.includes("select#resource"))) {
    failures.push(`keyboard: resource filter not reachable. Order: ${joined}`);
  }
  if (!seen.some((entry) => entry.includes("button"))) {
    failures.push(`keyboard: submit control not reachable. Order: ${joined}`);
  }
  if (!markerReached) {
    failures.push(`keyboard: no map marker reachable by Tab. Order: ${joined}`);
  } else if (!markerActivated) {
    failures.push("keyboard: Enter on a marker did not jump to its row/target");
  }
  notes.push(`keyboard order: ${joined}`);
}

async function narrowViewportCheck(page) {
  await page.setViewportSize({ width: 640, height: 900 });
  await page.goto(`${BASE}/replay`, { waitUntil: "networkidle" });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth
  );
  if (overflow > 1) {
    failures.push(`narrow viewport: horizontal overflow of ${overflow}px`);
  }
  await page.setViewportSize({ width: 1280, height: 900 });
}

async function lighthouseScore(browserExe, path) {
  const chrome = await chromeLauncher.launch({
    chromePath: browserExe || undefined,
    chromeFlags: ["--headless=new", "--no-sandbox"],
  });
  try {
    const result = await lighthouse(`${BASE}${path}`, {
      port: chrome.port,
      onlyCategories: ["accessibility"],
      output: "json",
    });
    const score = result.lhr.categories.accessibility.score ?? 0;
    notes.push(`lighthouse ${path}: accessibility ${Math.round(score * 100)}`);
    if (score < 0.95) {
      failures.push(`lighthouse ${path}: accessibility ${score} < 0.95`);
    }
  } finally {
    await chrome.kill();
  }
}

async function main() {
  const tmp = mkdtempSync(join(tmpdir(), "resilinet-audit-"));
  const dbPath = join(tmp, "audit.db");
  const dbUrl = `sqlite+aiosqlite:///${dbPath}`;
  let server;
  let browser;
  try {
    run("uv", ["run", "alembic", "upgrade", "head"], {
      env: { ...process.env, RESILINET_DATABASE_URL: dbUrl },
    });
    const seedOut = run("uv", [
      "run",
      "python",
      "scripts/seed_audit.py",
      "--database-url",
      dbUrl,
    ]);
    const seed = JSON.parse(seedOut.trim().split("\n").at(-1));

    server = spawn(
      "uv",
      [
        "run",
        "uvicorn",
        "scripts.run_audit_server:create_audit_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        String(PORT),
      ],
      {
        cwd: ROOT,
        env: {
          ...process.env,
          RESILINET_APP_ENV: "replay",
          RESILINET_DATABASE_URL: dbUrl,
          RESILINET_INCIDENT_KEY: "demo-incident",
          RESILINET_PUBLIC_BASE_URL: BASE,
          RESILINET_RUN_WORKER_IN_APP: "false",
        },
        stdio: "ignore",
      }
    );
    await waitForHealth();

    browser = await chromium.launch();
    const publicContext = await browser.newContext({ bypassCSP: true });
    const publicPage = await publicContext.newPage();

    // Self-test: the checker must be able to fail.
    await publicPage.setContent(
      `<html><body><img src="x.png"><p style="color:#eeeeee;background:#ffffff">low contrast</p><input type="text"></body></html>`
    );
    await axeAudit(publicPage, "self-test", { expectViolations: true });

    for (const [label, path] of [
      ["replay", "/replay"],
      ["lite", "/replay/lite"],
      ["operator-login", "/operator/login"],
    ]) {
      await publicPage.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
      await axeAudit(publicPage, label);
    }

    await keyboardWalk(publicPage);
    await narrowViewportCheck(publicPage);

    const operatorContext = await browser.newContext({ bypassCSP: true });
    await operatorContext.addCookies([
      {
        name: seed.session_cookie_name,
        value: seed.operator_cookie,
        domain: "127.0.0.1",
        path: "/",
      },
    ]);
    const operatorPage = await operatorContext.newPage();
    await operatorPage.goto(`${BASE}${seed.reviews_path}`, {
      waitUntil: "networkidle",
    });
    await axeAudit(operatorPage, "operator-reviews");

    const coordinatorContext = await browser.newContext({ bypassCSP: true });
    await coordinatorContext.addCookies([
      {
        name: seed.session_cookie_name,
        value: seed.coordinator_cookie,
        domain: "127.0.0.1",
        path: "/",
      },
    ]);
    const actionPage = await coordinatorContext.newPage();
    await actionPage.goto(`${BASE}${seed.action_path}`, {
      waitUntil: "networkidle",
    });
    await axeAudit(actionPage, "action-form");

    await lighthouseScore(browser.executablePath?.() ?? "", "/replay");
    await lighthouseScore(browser.executablePath?.() ?? "", "/replay/lite");
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (server) server.kill("SIGTERM");
    rmSync(tmp, { recursive: true, force: true });
  }

  for (const note of notes) console.log(note);
  if (failures.length > 0) {
    console.error("\nACCESSIBILITY AUDIT FAILED:");
    for (const failure of failures) console.error(` - ${failure}`);
    process.exit(1);
  }
  console.log("\nACCESSIBILITY AUDIT PASSED");
}

await main();
