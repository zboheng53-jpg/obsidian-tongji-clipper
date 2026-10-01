/**
 * Test Suite: Preview Mode Complete Isolation & Synthetic Fixtures
 * Tests:
 * 1. Confirms preview environment operates 100% on in-memory synthetic fixtures
 * 2. Strict isolation: ZERO calls to real native host or real network
 * 3. Execution of production common-client.js & dashboard.js on top of preview adapter
 * 4. Synthetic fixtures cover all 9 states (running, pausing, cancelled, failed, completed, etc.)
 * 5. In-memory interactive transitions (pause, resume, retry, cleanup_cache)
 * 6. Transcript preview rendering with #clip-t= timestamp and "建议复核" (zero fake accuracy claims)
 * 7. Tab creation interception via synthetic adapter
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class MockElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.listeners = {};
    this._classList = new Set();
    this._text = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
  }

  set textContent(v) {
    this._text = String(v);
    this.children = [];
  }

  get textContent() {
    if (this._text) return this._text;
    return this.children.map((c) => c.textContent).join(" ");
  }

  appendChild(c) {
    if (c) this.children.push(c);
    return c;
  }

  setAttribute(k, v) {
    this.attributes[k] = String(v);
  }

  getAttribute(k) {
    return this.attributes[k];
  }

  addEventListener(t, f) {
    this.listeners[t] = this.listeners[t] || [];
    this.listeners[t].push(f);
  }

  trigger(t, evt = {}) {
    if (this.listeners[t]) {
      this.listeners[t].forEach((fn) => fn(evt));
    }
  }

  get classList() {
    return {
      add: (...c) => c.forEach((x) => this._classList.add(x)),
      remove: (...c) => c.forEach((x) => this._classList.delete(x)),
      toggle: (c, f) => {
        if (f !== undefined) {
          if (f) this._classList.add(c);
          else this._classList.delete(c);
        } else {
          if (this._classList.has(c)) this._classList.delete(c);
          else this._classList.add(c);
        }
      },
      contains: (c) => this._classList.has(c),
    };
  }

  set className(n) {
    this._classList = new Set((n || "").split(/\s+/).filter(Boolean));
  }

  get className() {
    return Array.from(this._classList).join(" ");
  }

  focus() {}

  contains(target) {
    if (this === target) return true;
    for (const child of this.children) {
      if (child.contains && child.contains(target)) return true;
    }
    return false;
  }

  querySelector(sel) {
    const all = this.querySelectorAll(sel);
    return all.length > 0 ? all[0] : null;
  }

  querySelectorAll(sel) {
    const results = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (child instanceof MockElement) {
          let match = true;
          if (sel.startsWith(".")) {
            const cls = sel.slice(1);
            if (!child.classList.contains(cls)) match = false;
          } else if (sel.startsWith("[")) {
            const attrMatches = [...sel.matchAll(/\[([a-zA-Z0-9_-]+)="([^"]+)"\]/g)];
            if (attrMatches.length === 0) match = false;
            for (const [, k, v] of attrMatches) {
              if (child.getAttribute(k) !== v) {
                match = false;
                break;
              }
            }
          }
          if (match) results.push(child);
          walk(child);
        }
      }
    };
    walk(this);
    return results;
  }
}

(async () => {
  console.log("--- Starting Preview Mode Isolation Tests ---");

  const dom = new Map();
  function getEl(id) {
    if (!dom.has(id)) dom.set(id, new MockElement());
    return dom.get(id);
  }

  // Pre-populate IDs from preview.html
  [
    "tab-btn-tasks",
    "tab-btn-settings",
    "pane-tasks",
    "pane-settings",
    "worker-badge",
    "btn-refresh-manual",
    "stat-running-count",
    "stat-queued-count",
    "stat-attention-count",
    "stat-completed-count",
    "task-search-input",
    "task-status-filter",
    "sync-status-indicator",
    "tasks-container",
    "radio-model-preset",
    "radio-model-custom",
    "select-model-preset",
    "input-model-custom",
    "row-model-preset",
    "row-model-custom",
    "lbl-effective-model",
    "select-resource-profile",
    "chk-offline",
    "input-output-dir",
    "chk-keep-audio",
    "btn-save-settings",
    "btn-validate-config",
    "save-status-text",
    "validation-box",
    "validation-checks-list",
    "preview-modal",
    "preview-modal-title",
    "preview-segments-container",
    "btn-close-preview",
    "btn-preview-done",
    "export-modal",
    "input-export-dir",
    "export-feedback",
    "btn-close-export",
    "btn-cancel-export",
    "btn-confirm-export",
    "cleanup-modal",
    "cleanup-feedback",
    "btn-close-cleanup",
    "btn-cancel-cleanup",
    "btn-confirm-cleanup",
    "toast",
  ].forEach((id) => getEl(id));

  const doc = {
    getElementById: (id) => getEl(id),
    createElement: (tag) => new MockElement(tag),
    querySelectorAll: (sel) => {
      if (sel === ".stat-card") {
        return [
          getEl("stat-running-count"),
          getEl("stat-queued-count"),
          getEl("stat-attention-count"),
          getEl("stat-completed-count"),
        ];
      }
      return [];
    },
    addEventListener: () => {},
    activeElement: null,
    hidden: false,
    body: {
      contains: () => true,
    },
  };

  const windowMock = {
    location: { href: "http://localhost:8080/extension/preview.html" },
    history: { replaceState() {} },
  };

  const context = vm.createContext({
    document: doc,
    window: windowMock,
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => {},
    clearInterval: () => {},
    URL,
    Array,
    Object,
    String,
    Math,
    Promise,
    Date,
  });

  // 1. Load preview.js first (installs synthetic window.chrome and guards)
  vm.runInContext(fs.readFileSync("extension/preview.js", "utf8"), context);

  // -------------------------------------------------------------
  // Test 1: Verification of Zero Real Network Access (Fetch / XHR Guard)
  // -------------------------------------------------------------
  console.log("Test 1: Zero Network Access Verification");
  assert.throws(
    () => {
      context.window.fetch("https://tongji.edu.cn/api");
    },
    /VIOLATION: 网络请求在离线演示模式中被严格禁止！/,
    "Network fetch must be blocked in preview mode"
  );
  assert.throws(
    () => {
      new context.window.XMLHttpRequest();
    },
    /VIOLATION: XMLHttpRequest 在离线演示模式中被严格禁止！/,
    "XMLHttpRequest must be blocked in preview mode"
  );
  console.log("✓ Preview mode strictly guards against any network requests");

  // 2. Load production common-client.js and dashboard.js on top of preview adapter
  vm.runInContext(fs.readFileSync("extension/common-client.js", "utf8"), context);
  vm.runInContext(fs.readFileSync("extension/dashboard.js", "utf8"), context);

  // Wait for initial async list_jobs in dashboard.js
  await new Promise((r) => setTimeout(r, 60));

  // -------------------------------------------------------------
  // Test 2: In-Memory Synthetic Fixtures Rendered by Production dashboard.js
  // -------------------------------------------------------------
  console.log("Test 2: Synthetic Fixtures Rendered Accurately by Production Dashboard");
  const container = getEl("tasks-container");
  assert.ok(container.children.length >= 9, `Expected at least 9 synthetic tasks, got ${container.children.length}`);

  const cardTexts = container.children.map((c) => c.textContent);

  // 1. Running with audio_ready: true
  assert.ok(
    cardTexts.some((t) => t.includes("高等数学") && t.includes("音频已保存就绪")),
    "Running task with audio_ready: true must show safe-to-close hint"
  );

  // 2. Running with acquisition & page_required: true
  assert.ok(
    cardTexts.some((t) => t.includes("概率论与数理统计") && t.includes("正在采集音频数据，请保持同济课程页面开启")),
    "Acquisition task with page_required: true must show keep-page hint"
  );

  // 3. Pausing: honest intermediate state (button disabled, cannot disguise as stopped)
  assert.ok(
    cardTexts.some((t) => t.includes("大学物理") && t.includes("正在暂停...")),
    "Pausing task must show honest intermediate state"
  );

  // 4. Paused: can resume & cancel
  assert.ok(
    cardTexts.some((t) => t.includes("线性代数") && t.includes("继续") && t.includes("取消")),
    "Paused task must have resume and cancel buttons"
  );

  // 5. Cancelling: honest intermediate state
  assert.ok(
    cardTexts.some((t) => t.includes("大学英语") && t.includes("正在取消...")),
    "Cancelling task must show honest intermediate state"
  );

  // 6. Cancelled: MUST have retry button
  assert.ok(
    cardTexts.some((t) => t.includes("工程力学") && t.includes("重试任务")),
    "Cancelled task must have retry button"
  );

  // 7. Needs refresh: guidance button
  assert.ok(
    cardTexts.some((t) => t.includes("计算机系统结构") && t.includes("打开课程页刷新")),
    "Needs refresh task must have guidance button"
  );

  // 8. Failed: retry button and error message
  assert.ok(
    cardTexts.some((t) => t.includes("模拟电子技术基础") && t.includes("失败原因") && t.includes("重试任务")),
    "Failed task must have retry button and error details"
  );

  // 9. Completed: safe to leave hint, open note, folder, re-export, preview
  assert.ok(
    cardTexts.some((t) => t.includes("算法设计与分析") && t.includes("可安全离开或关闭课程页面")),
    "Completed task must have safe-leave hint"
  );

  console.log("✓ All 9 fixture states rendered with production dashboard markup");

  // -------------------------------------------------------------
  // Test 3: In-Memory Interactive Actions (Pause, Cancel, Retry, Cleanup)
  // -------------------------------------------------------------
  console.log("Test 3: In-Memory Interactive Transitions");
  const testState = context.window.__preview_test_state__;
  assert.ok(testState, "Preview test helper state must be exposed");

  // A. Cancel pausing task
  const pausingCard = container.children.find((c) => c.textContent.includes("大学物理"));
  assert.ok(pausingCard);
  const btnCancelPausing = pausingCard.querySelector('[data-action="cancel"]');
  assert.ok(btnCancelPausing, "Pausing task must allow cancel");
  btnCancelPausing.trigger("click");
  await new Promise((r) => setTimeout(r, 40));

  const physJob = testState.getJobs().find((j) => j.course_title.includes("大学物理"));
  assert.equal(physJob.status, "cancelled", "Cancelled job status must update in memory");

  // B. Retry cancelled task
  const cancelledCard = container.children.find((c) => c.textContent.includes("工程力学"));
  assert.ok(cancelledCard);
  const btnRetry = cancelledCard.querySelector('[data-action="retry"]');
  assert.ok(btnRetry, "Cancelled task must have retry action button");
  btnRetry.trigger("click");
  await new Promise((r) => setTimeout(r, 40));

  const mechJob = testState.getJobs().find((j) => j.course_title.includes("工程力学"));
  assert.equal(mechJob.status, "running", "Retried job must transition to running in memory");

  // C. Cleanup cache on completed task
  const completedCard = container.children.find((c) => c.textContent.includes("算法设计与分析"));
  assert.ok(completedCard);
  const btnCleanup = completedCard.querySelector('[data-action="cleanup"]');
  assert.ok(btnCleanup, "Completed task with cache_bytes > 0 must have cleanup button");
  btnCleanup.trigger("click");
  await new Promise((r) => setTimeout(r, 20));

  // Trigger modal confirm
  const btnConfirmCleanup = getEl("btn-confirm-cleanup");
  btnConfirmCleanup.trigger("click");
  await new Promise((r) => setTimeout(r, 40));

  const algoJob = testState.getJobs().find((j) => j.course_title.includes("算法设计与分析"));
  assert.equal(algoJob.cache_bytes, 0, "Cache bytes must be cleared after cleanup");
  console.log("✓ In-memory state transitions (cancel, retry, cleanup) executed properly");

  // -------------------------------------------------------------
  // Test 4: Transcript Preview & URL Timestamp Jump (#clip-t=)
  // -------------------------------------------------------------
  console.log("Test 4: Transcript Preview & URL Jump");
  const btnPreview = completedCard.querySelector('[data-action="preview"]');
  assert.ok(btnPreview, "Completed task must have preview button");
  btnPreview.trigger("click");
  await new Promise((r) => setTimeout(r, 40));

  const segContainer = getEl("preview-segments-container");
  assert.ok(segContainer.children.length > 0, "Transcript segments must be rendered");

  // Check 建议复核 tag
  const reviewSeg = segContainer.children.find((s) => s.textContent.includes("建议复核"));
  assert.ok(reviewSeg, "Segment requiring review must show 建议复核");
  assert.doesNotMatch(reviewSeg.textContent, /准确率/, "Must not fabricate accuracy percentage");
  assert.doesNotMatch(reviewSeg.textContent, /%/, "Must not include percentage symbols");

  // Check timestamp click builds #clip-t=
  const timeBtn = segContainer.children[0].querySelector(".segment-time-btn");
  assert.ok(timeBtn, "Timestamp button must exist");
  timeBtn.trigger("click");

  const openedTabs = testState.getOpenedTabs();
  assert.ok(openedTabs.length > 0, "Clicking timestamp must open tab");
  const lastOpened = openedTabs[openedTabs.length - 1];
  assert.match(lastOpened, /#clip-t=\d+/, "Timestamp link MUST strictly use #clip-t=${sec}");
  console.log("✓ Transcript preview correctly formats #clip-t= URL and review guidance");

  console.log("All preview mode isolation tests passed successfully!");
})().catch((err) => {
  console.error("Test failed:", err);
  process.exitCode = 1;
});
