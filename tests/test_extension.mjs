import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";


const root = new URL("../", import.meta.url);

function makeElement(text, title = "") {
  return { textContent: text, title };
}

function testExtractor() {
  const detailElements = {
    ".teacher_span": makeElement("沈坚", "沈坚"),
    ".address_span": makeElement("四平路校区教学南楼-329"),
    ".time_span": makeElement("13:30-15:05")
  };
  const context = {
    location: {
      hostname: "look.tongji.edu.cn",
      pathname: "/livingroom",
      href: "https://look.tongji.edu.cn/livingroom?course_id=91006&sub_id=1834110&tenant_code=222"
    },
    document: {
      querySelector(selector) {
        if (selector === ".course_name") {
          return makeElement("回放 高级语言程序设计实验 2026-03-04第5-6节", "高级语言程序设计实验 2026-03-04第5-6节");
        }
        if (selector === "video") {
          return { currentSrc: "https://look.tongji.edu.cn/play/course.mp4?t=signed" };
        }
        if (selector === ".course_info_detail") {
          return { querySelector: (child) => detailElements[child] || null };
        }
        return null;
      }
    }
  };
  const result = vm.runInNewContext(fs.readFileSync(new URL("extension/extractor.js", root), "utf8"), context);
  assert.equal(result.title, "高级语言程序设计实验 2026-03-04第5-6节");
  assert.equal(result.course_name, "高级语言程序设计实验");
  assert.equal(result.author, "沈坚");
  assert.equal(result.published, "2026-03-04");
  assert.equal(result.video_url, "https://look.tongji.edu.cn/play/course.mp4?t=signed");
}

async function testPopupSubmissionMessage() {
  let onMessage;
  let onAlarm;
  let posted;
  const notifications = [];
  const payload = {
    page_url: "https://look.tongji.edu.cn/livingroom?course_id=91006&sub_id=1834110&tenant_code=222",
    video_url: "https://look.tongji.edu.cn/play/course.mp4?t=signed",
    title: "高级语言程序设计实验 2026-03-04第5-6节"
  };
  const chrome = {
    action: {
      async setBadgeText() {},
      async setBadgeBackgroundColor() {}
    },
    tabs: {
      async query() { return [{ id: 7, url: payload.page_url }]; }
    },
    notifications: { async create(value) { notifications.push(value); } },
    scripting: {
      async executeScript() {
        return [{ result: payload }];
      }
    },
    storage: {
      local: {
        data: {},
        async get() { return this.data; },
        async set(value) { this.data = { ...this.data, ...value }; }
      }
    },
    alarms: {
      onAlarm: { addListener(fn) { onAlarm = fn; } },
      async create() {},
      async clear() {}
    },
    runtime: { onMessage: { addListener(fn) { onMessage = fn; } } }
  };
  const context = {
    chrome,
    fetch: async (url, options) => {
      if (url.endsWith("/health")) {
        return { ok: true, status: 200, async json() { return { status: "ok", configured: true }; } };
      }
      posted = { url, options };
      return { ok: true, status: 202, async json() { return { id: "abc123", title: payload.title }; } };
    },
    console
  };
  vm.runInNewContext(fs.readFileSync(new URL("extension/background.js", root), "utf8"), context);
  assert.equal(typeof onMessage, "function");
  assert.equal(typeof onAlarm, "function");
  const reply = await new Promise((resolve) => {
    assert.equal(onMessage({ type: "clip-current-page" }, {}, resolve), true);
  });
  assert.equal(reply.ok, true);
  assert.equal(posted.url, "http://127.0.0.1:8765/jobs");
  assert.equal(posted.options.method, "POST");
  assert.equal(posted.options.headers["X-Tongji-Clipper"], "1");
  assert.deepEqual(JSON.parse(posted.options.body), payload);
  assert.equal(notifications[0].title, "同济课程剪藏已开始");
}

async function testUnconfiguredServiceReturnsError() {
  let onMessage;
  let scriptInjected = false;
  const notifications = [];
  const chrome = {
    action: {
      async setBadgeText() {},
      async setBadgeBackgroundColor() {}
    },
    tabs: { async query() { return [{ id: 8, url: "https://look.tongji.edu.cn/livingroom?course_id=1" }]; } },
    notifications: { async create(value) { notifications.push(value); } },
    scripting: { async executeScript() { scriptInjected = true; return []; } },
    storage: { local: { async get() { return {}; }, async set() {} } },
    alarms: {
      onAlarm: { addListener() {} },
      async create() {},
      async clear() {}
    },
    runtime: { onMessage: { addListener(fn) { onMessage = fn; } } }
  };
  const context = {
    chrome,
    fetch: async () => ({
      ok: true,
      status: 200,
      async json() { return { status: "ok", configured: false }; }
    }),
    console
  };
  vm.runInNewContext(fs.readFileSync(new URL("extension/background.js", root), "utf8"), context);
  const reply = await new Promise((resolve) => {
    assert.equal(onMessage({ type: "clip-current-page" }, {}, resolve), true);
  });
  assert.equal(reply.ok, false);
  assert.match(reply.error, /Vault/);
  assert.equal(scriptInjected, false);
  assert.equal(notifications[0].title, "同济课程剪藏失败");
}

async function testMissingJobIsReportedAndRemoved() {
  let onAlarm;
  const notifications = [];
  const local = {
    data: {
      jobs: {
        missing: { id: "missing", tabId: 9, title: "丢失的任务" }
      }
    },
    async get() { return this.data; },
    async set(value) { this.data = { ...this.data, ...value }; }
  };
  const chrome = {
    action: {
      async setBadgeText() {},
      async setBadgeBackgroundColor() {}
    },
    notifications: { async create(value) { notifications.push(value); } },
    storage: { local },
    alarms: {
      onAlarm: { addListener(fn) { onAlarm = fn; } },
      async create() {},
      async clear() {}
    },
    runtime: { onMessage: { addListener() {} } }
  };
  const context = {
    chrome,
    fetch: async () => ({
      ok: false,
      status: 404,
      async json() { return { error: "job not found" }; }
    }),
    console
  };

  vm.runInNewContext(fs.readFileSync(new URL("extension/background.js", root), "utf8"), context);
  await onAlarm({ name: "poll-tongji-jobs" });

  assert.equal(Object.keys(local.data.jobs).length, 0);
  assert.equal(notifications[0].title, "同济课程剪藏失败");
  assert.match(notifications[0].message, /状态已丢失/);
}


testExtractor();
await testPopupSubmissionMessage();
await testUnconfiguredServiceReturnsError();
await testMissingJobIsReportedAndRemoved();
console.log("extension tests: PASS");
