/**
 * Test Suite: Dashboard UI, Tasks Management & Mutually Exclusive Settings
 * Tests:
 * 1. Model selection mutual exclusivity (preset vs custom path, no covert overrides)
 * 2. Shared settings resolution (local path in defaults, explicit false preservation, empty custom path guard)
 * 3. Resource profile independence and JobConfig mapping
 * 4. Task status visibility across all 9 states (including cancelling/pausing not disguised)
 * 5. Action availability (retry on cancelled, pause on queued, cancel on pausing, cleanup on inactive)
 * 6. Reliable ETA formatting (no fabricated values when missing/null, '-' for non-running)
 * 7. Audio ready & page required hint indicators
 * 8. Transcript preview rendering & buildClipUrl #clip-t= without fake accuracy claims
 * 9. Config validation (validate_config) checks rendering & empty checks guard
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// Minimal DOM Mock for Dashboard Testing
class MockElement {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this._classListSet = new Set();
    this.style = {};
    this.listeners = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this._textContent = "";
  }

  get textContent() {
    if (this._textContent) return this._textContent;
    return this.children.map((c) => c.textContent).join(" ");
  }

  set textContent(val) {
    this._textContent = String(val);
    this.children = [];
  }

  appendChild(child) {
    if (child) this.children.push(child);
    return child;
  }

  setAttribute(k, v) {
    this.attributes[k] = String(v);
  }

  getAttribute(k) {
    return this.attributes[k];
  }

  addEventListener(type, fn) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(fn);
  }

  trigger(type, eventObj = {}) {
    if (this.listeners[type]) {
      this.listeners[type].forEach((fn) => fn(eventObj));
    }
  }

  focus() {}

  get classList() {
    const set = this._classListSet;
    return {
      add: (...classes) => classes.forEach((c) => set.add(c)),
      remove: (...classes) => classes.forEach((c) => set.delete(c)),
      toggle: (cls, force) => {
        if (force !== undefined) {
          if (force) set.add(cls);
          else set.delete(cls);
        } else {
          if (set.has(cls)) set.delete(cls);
          else set.add(cls);
        }
      },
      contains: (cls) => set.has(cls),
    };
  }

  set className(names) {
    this._classListSet = new Set((names || "").split(/\s+/).filter(Boolean));
  }

  get className() {
    return Array.from(this._classListSet).join(" ");
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
  console.log("--- Starting Dashboard & Settings Tests ---");

  const domElements = new Map();
  function getOrCreateElement(id) {
    if (!domElements.has(id)) {
      domElements.set(id, new MockElement());
    }
    return domElements.get(id);
  }

  // Pre-seed known dashboard IDs
  const knownIds = [
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
    "btnCloseCleanup",
    "btnCancelCleanup",
    "btnConfirmCleanup",
    "toast",
  ];
  knownIds.forEach((id) => getOrCreateElement(id));

  let nativeCallHandler = async (payload) => ({ status: "ok" });
  let mockStorage = {};

  const mockChrome = {
    runtime: {
      id: "tongji-clipper-ext-id",
      sendMessage(msg, cb) {
        if (msg.type === "NATIVE_ACTION") {
          nativeCallHandler(msg.payload).then((resp) => {
            if (cb) cb(resp);
          });
        }
      },
    },
    storage: {
      local: {
        get(keys, cb) {
          const res = {};
          (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
            if (mockStorage[k] !== undefined) res[k] = mockStorage[k];
          });
          cb(res);
        },
        set(data, cb) {
          Object.assign(mockStorage, data);
          if (cb) cb();
        },
      },
    },
    tabs: {
      create(opts) {},
    },
  };

  const documentMock = {
    getElementById: (id) => getOrCreateElement(id),
    createElement: (tag) => new MockElement(tag),
    querySelectorAll: (sel) => {
      if (sel === ".stat-card") {
        return [
          getOrCreateElement("stat-running-count"),
          getOrCreateElement("stat-queued-count"),
          getOrCreateElement("stat-attention-count"),
          getOrCreateElement("stat-completed-count"),
        ];
      }
      return [];
    },
    addEventListener: () => {},
    hidden: false,
    activeElement: null,
    body: { contains: () => true },
  };

  const windowMock = {
    location: { href: "chrome-extension://tongji-clipper-ext-id/dashboard.html" },
    history: { replaceState() {} },
  };

  const context = vm.createContext({
    document: documentMock,
    window: windowMock,
    chrome: mockChrome,
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

  // Load common-client.js and dashboard.js
  vm.runInContext(fs.readFileSync("extension/common-client.js", "utf8"), context);
  vm.runInContext(fs.readFileSync("extension/dashboard.js", "utf8"), context);
  await new Promise((r) => setTimeout(r, 40));

  // -------------------------------------------------------------
  // Test 1: Shared Settings Resolution & Mutual Exclusivity
  // -------------------------------------------------------------
  console.log("Test 1: Shared Settings Resolution & Model Mutual Exclusivity");
  const resolveSettings = context.resolveSettings;
  assert.ok(resolveSettings, "resolveSettings must be exported on globalThis");

  // Case 1a: Defaults specify local path -> resolves to custom modelSource
  const pathDefaults = {
    model: "D:\\models\\whisper-large-v3",
    offline: true,
    output_dir: "D:\\notes",
    resource_profile: "fast",
  };
  const resolvedFromPathDefault = resolveSettings({}, pathDefaults);
  assert.equal(resolvedFromPathDefault.modelSource, "custom");
  assert.equal(resolvedFromPathDefault.customModelPath, "D:\\models\\whisper-large-v3");
  assert.equal(resolvedFromPathDefault.model, "small", "Preset model fallback must not be empty or invalid");
  assert.equal(resolvedFromPathDefault.isOffline, true);
  assert.equal(resolvedFromPathDefault.resourceProfile, "fast");

  // Case 1b: Explicit false values are preserved (not overwritten by truthy defaults)
  const savedWithFalse = {
    isOffline: false,
    keepAudio: false,
  };
  const resolvedFalse = resolveSettings(savedWithFalse, { offline: true, keep_audio: true });
  assert.equal(resolvedFalse.isOffline, false, "Saved explicit false must be preserved");
  assert.equal(resolvedFalse.keepAudio, false, "Saved explicit false must be preserved");

  // Case 1c: Empty custom path validation guard in UI
  const radioCustom = getOrCreateElement("radio-model-custom");
  const inputCustom = getOrCreateElement("input-model-custom");
  const radioPreset = getOrCreateElement("radio-model-preset");
  const selectPreset = getOrCreateElement("select-model-preset");
  const lblEffective = getOrCreateElement("lbl-effective-model");

  radioCustom.checked = true;
  radioPreset.checked = false;
  inputCustom.value = ""; // Empty path!
  radioCustom.trigger("change");

  assert.equal(inputCustom.disabled, false);
  assert.equal(selectPreset.disabled, true);
  assert.match(lblEffective.textContent, /未填写模型路径/);

  // Trigger validate_config with empty custom path -> MUST block and alert
  getOrCreateElement("btn-validate-config").trigger("click");
  const validationBox = getOrCreateElement("validation-box");
  assert.equal(validationBox.style.display, "block");
  assert.match(getOrCreateElement("validation-checks-list").textContent, /已选择本地自定义模型，但路径为空/);

  // Case 1d: Set valid custom path -> effective model updates
  inputCustom.value = "D:\\local-models\\whisper-custom";
  inputCustom.trigger("input");
  assert.equal(lblEffective.textContent, "D:\\local-models\\whisper-custom");

  // Case 1e: Switch back to preset -> custom path does NOT covertly override
  radioPreset.checked = true;
  radioCustom.checked = false;
  selectPreset.value = "tiny";
  radioPreset.trigger("change");
  assert.equal(lblEffective.textContent, "tiny");
  assert.notEqual(lblEffective.textContent, inputCustom.value);

  console.log("✓ Settings resolution and model mutual exclusivity verified");

  // -------------------------------------------------------------
  // Test 2: Resource profile mapping
  // -------------------------------------------------------------
  console.log("Test 2: Resource Profile Independence");
  const selectProfile = getOrCreateElement("select-resource-profile");
  selectProfile.value = "fast";
  selectProfile.trigger("change");

  getOrCreateElement("btn-save-settings").trigger("click");
  assert.equal(mockStorage.resourceProfile, "fast");
  assert.equal(mockStorage.model, "tiny");
  console.log("✓ Resource profile is distinct from model and saved correctly");

  // -------------------------------------------------------------
  // Test 3: Task Status Visibility Across All 9 States & Actions
  // -------------------------------------------------------------
  console.log("Test 3: Task Statuses & Actions Across All 9 States");
  const testJobs = [
    {
      job_id: "j_run",
      status: "running",
      course_title: "运行中课程",
      audio_ready: true,
      page_required: false,
      progress_info: {
        stage: "transcription",
        total_chunks: 10,
        completed_chunks: 4,
        chunk_percent: 40,
        processed_seconds: 600,
        total_seconds: 1500,
        elapsed_seconds: 180,
        eta_seconds: 270,
        message: "转写中",
        last_activity: "2026-10-01 21:30:00",
      },
    },
    {
      job_id: "j_acq",
      status: "running",
      course_title: "音频采集课程",
      audio_ready: false,
      page_required: true,
      progress_info: {
        stage: "acquisition",
        audio_seconds: 120,
        total_seconds: 2400,
        speed: "1.0x",
        message: "正在录制音频",
      },
    },
    {
      job_id: "j_queued",
      status: "queued",
      course_title: "排队中课程",
      progress_info: { stage: "preparing" },
    },
    {
      job_id: "j_pausing",
      status: "pausing",
      course_title: "暂停中课程",
      progress_info: { stage: "transcription" },
    },
    {
      job_id: "j_paused",
      status: "paused",
      course_title: "已暂停课程",
      cache_bytes: 10485760,
      progress_info: { stage: "transcription" },
    },
    {
      job_id: "j_cancelling",
      status: "cancelling",
      course_title: "取消中课程",
      progress_info: { stage: "acquisition" },
    },
    {
      job_id: "j_cancelled",
      status: "cancelled",
      course_title: "已取消课程",
      cache_bytes: 20971520,
    },
    {
      job_id: "j_refresh",
      status: "needs_refresh",
      course_title: "失效课程",
      source_url: "https://look.tongji.edu.cn/livingroom?course_id=1",
    },
    {
      job_id: "j_failed",
      status: "failed",
      course_title: "失败课程",
      cache_bytes: 5242880,
      error_message: "FFmpeg 解码失败",
    },
    {
      job_id: "j_completed",
      status: "completed",
      course_title: "已完成课程",
      output_dir: "D:\\逐字稿",
      cache_bytes: 52428800,
    },
  ];

  // Feed test jobs through list_jobs handler
  nativeCallHandler = async (payload) => {
    if (payload.action === "list_jobs") return { status: "ok", jobs: testJobs };
    return { status: "ok" };
  };

  context.refreshTasksList();
  await new Promise((r) => setTimeout(r, 60));

  const container = getOrCreateElement("tasks-container");
  assert.equal(container.children.length, 10);

  // A. Queued card: must support pause & cancel; ETA must be '-'
  const queuedCard = container.children.find((c) => c.textContent.includes("排队中课程"));
  assert.ok(queuedCard);
  assert.ok(queuedCard.querySelector('[data-action="pause"]'), "Queued task must allow pause");
  assert.ok(queuedCard.querySelector('[data-action="cancel"]'), "Queued task must allow cancel");
  const queuedEta = queuedCard.querySelectorAll(".metric-value")[1];
  assert.equal(queuedEta.textContent, "-", "Queued task ETA must be '-' (not fabricated or '计算中')");

  // B. Pausing card: honest intermediate state, can be cancelled
  const pausingCard = container.children.find((c) => c.textContent.includes("暂停中课程"));
  assert.ok(pausingCard);
  assert.match(pausingCard.textContent, /正在暂停\.\.\./);
  assert.ok(pausingCard.querySelector('[data-action="cancel"]'), "Pausing task must allow cancel");

  // C. Cancelling card: honest state, duplicate cancel disabled
  const cancellingCard = container.children.find((c) => c.textContent.includes("取消中课程"));
  assert.ok(cancellingCard);
  assert.match(cancellingCard.textContent, /正在取消\.\.\./);
  assert.equal(cancellingCard.querySelector('[data-action="cancel"]'), null, "Cancelling task must not allow duplicate cancel");

  // D. Cancelled card: MUST have retry and cleanup cache
  const cancelledCard = container.children.find((c) => c.textContent.includes("已取消课程"));
  assert.ok(cancelledCard);
  assert.ok(cancelledCard.querySelector('[data-action="retry"]'), "Cancelled task must have retry button");
  assert.ok(cancelledCard.querySelector('[data-action="cleanup"]'), "Cancelled task with cache_bytes must allow cleanup");

  // E. Failed card: MUST have retry and cleanup cache
  const failedCard = container.children.find((c) => c.textContent.includes("失败课程"));
  assert.ok(failedCard);
  assert.ok(failedCard.querySelector('[data-action="retry"]'), "Failed task must have retry button");
  assert.ok(failedCard.querySelector('[data-action="cleanup"]'), "Failed task with cache_bytes must allow cleanup");
  assert.match(failedCard.textContent, /FFmpeg 解码失败/);

  // F. Audio ready vs page required hints
  const runCard = container.children.find((c) => c.textContent.includes("运行中课程"));
  assert.match(runCard.textContent, /音频已保存就绪，可安全关闭同济课程页面/);
  assert.match(runCard.textContent, /约 4 分 30 秒/); // Reliable ETA: 270s
  assert.match(runCard.textContent, /2026-10-01 21:30:00/, "last_activity must be displayed separately");

  const acqCard = container.children.find((c) => c.textContent.includes("音频采集课程"));
  assert.match(acqCard.textContent, /正在采集音频数据，请保持同济课程页面开启/);
  assert.match(acqCard.textContent, /计算中/); // Running without eta_seconds displays '计算中'

  // G. Completed card: safe leave hint and output actions
  const completedCard = container.children.find((c) => c.textContent.includes("已完成课程"));
  assert.match(completedCard.textContent, /可安全离开或关闭课程页面/);
  assert.ok(completedCard.querySelector('[data-action="open-note"]'));
  assert.ok(completedCard.querySelector('[data-action="open-folder"]'));
  assert.ok(completedCard.querySelector('[data-action="reexport"]'));
  assert.ok(completedCard.querySelector('[data-action="cleanup"]'));
  assert.ok(completedCard.querySelector('[data-action="preview"]'));

  console.log("✓ All 9 task statuses and intermediate states verified");

  // -------------------------------------------------------------
  // Test 4: URL Hash Replacement & Transcript Preview
  // -------------------------------------------------------------
  console.log("Test 4: URL Hash strictly formatted as #clip-t=${sec}");
  const buildClipUrl = context.buildClipUrl;
  assert.ok(buildClipUrl, "buildClipUrl must be exported on globalThis");

  // Existing hash is replaced cleanly
  const testUrlWithHash = "https://look.tongji.edu.cn/livingroom?course_id=1#old-anchor";
  const clippedUrl = buildClipUrl(testUrlWithHash, 142);
  assert.equal(clippedUrl, "https://look.tongji.edu.cn/livingroom?course_id=1#clip-t=142");

  // Test preview modal segments
  nativeCallHandler = async (payload) => {
    if (payload.action === "preview") {
      return {
        status: "ok",
        source_url: "https://look.tongji.edu.cn/livingroom?course_id=1",
        segments: [
          { start: 0, end: 15, text: "正常识别片段", needs_review: false },
          { start: 15, end: 30, text: "含生僻专有名词片段", needs_review: true },
        ],
      };
    }
    return { status: "ok" };
  };

  context.openPreviewModal({ job_id: "j_completed", course_title: "已完成课程" });
  await new Promise((r) => setTimeout(r, 40));

  const segContainer = getOrCreateElement("preview-segments-container");
  assert.equal(segContainer.children.length, 2);
  const reviewSeg = segContainer.children[1];
  assert.match(reviewSeg.textContent, /建议复核/);
  assert.doesNotMatch(reviewSeg.textContent, /准确率/);
  assert.doesNotMatch(reviewSeg.textContent, /%/);
  console.log("✓ Transcript preview honors review guidance without fabricating accuracy metrics");

  // -------------------------------------------------------------
  // Test 5: Config Validation (validate_config) with Empty Checks Guard
  // -------------------------------------------------------------
  console.log("Test 5: Config Validation Checks Rendering & Empty Checks Guard");
  // 5a. Backend returns valid checks
  nativeCallHandler = async (payload) => {
    if (payload.action === "validate_config") {
      return {
        status: "ok",
        checks: [
          { name: "模型架构检查", ok: true, message: "Whisper 模型就绪" },
          { name: "目录写入权限", ok: true, message: "输出目录有效" },
        ],
      };
    }
    return { status: "ok" };
  };

  // Select valid preset to allow validation
  radioPreset.checked = true;
  radioCustom.checked = false;
  radioPreset.trigger("change");

  getOrCreateElement("btn-validate-config").trigger("click");
  await new Promise((r) => setTimeout(r, 40));

  const checksList = getOrCreateElement("validation-checks-list");
  assert.equal(checksList.children.length, 2);
  assert.match(checksList.textContent, /模型架构检查/);
  assert.match(checksList.textContent, /输出目录有效/);

  // 5b. Backend returns empty checks array -> MUST NOT claim success!
  nativeCallHandler = async (payload) => {
    if (payload.action === "validate_config") {
      return { status: "ok", checks: [] };
    }
    return { status: "ok" };
  };

  getOrCreateElement("btn-validate-config").trigger("click");
  await new Promise((r) => setTimeout(r, 40));

  assert.match(checksList.textContent, /后台服务未返回具体检查项详情/);
  console.log("✓ Config validation guarded against missing checks fabrication");

  console.log("All dashboard and settings tests passed successfully!");
})().catch((err) => {
  console.error("Test failed:", err);
  process.exitCode = 1;
});
